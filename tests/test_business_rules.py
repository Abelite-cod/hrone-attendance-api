import unittest
from datetime import datetime, timezone

from app.main import attendance_day, derived, late_minutes


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


if __name__ == "__main__":
    unittest.main()
