# Implementation decisions

1. Indexes: Unique `employees.emp_code` identifies employees and joins; unique `attendance_logs(emp_code, date)` prevents duplicate daily records and supports monthly queries. `employees(department, emp_code)` supports sorted lists; department/join-date and joined-date indexes support analytics. Attendance date/employee supports sorted paging, employee/date/status supports filtered lists, and employee/punch-in supports punch-out. I considered an `employees(name)` index but rejected it because no contract endpoint searches or sorts by name.

2. Punch-in race: Both requests insert without a prior existence check. The unique employee/date index accepts one; MongoDB rejects the other as a duplicate, returned as 409. The winner returns 201.

3. Ties: MongoDB `$rank` gives equal late totals the same rank. Ties at the cutoff are all returned, even if that exceeds `limit`; employee code orders tied rows consistently.

4. Headcount: Summary starts with employees who joined by month end, then looks up monthly attendance. Employees without logs remain and count once; later joiners are excluded.

5. Scale: Profile representative workloads and query plans before considering pre-aggregated summaries or sharding. Both add freshness and operational costs. Guarded 100k scale/explain checks passed on both `5e17a7f` and current commit `27eb112`; the current run did not use `--benchmark`, so its HTTP performance was not measured.

6. Rate precision: Compute reported percentages with `Decimal` and `ROUND_HALF_UP` at four decimal places, as R8 requires for rates. The unit regression `1 / 23 x 100 = 4.3478` distinguishes this from two-place rounding.

7. Verification boundaries: On current commit `27eb112f45b747e6d1933dd71758ef046c7f682f`, compilation passed and the full 12-test unit/ASGI/MongoDB suite passed against Atlas MongoDB 8.0.34 using fresh guarded database `attendance_disposable_full_261015`. A guarded 100k scale/explain audit passed on fresh `attendance_disposable_scale_261015`; both databases were dropped by cleanup. No HTTP benchmark was run on this revision (the historical `5e17a7f` scale run did include one). A read-only localhost ping failed because no MongoDB listener was available at localhost:27017; Atlas verification used the configured remote URI. These checks do not establish production SLOs or cover every hidden grader fixture; rerun them with the guarded commands in `README.md` after code changes.
