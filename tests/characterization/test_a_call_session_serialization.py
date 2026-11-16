"""Golden tests pinning CallSessionData serialization.

``to_safe_dict`` is the ``call_session_data`` payload written to the local event log; ``to_dict`` feeds the CALL_SESSION_DATA debug
log. Both outputs, including their quirks, must stay stable.
"""

import json
from types import SimpleNamespace

from data import (
    AgentsData,
    BookingMemoContext,
    CallSessionData,
    DynamicBookingResumeContext,
    TransferRecord,
)

DIAL_INFO = {
    "business_data": {
        "site_id": "site-1",
        "client_id": "42",
        "client_name": "Example Device Care",
        "client_phone": "0212345678",
        "client_address": "Seoul",
        "is_work_time": False,
    },
    "recipient_data": {
        "user_name": "Customer",
        "phone": "01000000000",
        "verification_code": "123456",
        "customer_no": "7",
        "sip_identity": "sip_01000000000",
        "caller_phone": "01000000000",
        "last_reservation": {"operator_id": 3, "operator_name": "Advisor A"},
    },
    "call_config_data": {
        "language": "ko-KR",
        "llm_model": "openai:gpt",
        "tts_model": "chirp_3",
        "transfer_to": "0299999999",
        "transfer_mode": "cold_transfer",
    },
    "agents_config": {"booking_agent": {"booking_create": "transfer"}},
    "workflow_config": {
        "booking": {"schedule_lookup_days": "30"},
        "leave_message": {"booking_contact_collection": "desired_datetime_and_message"},
    },
    "flow_config": {
        "entry_node_id": "greeting",
        "nodes": [{"id": "greeting", "type": "greeting", "name": "g", "message": "hi"}],
    },
    "queued": True,
    "queue_wait_seconds": 12,
}


def _build_call_session_data() -> CallSessionData:
    ctx = SimpleNamespace(room=SimpleNamespace(name="room-1"))
    data = CallSessionData.from_dial_info(ctx, json.loads(json.dumps(DIAL_INFO)))
    data.agents_data = AgentsData(user_request="req", dtmf_tool="booking_create")
    data.reservation_data = {"k": "v"}
    data.update_new_reservation_info(service_name="Phone Repair")
    data.lkapi_ref["instance"] = object()
    data.transfer_record = TransferRecord(
        type="warm_transfer", transfer_status="SUCCESS", total_wait_seconds=1.5
    )
    data.booking_memo_context = BookingMemoContext(
        service_name="Phone Repair", searched_ranges=["a"]
    )
    data.dynamic_booking_resume = DynamicBookingResumeContext(paused=True)
    data.pending_api_results.append({"api": "X", "success": True})
    data.recording_started_at = 1.25
    data.caller_disconnected = True
    data.call_config_data.call_start_time = "2026-10-20T10:00:00"
    return data


def _normalize(value: dict) -> dict:
    return json.loads(json.dumps(value, default=repr, sort_keys=True))


EXPECTED_SAFE_DICT = {
    "business_data": {
        "client_address": "Seoul",
        "client_id": 42,
        "client_name": "Example Device Care",
        "client_phone": "0212345678",
        "is_work_time": False,
        "site_id": "site-1",
    },
    "call_config_data": {
        "call_end_time": None,
        "call_start_time": "2026-10-20T10:00:00",
        "language": "ko-KR",
        "llm_model": "openai:gpt",
        "sip_outbound_trunk_id": None,
        "stt_model": None,
        "transfer_message": None,
        "transfer_mode": "cold_transfer",
        "transfer_to": "0299999999",
        "tts_model": "google:chirp_3",
        "tts_speed": 1.0,
        "voice": "ko-KR-Chirp3-HD-Kore",
    },
    "pending_api_results": [{"api": "X", "success": True}],
    "recipient_data": {
        "verification_code": "123456",
        "caller_phone": "01000000000",
        "customer_no": "7",
        "last_reservation": {
            "service_id": None,
            "service_name": None,
            "operator_id": 3,
            "operator_name": "Advisor A",
        },
        "phone": "01000000000",
        "sip_identity": "sip_01000000000",
        "user_name": "Customer",
    },
    "recording_filepath": None,
    "recording_started_at": 1.25,
    "reservation_info": {"service_name": "Phone Repair", "type": "NEW_RESERVATION"},
    "room_name": "room-1",
    "transfer_record": {
        "action_type": None,
        "briefing_text": None,
        "total_wait_seconds": 1.5,
        "transfer_requested_at": None,
        "transfer_status": "success",
        "transfer_target_phone": None,
        "type": "warm_transfer",
    },
}

EXPECTED_DEBUG_DICT = {
    "agents_data": {
        "agents": {},
        "dtmf_tool": "booking_create",
        "user_request": "req",
    },
    "booking_create_progress": {
        "service_id": None,
        "service_name": None,
        "operator_id": None,
        "operator_name": None,
        "restart_hint": None,
    },
    "business_data": {
        "client_address": "Seoul",
        "client_id": 42,
        "client_name": "Example Device Care",
        "client_phone": "0212345678",
        "is_work_time": False,
        "site_id": "site-1",
    },
    "call_config_data": {
        "call_end_time": None,
        "call_start_time": "2026-10-20T10:00:00",
        "language": "ko-KR",
        "llm_model": "openai:gpt",
        "sip_outbound_trunk_id": None,
        "stt_model": None,
        "transfer_message": None,
        "transfer_mode": "cold_transfer",
        "transfer_to": "0299999999",
        "tts_model": "google:chirp_3",
        "tts_speed": 1.0,
        "voice": "ko-KR-Chirp3-HD-Kore",
    },
    "caller_disconnected": True,
    "dynamic_booking_reference": None,
    "flow_config": {
        "entry_node_id": "greeting",
        "nodes": [
            "FlowNode(id='greeting', type='greeting', name='g', "
            "field=None, branches=None, input_method=None, "
            "message='hi', replay_message=None, "
            "dtmf_options=None, action_type=None, "
            "action_config=None, termination=None, events=None, "
            "label=None, next=None)"
        ],
    },
    "recipient_data": {
        "verification_code": "123456",
        "caller_phone": "01000000000",
        "customer_no": "7",
        "last_reservation": {
            "service_id": None,
            "service_name": None,
            "operator_id": 3,
            "operator_name": "Advisor A",
        },
        "phone": "01000000000",
        "sip_identity": "sip_01000000000",
        "user_name": "Customer",
    },
    "recording_filepath": None,
    "recording_started_at": 1.25,
    "reservation_data": {"k": "v"},
    "reservation_info": {"service_name": "Phone Repair", "type": "NEW_RESERVATION"},
    "room_name": "room-1",
    "transfer_record": {
        "action_type": None,
        "briefing_text": None,
        "total_wait_seconds": 1.5,
        "transfer_requested_at": None,
        "transfer_status": "success",
        "transfer_target_phone": None,
        "type": "warm_transfer",
    },
    "workflow_config": {
        "booking": {
            "booking_create": {
                "service_selection": {
                    "display_mode": "parent_with_child",
                    "free_utterance": False,
                },
                "select_operator": False,
                "verify_single_customer_identity": False,
            },
            "schedule_lookup_days": 30,
        },
        "leave_message": {
            "additional_instructions": "",
            "booking_contact_collection": "desired_datetime_and_message",
        },
    },
}


def test_safe_dict_matches_golden_payload():
    result = _build_call_session_data().to_safe_dict()

    assert list(result) == [
        "reservation_info",
        "room_name",
        "business_data",
        "recipient_data",
        "call_config_data",
        "pending_api_results",
        "recording_filepath",
        "recording_started_at",
        "transfer_record",
    ]
    assert _normalize(result) == EXPECTED_SAFE_DICT


def test_debug_dict_matches_golden_payload():
    result = _build_call_session_data().to_dict()

    assert list(result) == [
        "agents_data",
        "reservation_data",
        "reservation_info",
        "room_name",
        "business_data",
        "recipient_data",
        "call_config_data",
        "workflow_config",
        "booking_create_progress",
        "dynamic_booking_reference",
        "recording_filepath",
        "recording_started_at",
        "flow_config",
        "caller_disconnected",
        "transfer_record",
    ]
    assert _normalize(result) == EXPECTED_DEBUG_DICT


def test_safe_dict_quirks_are_preserved():
    data = _build_call_session_data()
    data.reservation_info = {}

    result = data.to_safe_dict()

    # Empty reservation_info is published as None.
    assert result["reservation_info"] is None
    # Nested dataclasses inside a dataclass without to_safe_dict are dropped.
    assert "agents_config" not in result["call_config_data"]


def test_safe_dict_honors_extra_exclusions():
    result = _build_call_session_data().to_safe_dict(
        exclude_fields={"room_name", "transfer_record"}
    )

    assert "room_name" not in result
    assert "transfer_record" not in result
    assert "business_data" in result
