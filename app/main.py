"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import math
import re
from bson import json_util
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import ObjectId

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=3000,
    tz_aware=True,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db["employees"]
attendance_logs = db["attendance_logs"]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employee_code",
    )
    employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_employee_department_code",
    )
    employees.create_index(
        [("joined_on", ASCENDING), ("department", ASCENDING)],
        name="ix_employee_joined_department",
    )
    attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )
    attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_employee",
    )
    attendance_logs.create_index(
        [("status", ASCENDING), ("date", ASCENDING)],
        name="ix_attendance_status_date",
    )



# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
NON_PRESENCE_STATUSES = {"ABSENT", "LEAVE"}
VALID_STATUSES = PRESENCE_STATUSES | NON_PRESENCE_STATUSES


def truncate_seconds(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def parse_epoch_ms(value: Optional[int]) -> datetime:
    if value is None:
        return truncate_seconds(datetime.now(UTC))

    # Epoch timestamps must be integer milliseconds, not seconds.
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(status_code=422, detail="Timestamp must be integer epoch milliseconds")

    if not 100_000_000_000 <= value <= 4_102_444_800_000:
        raise HTTPException(status_code=422, detail="Timestamp must be in epoch milliseconds")

    return truncate_seconds(datetime.fromtimestamp(value / 1000, tz=UTC))


def to_epoch_ms(value: Optional[datetime]):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def parse_hhmm(value: str) -> time:
    try:
        parsed = datetime.strptime(value, "%H:%M").time()
        if parsed.strftime("%H:%M") != value:
            raise ValueError
        return parsed
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid time: {value}. Expected HH:MM")


def shift_bounds(attendance_date: str, shift_start: str, shift_end: str):
    day = date.fromisoformat(attendance_date)
    start_time = parse_hhmm(shift_start)
    end_time = parse_hhmm(shift_end)

    start = datetime.combine(day, start_time, tzinfo=IST)
    end_day = day + timedelta(days=1) if end_time <= start_time else day
    end = datetime.combine(end_day, end_time, tzinfo=IST)
    return start, end


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = punch_in.astimezone(IST)
    start_time = parse_hhmm(shift_start)
    end_time = parse_hhmm(shift_end)

    # For overnight shifts, punches before shift_end belong to yesterday's shift.
    if end_time <= start_time and local.time().replace(tzinfo=None) < end_time:
        local -= timedelta(days=1)

    return local.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_date: str) -> int:
    shift_start_dt, _ = shift_bounds(attendance_date, shift_start, "23:59")
    # Construct the actual shift start using the supplied schedule.
    shift_start_dt = datetime.combine(
        date.fromisoformat(attendance_date),
        parse_hhmm(shift_start),
        tzinfo=IST,
    )
    punch_local = punch_in.astimezone(IST)
    seconds_late = (punch_local - shift_start_dt).total_seconds()

    # Strictly more than 10 minutes late; count full minutes after shift start.
    if seconds_late > 600:
        return max(0, math.floor(seconds_late / 60))
    return 0


def compute_overtime(punch_out: datetime, shift_end: str, attendance_date: str, shift_start: str) -> int:
    _, end_dt = shift_bounds(attendance_date, shift_start, shift_end)
    seconds_after = (punch_out.astimezone(IST) - end_dt).total_seconds()
    minutes_after = math.floor(seconds_after / 60)

    return minutes_after if minutes_after >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: Optional[datetime]):
    if punch_out is None:
        return None

    hours = Decimal(str((punch_out - punch_in).total_seconds())) / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_derived_fields(record: dict, employee: dict) -> dict:
    punch_in = record.get("punch_in")
    punch_out = record.get("punch_out")
    status = record.get("status", "PRESENT")

    if status in NON_PRESENCE_STATUSES:
        return {
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }

    late = compute_late_minutes(
        punch_in, employee["shift_start"], record["date"]
    ) if punch_in else 0

    overtime = compute_overtime(
        punch_out, employee["shift_end"], record["date"], employee["shift_start"]
    ) if punch_out else 0

    hours = compute_work_hours(punch_in, punch_out) if punch_in and punch_out else None

    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
        "half_day": hours is not None and hours < 4.50,
    }


def rounded(value, places=2):
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def is_working_day(day: date) -> bool:
    return day.weekday() < 5

def working_days_between(start: date, end: date) -> int:
    """Count Monday-Friday working days, inclusive of both dates."""
    if end < start:
        return 0

    total_days = (end - start).days + 1
    full_weeks, remaining_days = divmod(total_days, 7)

    working_days = full_weeks * 5

    for offset in range(remaining_days):
        weekday = (start.weekday() + offset) % 7
        if weekday < 5:
            working_days += 1

    return working_days

def weekday_expression(date_expression: str) -> dict:
    """MongoDB expression: Monday=0 through Sunday=6."""
    return {
        "$subtract": [
            {"$isoDayOfWeek": date_expression},
            1,
        ]
    }


def present_day_expression() -> dict:
    """MongoDB expression for attendance contribution."""
    return {
        "$cond": [
            {
                "$in": [
                    "$status",
                    ["PRESENT", "WFH", "ON_DUTY"],
                ]
            },
            {
                "$cond": [
                    {"$eq": ["$half_day", True]},
                    0.5,
                    1,
                ]
            },
            0,
        ]
    }

def monthly_range(month: str):
    """Return inclusive ISO date strings and numeric year/month."""
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(
            status_code=422,
            detail="month must be in YYYY-MM format",
        )

    year, month_num = map(int, month.split("-"))
    start_date = date(year, month_num, 1)

    if month_num == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month_num + 1, 1)

    end_date = next_month - timedelta(days=1)

    return (
        start_date.isoformat(),
        end_date.isoformat(),
        start_date,
        end_date,
    )

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        min_length=3,
        max_length=120,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @classmethod
    def validate_times(cls, values):
        return values


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[StrictInt] = None


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database unavailable")



def employee_response(doc):
    result = {k: v for k, v in doc.items() if k != "_id"}
    if "created_at" in result:
        result["created_at"] = to_epoch_ms(result["created_at"])
    return result


def attendance_response(doc):
    result = {k: v for k, v in doc.items() if k != "_id"}
    for field in ("punch_in", "punch_out"):
        result[field] = to_epoch_ms(result.get(field))
    for entry in result.get("history", []):
        entry["at"] = to_epoch_ms(entry["at"])
        for change in entry.get("changes", {}).values():
            for key in ("from", "to"):
                if isinstance(change.get(key), datetime):
                    change[key] = to_epoch_ms(change[key])
    return result



@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    try:
        parse_hhmm(body.shift_start)
        parse_hhmm(body.shift_end)
        date.fromisoformat(body.joined_on)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=422,
            detail="Invalid shift time or joined_on date",
        )

    if body.shift_start == body.shift_end:
        raise HTTPException(
            status_code=422,
            detail="shift_start and shift_end must differ",
        )

    # Fast rejection for an employee code that already exists.
    # The unique MongoDB index remains essential for concurrent requests.
    if employees.find_one({"emp_code": body.emp_code}, {"_id": 1}):
        raise HTTPException(status_code=409, detail="emp_code already exists")

    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC)

    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return employee_response(doc)



@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}
    if department is not None:
        query["department"] = department

    offset = (page - 1) * page_size
    docs = employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip(offset).limit(page_size)

    return {
        "items": [employee_response(doc) for doc in docs],
        "total": employees.count_documents(query),
        "page": page,
        "page_size": page_size,
    }



@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    ts = parse_epoch_ms(body.punched_at)
    day = attendance_date_for(ts, emp["shift_start"], emp["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this date")

    return attendance_response(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not exceed date_to")

    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if status:
        query["status"] = status
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from.isoformat()
        if date_to:
            query["date"]["$lte"] = date_to.isoformat()

    offset = (page - 1) * page_size
    docs = attendance_logs.find(query).sort(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    ).skip(offset).limit(page_size)

    return {
        "items": [attendance_response(doc) for doc in docs],
        "total": attendance_logs.count_documents(query),
        "page": page,
        "page_size": page_size,
    }

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    ts = parse_epoch_ms(body.punched_at)

    record = attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(status_code=404, detail="No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Already punched out")

    if ts <= record["punch_in"] or ts > record["punch_in"] + timedelta(hours=24):
        raise HTTPException(status_code=422, detail="Punch-out must be after punch-in and within 24 hours")

    fields = {
        "punch_out": ts,
        **compute_derived_fields({**record, "punch_out": ts}, emp),
    }

    updated = attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {"$set": fields},
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Already punched out")

    return attendance_response(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    try:
        parsed_date = datetime.strptime(date, "%Y-%m-%d").date()
        if parsed_date.isoformat() != date:
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date")

    emp = employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    payload = body.model_dump(exclude_unset=True)
    new_record = dict(record)
    supplied_times = "punch_in" in payload or "punch_out" in payload

    for field in ("status", "punch_in", "punch_out"):
        if field in payload:
            new_record[field] = (
                parse_epoch_ms(payload[field]) if field in ("punch_in", "punch_out")
                and payload[field] is not None else payload[field]
            )

    status = new_record.get("status")
    if status in NON_PRESENCE_STATUSES:
        if supplied_times and (body.punch_in is not None or body.punch_out is not None):
            raise HTTPException(status_code=422, detail="ABSENT/LEAVE cannot have punch times")
        new_record["punch_in"] = None
        new_record["punch_out"] = None
    elif new_record.get("punch_in") is None:
        raise HTTPException(status_code=422, detail="Presence status requires punch_in")

    if new_record.get("punch_in"):
        punch_local = new_record["punch_in"].astimezone(IST)
        if attendance_date_for(
            new_record["punch_in"], emp["shift_start"], emp["shift_end"]
        ) != date:
            raise HTTPException(status_code=422, detail="punch_in does not belong to this attendance date")

    pi, po = new_record.get("punch_in"), new_record.get("punch_out")
    if po is not None and (pi is None or po <= pi or po > pi + timedelta(hours=24)):
        raise HTTPException(status_code=422, detail="Invalid punch_out")

    derived = compute_derived_fields(new_record, emp)
    new_record.update(derived)

    tracked = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
    changes = {}
    for key in tracked:
        old, new = record.get(key), new_record.get(key)
        if old != new:
            changes[key] = {
                "from": to_epoch_ms(old) if key in ("punch_in", "punch_out") else old,
                "to": to_epoch_ms(new) if key in ("punch_in", "punch_out") else new,
            }

    if not changes:
        raise HTTPException(status_code=422, detail="Request does not change the record")

    history_entry = {
        "at": datetime.now(UTC),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    updated = attendance_logs.find_one_and_update(
        {"_id": record["_id"], "history": record.get("history", [])},
        {
            "$set": {k: new_record[k] for k in tracked},
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Record changed concurrently")

    return attendance_response(updated)

def build_employee_monthly_pipeline(emp_code: str, start: str, end: str):
    return [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": start, "$lte": end},
        }},
        {"$set": {
            "_attendance_day": {
                "$dateFromString": {"dateString": "$date"}
            },
        }},
       {"$set": {
            "_weekday_number": weekday_expression("$_attendance_day"),
            "_present_value": present_day_expression(),
        }},
        {"$set": {
            "_is_weekday": {"$lt": ["$_weekday_number", 5]},
        }},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {
                "$cond": ["$_is_weekday", "$_present_value", 0]
            }},
            "leave_days": {"$sum": {
                "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
            }},
            "late_count": {"$sum": {
                "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
            }},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    employee = employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    start, end, start_day, end_day = monthly_range(month)

    joined_on = date.fromisoformat(employee["joined_on"])
    first_working_day = max(start_day, joined_on)
    working_days = (
        working_days_between(first_working_day, end_day)
        if first_working_day <= end_day
        else 0
    )

    result = list(
        attendance_logs.aggregate(
            build_employee_monthly_pipeline(emp_code, start, end)
        )
    )
    totals = result[0] if result else {}

    present_days = rounded(totals.get("present_days", 0), 2)
    attendance_pct = (
        rounded(present_days / working_days * 100, 2)
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": totals.get("leave_days", 0),
        "late_count": totals.get("late_count", 0),
        "total_late_minutes": totals.get("total_late_minutes", 0),
        "total_overtime_minutes": totals.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }

def build_department_summary_pipeline(start: str, end: str, department=None):
    employee_match = {"joined_on": {"$lte": end}}
    if department is not None:
        employee_match["department"] = department

    return [
        {"$match": employee_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code", "joined": "$joined_on"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]},
                    {"$gte": ["$date", start]},
                    {"$lte": ["$date", end]},
                    {"$gte": ["$date", "$$joined"]},
                ]}}},
                {"$set": {
                    "_day": {"$dateFromString": {"dateString": "$date"}}
                }},
                {"$set": {
                    "_weekday": weekday_expression("$_day"),
                    "_present_value": present_day_expression(),
                }},
            ],
            "as": "logs",
        }},
        {"$unwind": {
            "path": "$logs",
            "preserveNullAndEmptyArrays": True,
        }},
        {"$group": {
            "_id": "$department",
            "employee_codes": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {
                "$cond": [
                    {"$and": [
                        {"$ne": ["$logs", None]},
                        {"$eq": ["$logs._weekday", True]},
                    ]},
                    "$logs._present_value",
                    0,
                ]
            }},
            "work_hours": {"$push": {
                "$cond": [
                    {"$and": [
                        {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$ne": ["$logs.work_hours", None]},
                    ]},
                    "$logs.work_hours",
                    None,
                ]
            }},
            "late_count": {"$sum": {
                "$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]
            }},
            "total_late_minutes": {"$sum": {
                "$ifNull": ["$logs.late_minutes", 0]
            }},
            "leave_count": {"$sum": {
                "$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]
            }},
            "on_duty_count": {"$sum": {
                "$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]
            }},
        }},
        {"$set": {
            "headcount": {"$size": "$employee_codes"},
            "work_hours": {
                "$filter": {
                    "input": "$work_hours",
                    "as": "hours",
                    "cond": {"$ne": ["$$hours", None]},
                }
            },
        }},
        {"$set": {
            "avg_work_hours": {
                "$cond": [
                    {"$gt": [{"$size": "$work_hours"}, 0]},
                    {"$avg": "$work_hours"},
                    None,
                ]
            }
        }},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"_id": 1}},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": 1,
            "avg_work_hours": 1,
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    start, end, _, _ = monthly_range(month)

    items = list(
        employees.aggregate(
            build_department_summary_pipeline(start, end, department)
        )
    )

    for item in items:
        item["present_days"] = rounded(item["present_days"], 2)
        item["avg_work_hours"] = (
            rounded(item["avg_work_hours"], 2)
            if item["avg_work_hours"] is not None
            else None
        )

    return {"month": month, "items": items}

def build_late_leaderboard_pipeline(
    start: str,
    end: str,
    limit: int,
    department=None,
):
    pipeline = [
        {"$match": {
            "date": {"$gte": start, "$lte": end},
            "late_minutes": {"$gt": 0},
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "employee",
        }},
        {"$unwind": "$employee"},
    ]

    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$employee.name",
            "department": "$employee.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ])
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, end, _, _ = monthly_range(month)

    items = list(
        attendance_logs.aggregate(
            build_late_leaderboard_pipeline(start, end, limit, department)
        )
    )

    return {"month": month, "items": items}

def build_department_trend_pipeline(
    department: str,
    from_date: str,
    to_date: str,
):
    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {
            "_id": 0,
            "days": {
                "$map": {
                    "input": {"$range": [
                        0,
                        {"$add": [
                            {"$dateDiff": {
                                "startDate": {
                                    "$dateFromString": {"dateString": from_date}
                                },
                                "endDate": {
                                    "$dateFromString": {"dateString": to_date}
                                },
                                "unit": "day",
                            }},
                            1,
                        ]},
                    ]},
                    "as": "offset",
                    "in": {"$dateToString": {
                        "date": {"$dateAdd": {
                            "startDate": {
                                "$dateFromString": {"dateString": from_date}
                            },
                            "unit": "day",
                            "amount": "$$offset",
                        }},
                        "format": "%Y-%m-%d",
                    }},
                }
            },
        }},
        {"$unwind": "$days"},
        {"$project": {
            "date": "$days",
            "_day": {"$dateFromString": {"dateString": "$days"}},
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"day": "$date"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$department", department]},
                    {"$lte": ["$joined_on", "$$day"]},
                ]}}},
                {"$count": "count"},
            ],
            "as": "_headcount",
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"day": "$date"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
                {"$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "_employee",
                }},
                {"$unwind": "$_employee"},
                {"$match": {"_employee.department": department}},
                {"$group": {
                    "_id": None,
                    "present_count": {"$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }},
                    "late_count": {"$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }},
                }},
            ],
            "as": "_daily",
        }},
        {"$set": {
            "headcount": {
                "$ifNull": [{"$arrayElemAt": ["$_headcount.count", 0]}, 0]
            },
            "present_count": {
                "$ifNull": [{"$arrayElemAt": ["$_daily.present_count", 0]}, 0]
            },
            "late_count": {
                "$ifNull": [{"$arrayElemAt": ["$_daily.late_count", 0]}, 0]
            },
            "is_working_day": weekday_expression("$_day"),
        }},
        {"$set": {
            "attendance_rate": {
                "$cond": [
                    {"$and": [
                        "$is_working_day",
                        {"$gt": ["$headcount", 0]},
                    ]},
                    {"$divide": ["$present_count", "$headcount"]},
                    None,
                ]
            }
        }},
        {"$setWindowFields": {
            "sortBy": {"_day": 1},
            "output": {
                "moving_avg_7d": {
                    "$avg": "$attendance_rate",
                    "window": {"documents": [-6, 0]},
                }
            },
        }},
        {"$sort": {"_day": 1}},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1,
        }},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    try:
        start = datetime.strptime(from_date, "%Y-%m-%d").date()
        end = datetime.strptime(to_date, "%Y-%m-%d").date()
        if start.isoformat() != from_date or end.isoformat() != to_date:
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail="from and to must be YYYY-MM-DD")

    if end < start:
        raise HTTPException(status_code=422, detail="'to' must not be before 'from'")

    if (end - start).days + 1 > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    if not employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(status_code=404, detail="Department not found")

    items = list(
        employees.aggregate(
            build_department_trend_pipeline(department, from_date, to_date)
        )
    )

    for item in items:
        item["present_count"] = rounded(item["present_count"], 2)
        item["attendance_rate"] = (
            rounded(item["attendance_rate"], 4)
            if item["attendance_rate"] is not None else None
        )
        item["moving_avg_7d"] = (
            rounded(item["moving_avg_7d"], 4)
            if item["moving_avg_7d"] is not None else None
        )

    return {"department": department, "items": items}

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal[
        "PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"
    ]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    allowed = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }

    if endpoint not in allowed:
        raise HTTPException(status_code=422, detail="Unsupported endpoint")

    if endpoint == "attendance_list":
        query = {}

        if emp_code:
            query["emp_code"] = emp_code
        if status:
            query["status"] = status

        if date_from or date_to:
            if date_from:
                try:
                    parsed_from = date.fromisoformat(date_from)
                    if parsed_from.isoformat() != date_from:
                        raise ValueError
                except ValueError:
                    raise HTTPException(422, "Invalid date_from")

            if date_to:
                try:
                    parsed_to = date.fromisoformat(date_to)
                    if parsed_to.isoformat() != date_to:
                        raise ValueError
                except ValueError:
                    raise HTTPException(422, "Invalid date_to")

            if date_from and date_to and date_from > date_to:
                raise HTTPException(422, "date_from must not exceed date_to")

            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to

        command = {
            "find": "attendance_logs",
            "filter": query,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        collection_name = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")

        start, end, _, _ = monthly_range(month)
        pipeline = build_employee_monthly_pipeline(emp_code, start, end)
        command = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        collection_name = "attendance_logs"

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")

        start, end, _, _ = monthly_range(month)
        pipeline = build_department_summary_pipeline(start, end, department)
        command = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }
        collection_name = "employees"

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")

        start, end, _, _ = monthly_range(month)
        pipeline = build_late_leaderboard_pipeline(
            start, end, limit, department
        )
        command = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        collection_name = "attendance_logs"

    else:
        if not department or not from_date or not to_date:
            raise HTTPException(
                422, "department, from and to are required"
            )

        try:
            start_day = date.fromisoformat(from_date)
            end_day = date.fromisoformat(to_date)
            if (
                start_day.isoformat() != from_date
                or end_day.isoformat() != to_date
            ):
                raise ValueError
        except ValueError:
            raise HTTPException(422, "from and to must be YYYY-MM-DD")

        if end_day < start_day or (end_day - start_day).days + 1 > 92:
            raise HTTPException(422, "Invalid trend date range")

        if not employees.find_one({"department": department}, {"_id": 1}):
            raise HTTPException(404, "Department not found")

        pipeline = build_department_trend_pipeline(
            department, from_date, to_date
        )
        command = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }
        collection_name = "employees"

    try:
        raw = db.command({
            "explain": command,
            "verbosity": "executionStats",
        })
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"MongoDB explain failed: {str(exc)}",
        )

    explain_json = json_util.loads(json_util.dumps(raw))

    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": explain_json,
    }
# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
