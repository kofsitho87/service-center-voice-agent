import asyncio
import contextlib
import inspect
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Literal, Optional
from zoneinfo import ZoneInfo

from livekit import api, rtc
from livekit.agents import (
    function_tool,
    get_job_context,
)
from livekit.agents.voice import Agent
from livekit.agents.voice.background_audio import (
    AudioConfig,
    BackgroundAudioPlayer,
    BuiltinAudioClip,
)
from loguru import logger

from data import (
    BookingMemoContext,
    CallSessionData,
    RunContext_T,
    TransferRecord,
    normalize_transfer_mode,
)
from services import fake_transfer_gateway
from services.redis_client import get_client as _get_redis_client
from services.trunk_allocation_client import (
    TrunkAllocationClient,
    TrunkStatus,
)
from services.warm_transfer_briefing import (
    extract_warm_transfer_conversation_history,
    generate_warm_transfer_briefing_text,
)
from services.warm_transfer_queue import (
    COMPLETED_ENTRY_TTL_SECONDS,
    CallDirection,
    WarmTransferQueue,
    WarmTransferQueueStatus,
)
from tasks.caller_wait_reconfirmation_task import (
    CallerWaitReconfirmationTask,
    ReconfirmationAction,
)
from tasks.leave_message_task import LeaveMessageEntryReason, LeaveMessageTask
from tasks.single_room_warm_transfer_task import (
    SingleRoomWarmTransferTask,
    WarmTransferResult,
    WarmTransferStatus,
)
from tasks.transfer_reason_task import TransferPurpose, TransferReasonTask
from utils.agent_event import agent_event_extra, transfer_queued_extra
from utils.i18n import t
from utils.korean_speech_guard import KoreanSpeechGuardMixin
from utils.langfuse_tags import langfuse_tags

if TYPE_CHECKING:
    from loguru import Logger

# Warm-Transfer FIFO queue configuration
WT_QUEUE_DEADLINE_SECONDS = 600  # 10-minute total wait
WT_QUEUE_POLL_INTERVAL_SECONDS = 30
WT_RETRY_COOLDOWN_SECONDS = 5  # TIMEOUT/ERROR/VOICEMAIL → wait then re-dial
WT_ZOMBIE_MAX_AGE_SECONDS = 660  # matches entry HASH TTL
WT_OCCUPANCY_TTL_REFRESH_INTERVAL_SECONDS = 300  # refresh HASH TTL every 5 minutes
WT_RECONFIRMATION_CHECKPOINTS_SECONDS = (120, 240, 360)
WT_ROOM_DELETE_TIMEOUT_SECONDS = 10  # per delete_room attempt after cleanup failure
WT_ROOM_DELETE_ATTEMPTS = 2
COLD_TRANSFER_MAX_ATTEMPTS = 3
COLD_TRANSFER_RETRY_DELAY_SECONDS = 1
# Booking actions whose staff handoff carries the grounded booking memo context.
BOOKING_MUTATION_ACTION_KEYS = frozenset(
    {"booking_create", "booking_modify", "booking_cancel"}
)
_KST = ZoneInfo("Asia/Seoul")
_trunk_client_singleton: Optional[TrunkAllocationClient] = None
_trunk_client_lock = asyncio.Lock()


def _normalize_transfer_failure_status(failure_reason: Optional[str]) -> str:
    return (failure_reason or "unknown").lower()


def _is_sip_forbidden_error(error: api.TwirpError) -> bool:
    metadata = error.metadata or {}
    return str(metadata.get("sip_status_code")) == "403"


def _queue_final_status(reason: str) -> WarmTransferQueueStatus:
    """Map internal dequeue reasons to the backend-facing final state."""
    if reason in {"conversation_ended", "caller_requested_leave_message"}:
        return WarmTransferQueueStatus.COMPLETED
    if reason in {"disconnect", "disconnect_listener"}:
        return WarmTransferQueueStatus.CALLER_DISCONNECTED
    if reason == "declined":
        return WarmTransferQueueStatus.DECLINED
    if reason == "timeout":
        return WarmTransferQueueStatus.QUEUE_TIMEOUT
    return WarmTransferQueueStatus.ERROR


async def _prepare_queued_warm_transfer_briefing(
    *,
    company_id: int,
    sip_identity: str,
    conversation_history: str,
    customer_name: str | None,
    is_new_customer: bool,
    language_code: str | None,
    transfer_record: TransferRecord,
    on_ready: Callable[[str], Awaitable[None]],
) -> str | None:
    """Generate and publish a queued caller's briefing without blocking FIFO."""
    try:
        briefing_text = await generate_warm_transfer_briefing_text(
            conversation_history=conversation_history,
            customer_name=customer_name,
            is_new_customer=is_new_customer,
            language_code=language_code,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.opt(exception=exc).bind(
            event="WT_BRIEFING_GENERATION_FAILED",
            company_id=company_id,
            sip_identity=sip_identity,
        ).warning("Failed to generate warm-transfer briefing while queued")
        return None

    transfer_record.briefing_text = briefing_text
    try:
        await on_ready(briefing_text)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.opt(exception=exc).bind(
            event="WT_BRIEFING_PUBLISH_FAILED",
            company_id=company_id,
            sip_identity=sip_identity,
        ).warning("Failed to publish queued warm-transfer briefing")

    return briefing_text


def _booking_memo_context_for(
    call_session_data: CallSessionData, action_key: Optional[str]
) -> BookingMemoContext | None:
    """Carry the booking memo context only into booking-mutation handoffs."""
    if action_key in BOOKING_MUTATION_ACTION_KEYS:
        return call_session_data.booking_memo_context
    return None


async def get_warm_transfer_queue() -> WarmTransferQueue:
    return WarmTransferQueue(await _get_redis_client())


async def get_trunk_allocation_client() -> TrunkAllocationClient:
    global _trunk_client_singleton
    if _trunk_client_singleton is None:
        async with _trunk_client_lock:
            if _trunk_client_singleton is None:
                _trunk_client_singleton = await TrunkAllocationClient.from_env_async()
    return _trunk_client_singleton


class BaseAgent(KoreanSpeechGuardMixin, Agent):
    def _record_call_termination(self, actor: str, reason: Optional[str]) -> None:
        """Record why the call ended; post-call analysis reads actor/reason."""
        self.session.history.add_message(
            role="developer",
            content="",
            extra=agent_event_extra(
                "CALL_TERMINATION",
                details={"actor": actor, "reason": reason},
            ),
        )

    async def _say_and_wait(
        self, text: str, *, failure_log: "Logger", failure_message: str
    ) -> None:
        """Play an uninterruptible message; a playout failure is only logged."""
        try:
            speech_handle = self.session.say(text, allow_interruptions=False)
            if speech_handle:
                await speech_handle.wait_for_playout()
        except Exception as e:
            failure_log.opt(exception=e).warning(failure_message)

    async def _monitor_transfer_conversation(
        self,
        caller_identity: str,
        agent_identity: Optional[str],
    ):
        """상담원-콜러 대화 모니터링 및 대화 종료 시 처리

        상담원 또는 콜러가 Room에서 연결 해제되면 대화가 종료된 것으로 간주하고
        필요한 후처리(대화 기록 저장 등)를 수행합니다.
        """
        job_ctx = get_job_context()

        disconnect_event = asyncio.Event()
        disconnected_identity = None

        def on_participant_disconnected(participant: rtc.RemoteParticipant):

            logger.bind(
                event="ON_PARTICIPANT_DISCONNECTED",
                participant_identity=participant.identity,
                metadata=participant.metadata,
                attributes=participant.attributes,
                participant_name=participant.name,
                participant_kind=participant.kind,
                participant_sid=participant.sid,
                agent_identity=agent_identity,
                caller_identity=caller_identity,
                room_name=job_ctx.room.name,
            ).debug("Participant disconnected from transferred call")

            nonlocal disconnected_identity
            # 상담원 또는 콜러가 연결 해제되면 이벤트 발생
            if participant.identity in (caller_identity, agent_identity):
                disconnected_identity = participant.identity
                logger.bind(
                    event="TRANSFER_PARTICIPANT_DISCONNECTED",
                    participant_identity=participant.identity,
                    room_name=job_ctx.room.name,
                ).debug("Participant disconnected from transferred call")
                disconnect_event.set()

        # 이벤트 핸들러 등록
        job_ctx.room.on("participant_disconnected", on_participant_disconnected)

        try:
            # 등록 직전에 이미 퇴장한 참여자는 이벤트를 받을 수 없으므로 즉시 재확인한다.
            # 없으면 그 퇴장을 disconnect 이벤트로 간주해 남은 참여자를 정리하고 큐 슬롯을
            # 해제하게 한다(그렇지 않으면 FIFO front 가 TTL 까지 점유된다).
            if not disconnect_event.is_set():
                remote_participants = job_ctx.room.remote_participants
                for identity in (caller_identity, agent_identity):
                    if identity and identity not in remote_participants:
                        disconnected_identity = identity
                        logger.bind(
                            event="TRANSFER_PARTICIPANT_ALREADY_ABSENT",
                            participant_identity=identity,
                            room_name=job_ctx.room.name,
                        ).info("Participant already absent when monitoring started")
                        disconnect_event.set()
                        break

            # 대화 종료 대기 (상담원 또는 콜러 연결 해제)
            await disconnect_event.wait()

            logger.bind(
                event="TRANSFER_CONVERSATION_ENDED",
                room_name=job_ctx.room.name,
                caller_identity=caller_identity,
                agent_identity=agent_identity,
                disconnected_identity=disconnected_identity,
            ).debug("Transferred conversation ended")

            # 남은 참여자 자동 연결 해제
            remaining_identity = (
                agent_identity
                if disconnected_identity == caller_identity
                else caller_identity
            )

            if remaining_identity:
                try:
                    logger.bind(
                        event="TRANSFER_REMOVE_REMAINING_PARTICIPANT",
                        remaining_identity=remaining_identity,
                        room_name=job_ctx.room.name,
                    ).debug("Removing remaining participant after disconnect")
                    await job_ctx.api.room.remove_participant(
                        api.RoomParticipantIdentity(
                            room=job_ctx.room.name,
                            identity=remaining_identity,
                        )
                    )
                except Exception as e:
                    logger.opt(exception=e).warning(
                        f"Failed to remove remaining participant: {remaining_identity}"
                    )

        finally:
            # 이벤트 핸들러 제거
            job_ctx.room.off("participant_disconnected", on_participant_disconnected)

            # AI도 Room에서 퇴장
            try:
                logger.debug("Removing AI participant from transferred call room")
                await job_ctx.api.room.remove_participant(
                    api.RoomParticipantIdentity(
                        room=job_ctx.room.name,
                        identity=job_ctx.room.local_participant.identity,
                    )
                )
            except Exception as e:
                logger.opt(exception=e).warning("Failed to remove AI from room")

    async def hangup(self):
        """Helper function to hang up the call by shutting down the session"""

        call_session_data: CallSessionData = self.session.userdata

        logger.bind(
            event="HANGUP",
            phone=call_session_data.recipient_data.phone,
            room_name=call_session_data.room_name,
        ).info("Hanging up the call")

        self._record_call_termination("SYSTEM", "HANGUP")

        self.session.shutdown(drain=True)

    @langfuse_tags("leave_message")
    async def _handle_leave_message(
        self,
        call_session_data: CallSessionData,
        *,
        is_after_transfer_failure: bool,
        action_key: Optional[str] = None,
        booking_context: BookingMemoContext | None = None,
        transfer_purpose: TransferPurpose | None = None,
        entry_reason: LeaveMessageEntryReason | None = None,
    ) -> None:
        """
        Leave a message for the caller.

        Contract:
        - Purpose: Leave a message for the caller.
        - Policy: Follow system prompt — confirm for normal cases; allow immediate leave for emergencies.
        """

        self.session.input.set_audio_enabled(False)

        logger.bind(
            event="LEAVE_MESSAGE",
            room_name=call_session_data.room_name,
            phone=call_session_data.recipient_data.phone,
            is_after_transfer_failure=is_after_transfer_failure,
            entry_reason=entry_reason,
            action_key=action_key,
        ).debug("Leave message task started")

        participant_phone = call_session_data.recipient_data.phone
        is_business_hours = call_session_data.business_data.is_work_time
        collect_desired_datetime = (
            call_session_data.leave_message_flow_config.should_collect_desired_datetime(
                action_key
            )
        )

        if (
            action_key in {"booking_modify", "booking_cancel"}
            and booking_context
            and booking_context.exit_reason_code == "no_reservations"
        ):
            # Without a registered appointment, collect what staff should verify
            # without asking for an appointment date.
            collect_desired_datetime = False

        self.session.history.add_message(
            role="developer",
            content="",
            extra=agent_event_extra(
                "LEAVE_MESSAGE",
                status="STARTED",
                details={
                    "business_hours": is_business_hours,
                    "after_transfer_failure": is_after_transfer_failure,
                    "action_key": action_key,
                    "collect_desired_datetime": collect_desired_datetime,
                },
            ),
        )

        # 기존 범용 메시지 수집
        result = await LeaveMessageTask(
            chat_ctx=self.chat_ctx.copy(
                exclude_empty_message=True,
                exclude_function_call=True,
                exclude_instructions=True,
                exclude_handoff=True,
                exclude_config_update=True,
                # Match booking tools for continuity; memo facts still come only from
                # structured context, never inference from this history.
            ).truncate(max_items=6),
            is_after_transfer_failure=is_after_transfer_failure,
            action_key=action_key,
            collect_desired_datetime=collect_desired_datetime,
            language=call_session_data.call_config_data.language,
            additional_instructions=(
                call_session_data.leave_message_flow_config.additional_instructions
            ),
            booking_context=booking_context,
            transfer_purpose=transfer_purpose,
            entry_reason=entry_reason,
        )

        self.session.input.set_audio_enabled(False)

        logger.bind(
            event="LEAVE_MESSAGE",
            scenario=result.scenario,
            room_name=call_session_data.room_name,
            phone=participant_phone,
        ).info("Leave message task completed")

        is_business_hours = call_session_data.business_data.is_work_time

        self.session.history.add_message(
            role="developer",
            content="",
            extra=agent_event_extra(
                "LEAVE_MESSAGE",
                status="SUCCESS",
                details={
                    "scenario": result.scenario,
                    "business_hours": is_business_hours,
                    "after_transfer_failure": is_after_transfer_failure,
                    "action_key": action_key,
                    "desired_datetime": result.desired_datetime,
                    "desired_condition": result.desired_condition,
                    "message": result.message,
                    "booking_context": (
                        {
                            "service_name": booking_context.service_name,
                            "operator_name": booking_context.operator_name,
                            "time_condition": booking_context.time_condition,
                            "searched_ranges": booking_context.searched_ranges,
                            "exit_reason_code": booking_context.exit_reason_code,
                        }
                        if booking_context
                        else None
                    ),
                },
            ),
        )

        # LeaveMessageTask가 종료 문구의 playout 완료 후 반환한다.
        current_speech = self.session.current_speech
        if current_speech:
            try:
                await current_speech.wait_for_playout()
            except Exception as e:
                logger.bind(
                    event="LEAVE_MESSAGE",
                    stage="wait_playout_error",
                    room_name=call_session_data.room_name,
                ).opt(exception=e).error(
                    "Error waiting for playout before system hangup"
                )

        await self.hangup()
        return None

    @staticmethod
    def _resolve_transfer_meta(transfer_type: str) -> tuple[str, str]:
        """transfer_type 문자열에서 (type, action_type) 파생.

        transfer_type 값:
        - "WARM"        → ("warm_transfer", "transfer")
        - "DIRECT_WARM" → ("warm_transfer", "transfer_direct")
        - "COLD"        → ("cold_transfer", "transfer")
        - "DIRECT_COLD" → ("cold_transfer", "transfer_direct")
        """
        record_type = "warm_transfer" if "WARM" in transfer_type else "cold_transfer"
        record_action_type = (
            "transfer_direct" if "DIRECT" in transfer_type else "transfer"
        )
        return record_type, record_action_type

    @classmethod
    def _new_transfer_record(
        cls, transfer_type: str, transfer_to: str, transfer_status: str
    ) -> TransferRecord:
        record_type, record_action_type = cls._resolve_transfer_meta(transfer_type)
        return TransferRecord(
            type=record_type,
            action_type=record_action_type,
            transfer_requested_at=datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S"),
            transfer_target_phone=transfer_to,
            transfer_status=transfer_status,
        )

    @langfuse_tags("cold_transfer")
    async def _handle_cold_transfer_call(
        self,
        call_session_data: CallSessionData,
        transfer_to: str,
        transfer_type: Literal["COLD", "DIRECT_COLD"] = "COLD",
    ) -> None:
        """
        Handle cold transfer call.
        """

        # ! 오디오 입력 비활성화
        self.session.input.set_audio_enabled(False)

        job_ctx = get_job_context()

        participant_phone = call_session_data.recipient_data.phone

        logger.bind(
            event=f"{transfer_type}_TRANSFER_CALL",
            room_name=call_session_data.room_name,
            phone=participant_phone,
            participant_identity=call_session_data.recipient_data.sip_identity,
            transfer_to=transfer_to,
        ).info("Cold transfer call started")

        if call_session_data.call_config_data.transfer_message:
            logger.debug(
                f"Playing previous message before {transfer_type.lower()} transfer: {call_session_data.call_config_data.transfer_message[:50]}..."
            )
            await self._say_and_wait(
                call_session_data.call_config_data.transfer_message,
                failure_log=logger.bind(
                    event=f"{transfer_type}_TRANSFER_CALL",
                    message=call_session_data.call_config_data.transfer_message,
                ),
                failure_message="Error waiting for playout",
            )

        # 전환 시작을 대화 이력에도 남긴다.
        #
        # 위 logger.bind 는 stdout 으로만 나가고 통화 후 분석은 history 만 본다.
        # 그래서 cold 는 SUCCESS 이벤트에서만 시도가 열렸고, REFER 자체가 실패한
        # 통화는 시도조차 만들어지지 않아 분모에 들어오지 못했다. 분자와 분모가
        # 같은 집합이 되어 cold 완수율이 99.9% 로 나오던 원인이다.
        #
        # 방출 시점은 warm(_handle_warm_transfer_call)과 맞춘다. 계열마다
        # STARTED 의 뜻이 달라지면 두 지표를 나란히 놓을 수 없다.
        self.session.history.add_message(
            role="developer",
            content=(
                f"[{transfer_type}_TRANSFER_CALL: STATUS=STARTED, TARGET={transfer_to}]"
            ),
            extra=agent_event_extra(
                f"{transfer_type}_TRANSFER_CALL",
                status="STARTED",
                details={"target": transfer_to},
            ),
        )

        # TransferRecord 초기화
        call_session_data.transfer_record = self._new_transfer_record(
            transfer_type, transfer_to, "requested"
        )

        try:
            for attempt in range(1, COLD_TRANSFER_MAX_ATTEMPTS + 1):
                try:
                    # Simulated gateway, never a real SIP REFER.
                    await fake_transfer_gateway.transfer_sip_participant(
                        job_ctx,
                        api.TransferSIPParticipantRequest(
                            room_name=job_ctx.room.name,
                            participant_identity=call_session_data.recipient_data.sip_identity,
                            transfer_to=f"sip:{transfer_to}@transfer.invalid",
                            play_dialtone=True,
                        )
                    )
                    break
                except api.TwirpError as e:
                    should_retry = (
                        _is_sip_forbidden_error(e)
                        and attempt < COLD_TRANSFER_MAX_ATTEMPTS
                    )
                    if not should_retry:
                        raise

                    logger.bind(
                        event=f"{transfer_type}_TRANSFER_CALL",
                        stage="transfer_retry",
                        room_name=call_session_data.room_name,
                        phone=participant_phone,
                        attempt=attempt,
                        max_attempts=COLD_TRANSFER_MAX_ATTEMPTS,
                        retry_delay_seconds=COLD_TRANSFER_RETRY_DELAY_SECONDS,
                        twirp_code=e.code,
                        twirp_message=e.message,
                        twirp_status=e.status,
                        sip_status_code=(e.metadata or {}).get("sip_status_code"),
                        sip_status=(e.metadata or {}).get("sip_status"),
                    ).warning("Cold transfer rejected with SIP 403; retrying")
                    await asyncio.sleep(COLD_TRANSFER_RETRY_DELAY_SECONDS)

            self.session.history.add_message(
                role="developer",
                content="",
                extra=agent_event_extra(
                    f"{transfer_type}_TRANSFER_CALL",
                    status="SUCCESS",
                    details={
                        "target": transfer_to,
                        "business_hours": call_session_data.business_data.is_work_time,
                    },
                ),
            )
        except Exception as e:
            call_session_data.transfer_record.transfer_status = "error"
            self.session.history.add_message(
                role="developer",
                content="",
                extra=agent_event_extra(
                    f"{transfer_type}_TRANSFER_CALL",
                    status="ERROR",
                    details={"reason": str(e)},
                ),
            )
            twirp_fields = (
                {
                    "twirp_code": e.code,
                    "twirp_message": e.message,
                    "twirp_status": e.status,
                }
                if isinstance(e, api.TwirpError)
                else {}
            )
            logger.bind(
                event=f"{transfer_type}_TRANSFER_CALL",
                stage="transfer_error",
                room_name=call_session_data.room_name,
                phone=participant_phone,
                **twirp_fields,
            ).opt(exception=e).error("Error transferring SIP participant")

            speech_handle = await self.session.generate_reply(
                instructions=(
                    "Inform that an error occurred during the representative connection process. "
                    "Apologize and inform that you will end the call."
                )
            )

            if speech_handle:
                try:
                    await speech_handle.wait_for_playout()
                except Exception as playout_error:
                    logger.opt(exception=playout_error).warning(
                        "Error waiting for playout after transfer failure"
                    )

        return None

    @langfuse_tags("warm_transfer")
    async def _handle_warm_transfer_call(
        self,
        call_session_data: CallSessionData,
        transfer_to: str,
        transfer_type: Literal["WARM", "DIRECT_WARM"] = "WARM",
        action_key: Optional[str] = None,
        transfer_purpose: TransferPurpose | None = None,
        briefing_enabled: bool = True,
    ) -> None:
        """
        Handle warm transfer call with retry logic.
        Max retries: 3 (for TIMEOUT, ERROR, VOICEMAIL)
        DECLINED: No retry, immediate hangup

        briefing_enabled=False (warm_transfer_no_briefing) keeps the queue and
        single-room bridge but skips queue-time briefing generation; the task
        bridges the caller as soon as the agent answers.
        """

        company_id = call_session_data.business_data.client_id
        if company_id is None:
            logger.bind(
                event="WT_QUEUE_SKIPPED_NO_COMPANY",
                room_name=call_session_data.room_name,
            ).warning("client_id missing — falling back to leave_message")
            await self._handle_leave_message(
                call_session_data,
                is_after_transfer_failure=True,
                entry_reason="preflight_failure",
                action_key=action_key,
                transfer_purpose=transfer_purpose,
                booking_context=_booking_memo_context_for(
                    call_session_data, action_key
                ),
            )
            return None

        # ! 오디오 입력 비활성화
        self.session.input.set_audio_enabled(False)

        participant_phone = call_session_data.recipient_data.phone
        sip_identity = call_session_data.recipient_data.sip_identity

        # Warm transfer 진행 중 플래그 설정 (user_presence_task 중지)
        call_session_data.is_warm_transferring = True

        if call_session_data.call_config_data.transfer_message:
            logger.debug(
                f"Playing previous message before warm transfer: {call_session_data.call_config_data.transfer_message[:50]}..."
            )

            speech_handle = self.session.say(
                call_session_data.call_config_data.transfer_message,
                allow_interruptions=False,
            )
            try:
                await speech_handle.wait_for_playout()
            except Exception as e:
                logger.bind(
                    event="WARM_TRANSFER_CALL",
                    message=call_session_data.call_config_data.transfer_message,
                ).opt(exception=e).warning("Error waiting for playout")

        logger.bind(
            event=f"{transfer_type}_TRANSFER_START",
            target_phone=transfer_to,
            caller_identity=participant_phone,
            room_name=call_session_data.room_name,
            sip_identity=sip_identity,
            briefing_enabled=briefing_enabled,
        ).info("Warm transfer started")

        self.session.history.add_message(
            role="developer",
            content=f"[WARM_TRANSFER_CALL: STATUS=STARTED, TARGET={transfer_to}]",
            extra=agent_event_extra(
                f"{transfer_type}_TRANSFER_CALL",
                status="STARTED",
                details={"target": transfer_to, "briefing": briefing_enabled},
            ),
        )

        transfer_start_time = time.monotonic()  # 전체 대기시간 측정 시작

        # Transfer 결과 기록
        # Fail safe: treat shutdown before finalization as caller disconnect.
        call_session_data.transfer_record = self._new_transfer_record(
            transfer_type,
            transfer_to,
            WarmTransferStatus.CALLER_DISCONNECTED.value,
        )

        # ! 발화 완료(정상 or 예외) 후 참여자 연결 상태 확인
        # 발화 완료(정상 or 예외) 후 참여자 연결 상태 확인
        # wait_for_playout()은 참여자가 끊겨도 예외 없이 반환될 수 있음
        job_ctx = get_job_context()
        if sip_identity not in job_ctx.room.remote_participants:
            logger.bind(
                event="WARM_TRANSFER_CALLER_DISCONNECTED",
                room_name=call_session_data.room_name,
                sip_identity=sip_identity,
            ).info("Caller already disconnected before warm transfer, returning early")
            call_session_data.is_warm_transferring = False
            call_session_data.transfer_record.transfer_status = (
                WarmTransferStatus.CALLER_DISCONNECTED.value
            )
            call_session_data.transfer_record.total_wait_seconds = round(
                time.monotonic() - transfer_start_time, 1
            )
            return None

        # Pre-flight: warm-transfer infra init. On failure (Redis config /
        # env var missing / network) skip queuing entirely and fall back to
        # leave_message rather than propagating the exception.
        try:
            queue = await get_warm_transfer_queue()
            trunk_client = await get_trunk_allocation_client()
        except Exception as exc:
            logger.opt(exception=exc).bind(
                event="WT_QUEUE_REDIS_UNAVAILABLE",
                company_id=company_id,
                room_name=call_session_data.room_name,
            ).error("Warm-transfer infra init failed; falling back to leave_message")
            call_session_data.is_warm_transferring = False
            call_session_data.transfer_record.transfer_status = "infra_unavailable"
            call_session_data.transfer_record.total_wait_seconds = round(
                time.monotonic() - transfer_start_time, 1
            )
            await self._handle_leave_message(
                call_session_data,
                is_after_transfer_failure=True,
                entry_reason="preflight_failure",
                action_key=action_key,
                transfer_purpose=transfer_purpose,
                booking_context=_booking_memo_context_for(
                    call_session_data, action_key
                ),
            )
            return None

        hold_music_player: Optional[BackgroundAudioPlayer] = None
        hold_music_handle = None
        warm_transfer_result: Optional[WarmTransferResult] = None
        failure_reason: Optional[str] = None
        queue_dequeue_reason = "unknown"
        release_after_monitor = False
        refresh_task: Optional[asyncio.Task] = None
        briefing_generation_task: asyncio.Task[str | None] | None = None
        refresh_started = False
        last_warm_transfer_task = None
        occupancy_lock_lost = False
        disconnect_flag = {"left": False}
        dequeue_state = {"done": False}
        dequeue_lock = asyncio.Lock()
        background_cleanup_tasks: set[asyncio.Task] = set()
        queue_wait_started_at: Optional[float] = None
        completed_reconfirmation_indexes: set[int] = set()
        caller_requested_leave_message = False
        terminate_after_reconfirmation = False
        terminate_after_cleanup_failure = False
        hold_queue_slot_after_cleanup_failure = False
        # While a dial is in flight the disconnect listener only flags the
        # caller; the slot is released after the dial (and room deletion) ends.
        dial_state = {"in_flight": False}
        queue_enqueued = False

        async def _safe_dequeue(reason: str) -> bool:
            nonlocal queue_dequeue_reason

            async with dequeue_lock:
                if dequeue_state["done"] or not queue_enqueued:
                    return False

                await queue.dequeue(
                    company_id,
                    sip_identity,
                    reason=reason,
                    final_status=_queue_final_status(reason),
                    retain_metadata_seconds=COMPLETED_ENTRY_TTL_SECONDS,
                )
                dequeue_state["done"] = True
                queue_dequeue_reason = reason

                logger.bind(
                    event="WT_QUEUE_DEQUEUED",
                    company_id=company_id,
                    sip_identity=sip_identity,
                    reason=reason,
                ).debug("Dequeued from warm-transfer queue")
                return True

        def _spawn_dequeue_cleanup(reason: str) -> asyncio.Task:
            async def _runner():
                try:
                    await _safe_dequeue(reason)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.opt(exception=exc).warning(
                        "Failed background dequeue cleanup"
                    )

            task = asyncio.create_task(_runner())
            background_cleanup_tasks.add(task)
            task.add_done_callback(background_cleanup_tasks.discard)
            return task

        async def _release_queue(reason: str) -> None:
            try:
                await asyncio.shield(_safe_dequeue(reason))
            except asyncio.CancelledError:
                if not dequeue_state["done"]:
                    _spawn_dequeue_cleanup(
                        reason if reason != "unknown" else "finally_cancelled"
                    )
                raise
            except Exception as exc:
                logger.opt(exception=exc).warning("Failed to dequeue on exit")

        async def _delete_room_after_cleanup_failure() -> bool:
            """Delete the room so a stale agent leg is disconnected. True if done."""
            for attempt in range(1, WT_ROOM_DELETE_ATTEMPTS + 1):
                try:
                    await asyncio.wait_for(
                        job_ctx.api.room.delete_room(
                            api.DeleteRoomRequest(room=job_ctx.room.name)
                        ),
                        timeout=WT_ROOM_DELETE_TIMEOUT_SECONDS,
                    )
                except Exception as exc:
                    if attempt < WT_ROOM_DELETE_ATTEMPTS:
                        logger.opt(exception=exc).warning(
                            "delete_room failed after agent cleanup failure; retrying"
                        )
                        continue
                    logger.opt(exception=exc).bind(
                        event="WT_AGENT_CLEANUP_ROOM_DELETE_FAILED",
                        company_id=company_id,
                        sip_identity=sip_identity,
                        room_name=job_ctx.room.name,
                        attempts=attempt,
                    ).error("Room could not be deleted after agent cleanup failure")
                    return False
                logger.bind(
                    event="WT_AGENT_CLEANUP_ROOM_DELETED",
                    company_id=company_id,
                    sip_identity=sip_identity,
                    room_name=job_ctx.room.name,
                    attempt=attempt,
                ).warning("Room deleted after agent cleanup failure")
                return True
            return False

        async def _update_realtime_entry(
            event: str,
            *,
            status: WarmTransferQueueStatus | str | None = None,
            fields: Optional[dict[str, object]] = None,
            increment_attempt: bool = False,
        ) -> None:
            """Publish dashboard state without disrupting the call on failure."""
            try:
                updated = await queue.update_entry(
                    company_id,
                    sip_identity,
                    status=status,
                    event=event,
                    fields=fields,
                    increment_attempt=increment_attempt,
                )
                if not updated:
                    logger.bind(
                        event="WT_REALTIME_ENTRY_MISSING",
                        company_id=company_id,
                        sip_identity=sip_identity,
                        realtime_event=event,
                    ).warning("Warm-transfer realtime entry no longer exists")
            except Exception as exc:
                logger.opt(exception=exc).bind(
                    event="WT_REALTIME_UPDATE_FAILED",
                    company_id=company_id,
                    sip_identity=sip_identity,
                    realtime_event=event,
                ).warning("Failed to update warm-transfer realtime state")

        async def _task_realtime_update(event: str, fields: Mapping[str, str]) -> None:
            update_fields: dict[str, object] = dict(fields)
            status = update_fields.pop("status", None)
            await _update_realtime_entry(
                event,
                status=status,
                fields=update_fields,
            )

        try:
            hold_music_player = BackgroundAudioPlayer()
            await hold_music_player.start(room=get_job_context().room)
            hold_music_handle = hold_music_player.play(
                AudioConfig(BuiltinAudioClip.HOLD_MUSIC, volume=0.8),
                loop=True,
            )

            enqueue_ts = await queue.enqueue(
                company_id=company_id,
                sip_identity=sip_identity,
                room_name=call_session_data.room_name,
                call_direction=CallDirection.INBOUND,
                metadata={
                    "phone": call_session_data.recipient_data.phone,
                    "user_name": call_session_data.recipient_data.user_name,
                    "verification_code": call_session_data.recipient_data.verification_code,
                    "customer_no": call_session_data.recipient_data.customer_no,
                    "customer_visit": (call_session_data.recipient_data.customer_visit),
                    "transfer_target_phone": transfer_to,
                    "briefing_text": "",
                },
            )
            queue_enqueued = True
            queue_wait_started_at = time.monotonic()

            customer_name = call_session_data.recipient_data.user_name
            is_new_customer = (
                call_session_data.recipient_data.customer_visit != "REVISIT"
            )
            # No-briefing mode: skip history extraction and LLM briefing so the
            # caller is bridged as soon as the agent answers.
            if briefing_enabled:
                conversation_history = extract_warm_transfer_conversation_history(
                    self.chat_ctx
                )
                if transfer_purpose and (
                    purpose_text := transfer_purpose.describe(
                        call_session_data.call_config_data.language
                    )
                ):
                    conversation_history = (
                        f"Confirmed transfer purpose: {purpose_text}\n"
                        f"{conversation_history}"
                    )

                async def _publish_queued_briefing(briefing_text: str) -> None:
                    await _update_realtime_entry(
                        "briefing_ready",
                        fields={"briefing_text": briefing_text},
                    )

                briefing_generation_task = asyncio.create_task(
                    _prepare_queued_warm_transfer_briefing(
                        company_id=company_id,
                        sip_identity=sip_identity,
                        conversation_history=conversation_history,
                        customer_name=customer_name,
                        is_new_customer=is_new_customer,
                        language_code=call_session_data.call_config_data.language,
                        transfer_record=call_session_data.transfer_record,
                        on_ready=_publish_queued_briefing,
                    )
                )
            try:
                queue_length = await queue.get_queue_length(company_id)
            except Exception:
                queue_length = None
            logger.bind(
                event="WT_QUEUE_ENQUEUED",
                company_id=company_id,
                sip_identity=sip_identity,
                room_name=call_session_data.room_name,
                queue_length=queue_length,
            ).debug("Enqueued for warm transfer")
            self.session.history.add_message(
                role="developer",
                content="",
                extra=transfer_queued_extra(
                    transfer_type=transfer_type,
                    queue_length=queue_length,
                ),
            )

            async def _refresh_once() -> bool:
                refreshed = await queue.refresh_entry_ttl(company_id, sip_identity)
                if not refreshed:
                    logger.bind(
                        event="WT_OCCUPANCY_LOCK_LOST",
                        company_id=company_id,
                        sip_identity=sip_identity,
                    ).error("Warm-transfer occupancy lock metadata disappeared")
                return refreshed

            async def _refresh_entry_ttl() -> None:
                nonlocal occupancy_lock_lost

                while True:
                    await asyncio.sleep(WT_OCCUPANCY_TTL_REFRESH_INTERVAL_SECONDS)
                    try:
                        refreshed = await _refresh_once()
                    except Exception as exc:
                        logger.opt(exception=exc).bind(
                            event="WT_OCCUPANCY_TTL_REFRESH_FAILED",
                            company_id=company_id,
                            sip_identity=sip_identity,
                        ).warning(
                            "Failed to refresh occupancy HASH TTL; will retry next interval"
                        )
                        continue

                    if not refreshed:
                        occupancy_lock_lost = True
                        return

            def _on_caller_disconnect(participant):
                if participant.identity == sip_identity:
                    disconnect_flag["left"] = True
                    if not dial_state["in_flight"]:
                        _spawn_dequeue_cleanup("disconnect_listener")

            def _get_due_reconfirmation_index() -> Optional[int]:
                if queue_wait_started_at is None:
                    return None

                elapsed = time.monotonic() - queue_wait_started_at
                for idx, checkpoint_seconds in enumerate(
                    WT_RECONFIRMATION_CHECKPOINTS_SECONDS
                ):
                    if idx in completed_reconfirmation_indexes:
                        continue
                    if elapsed >= checkpoint_seconds:
                        return idx
                return None

            async def _run_due_reconfirmation() -> Optional[ReconfirmationAction]:
                nonlocal hold_music_handle

                due_index = _get_due_reconfirmation_index()
                if due_index is None:
                    return None

                checkpoint_seconds = WT_RECONFIRMATION_CHECKPOINTS_SECONDS[due_index]
                logger.bind(
                    event="WT_RECONFIRMATION_STARTED",
                    company_id=company_id,
                    sip_identity=sip_identity,
                    checkpoint_seconds=checkpoint_seconds,
                ).debug("Starting wait reconfirmation")

                if hold_music_handle is not None:
                    hold_music_handle.stop()
                    hold_music_handle = None

                _reconfirmation_task = CallerWaitReconfirmationTask(
                    caller_identity=sip_identity,
                    language_code=call_session_data.call_config_data.language,
                )
                result = (
                    await _reconfirmation_task
                    if inspect.isawaitable(_reconfirmation_task)
                    else _reconfirmation_task
                )
                completed_reconfirmation_indexes.add(due_index)

                logger.bind(
                    event="WT_RECONFIRMATION_RESULT",
                    company_id=company_id,
                    sip_identity=sip_identity,
                    checkpoint_seconds=checkpoint_seconds,
                    action=result.action.value,
                    attempt_count=result.attempt_count,
                ).debug("Completed wait reconfirmation")

                if result.action is ReconfirmationAction.CONTINUE_WAITING:
                    if hold_music_player is not None:
                        hold_music_handle = hold_music_player.play(
                            AudioConfig(BuiltinAudioClip.HOLD_MUSIC, volume=0.8),
                            loop=True,
                        )
                    logger.bind(
                        event="WT_RECONFIRMATION_CONTINUE",
                        company_id=company_id,
                        sip_identity=sip_identity,
                        checkpoint_seconds=checkpoint_seconds,
                        attempt_count=result.attempt_count,
                    ).info("Caller chose to keep waiting")
                elif result.action is ReconfirmationAction.EXIT_TO_LEAVE_MESSAGE:
                    logger.bind(
                        event="WT_RECONFIRMATION_EXIT_TO_LEAVE_MESSAGE",
                        company_id=company_id,
                        sip_identity=sip_identity,
                        checkpoint_seconds=checkpoint_seconds,
                        attempt_count=result.attempt_count,
                    ).info("Caller chose to stop waiting and leave a message")
                elif result.action is ReconfirmationAction.TERMINATE_CALL:
                    logger.bind(
                        event="WT_RECONFIRMATION_MAX_ATTEMPTS",
                        company_id=company_id,
                        sip_identity=sip_identity,
                        checkpoint_seconds=checkpoint_seconds,
                        attempt_count=result.attempt_count,
                    ).info("Caller did not complete reconfirmation within max attempts")
                elif result.action is ReconfirmationAction.CALLER_DISCONNECTED:
                    logger.bind(
                        event="WT_RECONFIRMATION_CALLER_DISCONNECTED",
                        company_id=company_id,
                        sip_identity=sip_identity,
                        checkpoint_seconds=checkpoint_seconds,
                    ).info("Caller disconnected during wait reconfirmation")

                return result.action

            job_ctx.room.on("participant_disconnected", _on_caller_disconnect)

            deadline = time.monotonic() + WT_QUEUE_DEADLINE_SECONDS
            my_turn_logged = False

            try:
                while time.monotonic() < deadline and not disconnect_flag["left"]:
                    reconfirmation_action = await _run_due_reconfirmation()
                    if (
                        reconfirmation_action
                        is ReconfirmationAction.EXIT_TO_LEAVE_MESSAGE
                    ):
                        caller_requested_leave_message = True
                        failure_reason = "caller_requested_leave_message"
                        queue_dequeue_reason = "caller_requested_leave_message"
                        break
                    if reconfirmation_action is ReconfirmationAction.TERMINATE_CALL:
                        terminate_after_reconfirmation = True
                        failure_reason = "wt_reconfirmation_max_attempts"
                        queue_dequeue_reason = "reconfirmation_terminated"
                        break
                    if (
                        reconfirmation_action
                        is ReconfirmationAction.CALLER_DISCONNECTED
                    ):
                        disconnect_flag["left"] = True
                        queue_dequeue_reason = "disconnect"
                        break

                    rank = await queue.get_rank(company_id, sip_identity)
                    if rank is None:
                        # External dequeue — re-enqueue would reset FIFO; bail to leave_message
                        failure_reason = "queue_missing"
                        queue_dequeue_reason = "missing"
                        break

                    logger.bind(
                        event="WT_QUEUE_RANK_CHECK",
                        company_id=company_id,
                        sip_identity=sip_identity,
                        rank=rank,
                    ).debug("Queue rank checked")

                    if rank == 0 and not refresh_started:
                        refresh_started = True
                        await _update_realtime_entry(
                            "reached_front",
                            status=WarmTransferQueueStatus.WAITING_FOR_TRUNK,
                            fields={"front_reached_at": time.time()},
                        )
                        logger.bind(
                            event="WT_OCCUPANCY_STARTED",
                            company_id=company_id,
                            sip_identity=sip_identity,
                        ).debug("Warm-transfer occupancy lock acquired at rank 0")
                        try:
                            refreshed = await _refresh_once()
                        except Exception as exc:
                            logger.opt(exception=exc).bind(
                                event="WT_OCCUPANCY_TTL_REFRESH_FAILED",
                                company_id=company_id,
                                sip_identity=sip_identity,
                            ).warning(
                                "Failed to refresh occupancy HASH TTL; will retry next interval"
                            )
                            refresh_task = asyncio.create_task(_refresh_entry_ttl())
                        else:
                            if not refreshed:
                                occupancy_lock_lost = True
                            else:
                                refresh_task = asyncio.create_task(_refresh_entry_ttl())

                    if rank > 0:
                        # Opportunistic zombie sweep on front entry
                        await queue.cleanup_stale_front(
                            company_id, max_age_seconds=WT_ZOMBIE_MAX_AGE_SECONDS
                        )
                        await asyncio.sleep(WT_QUEUE_POLL_INTERVAL_SECONDS)
                        continue

                    if not my_turn_logged:
                        logger.bind(
                            event="WT_QUEUE_MY_TURN",
                            company_id=company_id,
                            sip_identity=sip_identity,
                            waited_seconds=round(time.time() - enqueue_ts, 1),
                        ).debug("Reached rank 0")
                        my_turn_logged = True

                    trunk_result = await trunk_client.check(company_id)
                    logger.bind(
                        event="WT_TRUNK_API_CALL",
                        company_id=company_id,
                        status_code=trunk_result.status_code,
                        elapsed_ms=trunk_result.elapsed_ms,
                    ).debug("Trunk API check")

                    if trunk_result.status is TrunkStatus.AVAILABLE:
                        logger.bind(
                            event="WT_DIAL_ATTEMPT",
                            company_id=company_id,
                            sip_identity=sip_identity,
                        ).debug("Dialing agent")
                        await _update_realtime_entry(
                            "dial_started",
                            status=WarmTransferQueueStatus.DIALING,
                            fields={
                                "dial_started_at": time.time(),
                                "last_status": "",
                            },
                            increment_attempt=True,
                        )

                        _wt_task = SingleRoomWarmTransferTask(
                            target_phone_number=transfer_to,
                            sip_trunk_id=call_session_data.call_config_data.sip_outbound_trunk_id,
                            caller_identity=sip_identity,
                            chat_ctx=self.chat_ctx if briefing_enabled else None,
                            customer_name=customer_name,
                            is_new_customer=is_new_customer,
                            transfer_record=call_session_data.transfer_record,
                            transfer_start_time=transfer_start_time,
                            external_hold_music_player=hold_music_player,
                            restore_caller_ai_audio_on_failure=False,
                            language_code=call_session_data.call_config_data.language,
                            realtime_update_callback=_task_realtime_update,
                            queued_briefing_future=briefing_generation_task,
                            briefing_enabled=briefing_enabled,
                        )
                        if hasattr(_wt_task, "_restore_caller_ai_audio"):
                            last_warm_transfer_task = _wt_task
                        dial_state["in_flight"] = True
                        warm_transfer_result = (
                            await _wt_task
                            if inspect.isawaitable(_wt_task)
                            else _wt_task
                        )

                        if warm_transfer_result.terminate_call:
                            # Agent leg could not be removed: never re-dial.
                            # Delete the room (disconnecting the stale agent
                            # leg) before the queue slot is released.
                            failure_reason = "agent_cleanup_failed"
                            queue_dequeue_reason = "error"
                            terminate_after_cleanup_failure = True
                            if not await _delete_room_after_cleanup_failure():
                                hold_queue_slot_after_cleanup_failure = True
                            break
                        dial_state["in_flight"] = False
                        if warm_transfer_result.status == WarmTransferStatus.SUCCESS:
                            await _update_realtime_entry(
                                "connected",
                                status=WarmTransferQueueStatus.CONNECTED,
                                fields={
                                    "connected_at": time.time(),
                                    "last_status": (WarmTransferStatus.SUCCESS.value),
                                    "briefing_text": (
                                        warm_transfer_result.briefing_text or ""
                                    ),
                                },
                            )
                            release_after_monitor = True
                            queue_dequeue_reason = "success"  # sentinel only
                            break
                        if warm_transfer_result.status == WarmTransferStatus.DECLINED:
                            await _update_realtime_entry(
                                "transfer_declined",
                                status=WarmTransferQueueStatus.DECLINED,
                                fields={
                                    "last_status": (WarmTransferStatus.DECLINED.value)
                                },
                            )
                            failure_reason = "agent_declined"
                            queue_dequeue_reason = "declined"
                            break
                        if (
                            warm_transfer_result.status
                            == WarmTransferStatus.CALLER_DISCONNECTED
                        ):
                            queue_dequeue_reason = "disconnect"
                            break
                        # TIMEOUT / ERROR / VOICEMAIL — keep front slot, cooldown, re-dial
                        await _update_realtime_entry(
                            "dial_failed",
                            status=WarmTransferQueueStatus.RETRYING,
                            fields={
                                "last_status": warm_transfer_result.status.value,
                                "retry_started_at": time.time(),
                            },
                        )
                        logger.bind(
                            event="WT_DIAL_RETRIABLE",
                            status=warm_transfer_result.status.value,
                            company_id=company_id,
                        ).debug("Retriable dial failure, keeping front slot")

                        reconfirmation_action = await _run_due_reconfirmation()
                        if (
                            reconfirmation_action
                            is ReconfirmationAction.EXIT_TO_LEAVE_MESSAGE
                        ):
                            caller_requested_leave_message = True
                            failure_reason = "caller_requested_leave_message"
                            queue_dequeue_reason = "caller_requested_leave_message"
                            break
                        if reconfirmation_action is ReconfirmationAction.TERMINATE_CALL:
                            terminate_after_reconfirmation = True
                            failure_reason = "wt_reconfirmation_max_attempts"
                            queue_dequeue_reason = "reconfirmation_terminated"
                            break
                        if (
                            reconfirmation_action
                            is ReconfirmationAction.CALLER_DISCONNECTED
                        ):
                            disconnect_flag["left"] = True
                            queue_dequeue_reason = "disconnect"
                            break

                        await asyncio.sleep(WT_RETRY_COOLDOWN_SECONDS)
                        continue

                    if trunk_result.status is TrunkStatus.BUSY:
                        await _update_realtime_entry(
                            "trunk_busy",
                            status=WarmTransferQueueStatus.WAITING_FOR_TRUNK,
                            fields={"last_status": TrunkStatus.BUSY.value},
                        )
                        logger.bind(
                            event="WT_TRUNK_API_BUSY",
                            company_id=company_id,
                        ).debug("Trunk busy; keeping front slot")
                        await asyncio.sleep(WT_QUEUE_POLL_INTERVAL_SECONDS)
                        continue

                    if trunk_result.status is TrunkStatus.NETWORK_ERROR:
                        await _update_realtime_entry(
                            "trunk_network_error",
                            status=WarmTransferQueueStatus.WAITING_FOR_TRUNK,
                            fields={"last_status": TrunkStatus.NETWORK_ERROR.value},
                        )
                        logger.bind(
                            event="WT_TRUNK_API_NETWORK_ERROR",
                            company_id=company_id,
                            error=trunk_result.error,
                        ).warning("Trunk API network error; keeping front slot")
                        await asyncio.sleep(WT_QUEUE_POLL_INTERVAL_SECONDS)
                        continue

                    # FATAL (401 / 404 / other)
                    logger.bind(
                        event="WT_TRUNK_API_FATAL",
                        company_id=company_id,
                        status_code=trunk_result.status_code,
                    ).error("Trunk API fatal status")
                    failure_reason = "trunk_fatal"
                    queue_dequeue_reason = "fatal"
                    break

                else:
                    # while-loop exited via condition, not break
                    if disconnect_flag["left"]:
                        queue_dequeue_reason = "disconnect"
                    else:
                        failure_reason = "queue_timeout"
                        queue_dequeue_reason = "timeout"
                        logger.bind(
                            event="WT_QUEUE_TIMEOUT",
                            company_id=company_id,
                            sip_identity=sip_identity,
                        ).warning("Queue wait exceeded 10 minutes")
            finally:
                job_ctx.room.off("participant_disconnected", _on_caller_disconnect)

        except Exception as exc:
            release_after_monitor = False
            failure_reason = failure_reason or "error"
            queue_dequeue_reason = "error"
            logger.opt(exception=exc).bind(
                event="WT_RUNTIME_ERROR",
                company_id=company_id,
                sip_identity=sip_identity,
            ).error("Warm-transfer runtime failed; falling back to leave_message")
        finally:
            if briefing_generation_task is not None and not release_after_monitor:
                if not briefing_generation_task.done():
                    briefing_generation_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await briefing_generation_task
            if (
                refresh_task is not None
                and not release_after_monitor
                and not refresh_task.done()
            ):
                refresh_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await refresh_task
            if hold_queue_slot_after_cleanup_failure:
                # Room deletion failed: keep the slot so the next caller is not
                # dialed while the stale agent leg may still be up; the entry
                # expires via its TTL (refresh task cancelled above).
                logger.bind(
                    event="WT_QUEUE_SLOT_HELD_AFTER_CLEANUP_FAILURE",
                    company_id=company_id,
                    sip_identity=sip_identity,
                    ttl_seconds=WT_ZOMBIE_MAX_AGE_SECONDS,
                ).error("Queue slot held until TTL after agent cleanup failure")
            elif not release_after_monitor:
                await _release_queue(queue_dequeue_reason)
            if hold_music_player is not None:
                try:
                    await hold_music_player.aclose()
                except Exception as exc:
                    logger.opt(exception=exc).warning("Failed to stop hold music")

        # Handle final outcome (SUCCESS → monitor, else → leave_message)
        total_wait_seconds = round(time.monotonic() - transfer_start_time, 1)
        call_session_data.transfer_record.total_wait_seconds = total_wait_seconds

        # Checked before caller-disconnect handling: a caller leaving during
        # agent cleanup must not skip ending the call.
        if terminate_after_cleanup_failure:
            # The room was deleted above (or its slot is held until TTL); end
            # the session without TTS / leave-message / audio restore.
            call_session_data.is_warm_transferring = False
            call_session_data.transfer_record.transfer_status = "agent_cleanup_failed"
            self.session.history.add_message(
                role="developer",
                content=f"[{transfer_type}_TRANSFER_CALL: STATUS=FAILED]",
                extra=agent_event_extra(
                    f"{transfer_type}_TRANSFER_CALL",
                    status="FAILED",
                    details={
                        "reason": "agent_cleanup_failed",
                        "wait_seconds": total_wait_seconds,
                        "target": transfer_to,
                    },
                ),
            )
            await self.hangup()
            return None

        # Caller disconnected (queue-wait or dial-phase): exit cleanly without TTS/leave_message.
        caller_disconnected = disconnect_flag["left"] or (
            warm_transfer_result is not None
            and warm_transfer_result.status == WarmTransferStatus.CALLER_DISCONNECTED
        )
        if caller_disconnected:
            call_session_data.is_warm_transferring = False
            call_session_data.transfer_record.transfer_status = (
                WarmTransferStatus.CALLER_DISCONNECTED.value
            )
            self.session.history.add_message(
                role="developer",
                content=(
                    f"[{transfer_type}_TRANSFER_CALL: STATUS=CALLER_DISCONNECTED]"
                ),
                extra=agent_event_extra(
                    f"{transfer_type}_TRANSFER_CALL",
                    status="CALLER_DISCONNECTED",
                    details={
                        "target": transfer_to,
                        "wait_seconds": total_wait_seconds,
                    },
                ),
            )
            return None

        if terminate_after_reconfirmation:
            call_session_data.is_warm_transferring = False
            call_session_data.transfer_record.transfer_status = (
                "wait_reconfirmation_terminated"
            )
            self.session.history.add_message(
                role="developer",
                content=(
                    f"[{transfer_type}_TRANSFER_CALL: "
                    "STATUS=WAIT_RECONFIRMATION_TERMINATED]"
                ),
                extra=agent_event_extra(
                    f"{transfer_type}_TRANSFER_CALL",
                    status="WAIT_RECONFIRMATION_TERMINATED",
                    details={
                        "wait_seconds": total_wait_seconds,
                        "target": transfer_to,
                    },
                ),
            )
            await self.hangup()
            return None

        if (
            warm_transfer_result is not None
            and warm_transfer_result.status == WarmTransferStatus.SUCCESS
        ):
            call_session_data.transfer_record.transfer_status = (
                WarmTransferStatus.SUCCESS.value
            )
            call_session_data.transfer_record.briefing_text = (
                warm_transfer_result.briefing_text if briefing_enabled else None
            )
            self.session.history.add_message(
                role="developer",
                content=f"[{transfer_type}_TRANSFER_CALL: STATUS=SUCCESS]",
                extra=agent_event_extra(
                    f"{transfer_type}_TRANSFER_CALL",
                    status="SUCCESS",
                    details={
                        "target": transfer_to,
                        "business_hours": call_session_data.business_data.is_work_time,
                        "wait_seconds": total_wait_seconds,
                    },
                ),
            )
            monitor_reason = "conversation_ended"
            try:
                await self._monitor_transfer_conversation(
                    caller_identity=sip_identity,
                    agent_identity=warm_transfer_result.agent_identity,
                )
            except Exception:
                monitor_reason = "monitor_failed"
                raise
            finally:
                if refresh_task is not None:
                    refresh_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await refresh_task
                await _release_queue(monitor_reason)
            return None

        # Failure path
        call_session_data.is_warm_transferring = False
        status_value = (
            warm_transfer_result.status.value
            if warm_transfer_result is not None
            else _normalize_transfer_failure_status(failure_reason)
        )
        call_session_data.transfer_record.transfer_status = status_value

        if last_warm_transfer_task is not None:
            try:
                await last_warm_transfer_task._restore_caller_ai_audio()
            except Exception as exc:
                logger.opt(exception=exc).warning(
                    "Failed to restore caller audio before failure follow-up"
                )

        self.session.history.add_message(
            role="developer",
            content=f"[{transfer_type}_TRANSFER_CALL: STATUS=FAILED]",
            extra=agent_event_extra(
                f"{transfer_type}_TRANSFER_CALL",
                status="FAILED",
                details={
                    "reason": failure_reason or status_value,
                    "wait_seconds": total_wait_seconds,
                    "target": transfer_to,
                },
            ),
        )

        try:
            if not caller_requested_leave_message:
                speech_handle = self.session.say(
                    t("transfer.failed", call_session_data.call_config_data.language),
                    allow_interruptions=False,
                )
                await speech_handle.wait_for_playout()
        except Exception as exc:
            logger.opt(exception=exc).warning("Failed to announce transfer failure")

        await self._handle_leave_message(
            call_session_data,
            is_after_transfer_failure=True,
            entry_reason=(
                "caller_exit" if caller_requested_leave_message else "final_failure"
            ),
            action_key=action_key,
            transfer_purpose=transfer_purpose,
            booking_context=_booking_memo_context_for(call_session_data, action_key),
        )
        return None

    async def _dispatch_transfer(
        self,
        call_session_data: CallSessionData,
        transfer_to: str,
        transfer_mode: str,
        *,
        direct: bool,
        action_key: Optional[str] = None,
        transfer_purpose: TransferPurpose | None = None,
    ) -> None:
        """transfer_mode 에 따라 cold/warm transfer 핸들러로 디스패치한다.

        `_handle_transfer_call`(전역 설정 기반)과 SingleAgent 의 `transfer_direct`
        (플로우 노드 기반)가 공유하는 유일한 공통 로직(콜드/웜 분기)만 담당한다.
        전화번호 해석·leave_memo 폴백·Confirmation 은 각 호출부가 소유한다.

        Args:
            direct: True 면 DIRECT_* (플로우 노드 직접 연결, Confirmation 없음),
                    False 면 COLD/WARM.
        """
        transfer_mode = normalize_transfer_mode(transfer_mode)
        briefing_enabled = transfer_mode != "warm_transfer_no_briefing"

        # 사유는 warm 브리핑에만 쓰인다. 키패드 직접 연결(direct)과 cold transfer 는
        # 사유를 전달할 곳이 없으므로 묻지 않는다. warm_transfer_no_briefing 은
        # 가장 빠른 연결이 목적이므로 묻지 않는다(실패 시 메모 폴백은 purpose=None).
        if transfer_purpose is None and not direct and transfer_mode == "warm_transfer":
            result = await TransferReasonTask(
                caller_identity=call_session_data.recipient_data.sip_identity,
                language=call_session_data.call_config_data.language,
            )
            if result.caller_disconnected:
                return None
            transfer_purpose = result.purpose

        self.session.input.set_audio_enabled(False)
        if transfer_mode == "cold_transfer":
            await self._handle_cold_transfer_call(
                call_session_data=call_session_data,
                transfer_to=transfer_to,
                transfer_type="DIRECT_COLD" if direct else "COLD",
            )
            return None

        await self._handle_warm_transfer_call(
            call_session_data=call_session_data,
            transfer_to=transfer_to,
            transfer_type="DIRECT_WARM" if direct else "WARM",
            action_key=action_key,
            transfer_purpose=transfer_purpose,
            briefing_enabled=briefing_enabled,
        )
        return None

    async def _handle_transfer_call(
        self,
        call_session_data: CallSessionData,
        transfer_to: Optional[str] = None,
        action_key: Optional[str] = None,
        transfer_purpose: TransferPurpose | None = None,
    ):
        """
        Transfer the caller to a human agent or provide memo guidance.

        상담원 연결 확인 단계 없이, 연결 대상이 있으면 바로 상담원에게 연결한다.
        (transfer_to 없음·leave_memo 설정이면 상담메모 수집 후 통화 종료)
        """

        transfer_to = transfer_to or call_session_data.call_config_data.transfer_to
        participant_phone = call_session_data.recipient_data.phone

        logger.bind(
            event="_handle_transfer_call",
            transfer_to=transfer_to,
            room_name=call_session_data.room_name,
            phone=participant_phone,
            transfer_mode=call_session_data.call_config_data.transfer_mode,
        ).debug("Handle transfer call")

        # ! 상담원 연결 전화번호 없으면 상담메모 수집 후 통화 종료 처리
        if (
            not transfer_to
            or call_session_data.call_config_data.transfer_mode == "leave_memo"
        ):
            await self._handle_leave_message(
                call_session_data,
                is_after_transfer_failure=False,
                entry_reason="memo_only",
                action_key=action_key,
                transfer_purpose=transfer_purpose,
                booking_context=_booking_memo_context_for(
                    call_session_data, action_key
                ),
            )
            return None

        # ! 상담원 연결 전화번호 있으면 확인 단계 없이 바로 상담원 연결
        await self._dispatch_transfer(
            call_session_data=call_session_data,
            transfer_to=transfer_to,
            transfer_mode=call_session_data.call_config_data.transfer_mode,
            direct=False,
            action_key=action_key,
            transfer_purpose=transfer_purpose,
        )

        return None

    @function_tool()
    async def transfer_call(self, ctx: RunContext_T, urgent: bool = False):
        """
        Connect the caller to a human representative or leave a configured memo.

        USE ONLY when:
        - Caller explicitly asks to speak with a human/representative
        - Urgent device safety issue (battery swelling, overheating, smoke,
          burning smell) AFTER the caller said yes to the offered connection.
          Ask first; if the caller declines, continue their original request.
        - Issue that cannot be resolved by AI agents
        - The deterministic booking-exit router requests a staff handoff

        Args:
            urgent: True only for a device safety issue whose connection the
                caller has just accepted.
        """
        ctx.disallow_interruptions()

        call_session_data = ctx.userdata

        transfer_to = call_session_data.call_config_data.transfer_to
        participant_phone = call_session_data.recipient_data.phone

        logger.bind(
            event="TRANSFER_CALL",
            transfer_to=transfer_to,
            room_name=call_session_data.room_name,
            phone=participant_phone,
        ).info("Transferring call")

        booking_context = getattr(call_session_data, "booking_memo_context", None)
        action_key = (
            "booking_create"
            if booking_context
            and booking_context.exit_reason_code
            in {"no_slot_memo", "staff_connection", "service_not_offered"}
            else None
        )
        await self._handle_transfer_call(
            call_session_data=call_session_data,
            action_key=action_key,
            transfer_purpose=(
                TransferPurpose(code="urgent_handoff") if urgent else None
            ),
        )
        return None

    @function_tool()
    async def end_call_by_user_request(self, ctx: RunContext_T):
        """
        User-requested call termination.

        This is a terminal tool. It owns the goodbye message and call termination.
        Invoke it without a preamble, additional confirmation, or separate goodbye,
        and produce no further output after the call.

        Invoke ONLY when the caller explicitly says they want to end/hang up the call.

        INVOKE examples:
        - "끊어주세요" / "끊을게요" / "전화 끝내주세요"
        - "Please end the call." / "I'll hang up now."

        DO NOT invoke:
        - Short acknowledgments after task completion ("네", "알겠습니다", "감사합니다")
        - Task just finished — ask if anything else is needed instead
        - Gratitude or silence — these are NOT hangup requests
        """

        return await self._handle_end_call_by_user_request(ctx)

    async def _handle_end_call_by_user_request(self, ctx: RunContext_T) -> None:
        """Own the goodbye and hangup for an explicit caller request."""

        ctx.session.input.set_audio_enabled(False)

        call_session_data = ctx.userdata

        logger.bind(
            event="END_CALL_USER",
            stage="start",
            participant=call_session_data.recipient_data.phone,
        ).info("Ending call at user request")

        # Before
        current_speech = ctx.session.current_speech
        if current_speech:
            try:
                await ctx.wait_for_playout()
            except Exception as e:
                logger.bind(
                    event="END_CALL_USER",
                    stage="wait_playout_error",
                    room_name=call_session_data.room_name,
                ).opt(exception=e).error(
                    "Error waiting for playout before user-requested hangup"
                )

        goodbye_handle = ctx.session.say(
            t("end_call.goodbye", call_session_data.call_config_data.language),
            allow_interruptions=False,
        )

        try:
            await goodbye_handle.wait_for_playout()
        except Exception as e:
            logger.bind(
                event="END_CALL_USER",
                stage="wait_playout_error",
                room_name=call_session_data.room_name,
            ).opt(exception=e).error("Error waiting for playout after user hangup")

        self._record_call_termination("USER", "EXPLICIT_REQUEST")

        await self.hangup()

    @function_tool()
    async def end_call_by_system(self, ctx: RunContext_T, reason: Optional[str] = None):
        """
        System-triggered call termination after explicit closing confirmation.

        This is a terminal tool. It owns the goodbye message and call termination.
        Invoke it without a separate goodbye, and produce no further output afterward.

        Invoke ONLY after ALL of the following conditions are satisfied:
        - The caller's non-terminal request has already been completed.
        - The assistant explicitly asked whether anything else is needed or whether
          the call may end.
        - The caller confirmed in a subsequent turn that no further help is needed
          or that the call may end.

        Self-check before every call: can you point to your own previous turn that
        literally asked a closing question, and to the caller's next turn that
        answered it? If not, do NOT call this tool — respond normally instead.

        DO NOT use when:
        - A task merely appears complete or information has just been provided.
        - Receiving or answering an information request in the same turn.
        - A DTMF selection or informational request is the only apparent signal;
          neither counts as closing confirmation.
        - A terminal booking tool has already announced its outcome and started
          transfer or call shutdown.
        - The caller only acknowledged or expressed gratitude without directly
          answering an explicit closing question.
        - You have no way to help further (no matching tool, no info available).
          State that and keep listening — that alone is never grounds to end
          the call.
        - The caller's reply is a filler acknowledgment ("예", "네", "응", "yeah")
          that does not directly answer a closing question you just asked — e.g.
          right after a greeting or a disclosure statement, before any request
          was made.

        Examples of WRONG invocations (do not repeat these):
        - Greeting/recording-notice just finished, caller says only "예" with no
          request made yet -> wrong to end the call; ask what they need instead.
        - Caller asks an information question you cannot answer -> wrong to end
          the call in that same turn; state the information is unavailable and
          keep waiting for the caller's next turn.

        Parameters:
        - reason: Short tag describing the confirmed termination
          (e.g., "task_completed").

        Notes:
        - Avoid invoking while an urgent safety handoff is in progress.
        """

        ctx.session.input.set_audio_enabled(False)

        call_session_data = ctx.userdata

        logger.bind(
            event="END_CALL_SYSTEM",
            reason=reason,
            stage="start",
            participant=call_session_data.recipient_data.phone,
            room_name=call_session_data.room_name,
        ).info("Ending call initiated by system")

        current_speech = ctx.session.current_speech
        if current_speech:
            try:
                await ctx.wait_for_playout()
            except Exception as e:
                logger.bind(
                    event="END_CALL_SYSTEM",
                    stage="wait_playout_error",
                    room_name=call_session_data.room_name,
                ).opt(exception=e).error(
                    "Error waiting for playout before system hangup"
                )

        goodbye_handle = ctx.session.say(
            t("end_call.goodbye", call_session_data.call_config_data.language),
            allow_interruptions=False,
        )

        try:
            await goodbye_handle.wait_for_playout()
        except Exception as e:
            logger.bind(
                event="END_CALL_SYSTEM",
                stage="wait_playout_error",
                room_name=call_session_data.room_name,
            ).opt(exception=e).error("Error waiting for playout after system hangup")

        self._record_call_termination("SYSTEM", reason)

        await self.hangup()
