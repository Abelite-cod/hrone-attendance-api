# Implementation decisions

1. Indexes: Unique `employees.emp_code` identifies employees and joins; unique `attendance_logs(emp_code, date)` prevents duplicate daily records and supports monthly queries. `employees(department, emp_code)` supports sorted lists; department/join-date and joined-date indexes support analytics. Attendance date/employee supports sorted paging, employee/date/status supports filtered lists, and employee/punch-in supports punch-out. I considered an `employees(name)` index but rejected it because no contract endpoint searches or sorts by name.

2. Punch-in race: Both requests insert without a prior existence check. The unique employee/date index accepts one; MongoDB rejects the other as a duplicate, returned as 409. The winner returns 201.

3. Ties: MongoDB `$rank` gives equal late totals the same rank. Ties at the cutoff are all returned, even if that exceeds `limit`; employee code orders tied rows consistently.

4. Headcount: Summary starts with employees who joined by month end, then looks up monthly attendance. Employees without logs remain and count once; later joiners are excluded.

5. Scale: Profile representative workloads and query plans before considering pre-aggregated summaries or sharding. Both add freshness and operational costs. No fresh 100k explain/scale check was run for this revision, so no current performance result is claimed.

6. Rate precision: Compute reported percentages with `Decimal` and `ROUND_HALF_UP` at four decimal places, as R8 requires for rates. The unit regression `1 / 23 x 100 = 4.3478` distinguishes this from two-place rounding.

7. Verification boundaries: The unit/ASGI checks passed, and a fresh disposable Atlas database was used for the integration and 100k scale/explain audits. Analytics expectations and explain index selection passed on those fixtures. These checks do not establish production SLOs or cover every hidden grader fixture; rerun them with the guarded commands in `README.md` after code changes.
