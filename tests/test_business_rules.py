import unittest
from datetime import datetime, timezone

from app.main import app, attendance_day, attendance_percentage, derived, late_minutes


class AttendanceRuleTests(unittest.TestCase):
    def setUp(self):
        self.day_emp = {"shift_start": "09:30", "shift_end": "18:30"}
        self.night_emp = {"shift_start": "22:00", "shift_end": "06:00"}

    def test_late_grace_is_strict_and_floor_minutes(self):
        self.assertEqual(late_minutes(datetime(2026, 7, 6, 4, 10, tzinfo=timezone.utc), self.day_emp), 0)
        self.assertEqual(late_minutes(datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc), self.day_emp), 10)
        self.assertEqual(late_minutes(datetime(2026, 7, 6, 4, 45, 59, tzinfo=timezone.utc), self.day_emp), 45)

    def test_overnight_work_date_for_after_midnight_punch(self):
        instant = datetime(2026, 7, 7, 0, 29, tzinfo=timezone.utc)
        self.assertEqual(attendance_day(instant, self.night_emp).isoformat(), "2026-07-06")
        self.assertEqual(late_minutes(instant, self.night_emp), 479)
        exact_shift_end = datetime(2026, 7, 7, 0, 30, tzinfo=timezone.utc)
        self.assertEqual(attendance_day(exact_shift_end, self.night_emp).isoformat(), "2026-07-07")

    def test_overtime_threshold_and_half_day(self):
        pi = datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc)
        self.assertEqual(derived(pi, datetime(2026, 7, 6, 13, 29, 59, tzinfo=timezone.utc), self.day_emp)["overtime_minutes"], 0)
        self.assertEqual(derived(pi, datetime(2026, 7, 6, 13, 30, tzinfo=timezone.utc), self.day_emp)["overtime_minutes"], 30)
        short_in = datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc)
        short_out = datetime(2026, 7, 6, 8, 29, 0, tzinfo=timezone.utc)
        self.assertTrue(derived(short_in, short_out, self.day_emp)["half_day"])
        rounded_boundary = datetime(2026, 7, 6, 8, 29, 42, tzinfo=timezone.utc)
        self.assertEqual(derived(short_in, rounded_boundary, self.day_emp)["work_hours"], 4.5)
        self.assertFalse(derived(short_in, rounded_boundary, self.day_emp)["half_day"])

    def test_timestamp_seconds_are_truncated_before_rule_calculations(self):
        fractional = datetime(2026, 7, 6, 4, 10, 0, 900000, tzinfo=timezone.utc)
        self.assertEqual(late_minutes(fractional, self.day_emp), 0)

    def test_monthly_rate_uses_four_decimal_half_up(self):
        rate = attendance_percentage(1, 23)
        self.assertEqual(rate, 4.3478)
        self.assertNotEqual(rate, 4.35)

    def test_generated_openapi_has_success_schemas_and_contract_statuses(self):
        schema = app.openapi()
        expected = {
            ("/health", "get"): {"200", "503"},
            ("/employees", "post"): {"201", "409", "422", "503"},
            ("/employees", "get"): {"200", "422", "503"},
            ("/attendance/punch-in", "post"): {"201", "404", "409", "422", "503"},
            ("/attendance/punch-out", "post"): {"200", "404", "409", "422", "503"},
            ("/attendance", "get"): {"200", "422", "503"},
            ("/attendance/{emp_code}/{date}", "patch"): {"200", "404", "409", "422", "503"},
            ("/analytics/employees/{emp_code}/monthly", "get"): {"200", "404", "422", "503"},
            ("/analytics/departments/summary", "get"): {"200", "422", "503"},
            ("/analytics/leaderboard/late", "get"): {"200", "422", "503"},
            ("/analytics/departments/{department}/trend", "get"): {"200", "404", "422", "503"},
            ("/admin/explain/{endpoint}", "get"): {"200", "422", "503"},
        }
        for (path, method), statuses in expected.items():
            operation = schema["paths"][path][method]
            self.assertTrue(statuses <= operation["responses"].keys(), (path, method))
            success = operation["responses"][next(code for code in statuses if code not in {"404", "409", "422", "503"})]
            self.assertIn("schema", success["content"]["application/json"])
            self.assertNotEqual(success["content"]["application/json"]["schema"], {})

        models = schema["components"]["schemas"]
        employee_body = models["EmployeeIn"]["properties"]
        self.assertEqual(employee_body["joined_on"]["format"], "date")
        punch_body = models["PunchInIn"]["properties"]
        self.assertEqual(punch_body["punched_at"]["type"], "integer")
        self.assertEqual((punch_body["punched_at"]["minimum"], punch_body["punched_at"]["maximum"]), (100000000000, 4102444800000))
        self.assertNotIn("_id", models["AttendanceRecord"]["properties"])


if __name__ == "__main__":
    unittest.main()
