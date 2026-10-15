"""Employee attendance API. All persisted instants are UTC BSON dates; API instants are epoch ms."""
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from bson import json_util
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, tzinfo=UTC, serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = (*PRESENCE, "ABSENT", "LEAVE")
EPOCH_MIN, EPOCH_MAX = 100_000_000_000, 4_102_444_800_000
EpochMillis = Annotated[int, Field(ge=EPOCH_MIN, le=EPOCH_MAX, json_schema_extra={"format": "int64"})]
NullableEpochMillis = EpochMillis | None

@asynccontextmanager
async def lifespan(_: FastAPI):
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="department_employee_code")
    db.employees.create_index([("joined_on", ASCENDING)], name="employee_joined_on")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="attendance_employee_punch_in")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="attendance_employee_date_status")
    yield
    client.close()

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)

class Employee(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str = Field(json_schema_extra={"format": "date"})
    created_at: EpochMillis

class EmployeePage(BaseModel):
    items: list[Employee]
    total: int
    page: int
    page_size: int

class HistoryEntry(BaseModel):
    at: EpochMillis
    by: str
    reason: str
    changes: dict[str, dict[str, Any]]

class AttendanceRecord(BaseModel):
    emp_code: str
    date: str = Field(json_schema_extra={"format": "date"})
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
    punch_in: NullableEpochMillis
    punch_out: NullableEpochMillis
    work_hours: float | None
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: list[HistoryEntry]

class AttendancePage(BaseModel):
    items: list[AttendanceRecord]
    total: int
    page: int
    page_size: int

class Health(BaseModel):
    status: Literal["ok"]

class EmployeeMonthly(BaseModel):
    emp_code: str
    month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: float | None

class DepartmentRow(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: float | None
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int

class DepartmentSummary(BaseModel):
    month: str
    items: list[DepartmentRow]

class LeaderboardRow(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int

class Leaderboard(BaseModel):
    month: str
    items: list[LeaderboardRow]

class TrendRow(BaseModel):
    date: str = Field(json_schema_extra={"format": "date"})
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: float | None
    moving_avg_7d: float | None

class Trend(BaseModel):
    department: str
    items: list[TrendRow]

class ExplainResponse(BaseModel):
    endpoint: str
    collection: str
    explain: dict[str, Any]

class ErrorResponse(BaseModel):
    detail: str

@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, __: PyMongoError):
    return JSONResponse(status_code=503, content={"detail": "Database operation failed"})

def parse_day(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value: raise ValueError
        return parsed
    except (TypeError, ValueError):
        raise HTTPException(422, "Date must be YYYY-MM-DD")

def parse_month(value: str) -> tuple[date, date]:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise HTTPException(422, "Month must be YYYY-MM")
    y, m = map(int, value.split("-"))
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])

def epoch_datetime(value: int | None) -> datetime:
    if value is None:
        return datetime.now(UTC).replace(microsecond=0)
    if type(value) is not int or not EPOCH_MIN <= value <= EPOCH_MAX:
        raise HTTPException(422, "Timestamp must be epoch milliseconds in the supported range")
    return datetime.fromtimestamp(value // 1000, UTC)

def to_ms(value: datetime | None) -> int | None:
    if value is None: return None
    if value.tzinfo is None: value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)

def json_value(value: Any) -> Any:
    if isinstance(value, datetime): return to_ms(value)
    if isinstance(value, dict): return {k: json_value(v) for k, v in value.items() if k != "_id"}
    if isinstance(value, list): return [json_value(v) for v in value]
    return value

def record_out(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    out.setdefault("punch_in", None); out.setdefault("punch_out", None)
    out.setdefault("work_hours", None); out.setdefault("late_minutes", 0)
    out.setdefault("overtime_minutes", 0); out.setdefault("half_day", False); out.setdefault("history", [])
    return json_value(out)

def shift_start_dt(punch: datetime, shift_start: str, shift_end: str) -> datetime:
    local = punch.astimezone(IST)
    h, m = map(int, shift_start.split(":"))
    start_day = local.date()
    if tuple(map(int, shift_end.split(":"))) <= (h, m) and (local.hour, local.minute) < tuple(map(int, shift_end.split(":"))):
        start_day -= timedelta(days=1)
    return datetime.combine(start_day, time(h, m), IST).astimezone(UTC)

def shift_end_dt(punch_in: datetime, shift_start: str, shift_end: str) -> datetime:
    local_in = punch_in.astimezone(IST)
    sh, sm = map(int, shift_start.split(":")); eh, em = map(int, shift_end.split(":"))
    overnight = (eh, em) <= (sh, sm)
    work_date = local_in.date()
    if overnight and (local_in.hour, local_in.minute) < (eh, em): work_date -= timedelta(days=1)
    end_day = work_date + (timedelta(days=1) if overnight else timedelta())
    return datetime.combine(end_day, time(eh, em), IST).astimezone(UTC)

def attendance_day(punch: datetime, employee: dict) -> date:
    punch = punch.replace(microsecond=0)
    local = punch.astimezone(IST); start = tuple(map(int, employee["shift_start"].split(":")))
    end = tuple(map(int, employee["shift_end"].split(":")))
    d = local.date()
    if end <= start and (local.hour, local.minute) < end: d -= timedelta(days=1)
    return d

def late_minutes(punch: datetime, employee: dict) -> int:
    punch = punch.replace(microsecond=0)
    start = shift_start_dt(punch, employee["shift_start"], employee["shift_end"])
    elapsed = int((punch - start).total_seconds())
    return elapsed // 60 if elapsed > 600 else 0

def derived(punch_in: datetime | None, punch_out: datetime | None, employee: dict) -> dict:
    if not punch_in:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    punch_in = punch_in.replace(microsecond=0)
    if punch_out is not None:
        punch_out = punch_out.replace(microsecond=0)
    late = late_minutes(punch_in, employee)
    if not punch_out: return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    seconds = int((punch_out - punch_in).total_seconds())
    hours = float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    overtime_delta = int((punch_out - shift_end_dt(punch_in, employee["shift_start"], employee["shift_end"])).total_seconds())
    overtime = overtime_delta // 60 if overtime_delta >= 1800 else 0
    return {"work_hours": hours, "late_minutes": late, "overtime_minutes": overtime, "half_day": hours < 4.50}

def working_days(start: date, end: date) -> int:
    if end < start: return 0
    weeks, rem = divmod((end - start).days + 1, 7)
    return weeks * 5 + sum((start + timedelta(days=weeks * 7 + i)).weekday() < 5 for i in range(rem))

def attendance_percentage(present_days: float, days: int) -> float | None:
    if not days:
        return None
    return float((Decimal(str(present_days)) * Decimal(100) / Decimal(days)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))

def must_employee(code: str) -> dict:
    emp = db.employees.find_one({"emp_code": code}, {"_id": 0})
    if not emp: raise HTTPException(404, "Employee not found")
    return emp

class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(json_schema_extra={"format": "date"})
    @field_validator("joined_on")
    @classmethod
    def valid_date(cls, v): parse_day(v); return v
    @model_validator(mode="after")
    def different_shift_times(self):
        if self.shift_start == self.shift_end: raise ValueError("shift_start must differ from shift_end")
        return self

class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: StrictInt = Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX, json_schema_extra={"format": "int64"})
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"
    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, v):
        if v is not None and (type(v) is not int or not EPOCH_MIN <= v <= EPOCH_MAX): raise ValueError("Invalid epoch milliseconds")
        return v
    @model_validator(mode="after")
    def reject_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None: raise ValueError("punched_at cannot be null")
        return self

class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: StrictInt = Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX, json_schema_extra={"format": "int64"})
    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, v):
        if v is not None and (type(v) is not int or not EPOCH_MIN <= v <= EPOCH_MAX): raise ValueError("Invalid epoch milliseconds")
        return v
    @model_validator(mode="after")
    def reject_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None: raise ValueError("punched_at cannot be null")
        return self

class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] = None
    punch_in: StrictInt = Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX, json_schema_extra={"format": "int64"})
    punch_out: StrictInt = Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX, json_schema_extra={"format": "int64"})
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_timestamp(cls, v):
        if v is not None and (type(v) is not int or not EPOCH_MIN <= v <= EPOCH_MAX): raise ValueError("Invalid epoch milliseconds")
        return v
    @model_validator(mode="after")
    def reject_explicit_nulls(self):
        if "status" in self.model_fields_set and self.status is None: raise ValueError("status cannot be null")
        for field in ("punch_in", "punch_out"):
            if field in self.model_fields_set and getattr(self, field) is None: raise ValueError(f"{field} cannot be null")
        return self

@app.get("/health", response_model=Health, responses={503: {"model": ErrorResponse, "description": "Database unavailable"}})
def health():
    try: db.command("ping")
    except PyMongoError: raise HTTPException(503, "Database unavailable")
    return {"status": "ok"}

@app.post("/employees", status_code=201, response_model=Employee, responses={409: {"model": ErrorResponse, "description": "Conflict"}, 503: {"model": ErrorResponse, "description": "Database unavailable"}})
def create_employee(body: EmployeeIn):
    doc = body.model_dump(); doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try: db.employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, "emp_code already exists")
    return json_value(doc)

@app.get("/employees", response_model=EmployeePage, responses={503: {"model": ErrorResponse, "description": "Database unavailable"}})
def list_employees(department: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    query = {"department": department} if department is not None else {}
    items = list(db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page-1)*page_size).limit(page_size))
    return {"items": json_value(items), "total": db.employees.count_documents(query), "page": page, "page_size": page_size}

@app.post("/attendance/punch-in", status_code=201, response_model=AttendanceRecord, responses={404: {"model": ErrorResponse, "description": "Not found"}, 409: {"model": ErrorResponse, "description": "Conflict"}, 503: {"model": ErrorResponse, "description": "Database unavailable"}})
def punch_in(body: PunchInIn):
    emp = must_employee(body.emp_code); ts = epoch_datetime(body.punched_at)
    day = attendance_day(ts, emp).isoformat()
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": ts, "punch_out": None,
           **derived(ts, None, emp), "history": []}
    try: db.attendance_logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, "Already punched in for this date")
    return record_out(doc)

@app.post("/attendance/punch-out", response_model=AttendanceRecord, responses={404: {"model": ErrorResponse, "description": "Not found"}, 409: {"model": ErrorResponse, "description": "Conflict"}, 503: {"model": ErrorResponse, "description": "Database unavailable"}})
def punch_out(body: PunchOutIn):
    emp = must_employee(body.emp_code); ts = epoch_datetime(body.punched_at)
    doc = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if not doc: raise HTTPException(404, "No punch-in found")
    pi = doc.get("punch_in")
    if not pi: raise HTTPException(404, "No punch-in found")
    pi = pi.replace(microsecond=0)
    seconds = (ts - pi).total_seconds()
    if seconds <= 0 or seconds > 86400: raise HTTPException(422, "Punch-out must be after punch-in and within 24 hours")
    fields = {"punch_in": pi, "punch_out": ts, **derived(pi, ts, emp)}
    result = db.attendance_logs.find_one_and_update({"_id": doc["_id"], "punch_out": None}, {"$set": fields}, return_document=ReturnDocument.AFTER)
    if result is None: raise HTTPException(409, "Attendance record is already punched out")
    return record_out(result)

@app.get("/attendance", response_model=AttendancePage, responses={503: {"model": ErrorResponse, "description": "Database unavailable"}})
def list_attendance(emp_code: str | None = None, date_from: Annotated[str | None, Query(json_schema_extra={"format": "date"})] = None, date_to: Annotated[str | None, Query(json_schema_extra={"format": "date"})] = None,
                    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q: dict[str, Any] = {}
    if emp_code is not None: q["emp_code"] = emp_code
    if date_from is not None:
        parse_day(date_from); q.setdefault("date", {})["$gte"] = date_from
    if date_to is not None:
        parse_day(date_to); q.setdefault("date", {})["$lte"] = date_to
    if date_from is not None and date_to is not None and date_from > date_to: raise HTTPException(422, "date_from must be on or before date_to")
    if status: q["status"] = status
    total = db.attendance_logs.count_documents(q)
    docs = db.attendance_logs.find(q, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page-1)*page_size).limit(page_size)
    return {"items": [record_out(d) for d in docs], "total": total, "page": page, "page_size": page_size}

@app.patch("/attendance/{emp_code}/{date}", response_model=AttendanceRecord, responses={404: {"model": ErrorResponse, "description": "Not found"}, 409: {"model": ErrorResponse, "description": "Conflict"}, 503: {"model": ErrorResponse, "description": "Database unavailable"}})
def regularize(emp_code: str, date: Annotated[str, Path(json_schema_extra={"format": "date"})], body: RegularizeIn):
    parse_day(date); emp = must_employee(emp_code)
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if old is None: raise HTTPException(404, "Attendance record not found")
    status = body.status if body.status is not None else old["status"]
    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None: raise HTTPException(422, "Absent and leave records cannot have punch times")
        pi = po = None
    else:
        pi = epoch_datetime(body.punch_in) if body.punch_in is not None else old.get("punch_in")
        po = epoch_datetime(body.punch_out) if body.punch_out is not None else old.get("punch_out")
        if pi is not None: pi = pi.replace(microsecond=0)
        if po is not None: po = po.replace(microsecond=0)
        if not pi: raise HTTPException(422, "Presence status requires punch_in")
        if attendance_day(pi, emp).isoformat() != date: raise HTTPException(422, "punch_in must belong to the record date")
        if po and ((po-pi).total_seconds() <= 0 or (po-pi).total_seconds() > 86400): raise HTTPException(422, "Invalid punch interval")
    new = {"status": status, "punch_in": pi, "punch_out": po, **derived(pi, po, emp)}
    before = {k: old.get(k, False if k == "half_day" else None if k in ("punch_in", "punch_out", "work_hours") else 0) for k in new}
    changes = {k: {"from": before[k], "to": v} for k, v in new.items() if before[k] != v}
    if not changes: raise HTTPException(422, "Correction does not change the record")
    entry = {"at": datetime.now(UTC).replace(microsecond=0), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Compare-and-swap on the complete prior state prevents lost edits and history entries.
    expected = {"emp_code": emp_code, "date": date, "status": old["status"]}
    for field in ("punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day", "history"):
        expected[field] = old[field] if field in old else {"$exists": False}
    result = db.attendance_logs.find_one_and_update(expected, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if result is None: raise HTTPException(409, "Record changed concurrently; retry the correction")
    return record_out(result)

def month_match(start: date, end: date, **extra):
    return {**extra, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}

def employee_monthly_pipeline(emp_code: str, start: date, end: date):
    return [{"$match": month_match(start, end, emp_code=emp_code)}, {"$group": {"_id": None,
        "present_raw": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$not": [{"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [1, 7]]}]}]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
        "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}},
        {"$project": {"_id": 0, "present_days": "$present_raw", "leave_days": 1, "late_count": 1, "total_late_minutes": 1, "total_overtime_minutes": 1}}]

def department_summary_pipeline(start: date, end: date, department: str | None = None):
    match = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None: match["department"] = department
    return [{"$match": match}, {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
        {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start.isoformat()]}, {"$lte": ["$date", end.isoformat()]}]}}}], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department", "headcount": {"$addToSet": "$emp_code"},
          "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$not": [{"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, [1,7]]}]}]}, {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]}, 0]}},
          "avg_work_hours": {"$avg": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", None]}},
          "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes",0]},0]},1,0]}},
          "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes",0]}},
          "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status","LEAVE"]},1,0]}}, "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status","ON_DUTY"]},1,0]}}}},
        {"$project": {"_id": 0,"department":"$_id","headcount":{"$size":"$headcount"},"present_days":1,"avg_work_hours":1,"late_count":1,"total_late_minutes":1,"leave_count":1,"on_duty_count":1}},
        {"$sort":{"department":1}}]

def late_leaderboard_pipeline(start: date, end: date, limit: int, department: str | None = None):
    pipeline = [{"$match": month_match(start, end, late_minutes={"$gt": 0})}, {"$group": {"_id":"$emp_code","total_late_minutes":{"$sum":"$late_minutes"},"late_count":{"$sum":1}}},
         {"$lookup":{"from":"employees","localField":"_id","foreignField":"emp_code","as":"employee"}}, {"$unwind":"$employee"}]
    if department is not None: pipeline.append({"$match":{"employee.department":department}})
    pipeline.extend([{"$setWindowFields":{"sortBy":{"total_late_minutes":-1},"output":{"rank":{"$rank":{}}}}}, {"$match":{"rank":{"$lte":limit}}},
              {"$sort":{"total_late_minutes":-1,"_id":1}}, {"$project":{"_id":0,"rank":1,"emp_code":"$_id","name":"$employee.name","department":"$employee.department","total_late_minutes":1,"late_count":1}}])
    return pipeline

@app.get("/analytics/employees/{emp_code}/monthly", response_model=EmployeeMonthly, responses={404: {"model": ErrorResponse, "description": "Not found"}, 503: {"model": ErrorResponse, "description": "Database unavailable"}})
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = must_employee(emp_code); start, end = parse_month(month)
    pipe = employee_monthly_pipeline(emp_code, start, end)
    # Weekday-only present_days is enforced in the accumulator; weekends still contribute late/overtime.
    row = next(db.attendance_logs.aggregate(pipe), {})
    wd = working_days(max(start, parse_day(emp["joined_on"])), end)
    present = round(float(row.get("present_days", 0)), 2)
    return {"emp_code": emp_code, "month": month, "working_days": wd, "present_days": present,
            "leave_days": row.get("leave_days", 0), "late_count": row.get("late_count", 0),
            "total_late_minutes": row.get("total_late_minutes", 0), "total_overtime_minutes": row.get("total_overtime_minutes", 0),
            "attendance_pct": attendance_percentage(present, wd)}

@app.get("/analytics/departments/summary", response_model=DepartmentSummary, responses={503: {"model": ErrorResponse, "description": "Database unavailable"}})
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: str | None = None):
    start, end = parse_month(month)
    pipeline = department_summary_pipeline(start, end, department)
    items = list(db.employees.aggregate(pipeline))
    for x in items:
        average = x.get("avg_work_hours")
        x["avg_work_hours"] = float(Decimal(str(average)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if average is not None else None
        if x["headcount"] == 0: continue
    return {"month": month, "items": [json_value(x) for x in items if x["headcount"] > 0]}

@app.get("/analytics/leaderboard/late", response_model=Leaderboard, responses={503: {"model": ErrorResponse, "description": "Database unavailable"}})
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(10, ge=1, le=50), department: str | None = None):
    start, end = parse_month(month)
    p = late_leaderboard_pipeline(start, end, limit, department)
    return {"month":month,"items":list(db.attendance_logs.aggregate(p))}

def department_trend_pipeline(department: str, from_date: str, days: int):
    # Use one indexed seed and collect this department's employee codes once. The
    # codes let each daily attendance lookup use (emp_code, date), avoiding a
    # date-wide scan followed by an employee lookup for every attendance row.
    # The route validates that the department has a seed employee before running.
    pipeline = [{"$match":{"department":department}}, {"$limit":1},
      {"$lookup":{"from":"employees","pipeline":[{"$match":{"department":department}},{"$project":{"_id":0,"emp_code":1}}],"as":"department_employees"}},
      {"$project":{"employee_codes":"$department_employees.emp_code","dates":{"$map":{"input":{"$range":[0,days]},"as":"i","in":{"$dateToString":{"date":{"$dateAdd":{"startDate":{"$dateFromString":{"dateString":from_date}},"unit":"day","amount":"$$i"}},"format":"%Y-%m-%d"}}}}}}, {"$unwind":"$dates"},
      {"$lookup":{"from":"employees","let":{"d":"$dates"},"pipeline":[{"$match":{"department":department,"$expr":{"$lte":["$joined_on","$$d"]}}},{"$count":"n"}],"as":"hc"}},
      {"$lookup":{"from":"attendance_logs","localField":"employee_codes","foreignField":"emp_code","let":{"d":"$dates"},"pipeline":[{"$match":{"$expr":{"$eq":["$date","$$d"]}}},{"$group":{"_id":None,"present":{"$sum":{"$cond":[{"$in":["$status",list(PRESENCE)]},{"$cond":[{"$ifNull":["$half_day",False]},0.5,1]},0]}},"late":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}}}}],"as":"stats"}},
      {"$set":{"headcount":{"$ifNull":[{"$arrayElemAt":["$hc.n",0]},0]},"present_count":{"$ifNull":[{"$arrayElemAt":["$stats.present",0]},0]},"late_count":{"$ifNull":[{"$arrayElemAt":["$stats.late",0]},0]}}},
      {"$set":{"is_working_day":{"$not":[{"$in":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$dates"}}},[1,7]]}]}}},
      {"$set":{"attendance_rate":{"$cond":[{"$and":["$is_working_day",{"$gt":["$headcount",0]}]},{"$divide":[{"$floor":{"$add":[{"$multiply":[{"$divide":["$present_count","$headcount"]},10000]},0.5]}},10000]},None]}}},
      {"$setWindowFields":{"sortBy":{"dates":1},"output":{"moving_avg_7d":{"$avg":"$attendance_rate","window":{"documents":[-6,0]}}}}},
      {"$project":{"_id":0,"date":"$dates","is_working_day":1,"headcount":1,"present_count":1,"late_count":1,"attendance_rate":1,"moving_avg_7d":{"$cond":[{"$ne":["$moving_avg_7d",None]},{"$divide":[{"$floor":{"$add":[{"$multiply":["$moving_avg_7d",10000]},0.5]}},10000]},None]}}}, {"$sort":{"date":1}}]
    return pipeline

@app.get("/analytics/departments/{department}/trend", response_model=Trend, responses={404: {"model": ErrorResponse, "description": "Not found"}, 503: {"model": ErrorResponse, "description": "Database unavailable"}})
def department_trend(department: str, from_date: str = Query(..., alias="from", json_schema_extra={"format": "date"}), to_date: str = Query(..., alias="to", json_schema_extra={"format": "date"})):
    first, last = parse_day(from_date), parse_day(to_date)
    if last < first or (last-first).days+1 > 92: raise HTTPException(422,"Invalid trend range")
    if not db.employees.find_one({"department":department}, {"_id":1}): raise HTTPException(404,"Department not found")
    days = (last-first).days + 1
    pipeline = department_trend_pipeline(department, from_date, days)
    return {"department":department,"items":list(db.employees.aggregate(pipeline))}

@app.get("/admin/explain/{endpoint}", response_model=ExplainResponse, responses={422: {"model": ErrorResponse, "description": "Invalid explain request"}, 503: {"model": ErrorResponse, "description": "Database unavailable"}})
def explain(endpoint: Literal["attendance_list","employee_monthly","department_summary","late_leaderboard","department_trend"],
            emp_code: str | None = None, month: str | None = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: str | None = None, limit: int = Query(10,ge=1,le=50),
            date_from: str | None = Query(None, json_schema_extra={"format": "date"}), date_to: str | None = Query(None, json_schema_extra={"format": "date"}), status: Literal["PRESENT","ABSENT","LEAVE","WFH","ON_DUTY"] | None = None,
            page: int = Query(1,ge=1), page_size: int = Query(20,ge=1,le=100), from_date: str | None = Query(None,alias="from", json_schema_extra={"format": "date"}), to_date: str | None = Query(None,alias="to", json_schema_extra={"format": "date"})):
    if endpoint == "attendance_list":
        q = {}
        if emp_code is not None: q["emp_code"] = emp_code
        if date_from is not None: parse_day(date_from); q.setdefault("date",{})["$gte"] = date_from
        if date_to is not None: parse_day(date_to); q.setdefault("date",{})["$lte"] = date_to
        if date_from is not None and date_to is not None and date_from > date_to: raise HTTPException(422,"Invalid date range")
        if status: q["status"] = status
        cmd = {"find":"attendance_logs","filter":q,"sort":{"date":-1,"emp_code":1},"skip":(page-1)*page_size,"limit":page_size}
        col = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(422,"emp_code and month are required")
        start,end = parse_month(month)
        cmd = {"aggregate":"attendance_logs","pipeline":employee_monthly_pipeline(emp_code,start,end),"cursor":{}}; col="attendance_logs"
    elif endpoint == "department_summary":
        if not month: raise HTTPException(422,"month is required")
        start,end=parse_month(month)
        cmd={"aggregate":"employees","pipeline":department_summary_pipeline(start,end,department),"cursor":{}}; col="employees"
    elif endpoint == "late_leaderboard":
        if not month: raise HTTPException(422,"month is required")
        start,end=parse_month(month)
        p=late_leaderboard_pipeline(start,end,limit,department)
        cmd={"aggregate":"attendance_logs","pipeline":p,"cursor":{}}; col="attendance_logs"
    else:
        if not department or not from_date or not to_date: raise HTTPException(422,"department, from and to are required")
        first,last=parse_day(from_date),parse_day(to_date)
        if last < first or (last-first).days+1 > 92: raise HTTPException(422,"Invalid trend range")
        days=(last-first).days+1
        cmd={"aggregate":"employees","pipeline":department_trend_pipeline(department,from_date,days),"cursor":{}}; col="employees"
    try:
        result=db.command("explain",cmd,verbosity="executionStats")
    except PyMongoError:
        raise HTTPException(422,"Unable to explain query")
    # Explain output contains BSON-only values (for example Timestamp) that FastAPI's
    # default encoder cannot serialize. Extended JSON preserves those values as JSON.
    explain_json = json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint":endpoint,"collection":col,"explain":explain_json}
