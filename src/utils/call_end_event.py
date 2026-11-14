"""Call-end event payload shared by the local consoles.

The local consoles publish the same payload shape as ``agent-server.py``
through the local event log, so the post-call consumer
(``conversation_analysis/``) also works for console calls. ``agent-server.py``
publishes its payload inline.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from external_apis.kafka_producer import DEFAULT_TOPIC, send_kafka_message_async


def build_call_end_payload(call_session_data: Any, session: Any) -> dict[str, Any]:
    """Same structure ``agent-server.py`` sends: safe session data, messages, events."""
    return {
        "call_session_data": call_session_data.to_safe_dict(),
        "conversation": {
            "items": [
                item.model_dump()
                for item in session.history.items
                if item.type == "message"
            ],
        },
        "agent_events": [
            event.model_dump()
            for event in getattr(session, "_recorded_events", [])
            if event.type in ("conversation_item_added", "function_tools_executed")
        ],
    }


async def publish_call_end_event(call_session_data: Any, session: Any) -> bool:
    try:
        payload = build_call_end_payload(call_session_data, session)
    except Exception as error:  # never break shutdown on export problems
        logger.bind(event="CALL_END_EVENT", stage="build_failed").opt(
            exception=error
        ).error("Failed to build call-end event")
        return False
    ok = await send_kafka_message_async(message=payload, topic=DEFAULT_TOPIC)
    logger.bind(event="CALL_END_EVENT", published=ok, topic=DEFAULT_TOPIC).info(
        "Call-end event published to the local event log"
    )
    return ok
