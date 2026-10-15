"""Opt-in 100k MongoDB scale audit. Requires MONGO_TEST_DB to be fresh and disposable."""
import asyncio
import importlib
import json
import os
from pathlib import Path
import socket
import statistics
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
from pymongo import MongoClient

ROOT = Path(__file__).resolve().parents[1]


def run_http_benchmark(db_name):
    """Measure real localhost HTTP requests against the already loaded 100k database."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = os.environ.copy()
    env["MONGO_DB"] = db_name
    command = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)]
    server = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 45
        while True:
            if server.poll() is not None:
                raise RuntimeError(f"Benchmark API exited early with code {server.returncode}")
            try:
                with urlopen(base + "/health", timeout=2) as response:
                    if response.status == 200:
                        break
            except (OSError, URLError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("Benchmark API did not become healthy within 45 seconds")
                time.sleep(0.25)

        def call(method, path, payload=None):
            data = json.dumps(payload).encode() if payload is not None else None
            req = Request(base + path, data=data, method=method,
                          headers={"Accept": "application/json", "Content-Type": "application/json"})
            try:
                response = urlopen(req, timeout=30)
            except HTTPError as error:
                response = error
            with response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None

        # Exercise the actual HTTP contract, including concurrent writes and JSON serialization.
        code = "EMP999999"
        employee = {"emp_code": code, "name": "HTTP Contract", "email": "http-contract@tests.invalid",
                    "department": "HTTPQA", "shift_start": "09:30", "shift_end": "18:30",
                    "joined_on": "2026-04-10"}
        assert call("GET", "/health")[0] == 200
        openapi_status, openapi = call("GET", "/openapi.json")
        assert openapi_status == 200 and all(path in openapi["paths"] for path in
            ("/employees", "/attendance", "/attendance/punch-in", "/attendance/punch-out",
             "/analytics/employees/{emp_code}/monthly", "/analytics/departments/summary",
             "/analytics/leaderboard/late", "/analytics/departments/{department}/trend",
             "/admin/explain/{endpoint}"))

        def simultaneous(method, path, payload):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(call, method, path, payload) for _ in range(2)]
                return [future.result() for future in futures]

        create_results = simultaneous("POST", "/employees", employee)
        assert sorted(status for status, _ in create_results) == [201, 409], create_results
        created = next(body for status, body in create_results if status == 201)
        assert {"emp_code", "name", "email", "department", "created_at"} <= created.keys()
        assert isinstance(created["created_at"], int)
        assert call("GET", "/employees?department=HTTPQA&page=1&page_size=20")[1]["total"] == 1

        punch_in = int(datetime(2026, 4, 10, 4, 0, tzinfo=timezone.utc).timestamp() * 1000)
        punch_in_body = {"emp_code": code, "punched_at": punch_in}
        punch_in_results = simultaneous("POST", "/attendance/punch-in", punch_in_body)
        assert sorted(status for status, _ in punch_in_results) == [201, 409], punch_in_results
        punch_out_body = {"emp_code": code,
                          "punched_at": int(datetime(2026, 4, 10, 13, 0, tzinfo=timezone.utc).timestamp() * 1000)}
        punch_out_results = simultaneous("POST", "/attendance/punch-out", punch_out_body)
        assert sorted(status for status, _ in punch_out_results) == [200, 409], punch_out_results

        def regularize(at_minute):
            return call("PATCH", f"/attendance/{code}/2026-04-10", {
                "punch_in": int(datetime(2026, 4, 10, 4, at_minute, tzinfo=timezone.utc).timestamp() * 1000),
                "reason": "HTTP audit correction", "regularized_by": "scale-audit"})

        correction_results = [regularize(10), regularize(15)]
        correction_successes = sum(status == 200 for status, _ in correction_results)
        assert correction_successes >= 1 and all(status in (200, 409) for status, _ in correction_results), correction_results
        correction_status, correction_body = regularize(20)
        assert correction_status == 200 and len(correction_body["history"]) == correction_successes + 1

        attendance_status, attendance_body = call("GET", f"/attendance?emp_code={code}&page=1&page_size=10")
        assert attendance_status == 200 and attendance_body["total"] == 1
        assert {"emp_code", "date", "status", "punch_in", "punch_out", "history"} <= attendance_body["items"][0].keys()
        route_cases = [
            ("GET", f"/analytics/employees/{code}/monthly?month=2026-04", {"emp_code", "month", "present_days", "attendance_pct"}),
            ("GET", "/analytics/departments/summary?month=2026-04&department=HTTPQA", {"month", "items"}),
            ("GET", "/analytics/leaderboard/late?month=2026-04&department=HTTPQA&limit=10", {"month", "items"}),
            ("GET", "/analytics/departments/HTTPQA/trend?from=2026-04-10&to=2026-04-11", {"department", "items"}),
        ]
        for method, path, required in route_cases:
            status, body = call(method, path)
            assert status == 200 and required <= body.keys(), (path, status, body)
        explain_cases = [
            ("attendance_list", "emp_code=EMP100000&date_from=2026-01-01&date_to=2026-01-31"),
            ("employee_monthly", "emp_code=EMP100000&month=2026-01"),
            ("department_summary", "month=2026-01&department=DEPT00"),
            ("late_leaderboard", "month=2026-01&department=DEPT00&limit=10"),
            ("department_trend", "department=DEPT00&from=2026-01-01&to=2026-01-31"),
        ]
        for endpoint, query in explain_cases:
            status, body = call("GET", f"/admin/explain/{endpoint}?{query}")
            assert status == 200 and body["endpoint"] == endpoint and isinstance(body["explain"], dict), (endpoint, status)
        assert call("POST", "/employees", {"emp_code": "invalid"})[0] == 422
        assert call("GET", "/attendance?date_from=2026-04-12&date_to=2026-04-10")[0] == 422
        print("HTTP contract audit passed: OpenAPI, all routes, schemas, 4xx/2xx statuses, concurrent writes, and audit history.", flush=True)

        routes = {
            "employees_department_page": "/employees?department=DEPT00&page=1&page_size=50",
            "attendance_employee_page": "/attendance?emp_code=EMP100000&page=1&page_size=50",
            "employee_monthly": "/analytics/employees/EMP100000/monthly?month=2026-01",
            "department_summary": "/analytics/departments/summary?month=2026-01&department=DEPT00",
            "late_leaderboard": "/analytics/leaderboard/late?month=2026-01&department=DEPT00&limit=10",
            "department_trend": "/analytics/departments/DEPT00/trend?from=2026-01-01&to=2026-01-31",
        }

        def request(path):
            started = time.perf_counter()
            with urlopen(base + path, timeout=30) as response:
                body = response.read()
                elapsed_ms = (time.perf_counter() - started) * 1000
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status} for {path}")
                return elapsed_ms, len(body)

        print("HTTP benchmark: localhost Uvicorn, 20 measured requests per route at concurrency 1 and 8; warm-up excluded.", flush=True)
        for label, path in routes.items():
            for concurrency in (1, 8):
                for _ in range(2):
                    request(path)
                count = 20
                latencies = []
                response_bytes = []
                started = time.perf_counter()
                if concurrency == 1:
                    results = [request(path) for _ in range(count)]
                else:
                    with ThreadPoolExecutor(max_workers=concurrency) as pool:
                        futures = [pool.submit(request, path) for _ in range(count)]
                        results = [future.result() for future in as_completed(futures)]
                elapsed = time.perf_counter() - started
                for latency, size in results:
                    latencies.append(latency)
                    response_bytes.append(size)
                ordered = sorted(latencies)
                p95 = ordered[max(0, int(0.95 * len(ordered)) - 1)]
                print(f"BENCH {label} concurrency={concurrency} n={count} "
                      f"rps={count / elapsed:.1f} p50_ms={statistics.median(ordered):.1f} "
                      f"p95_ms={p95:.1f} max_ms={max(ordered):.1f} "
                      f"response_bytes_avg={statistics.mean(response_bytes):.0f}", flush=True)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def main():
    benchmark = "--benchmark" in sys.argv[1:]
    load_dotenv(".env")
    db_name = os.getenv("MONGO_TEST_DB")
    uri = os.getenv("MONGO_URI")
    if not db_name or not uri:
        raise SystemExit("Set MONGO_TEST_DB to a new database name; MONGO_URI must be configured.")
    if not db_name.startswith("attendance_disposable_"):
        raise SystemExit("Refusing scale audit unless MONGO_TEST_DB starts with attendance_disposable_.")

    guard = MongoClient(uri, serverSelectionTimeoutMS=7000)
    try:
        guard.admin.command("ping")
        if db_name in guard.list_database_names():
            raise SystemExit(f"Refusing to use or drop existing database {db_name!r}.")
        version = guard.admin.command("buildInfo")["version"]
        print(f"MongoDB {version}; fresh database {db_name}", flush=True)
    finally:
        guard.close()

    os.environ["MONGO_DB"] = db_name
    previous = sys.modules.pop("app.main", None)
    if previous is not None:
        previous.client.close()
    api = importlib.import_module("app.main")
    database = api.db
    lifecycle = api.app.router.lifespan_context(api.app)
    cleanup = MongoClient(uri, serverSelectionTimeoutMS=7000)
    try:
        asyncio.run(lifecycle.__aenter__())
        started = time.perf_counter()
        first = date(2026, 1, 1)
        employees = [
            {"emp_code": f"EMP{i + 100000:06d}", "name": f"Employee {i:04d}",
             "email": f"e{i}@scale.test", "department": f"DEPT{i % 4:02d}",
             "shift_start": "09:30", "shift_end": "18:30", "joined_on": first.isoformat(),
             "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}
            for i in range(1000)
        ]
        database.employees.insert_many(employees, ordered=False)

        def make_batch(batch_number):
            docs = []
            first_employee = batch_number * 100
            base = datetime(2026, 1, 1, 4, tzinfo=timezone.utc)
            for emp_i in range(first_employee, first_employee + 100):
                code = f"EMP{emp_i + 100000:06d}"
                for day_index in range(100):
                    day = first + timedelta(days=day_index)
                    is_leave = day_index % 19 == 0
                    is_half = day_index % 11 == 0
                    docs.append({
                        "emp_code": code, "date": day.isoformat(),
                        "status": "LEAVE" if is_leave else ("WFH" if day_index % 7 == 0 else "PRESENT"),
                        "punch_in": base + timedelta(days=day_index),
                        "punch_out": base + timedelta(days=day_index, hours=4 if is_half else 8),
                        "work_hours": 4.0 if is_half else 8.0,
                        "late_minutes": 15 if day_index % 4 == 0 else 0,
                        "overtime_minutes": 60 if day_index % 10 == 0 else 0,
                        "half_day": is_half, "history": [],
                    })
            return docs

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(database.attendance_logs.insert_many, make_batch(i), ordered=False)
                       for i in range(10)]
            for i, future in enumerate(futures, 1):
                future.result()
                print(f"Inserted {i * 10000} / 100000 attendance records", flush=True)

        count = database.attendance_logs.count_documents({})
        assert count == 100000, count
        print(f"100k fixture load: {time.perf_counter() - started:.1f}s", flush=True)

        jan_dates = [first + timedelta(days=i) for i in range(31)]
        expected_present = sum(0.5 if i % 11 == 0 else 1 for i, day in enumerate(jan_dates)
                               if day.weekday() < 5 and i % 19 != 0)
        expected = (expected_present, 2, 8, 120, 240)
        monthly = api.employee_monthly("EMP100000", "2026-01")
        observed = (monthly["present_days"], monthly["leave_days"], monthly["late_count"],
                    monthly["total_late_minutes"], monthly["total_overtime_minutes"])
        assert observed == expected, {"observed": observed, "expected": expected}
        summary = api.department_summary("2026-01", "DEPT00")["items"][0]
        summary_expected = {"headcount": 250, "present_days": expected_present * 250,
                            "avg_work_hours": 7.72, "late_count": 2000,
                            "total_late_minutes": 30000, "leave_count": 500, "on_duty_count": 0}
        for field, value in summary_expected.items():
            assert summary[field] == value, (field, summary[field], value)
        board = api.late_leaderboard("2026-01", 10, "DEPT00")["items"]
        assert len(board) == 250 and {row["rank"] for row in board} == {1}, (len(board), board[:2])
        trend = api.department_trend("DEPT00", "2026-01-01", "2026-03-31")["items"]
        assert len(trend) == 90, len(trend)
        assert trend[0]["present_count"] == 0 and trend[0]["attendance_rate"] == 0.0, trend[0]
        page = api.list_attendance("EMP100000", "2026-01-01", "2026-04-10", None, 1, 100)
        assert page["total"] == 100 and len(page["items"]) == 100
        employees_page = api.list_employees("DEPT00", 1, 50)
        assert employees_page["total"] == 250 and len(employees_page["items"]) == 50
        assert employees_page["items"][0]["emp_code"] == "EMP100000"
        print("100k analytics matched independent monthly, department, ranking, and trend expectations.", flush=True)

        cases = [
            ("attendance_list", dict(emp_code="EMP100000", date_from="2026-01-01", date_to="2026-04-10", page=1, page_size=100)),
            ("employee_monthly", dict(emp_code="EMP100000", month="2026-01")),
            ("department_summary", dict(month="2026-01", department="DEPT00")),
            ("late_leaderboard", dict(month="2026-01", department="DEPT00", limit=10)),
            ("department_trend", dict(department="DEPT00", from_date="2026-01-01", to_date="2026-03-31")),
        ]
        for endpoint, args in cases:
            explain = api.explain(endpoint, **args)["explain"]
            nodes = list(walk(explain))
            winning_plans = [node["winningPlan"] for node in nodes if isinstance(node.get("winningPlan"), dict)]
            winning_nodes = [node for plan in winning_plans for node in walk(plan)]
            scans = [node for node in winning_nodes if node.get("stage") == "COLLSCAN"]
            scans += [node for node in nodes if isinstance(node.get("collectionScans"), int) and node["collectionScans"] > 0]
            indexes = sorted({node["indexName"] for node in winning_nodes if node.get("stage") == "IXSCAN"})
            indexes += sorted({index for node in nodes for index in node.get("indexesUsed", []) if isinstance(index, str)})
            assert indexes, f"{endpoint}: no IXSCAN in executionStats"
            assert not scans, f"{endpoint}: collection scan nodes {scans}"
            print(f"EXPLAIN {endpoint}: winning-plan/lookups used indexes {sorted(set(indexes))}; execution collection scans 0", flush=True)
            if endpoint == "department_trend":
                stats = [node["executionStats"] for node in nodes
                         if isinstance(node.get("executionStats"), dict)]
                for entry in stats:
                    print("TREND_EXECUTION_STATS " + " ".join(
                        f"{key}={entry[key]}" for key in
                        ("executionTimeMillis", "nReturned", "totalKeysExamined", "totalDocsExamined")
                        if key in entry
                    ), flush=True)
                lookup_stats = [node for node in nodes if "indexesUsed" in node or "collectionScans" in node]
                for entry in lookup_stats:
                    fields = ("stage", "nReturned", "totalKeysExamined", "totalDocsExamined",
                              "collectionScans", "indexesUsed")
                    print("TREND_LOOKUP_STATS " + " ".join(
                        f"{key}={entry[key]}" for key in fields if key in entry
                    ), flush=True)

        targeted_queries = [
            ("employee_list_department_sort", "employees", {
                "find": "employees", "filter": {"department": "DEPT00"},
                "sort": {"emp_code": 1}, "limit": 50}, "department_employee_code"),
            ("punch_out_latest_employee_punch", "attendance_logs", {
                "find": "attendance_logs", "filter": {"emp_code": "EMP100000", "punch_in": {"$lte": datetime(2026, 4, 10, tzinfo=timezone.utc)}},
                "sort": {"punch_in": -1}, "limit": 1}, "attendance_employee_punch_in"),
        ]
        for label, collection, command, expected_index in targeted_queries:
            explain = database.command("explain", command, verbosity="executionStats")
            nodes = list(walk(explain))
            winning = [node["winningPlan"] for node in nodes if isinstance(node.get("winningPlan"), dict)]
            winning_nodes = [node for plan in winning for node in walk(plan)]
            winning_indexes = {node.get("indexName") for node in winning_nodes if node.get("stage") == "IXSCAN"}
            assert expected_index in winning_indexes, (label, winning_indexes)
            assert not any(node.get("stage") == "COLLSCAN" for node in winning_nodes), label
            print(f"EXPLAIN {label}: winning plan uses {expected_index}; no COLLSCAN", flush=True)
        print("SCALE AUDIT PASSED: 100000 records", flush=True)
        if benchmark:
            run_http_benchmark(db_name)
    finally:
        try:
            asyncio.run(lifecycle.__aexit__(None, None, None))
        except Exception:
            pass
        cleanup.drop_database(db_name)
        cleanup.close()
        print(f"Dropped disposable scale database {db_name}", flush=True)


if __name__ == "__main__":
    main()
