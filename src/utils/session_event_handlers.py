import asyncio
import os

from livekit import api, rtc
from livekit.agents import (
    AgentSession,
    CloseEvent,
    ConversationItemAddedEvent,
    UserStateChangedEvent,
    get_job_context,
)
from loguru import logger

from data import CallSessionData
from fake_backend.config import FAKE_SIP_STATUS_BASEURL
from utils.agent_event import agent_event_extra
from utils.http_client import create_async_http_client
from utils.i18n import t

# SIP Inbound API URL for call status updates
# Recorded by the fake backend instead of a local SIP service.
SIP_INBOUND_API_URL = os.getenv("SIP_INBOUND_API_URL", FAKE_SIP_STATUS_BASEURL)

_USER_PRESENCE_PROMPTS_KO = {
    "dtmf": (
        "죄송합니다만, 입력하신 내용을 확인하지 못했습니다. "
        "원활한 상담을 위해 키패드로 번호를 입력해 주세요."
    ),
    "voice": (
        "죄송합니다만, 말씀하신 내용을 확인하지 못했습니다. "
        "원활한 상담을 위해 다시 말씀해 주세요."
    ),
    "mixed": (
        "죄송합니다만, 말씀하신 내용을 확인하지 못했습니다. "
        "원활한 상담을 위해 키패드로 번호를 입력하시거나 다시 말씀해 주세요."
    ),
}

_DEFAULT_USER_PRESENCE_PROMPTS = {
    "en-US": "Are you available to talk right now? If you can hear me, please respond.",
    "zh-CN": "您现在方便通话吗？如果能听到我，请回复一下。",  # noqa: RUF001
    "ja-JP": "今お話しできますか？聞こえていたら返事をお願いします。",  # noqa: RUF001
    "es-ES": "¿Le viene bien hablar ahora? Si me escucha, por favor responda.",
    "vi-VN": "Bạn có đang nghe máy không? Nếu nghe được, vui lòng trả lời giúp tôi.",
}


def _should_suppress_user_presence_task(session: AgentSession) -> bool:
    """현재 Task가 자체 사용자 부재 처리 정책을 사용하는지 확인한다."""
    try:
        current_agent = session.current_agent
    except RuntimeError:
        return False

    return bool(getattr(current_agent, "suppress_user_presence_task", False))


def _get_user_presence_prompt(session: AgentSession, lang: str) -> str:
    """현재 Task의 입력 방식에 맞는 사용자 부재 안내를 반환한다."""
    if lang != "ko-KR":
        return _DEFAULT_USER_PRESENCE_PROMPTS.get(
            lang, _DEFAULT_USER_PRESENCE_PROMPTS["en-US"]
        )

    mode = _get_user_presence_mode(session)
    return _USER_PRESENCE_PROMPTS_KO.get(mode, _USER_PRESENCE_PROMPTS_KO["voice"])


def _get_user_presence_mode(session: AgentSession) -> str:
    """현재 Task가 허용하는 사용자 입력 방식을 반환한다."""
    try:
        current_agent = session.current_agent
    except RuntimeError:
        return "voice"

    return getattr(current_agent, "user_presence_mode", "voice")


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

    inactivity_task: asyncio.Task | None = None
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

    async def user_presence_task():
        # Ping the user twice; if there is still no answer, close the session.
        lang = call_session_data.call_config_data.language

        for _ in range(2):
            if call_session_data.is_warm_transferring or (
                _should_suppress_user_presence_task(session)
            ):
                return
            await session.say(_get_user_presence_prompt(session, lang))
            await asyncio.sleep(10)

        # 대기 중 Warm Transfer 또는 자체 부재 처리 Task로 전환된 경우 종료하지 않는다.
        if call_session_data.is_warm_transferring or (
            _should_suppress_user_presence_task(session)
        ):
            return

        logger.bind(event="USER_PRESENCE_TASK", stage="completed").debug(
            "User Response Timeout"
        )

        session.history.add_message(
            role="developer",
            content="",
            extra=agent_event_extra(
                "CALL_TERMINATION",
                details={"actor": "SYSTEM", "reason": "USER_RESPONSE_DELAY"},
            ),
        )

        speech_handle = session.say(
            t("session.response_delay_exit", lang), allow_interruptions=False
        )
        await speech_handle.wait_for_playout()

        await job_ctx.api.room.delete_room(
            api.DeleteRoomRequest(
                room=job_ctx.room.name,
            )
        )

    def _cancel_inactivity_task() -> None:
        nonlocal inactivity_task

        if inactivity_task is not None:
            inactivity_task.cancel()
            inactivity_task = None

    def _schedule_inactivity_task() -> None:
        nonlocal inactivity_task

        _cancel_inactivity_task()
        inactivity_task = asyncio.create_task(user_presence_task())

    @session.on("user_state_changed")
    def _user_state_changed(ev: UserStateChangedEvent):
        # Warm transfer 진행 중에는 user_presence_task 시작하지 않음
        if call_session_data.is_warm_transferring:
            _cancel_inactivity_task()
            return

        # GreetingDtmfTask는 자체 DTMF 타임아웃/재안내 정책을 사용한다.
        if _should_suppress_user_presence_task(session):
            _cancel_inactivity_task()
            return

        if ev.new_state == "away":
            _schedule_inactivity_task()
            return

        # ev.new_state: listening, speaking, ..
        _cancel_inactivity_task()

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
