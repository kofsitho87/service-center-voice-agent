"""Scenarios S13, S18, S20, S21: staff handoff, DTMF entry, queue cleanup, call end.

S13/S20 cover the queue and trunk-check boundaries plus the simulated
telephony gateway. A connected staff leg is not simulated (it needs a second LiveKit
participant); see docs/evaluation.md. S21 covers safe event publication.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from livekit import api

from data import DTMF_TOOL_ACTION_KEY_TO_FUNCTION, parse_dtmf_option
from external_apis import booking_api
from external_apis.kafka_producer import send_kafka_message_async
from external_apis.types.request_types import GetCustomerRequest
from fake_backend import outbox
from services import fake_transfer_gateway
from services.redis_client import _build_in_process_client
from services.trunk_allocation_client import TrunkAllocationClient, TrunkStatus
from services.warm_transfer_queue import CallDirection, WarmTransferQueue
from tasks.dtmf_verification_code_task import _is_valid_verification_code
from utils.common import normalize_verification_code

from .harness import session_data

COMPANY = 9001


async def test_s13_busy_then_unavailable_staff_line_never_places_a_call(monkeypatch):
    monkeypatch.setenv("DEMO_FAULTS", '{"trunk": ["busy", "busy", "available"]}')
    client = await TrunkAllocationClient.from_env_async()
    try:
        statuses = [(await client.check(COMPANY)).status for _ in range(3)]
    finally:
        await client.aclose()
    assert statuses == [TrunkStatus.BUSY, TrunkStatus.BUSY, TrunkStatus.AVAILABLE]

    request = api.CreateSIPParticipantRequest(
        room_name="scenario-room",
        sip_trunk_id="none",
        sip_call_to="01000000999",
        participant_identity="human-agent-demo",
    )
    with pytest.raises(fake_transfer_gateway.SimulatedTelephonyError):
        await fake_transfer_gateway.create_sip_participant(SimpleNamespace(), request)

    record = outbox.read("telephony")[-1]
    assert record["operation"] == "create_sip_participant"
    assert record["target"] == "***0999"  # the staff number is never logged in full


async def test_s18_keypad_menu_routes_with_a_synthetic_demo_code():
    csd = session_data("dial_info.keypad.ko.yaml")
    menu = next(
        n for n in csd.flow_config.nodes if n.id == csd.flow_config.entry_node_id
    )
    # parse_dtmf_option -> (next_node_id, user_request, tool, on_select_message)
    tools = {key: parse_dtmf_option(opt)[2] for key, opt in menu.dtmf_options.items()}
    assert {
        k: DTMF_TOOL_ACTION_KEY_TO_FUNCTION.get(v) for k, v in tools.items() if v
    } == {
        "1": "create_new_appointment",
        "2": "get_my_appointment_list",
        "3": "cancel_appointment",
        "4": "modify_appointment",
    }

    # The caller's phone resolves to one synthetic customer; the 6-digit demo
    # verification code is validated and matched by the verification logic.
    lookup = await booking_api.get_customer_by_phone(
        GetCustomerRequest(
            site_id=csd.business_data.site_id, phone=csd.recipient_data.phone
        )
    )
    [customer] = lookup.data.customers
    assert _is_valid_verification_code("000202")
    assert normalize_verification_code(customer.verification_code) == "000202"
    assert not _is_valid_verification_code("00020")  # too short
    assert csd.recipient_data.verification_code in (None, "")  # no verification code preloaded


async def test_s20_queue_disconnect_releases_occupancy_without_sip():
    queue = WarmTransferQueue(_build_in_process_client())
    await queue.enqueue(
        COMPANY, "caller-a", room_name="room-a", call_direction=CallDirection.INBOUND
    )
    await queue.enqueue(
        COMPANY, "caller-b", room_name="room-b", call_direction=CallDirection.INBOUND
    )
    assert await queue.get_rank(COMPANY, "caller-b") == 1

    await queue.dequeue(COMPANY, "caller-a", reason="caller_disconnected")
    await queue.dequeue(
        COMPANY, "caller-a", reason="caller_disconnected"
    )  # duplicate release

    assert await queue.get_queue_length(COMPANY) == 1
    assert await queue.get_rank(COMPANY, "caller-b") == 0
    assert await queue.get_rank(COMPANY, "caller-a") is None
    assert outbox.read("telephony") == []  # no SIP attempt at all


async def test_s21_call_end_event_is_published_once_as_safe_json(demo_env):
    csd = session_data()
    csd.reservation_info = {"type": "NEW_RESERVATION", "reservation_record_id": "DC-R-x"}
    ok = await send_kafka_message_async(
        message={
            "call_session_data": csd.to_safe_dict(),
            "conversation": {"items": []},
        },
        topic="service-center.call-analysis",
    )
    lines = (
        (demo_env / "events" / "service-center.call-analysis.jsonl")
        .read_text()
        .splitlines()
    )
    assert ok is True and len(lines) == 1
    payload = json.loads(lines[0])["value"]["call_session_data"]
    assert payload["reservation_info"]["reservation_record_id"] == "DC-R-x"
    assert "flow_config" not in payload  # excluded by the safe serializer
