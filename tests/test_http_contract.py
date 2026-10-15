"""HTTP-level validation checks through FastAPI's ASGI interface (no MongoDB required)."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import patch

from app.main import app
from pymongo.errors import DuplicateKeyError, PyMongoError


async def asgi_request(method, target, payload=None):
    body = json.dumps(payload).encode() if payload is not None else b""
    parsed = urlsplit(target)
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": parsed.path,
        "raw_path": parsed.path.encode(), "query_string": parsed.query.encode(),
        "root_path": "", "headers": [(b"host", b"testserver"), (b"content-type", b"application/json")],
        "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
    }
    sent = []
    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    start = next(item for item in sent if item["type"] == "http.response.start")
    content = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
    return start["status"], json.loads(content) if content else None


class HttpContractTests(unittest.TestCase):
    def test_validation_status_and_body_over_http(self):
        status, body = asyncio.run(asgi_request("GET", "/employees?page=0"))
        self.assertEqual(status, 422)
        self.assertIn("detail", body)

        status, body = asyncio.run(asgi_request("GET", "/attendance?date_from="))
        self.assertEqual(status, 422)
        status, body = asyncio.run(asgi_request("GET", "/attendance?date_from=2026-07-10&date_to=2026-07-01"))
        self.assertEqual(status, 422)

        status, body = asyncio.run(asgi_request("POST", "/attendance/punch-in", {
            "emp_code": "EMP1234", "punched_at": "1783312500000",
        }))
        self.assertEqual(status, 422)
        self.assertIn("detail", body)

        status, body = asyncio.run(asgi_request("POST", "/attendance/punch-in", {
            "emp_code": "EMP1234", "punched_at": None,
        }))
        self.assertEqual(status, 422)
        self.assertIn("detail", body)

        status, body = asyncio.run(asgi_request("POST", "/employees", {
            "emp_code": "not-an-employee", "name": "A", "email": "bad", "department": "QA",
            "joined_on": "2026-02-30",
        }))
        self.assertEqual(status, 422)
        self.assertIn("detail", body)

    def test_served_openapi_has_nonempty_success_response_schemas(self):
        status, schema = asyncio.run(asgi_request("GET", "/openapi.json"))
        self.assertEqual(status, 200)
        for path, methods in schema["paths"].items():
            for method, operation in methods.items():
                if method not in {"get", "post", "patch"}:
                    continue
                success = "201" if method == "post" and path in {"/employees", "/attendance/punch-in"} else "200"
                response = operation["responses"][success]
                response_schema = response["content"]["application/json"]["schema"]
                self.assertTrue(response_schema, (path, method))

    def test_generated_openapi_calendar_response_constraints(self):
        schema = app.openapi()["components"]["schemas"]
        self.assertEqual(schema["AttendanceRecord"]["properties"]["date"]["format"], "date")
        self.assertEqual(schema["TrendRow"]["properties"]["date"]["format"], "date")
        self.assertEqual(
            schema["EmployeeMonthly"]["properties"]["month"]["pattern"],
            r"^\d{4}-(0[1-9]|1[0-2])$",
        )

    def test_conflict_and_database_errors_have_safe_http_bodies(self):
        class EmployeeCollection:
            def __init__(self, error):
                self.error = error

            def insert_one(self, _document):
                raise self.error

        payload = {
            "emp_code": "EMP1234", "name": "Test Employee", "email": "test@example.test",
            "department": "QA", "joined_on": "2026-07-01",
        }
        for error, expected in ((DuplicateKeyError("duplicate"), 409), (PyMongoError("private connection detail"), 503)):
            fake_db = SimpleNamespace(employees=EmployeeCollection(error))
            with patch("app.main.db", fake_db):
                status, body = asyncio.run(asgi_request("POST", "/employees", payload))
            self.assertEqual(status, expected)
            self.assertIn("detail", body)
            self.assertNotIn("private connection detail", json.dumps(body))

    def test_employee_response_model_is_applied_over_http(self):
        employee = {
            "emp_code": "EMP1234", "name": "Test Employee", "email": "test@example.test",
            "department": "QA", "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": "2026-07-01", "created_at": 1782864000000,
        }

        class Cursor:
            def sort(self, *_args): return self
            def skip(self, *_args): return self
            def limit(self, *_args): return self
            def __iter__(self): return iter([employee])

        class EmployeeCollection:
            def find(self, *_args): return Cursor()
            def count_documents(self, _query): return 1

        with patch("app.main.db", SimpleNamespace(employees=EmployeeCollection())):
            status, body = asyncio.run(asgi_request("GET", "/employees"))
        self.assertEqual(status, 200)
        self.assertEqual(body["items"][0]["emp_code"], "EMP1234")
        self.assertIsInstance(body["items"][0]["created_at"], int)
        self.assertNotIn("_id", body["items"][0])


if __name__ == "__main__":
    unittest.main()
