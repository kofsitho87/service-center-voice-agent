"""Call-end payload preserves structured events as JSON."""

from __future__ import annotations

import json
from types import SimpleNamespace

from livekit.agents import llm

from utils.agent_event import agent_event_extra
from utils.call_end_event import build_call_end_payload


def _session(room: str, *events: tuple[str, str]):
    history = llm.ChatContext()
    history.add_message(role="assistant", content="안녕하세요, AI 상담원입니다.")
    history.add_message(role="user", content="휴대폰 점검 예약할게요")
    for name, status in events:
        history.add_message(
            role="developer", content="", extra=agent_event_extra(name, status=status)
        )
    history.add_message(role="assistant", content="예약 신청이 완료되었습니다.")
    data = SimpleNamespace(
        to_safe_dict=lambda: {
            "room_name": room,
            "reservation_info": {
                "type": "NEW_RESERVATION",
                "reservation_record_id": "DC-R-1",
            }
            if ("BOOKING_CREATE", "SUCCESS") in events
            else {},
            "call_config_data": {
                "call_start_time": "2026-11-16T10:00:00",
                "call_end_time": "2026-11-16T10:02:30",
            },
        }
    )
    return data, SimpleNamespace(history=history, _recorded_events=[])



async def test_payload_keeps_developer_agent_events():
    data, session = _session("room-a", ("BOOKING_CREATE", "SUCCESS"))
    payload = build_call_end_payload(data, session)
    developer = [
        i for i in payload["conversation"]["items"] if i["role"] == "developer"
    ]
    assert developer[0]["extra"]["agent_event"]["name"] == "BOOKING_CREATE"
    json.dumps(payload)  # must stay JSON-serializable for the event log

