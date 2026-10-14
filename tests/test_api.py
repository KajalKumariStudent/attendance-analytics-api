from datetime import datetime, timezone, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pymongo import ASCENDING, DESCENDING

from app.main import app, db, employees, attendance_logs, IST


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def epoch_ms(day: int, hour: int, minute: int = 0, second: int = 0, *, month: int = 1, year: int = 2026):
    """Create epoch milliseconds for a local IST date/time."""
    local = datetime(year, month, day, hour, minute, second, tzinfo=IST)
    return int(local.timestamp() * 1000)


def employee_payload(*, department="QA_Test", shift_start="09:30", shift_end="18:30", joined_on="2026-01-01"):
    token = uuid4().hex[:10]
    return {
        "emp_code": f"EMP{uuid4().int % 1_000_000:06d}",
        "name": f"Test Employee {token}",
        "email": f"test_{token}@example.com",
        "department": department,
        "shift_start": shift_start,
        "shift_end": shift_end,
        "joined_on": joined_on,
    }


@pytest.fixture
def employee(client):
    payload = employee_payload()
    response = client.post("/employees", json=payload)
    assert response.status_code == 201, response.text
    try:
        yield payload
    finally:
        attendance_logs.delete_many({"emp_code": payload["emp_code"]})
        employees.delete_many({"emp_code": payload["emp_code"]})


def create_employee(client, **kwargs):
    payload = employee_payload(**kwargs)
    response = client.post("/employees", json=payload)
    assert response.status_code == 201, response.text
    return payload


def cleanup_employee(payload):
    attendance_logs.delete_many({"emp_code": payload["emp_code"]})
    employees.delete_many({"emp_code": payload["emp_code"]})


def punch_in(client, emp_code, timestamp, status="PRESENT"):
    return client.post("/attendance/punch-in", json={
        "emp_code": emp_code,
        "punched_at": timestamp,
        "status": status,
    })


def punch_out(client, emp_code, timestamp):
    return client.post("/attendance/punch-out", json={
        "emp_code": emp_code,
        "punched_at": timestamp,
    })


# ---------------------------------------------------------------------------
# Original employee/API tests
# ---------------------------------------------------------------------------

def test_health_endpoint(client):
    assert client.get("/health").status_code == 200


def test_employee_list_endpoint_responds(client):
    response = client.get("/employees")
    assert response.status_code == 200
    assert "items" in response.json()


def test_invalid_month_is_rejected(client):
    response = client.get("/analytics/departments/summary", params={"month": "2026-13"})
    assert response.status_code == 422


def test_create_employee_and_reject_duplicate(client):
    payload = employee_payload()
    try:
        first = client.post("/employees", json=payload)
        assert first.status_code == 201, first.text
        duplicate = client.post("/employees", json=payload)
        assert duplicate.status_code == 409, duplicate.text
    finally:
        cleanup_employee(payload)


def test_invalid_email_is_rejected(client):
    payload = employee_payload()
    payload["email"] = "not-an-email"
    assert client.post("/employees", json=payload).status_code == 422


def test_equal_shift_times_are_rejected(client):
    payload = employee_payload(shift_start="09:30", shift_end="09:30")
    assert client.post("/employees", json=payload).status_code == 422


# ---------------------------------------------------------------------------
# Attendance operations
# ---------------------------------------------------------------------------

def test_punch_in_creates_attendance_record(client, employee):
    response = punch_in(client, employee["emp_code"], epoch_ms(5, 9, 30))
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["emp_code"] == employee["emp_code"]
    assert data["date"] == "2026-01-05"
    assert data["status"] == "PRESENT"
    assert data["punch_in"] == epoch_ms(5, 9, 30)
    assert data["punch_out"] is None
    assert data["work_hours"] is None
    assert data["overtime_minutes"] == 0
    assert data["history"] == []


def test_duplicate_punch_in_returns_409(client, employee):
    timestamp = epoch_ms(5, 9, 30)
    first = punch_in(client, employee["emp_code"], timestamp)
    second = punch_in(client, employee["emp_code"], timestamp + 60_000)
    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text


def test_punch_in_unknown_employee_returns_404(client):
    response = punch_in(client, "EMP999999", epoch_ms(5, 9, 30))
    assert response.status_code == 404


@pytest.mark.parametrize("bad_timestamp", [1, 99_999_999_999, "1767585600000", 1767585600000.5])
def test_punch_in_rejects_invalid_timestamp(client, employee, bad_timestamp):
    response = client.post("/attendance/punch-in", json={
        "emp_code": employee["emp_code"], "punched_at": bad_timestamp,
    })
    assert response.status_code == 422, response.text


def test_punch_out_closes_attendance_record(client, employee):
    start = epoch_ms(5, 9, 30)
    end = epoch_ms(5, 18, 45)
    assert punch_in(client, employee["emp_code"], start).status_code == 201
    response = punch_out(client, employee["emp_code"], end)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["punch_out"] == end
    assert data["work_hours"] == 9.25
    assert data["overtime_minutes"] == 0  # 15 minutes is below the 30-minute threshold.


def test_punch_out_unknown_employee_returns_404(client):
    response = punch_out(client, "EMP999999", epoch_ms(5, 18, 30))
    assert response.status_code == 404


def test_punch_out_without_punch_in_returns_404(client, employee):
    response = punch_out(client, employee["emp_code"], epoch_ms(5, 18, 30))
    assert response.status_code == 404


def test_punch_out_invalid_timestamp_returns_422(client, employee):
    start = epoch_ms(5, 9, 30)
    assert punch_in(client, employee["emp_code"], start).status_code == 201
    response = punch_out(client, employee["emp_code"], start)
    assert response.status_code == 422


def test_second_punch_out_returns_409(client, employee):
    start, end = epoch_ms(5, 9, 30), epoch_ms(5, 18, 30)
    assert punch_in(client, employee["emp_code"], start).status_code == 201
    assert punch_out(client, employee["emp_code"], end).status_code == 200
    assert punch_out(client, employee["emp_code"], end + 60_000).status_code == 409


# ---------------------------------------------------------------------------
# Attendance business rules
# ---------------------------------------------------------------------------

def test_late_grace_period_and_strict_10_minute_boundary(client):
    cases = [(9, 40, 0, 0), (9, 40, 1, 10), (10, 15, 59, 45)]
    for hour, minute, second, expected_late in cases:
        emp = create_employee(client)
        try:
            response = punch_in(client, emp["emp_code"], epoch_ms(5, hour, minute, second))
            assert response.status_code == 201, response.text
            assert response.json()["late_minutes"] == expected_late
        finally:
            cleanup_employee(emp)


def test_overtime_threshold_is_30_minutes(client):
    # Shift ends at 18:30. 29:59 extra => 0; 30:00 extra => 30.
    for punch_hour, punch_minute, punch_second, expected in [
        (18, 59, 59, 0),
        (19, 0, 0, 30),
    ]:
        emp = create_employee(client)
        try:
            assert punch_in(client, emp["emp_code"], epoch_ms(5, 9, 30)).status_code == 201
            response = punch_out(client, emp["emp_code"], epoch_ms(5, punch_hour, punch_minute, punch_second))
            assert response.status_code == 200, response.text
            assert response.json()["overtime_minutes"] == expected
        finally:
            cleanup_employee(emp)


def test_half_day_is_true_below_4_5_hours(client):
    emp = create_employee(client)
    try:
        start = epoch_ms(5, 9, 30)
        # 4h 29m rounds to 4.48 hours and should be a half day.
        assert punch_in(client, emp["emp_code"], start).status_code == 201
        response = punch_out(client, emp["emp_code"], epoch_ms(5, 13, 59))
        assert response.status_code == 200, response.text
        assert response.json()["work_hours"] == 4.48
        assert response.json()["half_day"] is True
    finally:
        cleanup_employee(emp)


def test_overnight_shift_assigns_early_morning_punch_to_previous_date(client):
    emp = create_employee(client, shift_start="22:00", shift_end="06:00")
    try:
        response = punch_in(client, emp["emp_code"], epoch_ms(6, 5, 45))
        assert response.status_code == 201, response.text
        assert response.json()["date"] == "2026-01-05"
        out = punch_out(client, emp["emp_code"], epoch_ms(6, 6, 15))
        assert out.status_code == 200, out.text
        assert out.json()["punch_out"] == epoch_ms(6, 6, 15)
    finally:
        cleanup_employee(emp)


def test_regularization_appends_history_and_recomputes_fields(client, employee):
    start, end = epoch_ms(5, 9, 30), epoch_ms(5, 14, 0)
    assert punch_in(client, employee["emp_code"], start).status_code == 201
    assert punch_out(client, employee["emp_code"], end).status_code == 200
    response = client.patch(
        f"/attendance/{employee['emp_code']}/2026-01-05",
        json={"status": "WFH", "reason": "Correct attendance status", "regularized_by": "pytest"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "WFH"
    assert len(data["history"]) == 1
    assert data["history"][0]["by"] == "pytest"
    assert data["history"][0]["reason"] == "Correct attendance status"
    assert "status" in data["history"][0]["changes"]


def test_regularization_noop_returns_422(client, employee):
    start = epoch_ms(5, 9, 30)
    assert punch_in(client, employee["emp_code"], start).status_code == 201
    response = client.patch(
        f"/attendance/{employee['emp_code']}/2026-01-05",
        json={"status": "PRESENT", "reason": "No actual change", "regularized_by": "pytest"},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Analytics and index checks
# ---------------------------------------------------------------------------

def test_employee_monthly_analytics(client, employee):
    start, end = epoch_ms(5, 9, 30), epoch_ms(5, 18, 30)
    assert punch_in(client, employee["emp_code"], start).status_code == 201
    assert punch_out(client, employee["emp_code"], end).status_code == 200
    response = client.get(f"/analytics/employees/{employee['emp_code']}/monthly", params={"month": "2026-01"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["emp_code"] == employee["emp_code"]
    assert data["month"] == "2026-01"
    assert data["working_days"] == 22
    assert data["present_days"] == 1.0
    assert data["attendance_pct"] is not None


def test_department_summary_includes_employee_without_logs(client):
    emp = create_employee(client, department=f"QA_SUM_{uuid4().hex[:6]}")
    try:
        response = client.get("/analytics/departments/summary", params={"month": "2026-01", "department": emp["department"]})
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert len(items) == 1
        assert items[0]["department"] == emp["department"]
        assert items[0]["headcount"] == 1
        assert items[0]["present_days"] == 0
    finally:
        cleanup_employee(emp)


def test_late_leaderboard_returns_late_employee(client):
    emp = create_employee(client, department=f"QA_LATE_{uuid4().hex[:6]}")
    try:
        assert punch_in(client, emp["emp_code"], epoch_ms(5, 10, 15, 59)).status_code == 201
        response = client.get("/analytics/leaderboard/late", params={"month": "2026-01", "department": emp["department"]})
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert any(row["emp_code"] == emp["emp_code"] and row["rank"] == 1 for row in items)
    finally:
        cleanup_employee(emp)


def test_department_trend_validates_date_range(client, employee):
    department = employee["department"]
    invalid_order = client.get(f"/analytics/departments/{department}/trend", params={"from": "2026-01-10", "to": "2026-01-01"})
    too_long = client.get(f"/analytics/departments/{department}/trend", params={"from": "2026-01-01", "to": "2026-04-05"})
    assert invalid_order.status_code == 422
    assert too_long.status_code == 422


def test_department_trend_has_every_date(client, employee):
    response = client.get(
        f"/analytics/departments/{employee['department']}/trend",
        params={"from": "2026-01-05", "to": "2026-01-07"},
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["date"] for item in items] == ["2026-01-05", "2026-01-06", "2026-01-07"]


def test_explain_attendance_list_returns_execution_stats(client):
    response = client.get("/admin/explain/attendance_list", params={"date_from": "2026-01-01", "date_to": "2026-01-31", "page": 1, "page_size": 20})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["endpoint"] == "attendance_list"
    assert data["collection"] == "attendance_logs"
    assert "executionStats" in data["explain"]


def test_explain_requires_required_parameters(client):
    response = client.get("/admin/explain/employee_monthly")
    assert response.status_code == 422


def test_required_unique_indexes_exist(client):
    # TestClient context manager ensures the startup handler has run.
    employee_indexes = employees.index_information()
    attendance_indexes = attendance_logs.index_information()
    assert any(
        idx.get("unique") and idx.get("key") == [("emp_code", ASCENDING)]
        for idx in employee_indexes.values()
    )
    assert any(
        idx.get("unique") and idx.get("key") == [("emp_code", ASCENDING), ("date", ASCENDING)]
        for idx in attendance_indexes.values()
    )
