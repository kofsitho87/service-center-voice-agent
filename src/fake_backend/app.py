"""Request router for the fake backend.

Routes implement the HTTP contracts consumed by the clients in
``external_apis/`` and ``services/trunk_allocation_client.py``, including
``POST /reservation/{site}/requests`` for in-call reservation writes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from . import config, faults, outbox, store

Handler = Callable[[httpx.Request, re.Match[str]], httpx.Response]


def _json(status: int, body: Any) -> httpx.Response:
    return httpx.Response(status, json=body)


def _ok(contents: Any) -> httpx.Response:
    return _json(200, {"resultMsg": None, "contents": contents})


def _param_date(request: httpx.Request, name: str) -> date:
    return date.fromisoformat(request.url.params[name])


def _maybe_int(request: httpx.Request, name: str) -> int | None:
    value = request.url.params.get(name)
    return int(value) if value not in (None, "") else None


def _read_fault() -> httpx.Response | None:
    outcome = faults.next_outcome("reservation_read")
    if outcome == "error":
        return _json(
            500, {"resultMsg": "가짜 백엔드 장애(설정된 오류)입니다.", "contents": None}
        )
    if outcome == "timeout":
        raise httpx.ReadTimeout("simulated reservation API timeout")
    return None


# ---------------------------------------------------------------- reservation


def site_services(_request: httpx.Request, _m: re.Match[str]) -> httpx.Response:
    if fault := _read_fault():
        return fault
    with store.connection() as conn:
        return _ok({"services": store.list_services(conn)})


def site_operators(_request: httpx.Request, _m: re.Match[str]) -> httpx.Response:
    if fault := _read_fault():
        return fault
    with store.connection() as conn:
        return _ok(store.list_operators(conn))


def service_operators(_request: httpx.Request, m: re.Match[str]) -> httpx.Response:
    service_id = int(m["service_id"])
    with store.connection() as conn:
        operators = [
            op
            for op in store.list_operators(conn)
            if any(d["serviceId"] == service_id for d in op["services"])
        ]
    return _ok(operators)


def site_schedules(request: httpx.Request, _m: re.Match[str]) -> httpx.Response:
    if fault := _read_fault():
        return fault
    start = _param_date(request, "startDate")
    end = _param_date(request, "endDate")
    kind = request.url.params.get("type", "DATE")
    size = int(request.url.params.get("size", "10"))
    with store.connection() as conn:
        slots = store.filter_slots(
            conn,
            store.candidate_slots(conn, start, end),
            service_id=_maybe_int(request, "serviceId"),
            operator_id=_maybe_int(request, "operatorId"),
        )
    if kind == "DATE_TIME":
        rows = [
            {"time": f"{when}:00", "operatorIds": ids} for when, ids in slots.items()
        ]
    else:
        by_date: dict[str, list[int]] = {}
        for when, ids in slots.items():
            bucket = by_date.setdefault(when[:10], [])
            bucket.extend(i for i in ids if i not in bucket)
        rows = [{"time": day, "operatorIds": ids} for day, ids in by_date.items()]
    return _ok({"schedules": rows[:size]})


def operator_available_times(
    request: httpx.Request, m: re.Match[str]
) -> httpx.Response:
    if fault := _read_fault():
        return fault
    operator_id = int(m["operator_id"])
    start = _param_date(request, "startDate")
    end = _param_date(request, "endDate")
    with store.connection() as conn:
        slots = store.filter_slots(
            conn, store.candidate_slots(conn, start, end), operator_id=operator_id
        )
    # The API sends KST wall-clock time with a "Z" suffix and the client treats
    # it as KST (operator_schedule_selection_task).
    times = [when.replace(" ", "T") + ":00.000Z" for when in slots]
    return _ok({"operatorId": operator_id, "availableTimes": times})


def confirmed_reservations(request: httpx.Request, _m: re.Match[str]) -> httpx.Response:
    if fault := _read_fault():
        return fault
    with store.connection() as conn:
        rows = store.confirmed_reservations(
            conn,
            request.url.params.get("phoneNumber", ""),
            customer_no=request.url.params.get("customerNumber"),
            verification_code=request.url.params.get("verificationCode"),
        )
    return _json(200, rows)


def customer_by_phone(request: httpx.Request, _m: re.Match[str]) -> httpx.Response:
    if fault := _read_fault():
        return fault
    with store.connection() as conn:
        rows = store.customers_by_phone(conn, request.url.params.get("phoneNumber", ""))
    customers = [
        {
            "customerNo": row["customer_no"],
            "customerNm": row["name"],
            "verification_code": row["verification_code"],
            "telNum": row["phone"],
            "new": False,
            "fail": False,
        }
        for row in rows
    ]
    kind = "NEW" if not customers else ("SINGLE" if len(customers) == 1 else "MULTI")
    return _json(
        200,
        {
            "success": True,
            "resultMsg": None,
            "responseCode": "SUCCESS",
            "customerResponseType": kind,
            "single": kind == "SINGLE",
            "new": kind == "NEW",
            "multi": kind == "MULTI",
            "customers": customers,
        },
    )


def submit_reservation(request: httpx.Request, m: re.Match[str]) -> httpx.Response:
    outcome = faults.next_outcome("reservation_submit")
    if outcome == "error":
        return _json(
            500,
            {
                "success": False,
                "resultMsg": "가짜 백엔드 장애(설정된 오류)입니다.",
                "contents": {"status": "ERROR"},
            },
        )
    if outcome == "timeout":
        raise httpx.ReadTimeout("simulated reservation submit timeout")
    business = store.load_business()
    if m["site_id"] != business.site_id:
        return _json(
            404,
            {
                "success": False,
                "resultMsg": "unknown site",
                "contents": {"status": "NOT_FOUND"},
            },
        )
    payload = json.loads(request.content or b"{}")
    with store.connection() as conn:
        status, body = store.submit_reservation(
            conn, payload, request.headers.get("Idempotency-Key")
        )
    return _json(status, body)


# ----------------------------------------------------------------------- sms


def sms_send(request: httpx.Request, _m: re.Match[str]) -> httpx.Response:
    outcome = faults.next_outcome("sms")
    if outcome == "timeout":
        raise httpx.ReadTimeout("simulated SMS timeout")
    body = request.content.decode("utf-8", errors="replace")
    match = re.search(r"\{.*\}", body, flags=re.S)
    payload = json.loads(match.group(0)) if match else {}
    sent = outcome != "fail"
    outbox.append(
        "sms",
        {
            "site_id": payload.get("siteId"),
            "receiver": payload.get("receiverPhoneNumber"),
            "subject": payload.get("subject"),
            "content": payload.get("content"),
            "image_count": len(payload.get("imageUrls") or []),
            "delivered": False,  # simulated: nothing is ever sent
            "simulated_status": "SUCCESS" if sent else "FAILED",
        },
    )
    return _json(
        200,
        {
            "messageId": f"sim-{datetime.now(UTC).timestamp():.0f}",
            "messageType": "SMS",
            "sendStatus": "SUCCESS" if sent else "FAILED",
            "resultCode": 1 if sent else -1,
            "resultMessage": "simulated" if sent else "simulated failure",
        },
    )


# --------------------------------------------------------------- app context


def _fixture_text(name: str) -> str:
    path = config.FIXTURES_DIR / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


def call_context(request: httpx.Request, _m: re.Match[str]) -> httpx.Response:
    if faults.next_outcome("call_context") == "error":
        return _json(500, {"detail": "simulated call-context failure"})
    business = store.load_business()
    stamp = datetime(2026, 1, 1, tzinfo=config.BUSINESS_TZ).isoformat()
    return _json(
        200,
        {
            "data": {
                "id": "demo-call-context",
                "client_id": int(
                    request.url.params.get("client_id", business.client_id)
                ),
                "site_id": business.site_id,
                "center_name": business.name,
                "context": _fixture_text("call_context.md"),
                "content_updated_at": stamp,
                "created_at": stamp,
                "updated_at": stamp,
            }
        },
    )


# --------------------------------------------------------------------- trunk


def available_trunk(request: httpx.Request, m: re.Match[str]) -> httpx.Response:
    ts = request.headers.get("X-VB-Timestamp", "")
    signature = request.headers.get("X-VB-Signature", "")
    expected = hmac.new(
        config.FAKE_TRUNK_HMAC_SECRET.encode(),
        f"{ts}\nGET\n{request.url.raw_path.decode()}".encode(),
        hashlib.sha256,
    ).hexdigest()
    try:
        skew = abs(
            datetime.now(UTC)
            - datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        )
    except ValueError:
        skew = timedelta(days=1)
    if not hmac.compare_digest(signature, expected) or skew > timedelta(minutes=5):
        return _json(401, {"detail": "invalid signature"})
    outcome = faults.next_outcome("trunk")
    outbox.append("trunk", {"company_id": int(m["company_id"]), "outcome": outcome})
    if outcome == "network":
        raise httpx.ConnectError("simulated trunk API network failure")
    status = {"available": 200, "busy": 503}.get(outcome, 404)
    return _json(status, {"status": outcome})


def sip_call_status(request: httpx.Request, _m: re.Match[str]) -> httpx.Response:
    outbox.append("sip_status", json.loads(request.content or b"{}"))
    return _json(202, {"accepted": True})


ROUTES: list[tuple[str, str, str, Handler]] = [
    (
        config.RESERVATION_HOST,
        "GET",
        r"/reservation/operators/(?P<operator_id>\d+)/schedules/available",
        operator_available_times,
    ),
    (
        config.RESERVATION_HOST,
        "GET",
        r"/reservation/(?P<site_id>[^/]+)/services",
        site_services,
    ),
    (
        config.RESERVATION_HOST,
        "GET",
        r"/reservation/(?P<site_id>[^/]+)/operators",
        site_operators,
    ),
    (
        config.RESERVATION_HOST,
        "GET",
        r"/reservation/(?P<site_id>[^/]+)/schedules/available",
        site_schedules,
    ),
    (
        config.RESERVATION_HOST,
        "POST",
        r"/reservation/(?P<site_id>[^/]+)/requests",
        submit_reservation,
    ),
    (
        config.RESERVATION_HOST,
        "GET",
        r"/reservation/(?P<service_id>\d+)",
        service_operators,
    ),
    (
        config.RESERVATION_HOST,
        "GET",
        r"/reservation/(?P<site_id>[^/]+)/confirmed",
        confirmed_reservations,
    ),
    (
        config.RESERVATION_HOST,
        "GET",
        r"/customers/(?P<site_id>[^/]+)",
        customer_by_phone,
    ),
    (config.RESERVATION_HOST, "POST", r"/sms/send", sms_send),
    (config.CONTEXT_HOST, "GET", r"/call-context", call_context),
    (
        config.TRUNK_HOST,
        "GET",
        r"/api/v1/warm-transfer/companies/(?P<company_id>\d+)/available-trunk",
        available_trunk,
    ),
    (config.SIP_STATUS_HOST, "POST", r"/api/v1/sip/call-status", sip_call_status),
]


def dispatch(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    path = request.url.path
    for route_host, method, pattern, handler in ROUTES:
        if host == route_host and request.method == method:
            match = re.fullmatch(pattern, path)
            if match:
                response = handler(request, match)
                outbox.append(
                    "requests",
                    {
                        "method": method,
                        "host": host,
                        "path": path,
                        "status": response.status_code,
                    },
                )
                return response
    outbox.append(
        "requests",
        {"method": request.method, "host": host, "path": path, "status": 404},
    )
    return _json(404, {"resultMsg": "fake route not found", "contents": None})
