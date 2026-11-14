"""Deterministic call outcome from recorded agent events (no LLM)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

# Terminal booking statuses the tools record as developer agent events.
_RESOLUTION_BY_EVENT: dict[tuple[str, str], str] = {
    ("BOOKING_CREATE", "SUCCESS"): "booked",
    ("BOOKING_MODIFY", "SUCCESS"): "modified",
    ("BOOKING_CANCEL", "SUCCESS"): "cancelled",
}
_FAILED_SUBMIT = {"CONFLICT", "NOT_FOUND", "INVALID", "ERROR"}


@dataclass
class CallResult:
    room_name: str
    resolution: str
    duration_seconds: float | None
    caller_turns: int
    agent_turns: int
    tools: list[str] = field(default_factory=list)
    booking_events: list[str] = field(default_factory=list)
    reservation_record_id: str | None = None
    reservation_type: str | None = None
    left_message: bool = False
    transfer_attempted: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_items(conversation: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Keep message items only, oldest first (source ``_normalize_items`` role)."""
    items = (conversation or {}).get("items") or []
    messages = [
        i
        for i in items
        if isinstance(i, dict) and i.get("type", "message") == "message"
    ]
    return sorted(messages, key=lambda i: i.get("created_at") or 0)


def _agent_events(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        (i.get("extra") or {})["agent_event"]
        for i in items
        if i.get("role") == "developer" and "agent_event" in (i.get("extra") or {})
    ]


def _duration(call_config: dict[str, Any]) -> float | None:
    start, end = call_config.get("call_start_time"), call_config.get("call_end_time")
    try:
        return (
            datetime.fromisoformat(end) - datetime.fromisoformat(start)
        ).total_seconds()
    except (TypeError, ValueError):
        return None


def _tool_names(agent_events: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for event in agent_events or []:
        for call in event.get("function_calls") or []:
            if call.get("name"):
                names.append(call["name"])
    return names


def analyze_call(payload: dict[str, Any]) -> CallResult:
    session = payload.get("call_session_data") or {}
    items = normalize_items(payload.get("conversation"))
    events = _agent_events(items)
    reservation = session.get("reservation_info") or {}

    booking_events = [
        f"{e['name']}:{e.get('status')}"
        for e in events
        if e.get("name", "").startswith("BOOKING_") and e.get("status")
    ]
    resolution = "no_booking_action"
    for event in events:
        key = (event.get("name"), event.get("status"))
        if key in _RESOLUTION_BY_EVENT:
            resolution = _RESOLUTION_BY_EVENT[key]
    left_message = any(e.get("name") == "LEAVE_MESSAGE" for e in events)
    transfer = any(e.get("name", "").endswith("TRANSFER_CALL") for e in events)
    if resolution == "no_booking_action":
        if left_message:
            resolution = "memo_left"
        elif transfer:
            resolution = "transfer_attempted"

    warnings: list[str] = []
    if resolution in {"booked", "modified", "cancelled"} and not reservation:
        warnings.append("success_event_without_reservation_info")
    if any(status.split(":")[1] in _FAILED_SUBMIT for status in booking_events):
        warnings.append("reservation_submit_rejected")

    return CallResult(
        room_name=session.get("room_name") or "unknown",
        resolution=resolution,
        duration_seconds=_duration(session.get("call_config_data") or {}),
        caller_turns=sum(1 for i in items if i.get("role") == "user"),
        agent_turns=sum(1 for i in items if i.get("role") == "assistant"),
        tools=_tool_names(payload.get("agent_events")),
        booking_events=booking_events,
        reservation_record_id=reservation.get("reservation_record_id"),
        reservation_type=reservation.get("type"),
        left_message=left_message,
        transfer_attempted=transfer,
        warnings=warnings,
    )
