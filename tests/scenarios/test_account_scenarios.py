"""Scenarios S08, S09, S10, S15, S17: lookup, modify, cancel, failures, concurrency.

These run the reservation write hook (``submit_current_reservation``) with real session data
built from the synthetic dial_info, plus the real lookup client, against the fake
backend. The selection tasks that pick *which* appointment (AgentTasks driven by
the LLM) are represented by the ``reservation_info`` they produce.
"""

from __future__ import annotations

import asyncio

from external_apis import booking_api
from external_apis.types.request_types import GetConfirmedAppointmentsRequest
from tools.reservation_submit import submit_current_reservation

from .harness import (
    FakeSession,
    confirmed_rows,
    current_agent,
    session_data,
    tool_ctx,
)

SITE = "demo-device-care"


def _ctx(info: dict, **recipient):
    csd = session_data(**recipient)
    csd.reservation_info = dict(info)
    session = FakeSession(current_agent())
    return tool_ctx(csd, session), csd, session


async def _my_appointments(phone: str) -> list[str]:
    response = await booking_api.get_confirmed_appointments(
        GetConfirmedAppointmentsRequest(site_id=SITE, phone=phone)
    )
    return [item.reservationRecordId for item in response.data]


async def test_s08_valid_cancellation_cancels_only_the_owned_target():
    assert await _my_appointments("01000000001") == ["DC-R-SEED-1"]
    ctx, csd, session = _ctx(
        {"type": "CANCEL_RESERVATION", "reservation_record_id": "DC-R-SEED-1"}
    )

    outcome = await submit_current_reservation(ctx, "booking_cancel")

    assert outcome.accepted
    assert await _my_appointments("01000000001") == []
    # The other customer's appointments are untouched.
    assert await _my_appointments("01000000002") == ["DC-R-SEED-2", "DC-R-SEED-3"]


async def test_s09_foreign_reference_is_neither_disclosed_nor_mutated():
    # Lookup is scoped to the caller's own phone.
    assert "DC-R-SEED-2" not in await _my_appointments("01000000001")

    for kind in ("CANCEL_RESERVATION", "MODIFY_RESERVATION"):
        info = {"type": kind, "reservation_record_id": "DC-R-SEED-2"}
        if kind == "MODIFY_RESERVATION":
            info |= {
                "operator_id": 401,
                "reservation_request_date_time": "2026-11-17 15:00:00",
            }
        ctx, csd, session = _ctx(info)
        outcome = await submit_current_reservation(
            ctx, "booking_modify" if "MODIFY" in kind else "booking_cancel"
        )
        assert outcome.status == "NOT_FOUND"
        assert csd.reservation_info == {}
        assert not any("DC-R-SEED-2" in line for line in session.said)

    assert [
        r["start_at"] for r in confirmed_rows(reservation_record_id="DC-R-SEED-2")
    ] == ["2026-11-18 14:00"]


async def test_s10_backend_failure_is_reported_without_claiming_success(monkeypatch):
    monkeypatch.setenv("DEMO_FAULTS", '{"reservation_submit": "error"}')
    ctx, csd, session = _ctx(
        {
            "type": "NEW_RESERVATION",
            "service_id": 301,
            "operator_id": 401,
            "reservation_request_date_time": "2026-11-17 11:00:00",
        }
    )

    outcome = await submit_current_reservation(ctx, "booking_create")

    assert outcome.status == "ERROR"
    assert csd.reservation_info == {}
    assert confirmed_rows(start_at="2026-11-17 11:00") == []
    assert any("완료되지 않았" in line for line in session.said)
    assert session.current_agent.booking_exits[0]["reason_code"] == "staff_connection"


async def test_s15_concurrent_callers_for_one_slot_get_one_success_and_one_conflict():
    info = {
        "type": "NEW_RESERVATION",
        "service_id": 301,
        "operator_id": 401,
        "reservation_request_date_time": "2026-11-18 16:00:00",
    }
    a, csd_a, _ = _ctx(info, phone="01000000001", customer_no="DC-1001")
    b, csd_b, _ = _ctx(info, phone="01000000002", customer_no="DC-1002")
    csd_b.room_name = "scenario-room-b"

    first, second = await asyncio.gather(
        submit_current_reservation(a, "booking_create"),
        submit_current_reservation(b, "booking_create"),
    )

    assert sorted([first.status, second.status]) == ["ACCEPTED", "CONFLICT"]
    assert len(confirmed_rows(start_at="2026-11-18 16:00", operator_id=401)) == 1


async def test_s17_modification_moves_only_the_confirmed_target():
    ctx, csd, session = _ctx(
        {
            "type": "MODIFY_RESERVATION",
            "reservation_record_id": "DC-R-SEED-1",
            "service_id": 301,
            "operator_id": 401,
            "reservation_request_date_time": "2026-11-17 15:00:00",
            "previous_reservation_date_time": "2026-11-19 10:00:00",
        }
    )

    outcome = await submit_current_reservation(ctx, "booking_modify")

    assert outcome.accepted
    assert [
        r["start_at"] for r in confirmed_rows(reservation_record_id="DC-R-SEED-1")
    ] == ["2026-11-17 15:00"]
    assert (
        confirmed_rows(start_at="2026-11-19 10:00", operator_id=401) == []
    )  # released
    assert len(confirmed_rows(source="seed")) == 3  # no extra rows created
