"""Opt-in MongoDB checks. Set MONGO_TEST_DB to a new disposable database name."""
import importlib
import json
import os
import sys
import unittest
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import MongoClient


class MongoApiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_dotenv()
        cls.test_db_name = os.getenv("MONGO_TEST_DB")
        if not cls.test_db_name:
            raise unittest.SkipTest("Set MONGO_TEST_DB to an unused disposable database name")
        if not cls.test_db_name.startswith("attendance_disposable_"):
            raise RuntimeError("Refusing integration checks unless MONGO_TEST_DB starts with attendance_disposable_")
        uri = os.getenv("MONGO_URI")
        if not uri:
            raise unittest.SkipTest("MONGO_URI is not configured")
        cls.cleanup_client = MongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=5000)
        if cls.test_db_name in cls.cleanup_client.list_database_names():
            cls.cleanup_client.close()
            raise RuntimeError("Refusing to use or clear an existing MONGO_TEST_DB")
        cls.addClassCleanup(cls._cleanup_test_database)
        os.environ["MONGO_DB"] = cls.test_db_name
        # The unit-test module may already have imported app.main using the normal DB.
        prior = sys.modules.pop("app.main", None)
        if prior is not None:
            prior.client.close()
        cls.api = importlib.import_module("app.main")
        if cls.api.db.name != cls.test_db_name:
            raise AssertionError("Integration test did not select MONGO_TEST_DB")
        cls.lifespan = cls.api.app.router.lifespan_context(cls.api.app)
        asyncio.run(cls.lifespan.__aenter__())
        cls.db = cls.api.db
        employee_index_names = {entry["name"] for entry in cls.db.employees.list_indexes()}
        attendance_index_names = {entry["name"] for entry in cls.db.attendance_logs.list_indexes()}
        if not {"employee_joined_on", "department_employee_code"} <= employee_index_names:
            raise AssertionError("Startup did not create the employee analytics/list indexes")
        if "attendance_employee_punch_in" not in attendance_index_names:
            raise AssertionError("Startup did not create the punch-out query index")
        cls.db.employees.insert_many([
            {"emp_code":"EMP8001","name":"Asha Rao","email":"asha@qa.test","department":"QA","shift_start":"09:30","shift_end":"18:30","joined_on":"2026-07-01","created_at":datetime(2026,7,1,tzinfo=timezone.utc)},
            {"emp_code":"EMP8002","name":"Vikram Shah","email":"vikram@qa.test","department":"QA","shift_start":"09:30","shift_end":"18:30","joined_on":"2026-07-01","created_at":datetime(2026,7,1,tzinfo=timezone.utc)},
            {"emp_code":"EMP8003","name":"No Logs","email":"nologs@dev.test","department":"Dev","shift_start":"09:30","shift_end":"18:30","joined_on":"2026-07-15","created_at":datetime(2026,7,15,tzinfo=timezone.utc)},
            {"emp_code":"EMP8004","name":"Tie Result","email":"tie@qa.test","department":"QA","shift_start":"09:30","shift_end":"18:30","joined_on":"2026-07-09","created_at":datetime(2026,7,9,tzinfo=timezone.utc)},
        ])
        cls.db.attendance_logs.insert_many([
            {"emp_code":"EMP8001","date":"2026-07-06","status":"PRESENT","punch_in":datetime(2026,7,6,4,tzinfo=timezone.utc),"punch_out":datetime(2026,7,6,13,tzinfo=timezone.utc),"work_hours":8.0,"late_minutes":20,"overtime_minutes":0,"half_day":False,"history":[]},
            {"emp_code":"EMP8001","date":"2026-07-07","status":"WFH","punch_in":datetime(2026,7,7,4,tzinfo=timezone.utc),"punch_out":datetime(2026,7,7,8,15,tzinfo=timezone.utc),"work_hours":4.25,"late_minutes":10,"overtime_minutes":0,"half_day":True,"history":[]},
            {"emp_code":"EMP8002","date":"2026-07-06","status":"PRESENT","punch_in":datetime(2026,7,6,4,tzinfo=timezone.utc),"punch_out":datetime(2026,7,6,13,tzinfo=timezone.utc),"work_hours":7.5,"late_minutes":20,"overtime_minutes":0,"half_day":False,"history":[]},
            {"emp_code":"EMP8002","date":"2026-07-08","status":"LEAVE","punch_in":None,"punch_out":None,"work_hours":None,"late_minutes":0,"overtime_minutes":0,"half_day":False,"history":[]},
            {"emp_code":"EMP8004","date":"2026-07-09","status":"PRESENT","punch_in":datetime(2026,7,9,4,tzinfo=timezone.utc),"punch_out":datetime(2026,7,9,13,tzinfo=timezone.utc),"work_hours":8.0,"late_minutes":30,"overtime_minutes":0,"half_day":False,"history":[]},
        ])

    @classmethod
    def _cleanup_test_database(cls):
        if getattr(cls, "lifespan", None):
            asyncio.run(cls.lifespan.__aexit__(None, None, None))
        if getattr(cls, "cleanup_client", None):
            cls.cleanup_client.drop_database(cls.test_db_name)
            cls.cleanup_client.close()

    def test_crud_analytics_concurrency_and_explain(self):
        self.assertEqual(self.api.health(), {"status":"ok"})
        employee_body = self.api.EmployeeIn(emp_code="EMP9001", name="Concurrent Create", email="create@qa.test",
                                            department="QA", joined_on="2026-07-01")
        def create_employee():
            try:
                return (201, self.api.create_employee(employee_body))
            except Exception as exc:
                return (getattr(exc, "status_code", None), None)
        with ThreadPoolExecutor(max_workers=2) as pool:
            create_results = list(pool.map(lambda _: create_employee(), range(2)))
        self.assertEqual(sorted(code for code, _ in create_results), [201, 409])
        created_employee = next(payload for code, payload in create_results if code == 201)
        self.assertNotIn("_id", created_employee)
        self.assertIsInstance(created_employee["created_at"], int)
        self.assertTrue({"emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at"} <= created_employee.keys())
        self.db.attendance_logs.insert_one({
            "emp_code":"EMP9001", "date":"2026-07-11", "status":"WFH",
            "punch_in":datetime(2026,7,11,4,tzinfo=timezone.utc),
            "punch_out":datetime(2026,7,11,13,tzinfo=timezone.utc),
            "work_hours":9.0, "late_minutes":0, "overtime_minutes":30,
            "half_day":False, "history":[]})

        # Read APIs, filters, pagination, and no-log employees.
        employees = self.api.list_employees(department="QA", page=1, page_size=2)
        self.assertEqual((employees["total"], employees["page"], len(employees["items"])), (4, 1, 2))
        self.assertTrue({"items", "total", "page", "page_size"} <= employees.keys())
        attendance = self.api.list_attendance(emp_code="EMP8001", date_from="2026-07-06", date_to="2026-07-07",
                                              status=None, page=1, page_size=1)
        self.assertEqual((attendance["total"], len(attendance["items"])), (2, 1))
        self.assertTrue({"emp_code", "date", "status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day", "history"} <= attendance["items"][0].keys())
        self.assertEqual(attendance["items"][0]["date"], "2026-07-07")
        self.assertEqual(self.api.list_attendance(emp_code="EMP8001", date_from="2026-07-06", date_to="2026-07-07",
                                                  status="LEAVE", page=1, page_size=20)["total"], 0)
        monthly = self.api.employee_monthly("EMP8001", "2026-07")
        self.assertTrue({"emp_code", "month", "working_days", "present_days", "leave_days", "late_count", "total_late_minutes", "total_overtime_minutes", "attendance_pct"} <= monthly.keys())
        self.assertEqual(monthly["present_days"], 1.5)
        summary = self.api.department_summary("2026-07")["items"]
        qa = next(row for row in summary if row["department"] == "QA")
        self.assertTrue({"department", "headcount", "present_days", "avg_work_hours", "late_count", "total_late_minutes", "leave_count", "on_duty_count"} <= qa.keys())
        self.assertEqual(qa["headcount"], 4)
        self.assertEqual(qa["present_days"], 3.5)
        self.assertEqual(qa["avg_work_hours"], 7.35)
        dev = next(row for row in summary if row["department"] == "Dev")
        self.assertEqual((dev["headcount"], dev["present_days"], dev["avg_work_hours"]), (1, 0, None))
        board = self.api.late_leaderboard("2026-07", 1)["items"]
        self.assertTrue({"rank", "emp_code", "name", "department", "total_late_minutes", "late_count"} <= board[0].keys())
        self.assertEqual([(row["emp_code"],row["rank"]) for row in board], [("EMP8001",1),("EMP8004",1)])
        trend = self.api.department_trend("QA", "2026-07-06", "2026-07-08")["items"]
        self.assertTrue({"date", "is_working_day", "headcount", "present_count", "late_count", "attendance_rate", "moving_avg_7d"} <= trend[0].keys())
        self.assertEqual(len(trend), 3)
        self.assertEqual([row["attendance_rate"] for row in trend], [0.6667, 0.1667, 0.0])
        self.assertEqual(trend[-1]["moving_avg_7d"], 0.2778)
        weekend_trend = self.api.department_trend("QA", "2026-07-09", "2026-07-12")["items"]
        self.assertEqual(len(weekend_trend), 4)
        self.assertEqual((weekend_trend[2]["is_working_day"], weekend_trend[2]["present_count"], weekend_trend[2]["attendance_rate"]), (False, 1, None))
        weekend_monthly = self.api.employee_monthly("EMP9001", "2026-07")
        self.assertEqual((weekend_monthly["present_days"], weekend_monthly["total_overtime_minutes"]), (0, 30))
        for call, expected in (
            (lambda: self.api.employee_monthly("EMP8003", "2026-07"), {"present_days": 0, "attendance_pct": 0}),
            (lambda: self.api.department_summary("2026-07", department="missing"), {"items": []}),
        ):
            for key, value in expected.items():
                self.assertEqual(call()[key], value)

        # All five explain branches must return JSON-safe MongoDB explain documents.
        explain_cases = [
            ("attendance_list", {"date_from":"2026-07-06", "date_to":"2026-07-08", "page":1, "page_size":20}),
            ("employee_monthly", {"emp_code":"EMP8001", "month":"2026-07"}),
            ("department_summary", {"month":"2026-07", "department":"QA"}),
            ("late_leaderboard", {"month":"2026-07", "limit":1, "department":"QA"}),
            ("department_trend", {"department":"QA", "from_date":"2026-07-06", "to_date":"2026-07-08"}),
        ]
        for name, args in explain_cases:
            result = self.api.explain(name, **args)
            self.assertEqual(result["endpoint"], name)
            json.dumps(result)
        explained = self.api.explain("attendance_list", limit=10, date_from="2026-07-06", date_to="2026-07-08", page=1, page_size=20)
        self.assertEqual(explained["endpoint"], "attendance_list")
        json.dumps(explained)

        body = {"emp_code":"EMP8003","punched_at":1783339200000}
        def punch():
            try:
                return self.api.punch_in(self.api.PunchInIn(**body))["date"]
            except Exception as exc:
                return getattr(exc, "status_code", None)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: punch(), range(2)))
        self.assertEqual(sum(isinstance(x, str) for x in outcomes), 1)
        self.assertEqual(sum(x == 409 for x in outcomes), 1)
        created = self.db.attendance_logs.find_one({"emp_code":"EMP8003"})
        punch_out_ms = int(datetime(2026,7,6,13,30,tzinfo=timezone.utc).timestamp()*1000)
        def close_record():
            try:
                self.api.punch_out(self.api.PunchOutIn(emp_code="EMP8003",punched_at=punch_out_ms))
                return 200
            except Exception as exc:
                return getattr(exc, "status_code", None)
        with ThreadPoolExecutor(max_workers=2) as pool:
            close_outcomes = list(pool.map(lambda _: close_record(), range(2)))
        self.assertEqual(sorted(close_outcomes), [200, 409])
        corrected = self.api.regularize("EMP8001", "2026-07-06", self.api.RegularizeIn(**{
            "punch_in": int(datetime(2026,7,6,4,5,tzinfo=timezone.utc).timestamp()*1000),
            "reason":"clock fix", "regularized_by":"integration"}))
        self.assertEqual(len(corrected["history"]), 1)
        self.assertIsNotNone(created)

        # Two corrections racing on the same state must preserve history and report a conflict.
        correction_times = [
            int(datetime(2026,7,6,4,10,tzinfo=timezone.utc).timestamp()*1000),
            int(datetime(2026,7,6,4,15,tzinfo=timezone.utc).timestamp()*1000),
        ]
        def correct(ms):
            try:
                result = self.api.regularize("EMP8001", "2026-07-06", self.api.RegularizeIn(
                    punch_in=ms, reason="race correction", regularized_by="integration"))
                return (200, result)
            except Exception as exc:
                return (getattr(exc, "status_code", None), None)
        with ThreadPoolExecutor(max_workers=2) as pool:
            correction_results = list(pool.map(correct, correction_times))
        correction_successes = sum(code == 200 for code, _ in correction_results)
        self.assertGreaterEqual(correction_successes, 1)
        self.assertTrue(all(code in (200, 409) for code, _ in correction_results))
        final_record = self.db.attendance_logs.find_one({"emp_code":"EMP8001", "date":"2026-07-06"})
        self.assertEqual(len(final_record["history"]), 1 + correction_successes)

        # Empty database behavior and missing-resource status codes.
        self.db.attendance_logs.delete_many({})
        self.db.employees.delete_many({})
        self.assertEqual(self.api.list_employees(page=1, page_size=20)["items"], [])
        self.assertEqual(self.api.list_attendance(page=1, page_size=20)["total"], 0)
        self.assertEqual(self.api.department_summary("2026-07")["items"], [])
        self.assertEqual(self.api.late_leaderboard("2026-07", 10)["items"], [])
        for operation in (
            lambda: self.api.employee_monthly("EMP8001", "2026-07"),
            lambda: self.api.punch_in(self.api.PunchInIn(emp_code="EMP8001")),
            lambda: self.api.punch_out(self.api.PunchOutIn(emp_code="EMP8001")),
            lambda: self.api.regularize("EMP8001", "2026-07-06", self.api.RegularizeIn(
                status="LEAVE", reason="missing employee", regularized_by="integration")),
            lambda: self.api.department_trend("QA", "2026-07-01", "2026-07-03"),
        ):
            with self.assertRaises(self.api.HTTPException) as error:
                operation()
            self.assertEqual(error.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
