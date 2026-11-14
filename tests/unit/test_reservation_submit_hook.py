"""Reservation write hook: what the booking tools do with each submission outcome."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from data import BusinessData, DynamicBookingPending, RecipientData
from tools import reservation_submit
from tools.reservation_submit import idempotency_key, submit_current_reservation


class FakeHistory:
    def __init__(self):
        self.events = []

    def add_message(self, *, role, content, extra=None, **_):
        self.events.append(extra or {})


class FakeAgent:
    def __init__(self):
        self.exact_tool_calls = []

    def _request_exact_tool_call(self, tool_name, **_):
        self.exact_tool_calls.append(tool_name)


def make_ctx(reservation_info: dict):
    agent = FakeAgent()
    said: list[str] = []
    audio: list[bool] = []
    session = SimpleNamespace(
        history=FakeHistory(),
        current_agent=agent,
        say=lambda text, **_: said.append(text),
        input=SimpleNamespace(set_audio_enabled=audio.append),
    )
    userdata = SimpleNamespace(
        reservation_info=dict(reservation_info),
        recipient_data=RecipientData(
            phone="01000000001", customer_no="DC-1001", user_name="데모 고객 일"
        ),
        business_data=BusinessData(site_id="demo-device-care", client_id=9001),
        call_config_data=SimpleNamespace(language="ko-KR"),
        room_name="room-test",
        dynamic_booking_pending=DynamicBookingPending(
            301, "휴대폰 점검", 401, "상담사 A", "2026-11-17 11:00"
        ),
    )
    return SimpleNamespace(userdata=userdata, session=session), agent, said, audio


NEW_11 = {
    "type": "NEW_RESERVATION",
    "service_id": 301,
    "operator_id": 401,
    "reservation_request_date_time": "2026-11-17 11:00:00",
}


async def test_accepted_keeps_info_and_records_record_id():
    ctx, agent, said, _ = make_ctx(NEW_11)
    outcome = await submit_current_reservation(ctx, "booking_create")
    assert outcome.accepted
    assert ctx.userdata.reservation_info["reservation_record_id"].startswith("DC-R-")
    assert said == [] and agent.exact_tool_calls == []


async def test_retry_of_same_call_replays_instead_of_double_booking():
    ctx, *_ = make_ctx(NEW_11)
    first = await submit_current_reservation(ctx, "booking_create")
    ctx.userdata.reservation_info = dict(NEW_11)
    second = await submit_current_reservation(ctx, "booking_create")
    assert first.accepted and second.accepted and second.replayed
    assert first.reservation_record_id == second.reservation_record_id


async def test_conflict_clears_state_and_reenters_create_flow():
    other, *_ = make_ctx(NEW_11)
    other.userdata.recipient_data = RecipientData(
        phone="01000000002", customer_no="DC-1002"
    )
    other.userdata.room_name = "room-other"
    assert (await submit_current_reservation(other, "booking_create")).accepted

    ctx, agent, said, audio = make_ctx(NEW_11)
    outcome = await submit_current_reservation(ctx, "booking_create")

    assert outcome.status == "CONFLICT"
    assert ctx.userdata.reservation_info == {}  # never published as a reservation
    assert ctx.userdata.dynamic_booking_pending is None  # stale candidate dropped
    assert agent.exact_tool_calls == ["create_new_appointment"]
    assert "마감" in said[0] and audio == [True]
    assert {"name": "BOOKING_CREATE", "status": "CONFLICT"}.items() <= next(
        e["agent_event"]
        for e in ctx.session.history.events
        if e.get("agent_event", {}).get("name") == "BOOKING_CREATE"
    ).items()


async def test_cancel_of_missing_appointment_changes_nothing():
    ctx, agent, said, _ = make_ctx(
        {"type": "CANCEL_RESERVATION", "reservation_record_id": "DC-R-SEED-2"}  # not mine
    )
    outcome = await submit_current_reservation(ctx, "booking_cancel")
    assert outcome.status == "NOT_FOUND"
    assert agent.exact_tool_calls == [] and "찾을 수 없어" in said[0]


@pytest.mark.parametrize("fault", ["error", "timeout"])
async def test_backend_failure_hands_off_without_claiming_success(monkeypatch, fault):
    monkeypatch.setenv("DEMO_FAULTS", f'{{"reservation_submit": "{fault}"}}')
    routed = []

    async def fake_route(ctx, **kwargs):
        routed.append(kwargs)

    monkeypatch.setattr(reservation_submit, "route_booking_exit", fake_route)
    ctx, agent, said, _ = make_ctx(NEW_11)
    outcome = await submit_current_reservation(ctx, "booking_create")

    assert outcome.status == "ERROR" and not outcome.accepted
    assert ctx.userdata.reservation_info == {}
    assert routed and routed[0]["reason_code"] == "staff_connection"
    assert "완료되지 않았" in said[0]


def test_idempotency_key_is_stable_per_call_and_request():
    a = idempotency_key("room", {"x": 1, "y": 2})
    assert a == idempotency_key("room", {"y": 2, "x": 1})
    assert a != idempotency_key("room2", {"x": 1, "y": 2})
    assert a != idempotency_key("room", {"x": 1, "y": 3})
