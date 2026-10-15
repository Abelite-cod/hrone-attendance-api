# Employee Attendance & Analytics API

FastAPI and MongoDB implementation for the HROne trainee assessment. It supports employee CRUD, attendance punch-in/out, corrections with audit history, MongoDB aggregation analytics, and explain diagnostics. MongoDB 6.0+ is required; Atlas 8.0.34 was used for the recorded integration and scale checks.

## Run locally (Windows PowerShell)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:MONGO_URI = "<your MongoDB URI>"
$env:MONGO_DB = "attendance_db"
uvicorn app.main:app --port 8000
```

Swagger UI: `http://127.0.0.1:8000/docs`. To load the supplied sample documents, run `python sample_seed.py` first against a disposable database. It replaces the contents of the `employees` and `attendance_logs` collections.

## Verification

Run local rule checks with `python -m unittest discover -s tests`. For MongoDB integration checks, set `MONGO_TEST_DB` to a new, unused disposable database name; the test creates fixtures and drops that database afterward.

The opt-in `python tests/scale_audit.py --benchmark` creates 1,000 employees and 100,000 attendance records, verifies analytics and explain plans, exercises the API over localhost HTTP, then drops the temporary database. Use a fresh disposable `MONGO_TEST_DB` each time. The benchmark is a small smoke comparison, not a production capacity or SLO claim. Latest run used Atlas MongoDB 8.0.34; MongoDB 6.0 was not separately tested.

The recorded department-trend optimization reduced explain documents examined from 180,090 to 22,500 on the 100k fixture. See `REVIEW.md` for the starter-code review and `DECISIONS.md` for the requested design answers. The supplied sample set is only 6 employees and 9 attendance records; the 100k audit is generated separately.
