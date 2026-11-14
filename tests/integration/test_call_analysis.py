"""Local post-call pipeline: call-end event -> consumer -> SQLite (S21)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from call_analysis.consumer import consume
from call_analysis.repository import Repository
from livekit.agents import llm

from utils.agent_event import agent_event_extra
from utils.call_end_event import build_call_end_payload, publish_call_end_event


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


def _events_file(demo_env):
    return demo_env / "events" / "service-center.call-analysis.jsonl"


async def test_payload_keeps_developer_agent_events():
    data, session = _session("room-a", ("BOOKING_CREATE", "SUCCESS"))
    payload = build_call_end_payload(data, session)
    developer = [
        i for i in payload["conversation"]["items"] if i["role"] == "developer"
    ]
    assert developer[0]["extra"]["agent_event"]["name"] == "BOOKING_CREATE"
    json.dumps(payload)  # must stay JSON-serializable for the event log


async def test_consumer_saves_each_room_once_and_resumes_from_offset(demo_env):
    for room, events in [
        ("room-a", [("BOOKING_CREATE", "STARTED"), ("BOOKING_CREATE", "SUCCESS")]),
        ("room-b", [("BOOKING_CREATE", "STARTED"), ("BOOKING_CREATE", "CONFLICT")]),
        ("room-a", [("BOOKING_CREATE", "SUCCESS")]),  # redelivered duplicate
    ]:
        assert await publish_call_end_event(*_session(room, *events))
    with _events_file(demo_env).open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")

    repo = Repository(demo_env / "analysis.sqlite3")
    first = consume(_events_file(demo_env), repo)
    second = consume(_events_file(demo_env), repo)  # nothing new
    results = {r["room_name"]: r for r in repo.results()}
    repo.close()

    assert (first.read, first.saved, first.skipped_duplicate, first.malformed) == (
        4,
        2,
        1,
        1,
    )
    assert second.read == 0
    assert results["room-a"]["resolution"] == "booked"
    assert results["room-a"]["reservation_record_id"] == "DC-R-1"
    assert results["room-a"]["duration_seconds"] == 150.0
    assert results["room-a"]["caller_turns"] == 1
    assert results["room-b"]["resolution"] == "no_booking_action"
    assert "reservation_submit_rejected" in results["room-b"]["warnings"]


async def test_console_calls_sharing_a_room_name_are_kept_apart(demo_env):
    first, s1 = _session("console", ("BOOKING_CREATE", "SUCCESS"))
    second, s2 = _session("console", ("BOOKING_CANCEL", "SUCCESS"))
    base = first.to_safe_dict
    first.to_safe_dict = lambda: base() | {
        "call_config_data": {"call_start_time": "2026-11-16T10:00:00"}
    }
    second.to_safe_dict = lambda: base() | {
        "call_config_data": {"call_start_time": "2026-11-16T11:00:00"}
    }
    await publish_call_end_event(first, s1)
    await publish_call_end_event(second, s2)
    repo = Repository(demo_env / "analysis.sqlite3")
    stats = consume(_events_file(demo_env), repo)
    repo.close()
    assert (stats.saved, stats.skipped_duplicate) == (2, 0)
