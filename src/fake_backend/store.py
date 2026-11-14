"""SQLite-backed state for the fake reservation backend.

Availability is derived, not stored: candidate slots come from the business
calendar and advisor working hours, minus CONFIRMED appointments. Capacity is
one appointment per advisor and start time, enforced by a partial unique index
so concurrent writes cannot double-book.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS services (
    service_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    keywords_json TEXT NOT NULL,
    allow_new INTEGER NOT NULL,
    allow_revisit INTEGER NOT NULL,
    sort INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS operators (
    operator_id INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL,
    description TEXT,
    sort INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS operator_services (
    operator_id INTEGER NOT NULL,
    service_id INTEGER NOT NULL,
    PRIMARY KEY (operator_id, service_id)
);
CREATE TABLE IF NOT EXISTS operator_hours (
    operator_id INTEGER NOT NULL,
    weekday INTEGER NOT NULL,
    first_start TEXT NOT NULL,
    last_start TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS customers (
    customer_no TEXT PRIMARY KEY,
    phone TEXT NOT NULL,
    name TEXT NOT NULL,
    verification_code TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS appointments (
    reservation_record_id TEXT PRIMARY KEY,
    customer_no TEXT NOT NULL,
    service_id INTEGER NOT NULL,
    operator_id INTEGER NOT NULL,
    start_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('CONFIRMED', 'CANCELLED')),
    memo TEXT,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_slot
    ON appointments (operator_id, start_at) WHERE state = 'CONFIRMED';
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Business:
    site_id: str
    client_id: int
    name: str
    weekdays: frozenset[int]
    slot_minutes: int
    horizon_days: int
    closed_dates: frozenset[str]


def _read_json(name: str) -> Any:
    return json.loads((config.FIXTURES_DIR / name).read_text(encoding="utf-8"))


def load_business() -> Business:
    raw = _read_json("business.json")
    return Business(
        site_id=raw["site_id"],
        client_id=int(raw["client_id"]),
        name=raw["business_name"],
        weekdays=frozenset(raw["hours"]["weekdays"]),
        slot_minutes=int(raw["slot_minutes"]),
        horizon_days=int(raw["booking_horizon_days"]),
        closed_dates=frozenset(raw.get("closed_dates", [])),
    )


def _iso_now() -> str:
    return config.now().isoformat(timespec="seconds")


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    conn = _connect(config.db_path())
    try:
        conn.executescript(SCHEMA)
        _seed_once(conn)
        yield conn
    finally:
        conn.close()


def reset_database() -> Path:
    """Delete and reseed the demo database (only the configured demo path)."""
    path = config.db_path()
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(f"{path}{suffix}")
        if candidate.exists():
            candidate.unlink()
    with connection():
        pass
    return path


# --------------------------------------------------------------------- seeding


def _business_days_ahead(start: date, count: int, business: Business) -> date:
    """The ``count``-th open business day after ``start`` (count >= 1)."""
    current = start
    remaining = count
    while remaining > 0:
        current += timedelta(days=1)
        if _is_open_day(current, business):
            remaining -= 1
    return current


def _is_open_day(day: date, business: Business) -> bool:
    return (
        day.weekday() in business.weekdays
        and day.isoformat() not in business.closed_dates
    )


def _seed_once(conn: sqlite3.Connection) -> None:
    business = load_business()
    catalog = _read_json("catalog.json")
    customers = _read_json("customers.json")
    today = config.now().date()
    stamp = _iso_now()

    # Check-and-seed under the write lock: concurrent first connections would
    # otherwise all see an empty database and insert the seed rows twice.
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT 1 FROM meta WHERE key = 'seeded_at'").fetchone():
            conn.execute("COMMIT")
            return
        for sort, dept in enumerate(catalog["services"]):
            conn.execute(
                "INSERT INTO services VALUES (?, ?, ?, ?, ?, ?)",
                (
                    dept["service_id"],
                    dept["name"],
                    json.dumps(dept.get("keywords", []), ensure_ascii=False),
                    int("NEW" in dept.get("allow", ["NEW", "REVISIT"])),
                    int("REVISIT" in dept.get("allow", ["NEW", "REVISIT"])),
                    sort,
                ),
            )
        for sort, op in enumerate(catalog["advisors"]):
            conn.execute(
                "INSERT INTO operators VALUES (?, ?, ?, ?)",
                (op["operator_id"], op["display_name"], op.get("description"), sort),
            )
            for service_id in op["service_ids"]:
                conn.execute(
                    "INSERT INTO operator_services VALUES (?, ?)",
                    (op["operator_id"], service_id),
                )
            for block in op["hours"]:
                for weekday in block["weekdays"]:
                    conn.execute(
                        "INSERT INTO operator_hours VALUES (?, ?, ?, ?)",
                        (
                            op["operator_id"],
                            weekday,
                            block["first_start"],
                            block["last_start"],
                        ),
                    )
        for customer in customers["customers"]:
            conn.execute(
                "INSERT INTO customers VALUES (?, ?, ?, ?)",
                (
                    customer["customer_no"],
                    customer["phone"],
                    customer["name"],
                    customer["verification_code"],
                ),
            )
            for appt in customer.get("appointments", []):
                day = _business_days_ahead(
                    today, int(appt["business_days_ahead"]), business
                )
                conn.execute(
                    "INSERT INTO appointments VALUES (?, ?, ?, ?, ?, 'CONFIRMED', ?, 'seed', ?, ?)",
                    (
                        appt["reservation_record_id"],
                        customer["customer_no"],
                        appt["service_id"],
                        appt["operator_id"],
                        f"{day.isoformat()} {appt['time']}",
                        appt.get("memo"),
                        stamp,
                        stamp,
                    ),
                )
        conn.execute("INSERT INTO meta VALUES ('seeded_at', ?)", (stamp,))
        conn.execute("INSERT INTO meta VALUES ('seed_date', ?)", (today.isoformat(),))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ------------------------------------------------------------------ reference


def list_services(
    conn: sqlite3.Connection, setting: str | None
) -> list[dict[str, Any]]:
    column = "allow_revisit" if setting == "ALLOW_ONLY_REVISIT" else "allow_new"
    rows = conn.execute(
        f"SELECT * FROM services WHERE {column} = 1 ORDER BY sort"
    ).fetchall()
    result = []
    for row in rows:
        count = conn.execute(
            "SELECT COUNT(*) FROM operator_services WHERE service_id = ?",
            (row["service_id"],),
        ).fetchone()[0]
        result.append(
            {
                "serviceId": row["service_id"],
                "name": row["name"],
                "operatorCount": count,
                "keywords": json.loads(row["keywords_json"]),
                "services": None,
            }
        )
    return result


def list_operators(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    result = []
    for row in conn.execute("SELECT * FROM operators ORDER BY sort").fetchall():
        depts = conn.execute(
            """SELECT d.service_id, d.name FROM operator_services od
               JOIN services d USING (service_id)
               WHERE od.operator_id = ? ORDER BY d.sort""",
            (row["operator_id"],),
        ).fetchall()
        result.append(
            {
                "operatorId": row["operator_id"],
                "displayName": row["display_name"],
                "description": row["description"],
                "services": [
                    {"serviceId": d["service_id"], "name": d["name"]}
                    for d in depts
                ],
            }
        )
    return result


def _operator_services(conn: sqlite3.Connection) -> dict[int, set[int]]:
    mapping: dict[int, set[int]] = {}
    for row in conn.execute("SELECT * FROM operator_services").fetchall():
        mapping.setdefault(row["operator_id"], set()).add(row["service_id"])
    return mapping


# --------------------------------------------------------------- availability


def _hhmm_range(first: str, last: str, step: int) -> Iterator[str]:
    current = datetime.strptime(first, "%H:%M")
    end = datetime.strptime(last, "%H:%M")
    while current <= end:
        yield current.strftime("%H:%M")
        current += timedelta(minutes=step)


def candidate_slots(
    conn: sqlite3.Connection, start: date, end: date, *, include_booked: bool = False
) -> dict[str, list[int]]:
    """``"YYYY-MM-DD HH:MM"`` → advisor IDs free at that start (sorted by advisor).

    ``include_booked`` returns calendar-valid starts regardless of bookings.
    """
    business = load_business()
    now = config.now()
    horizon_end = now.date() + timedelta(days=business.horizon_days)
    end = min(end, horizon_end)
    hours: dict[int, list[tuple[int, str, str]]] = {}
    for row in conn.execute(
        """SELECT h.* FROM operator_hours h JOIN operators o USING (operator_id)
           ORDER BY o.sort"""
    ).fetchall():
        hours.setdefault(row["weekday"], []).append(
            (row["operator_id"], row["first_start"], row["last_start"])
        )
    booked = {
        (row["operator_id"], row["start_at"])
        for row in conn.execute(
            "SELECT operator_id, start_at FROM appointments WHERE state = 'CONFIRMED'"
        ).fetchall()
    }
    floor = now.strftime("%Y-%m-%d %H:%M")
    slots: dict[str, list[int]] = {}
    day = max(start, now.date())
    while day <= end:
        if _is_open_day(day, business):
            for operator_id, first, last in hours.get(day.weekday(), []):
                for hhmm in _hhmm_range(first, last, business.slot_minutes):
                    when = f"{day.isoformat()} {hhmm}"
                    if when <= floor:
                        continue
                    if not include_booked and (operator_id, when) in booked:
                        continue
                    slots.setdefault(when, []).append(operator_id)
        day += timedelta(days=1)
    return dict(sorted(slots.items()))


def filter_slots(
    conn: sqlite3.Connection,
    slots: dict[str, list[int]],
    *,
    service_id: int | None = None,
    operator_id: int | None = None,
) -> dict[str, list[int]]:
    by_operator = _operator_services(conn)
    result: dict[str, list[int]] = {}
    for when, operator_ids in slots.items():
        eligible = [
            oid
            for oid in operator_ids
            if (operator_id is None or oid == operator_id)
            and (service_id is None or service_id in by_operator.get(oid, set()))
        ]
        if eligible:
            result[when] = eligible
    return result


def slot_state(
    conn: sqlite3.Connection, when: str, operator_id: int, service_id: int | None
) -> str:
    """``free`` | ``taken`` | ``invalid`` (outside calendar, hours, horizon, or skills)."""
    day = datetime.strptime(when, "%Y-%m-%d %H:%M").date()
    valid = filter_slots(
        conn,
        candidate_slots(conn, day, day, include_booked=True),
        service_id=service_id,
        operator_id=operator_id,
    )
    if when not in valid:
        return "invalid"
    free = filter_slots(
        conn,
        candidate_slots(conn, day, day),
        service_id=service_id,
        operator_id=operator_id,
    )
    return "free" if when in free else "taken"


def _require_free(
    conn: sqlite3.Connection, when: str, operator_id: int, service_id: int | None
) -> None:
    state = slot_state(conn, when, operator_id, service_id)
    if state == "invalid":
        raise SubmitError("INVALID", 422, "예약할 수 없는 일시 또는 담당자입니다.")
    if state == "taken":
        raise SubmitError("CONFLICT", 409, "선택한 시간은 이미 예약이 찼습니다.")


# ------------------------------------------------------------------ customers


def customers_by_phone(conn: sqlite3.Connection, phone: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM customers WHERE phone = ? ORDER BY customer_no", (phone,)
    ).fetchall()


def confirmed_appointments(
    conn: sqlite3.Connection,
    phone: str,
    *,
    customer_no: str | None = None,
    verification_code: str | None = None,
) -> list[dict[str, Any]]:
    clauses = ["c.phone = ?", "a.state = 'CONFIRMED'", "a.start_at >= ?"]
    params: list[Any] = [phone, config.now().strftime("%Y-%m-%d 00:00")]
    if customer_no:
        clauses.append("c.customer_no = ?")
        params.append(customer_no)
    if verification_code:
        clauses.append("c.verification_code = ?")
        params.append(verification_code)
    rows = conn.execute(
        f"""SELECT a.*, c.name AS customer_name, d.name AS service_name,
                   o.display_name AS operator_name
            FROM appointments a
            JOIN customers c USING (customer_no)
            JOIN services d USING (service_id)
            JOIN operators o USING (operator_id)
            WHERE {" AND ".join(clauses)}
            ORDER BY a.start_at""",
        params,
    ).fetchall()
    business = load_business()
    result = []
    for row in rows:
        start = datetime.strptime(row["start_at"], "%Y-%m-%d %H:%M")
        end = start + timedelta(minutes=business.slot_minutes)
        result.append(
            {
                "reservationDateTime": start.strftime("%Y-%m-%d %H:%M:%S"),
                "reservationEndDateTime": end.strftime("%Y-%m-%d %H:%M:%S"),
                "operatorId": row["operator_id"],
                "operatorName": row["operator_name"],
                "serviceId": row["service_id"],
                "serviceName": row["service_name"],
                "customerNumber": row["customer_no"],
                "customerName": row["customer_name"],
                "state": row["state"],
                "memo": row["memo"],
                "appointmentDetail": row["service_name"],
                "reservationRecordId": row["reservation_record_id"],
                "todayModifiable": True,
                "todayCancellable": True,
            }
        )
    return result


# ----------------------------------------------------------------- submission


class SubmitError(Exception):
    def __init__(self, status: str, http_status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.http_status = http_status
        self.message = message


def _normalize_when(value: str) -> str:
    text = str(value).strip().replace("T", " ")
    try:
        return datetime.strptime(text[:16], "%Y-%m-%d %H:%M").strftime("%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise SubmitError(
            "INVALID", 422, "예약 일시 형식이 올바르지 않습니다."
        ) from exc


def _request_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def submit_reservation(
    conn: sqlite3.Connection, payload: dict[str, Any], idempotency_key: str | None
) -> tuple[int, dict[str, Any]]:
    """Apply one NEW/MODIFY/CANCEL request atomically; replay by idempotency key."""
    request_hash = _request_hash(payload)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if idempotency_key:
            prior = conn.execute(
                "SELECT * FROM idempotency WHERE key = ?", (idempotency_key,)
            ).fetchone()
            if prior is not None:
                conn.execute("COMMIT")
                if prior["request_hash"] != request_hash:
                    return 422, _envelope(
                        False, "같은 요청 키로 다른 요청이 들어왔습니다.", "INVALID"
                    )
                body = json.loads(prior["response_json"])
                body["contents"]["replayed"] = True
                return prior["status_code"], body
        try:
            status_code, body = _apply(conn, payload)
        except SubmitError as err:
            status_code, body = (
                err.http_status,
                _envelope(False, err.message, err.status),
            )
        if idempotency_key:
            conn.execute(
                "INSERT INTO idempotency VALUES (?, ?, ?, ?, ?)",
                (
                    idempotency_key,
                    request_hash,
                    status_code,
                    json.dumps(body),
                    _iso_now(),
                ),
            )
        conn.execute("COMMIT")
        return status_code, body
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _envelope(success: bool, message: str | None, status: str, **contents: Any) -> dict:
    return {
        "success": success,
        "resultMsg": message,
        "contents": {"status": status, **contents},
    }


def _owned_confirmed(
    conn: sqlite3.Connection, record_id: str | None, phone: str
) -> sqlite3.Row:
    row = conn.execute(
        """SELECT a.* FROM appointments a JOIN customers c USING (customer_no)
           WHERE a.reservation_record_id = ? AND c.phone = ? AND a.state = 'CONFIRMED'""",
        (record_id, phone),
    ).fetchone()
    if row is None:
        # Unknown, foreign, and already-cancelled are indistinguishable (no disclosure).
        raise SubmitError("NOT_FOUND", 404, "해당 예약을 찾을 수 없습니다.")
    return row


def _resolve_customer(conn: sqlite3.Connection, customer: dict[str, Any]) -> str:
    phone = str(customer.get("phone") or "").strip()
    if not phone:
        raise SubmitError("INVALID", 422, "고객 연락처가 없습니다.")
    customer_no = customer.get("customer_no")
    if customer_no:
        row = conn.execute(
            "SELECT customer_no FROM customers WHERE customer_no = ? AND phone = ?",
            (customer_no, phone),
        ).fetchone()
        if row is None:
            raise SubmitError("NOT_FOUND", 404, "고객 정보를 찾을 수 없습니다.")
        return row["customer_no"]
    new_customer_no = f"DC-NEW-{uuid.uuid4().hex[:8]}"
    conn.execute(
        "INSERT INTO customers VALUES (?, ?, ?, ?)",
        (
            new_customer_no,
            phone,
            customer.get("name") or "신규 데모 고객",
            customer.get("verification_code") or "",
        ),
    )
    return new_customer_no


def _apply(
    conn: sqlite3.Connection, payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    kind = payload.get("type")
    customer = payload.get("customer") or {}
    phone = str(customer.get("phone") or "").strip()
    stamp = _iso_now()

    if kind == "CANCEL_RESERVATION":
        row = _owned_confirmed(conn, payload.get("reservation_record_id"), phone)
        conn.execute(
            "UPDATE appointments SET state = 'CANCELLED', updated_at = ? WHERE reservation_record_id = ?",
            (stamp, row["reservation_record_id"]),
        )
        return 200, _envelope(
            True, None, "ACCEPTED", reservationRecordId=row["reservation_record_id"]
        )

    if kind not in {"NEW_RESERVATION", "MODIFY_RESERVATION"}:
        raise SubmitError("INVALID", 422, "알 수 없는 요청 유형입니다.")

    when = _normalize_when(payload.get("reservation_request_date_time", ""))
    try:
        operator_id = int(payload["operator_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SubmitError("INVALID", 422, "담당자 정보가 올바르지 않습니다.") from exc
    service_id = payload.get("service_id")
    service_id = int(service_id) if service_id is not None else None

    if kind == "MODIFY_RESERVATION":
        row = _owned_confirmed(conn, payload.get("reservation_record_id"), phone)
        service_id = service_id or row["service_id"]
        if row["start_at"] == when and row["operator_id"] == operator_id:
            return 200, _envelope(
                True, None, "ACCEPTED", reservationRecordId=row["reservation_record_id"]
            )
        _require_free(conn, when, operator_id, service_id)
        try:
            conn.execute(
                """UPDATE appointments SET start_at = ?, operator_id = ?, updated_at = ?
                   WHERE reservation_record_id = ?""",
                (when, operator_id, stamp, row["reservation_record_id"]),
            )
        except sqlite3.IntegrityError as exc:
            raise SubmitError(
                "CONFLICT", 409, "선택한 시간은 이미 예약이 찼습니다."
            ) from exc
        return 200, _envelope(
            True,
            None,
            "ACCEPTED",
            reservationRecordId=row["reservation_record_id"],
            reservationDateTime=f"{when}:00",
        )

    # NEW_RESERVATION
    if service_id is None:
        raise SubmitError("INVALID", 422, "서비스 정보가 없습니다.")
    _require_free(conn, when, operator_id, service_id)
    customer_no = _resolve_customer(conn, customer)
    record_id = f"DC-R-{uuid.uuid4().hex[:10]}"
    try:
        conn.execute(
            "INSERT INTO appointments VALUES (?, ?, ?, ?, ?, 'CONFIRMED', NULL, 'agent', ?, ?)",
            (record_id, customer_no, service_id, operator_id, when, stamp, stamp),
        )
    except sqlite3.IntegrityError as exc:
        raise SubmitError(
            "CONFLICT", 409, "선택한 시간은 이미 예약이 찼습니다."
        ) from exc
    return 200, _envelope(
        True,
        None,
        "ACCEPTED",
        reservationRecordId=record_id,
        reservationDateTime=f"{when}:00",
    )
