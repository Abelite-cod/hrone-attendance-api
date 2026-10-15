# Implementation decisions

1. Indexes: Unique `employees.emp_code` identifies employees and joins; unique `attendance_logs(emp_code, date)` prevents duplicate daily records and supports monthly queries. `employees(department, emp_code)` supports sorted lists; department/join-date and joined-date indexes support analytics. Attendance date/employee supports sorted paging, employee/date/status supports filtered lists, and employee/punch-in supports punch-out. I considered an `employees(name)` index but rejected it because no contract endpoint searches or sorts by name.

2. Punch-in race: Both requests insert without a prior existence check. The unique employee/date index accepts one; MongoDB rejects the other as a duplicate, returned as 409. The winner returns 201.

3. Ties: MongoDB `$rank` gives equal late totals the same rank. Ties at the cutoff are all returned, even if that exceeds `limit`; employee code orders tied rows consistently.

4. Headcount: Summary starts with employees who joined by month end, then looks up monthly attendance. Employees without logs remain and count once; later joiners are excluded.

5. 100x data: I would profile workload and query plans first, then consider pre-aggregated summaries or sharding if measurements justify them. Both add freshness and operational costs; the 100k audit does not justify either yet.
