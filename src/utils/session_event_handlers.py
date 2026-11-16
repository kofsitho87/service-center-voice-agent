import asyncio
import os

from livekit import rtc
from livekit.agents import (
    AgentSession,
    CloseEvent,
    ConversationItemAddedEvent,
    get_job_context,
)
from loguru import logger

from data import CallSessionData
from fake_backend.config import FAKE_SIP_STATUS_BASEURL
from utils.agent_event import agent_event_extra
from utils.http_client import create_async_http_client

# SIP Inbound API URL for call status updates
# Recorded by the fake backend instead of a local SIP service.
SIP_INBOUND_API_URL = os.getenv("SIP_INBOUND_API_URL", FAKE_SIP_STATUS_BASEURL)

def register_session_event_handlers(
    session: AgentSession,
    call_session_data: CallSessionData,
) -> None:
    """Register common session event handlers in a single place.

    Args:
        session: The LiveKit AgentSession instance.
        call_session_data: Per-call state shared with the agents.
    """

    job_ctx = get_job_context()
    room = job_ctx.room

    background_tasks: set[asyncio.Task] = set()

    @room.on("participant_connected")
    def _on_participant_connected(event: rtc.RemoteParticipant):
        logger.bind(
            event="SESSION_PARTICIPANT",
            stage="connected",
            room=room.name,
            participant=getattr(event, "identity", None),
        ).debug("Participant connected")

    @room.on("participant_attributes_changed")
    def _on_participant_attributes_changed(
        changed_attributes: dict[str, str], participant: rtc.Participant
    ):
        call_status = changed_attributes.get("sip.callStatus")
        if not call_status:
            return

        call_id = participant.attributes.get("sip.callID", "")
        logger.bind(
            event="SIP_CALL_STATUS_CHANGED",
            call_id=call_id,
            call_status=call_status,
            room_name=room.name,
            participant_id=participant.sid,
        ).info(f"SIP call status changed to {call_status}")

        async def send_webhook():
            try:
                async with await create_async_http_client(timeout=5.0) as client:
                    await client.post(
                        f"{SIP_INBOUND_API_URL}/api/v1/sip/call-status",
                        json={
                            "call_id": call_id,
                            "call_status": call_status,
                            "room_name": room.name,
                            "participant_id": participant.sid,
                        },
                    )
            except Exception as e:
                logger.bind(event="CALL_STATUS_WEBHOOK_ERROR").opt(exception=e).error(
                    "Failed to send call status webhook"
                )

        webhook_task = asyncio.create_task(send_webhook())
        background_tasks.add(webhook_task)
        webhook_task.add_done_callback(background_tasks.discard)

    @room.on("participant_disconnected")
    def _on_participant_disconnected(event: rtc.RemoteParticipant):
        identity = getattr(event, "identity", None)
        logger.bind(
            event="SESSION_PARTICIPANT",
            stage="disconnected",
            room=room.name,
            participant=identity,
        ).info("Participant disconnected")

        caller_identity = getattr(
            call_session_data.recipient_data, "sip_identity", None
        )
        if identity == caller_identity:
            call_session_data.caller_disconnected = True
            session.history.add_message(
                role="developer",
                content="",
                extra=agent_event_extra(
                    "PARTICIPANT_DISCONNECTED",
                    details={"identity": identity},
                ),
            )
            # close_on_disconnect가 처리하지 않는 SIP disconnect reason도 있다.
            # caller가 떠난 즉시 현재/대기 발화를 끊어 후속 TTS가 빈 room에서
            # 끝까지 재생되지 않게 한다. 세션 종료 자체는 기존 flow/RoomIO가 맡는다.
            try:
                session.interrupt(force=True)
            except RuntimeError as e:
                logger.bind(
                    event="SESSION_PARTICIPANT",
                    stage="disconnect_interrupt_skipped",
                    room=room.name,
                    participant=identity,
                ).opt(exception=e).debug(
                    "Agent session is not running; skipping caller disconnect interrupt"
                )

    @session.on("close")
    def _on_close(ev: CloseEvent):
        logger.bind(
            event="SESSION_CLOSE",
            stage="close",
            room=room.name,
            reason=ev.reason,
        ).info("Agent Session closed")

    if os.getenv("ENV") in ["local", "dev", "qa"]:

        @session.on("conversation_item_added")
        def _on_conversation_item_added(event: ConversationItemAddedEvent):
            if event.item.type != "message":
                return
            role = event.item.role
            content = event.item.text_content
            interrupted = event.item.interrupted
            if not content:
                return
            logger.bind(
                role=role,
                content=content,
                interrupted=interrupted,
            ).info("Conversation Item Added")
