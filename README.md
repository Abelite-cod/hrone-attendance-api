# Employee Attendance & Analytics API

FastAPI and MongoDB implementation for the HROne trainee assessment. It supports employee CRUD, attendance punch-in/out, corrections with audit history, MongoDB aggregation analytics, and explain diagnostics. `openapi.yaml` and `DATA_MODEL.md` define the contract and storage model. The pipelines use MongoDB 6.0+ features.

## Run locally (Windows PowerShell)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:MONGO_URI = "<your MongoDB URI>"
$env:MONGO_DB = "attendance_db"
uvicorn app.main:app --port 8000
```

Set configuration through environment variables; do not place a `.env` file in the submission. Swagger UI: `http://127.0.0.1:8000/docs`. To load sample documents, choose a new database name beginning `attendance_seed_`, set `MONGO_DB`, then run `python sample_seed.py`. The seed refuses an existing database.

## Verification

Run syntax checks with `python -m compileall -q app tests sample_seed.py` and the unit/ASGI checks with `python -m unittest discover -s tests -p 'test_*.py' -v`. For MongoDB checks, set `MONGO_URI` and choose a new database name beginning `attendance_disposable_` as `MONGO_TEST_DB`. The integration test verifies the name is absent before creating fixtures and drops only that database afterward. If `MONGO_TEST_DB` is unset, the integration test skips.

The opt-in `python tests/scale_audit.py --benchmark` creates 1,000 employees and 100,000 attendance records, checks analytics and explain plans, exercises the API over localhost HTTP, then drops the temporary database. It accepts only a fresh `attendance_disposable_` database. It is a smoke check, not a production capacity or SLO claim.

## Verification record

On commit `27eb112f45b747e6d1933dd71758ef046c7f682f` (the current schema-annotation revision), `python -m compileall -q app tests sample_seed.py` passed and `python -m unittest discover -s tests -p 'test_*.py' -v` passed all 12 tests with Atlas MongoDB 8.0.34. This includes the unit/ASGI checks and the MongoDB integration test, run against fresh guarded database `attendance_disposable_full_261015`, which was dropped by test cleanup. The generated OpenAPI checks cover routes/statuses and response schemas, including calendar formats and pattern constraints. The guarded 100k scale/explain audit also passed against fresh database `attendance_disposable_scale_261015`, which was dropped by its cleanup. It matched independent monthly, department, leaderboard, and trend expectations; five endpoint explains and two targeted explains used indexes with zero collection scans. The scale audit was run without `--benchmark`, so no HTTP benchmark result is claimed for this commit.

Historical database evidence also applies to commit `5e17a7f` (`Fix attendance API contract and verification`), before the schema-only commit above: Atlas MongoDB 8.0.34 integration passed and the guarded 100k scale audit passed analytics/explain checks, HTTP contract audit, and benchmark. Both commits' checks are assignment-sized smoke checks, not production capacity or SLO claims. The supplied sample set contains 6 employees and 9 attendance records; it does not establish analytics correctness on its own. See `REVIEW.md` for the evidence split and `DECISIONS.md` for implementation choices.
