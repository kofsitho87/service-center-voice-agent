from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Callable

from livekit import rtc
from livekit.agents import function_tool
from livekit.agents.beta.workflows.utils import DtmfEvent, format_dtmf
from livekit.agents.job import get_job_context
from livekit.agents.llm.chat_context import ChatContext
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.agents.utils.aio.debounce import Debounced, debounced
from livekit.agents.voice.agent import AgentTask
from livekit.agents.voice.events import AgentStateChangedEvent
from loguru import logger

from data import RunContext_T
from tasks.verification_code_confirmation_task import (
    build_verification_code_confirmation_announcement,
)
from utils.agent_event import agent_event_extra
from utils.async_utils import await_through_cancellation
from utils.dtmf_config import resolve_dtmf_config
from utils.dtmf_input_arbiter import DtmfInputArbiter
from utils.greeting_dtmf_audio import CallerDtmfAudio
from utils.i18n import t
from utils.korean_speech_guard import KoreanSpeechGuardMixin


def _is_valid_verification_code(digits: str) -> bool:
    """Accept any 6-digit numeric demo verification code."""
    return len(digits) == 6 and digits.isdigit()


@dataclass
class GetDtmfResult:
    user_input: str
    verification_code: str = ""
    confirmed: bool = False

    @classmethod
    def from_dtmf_inputs(cls, dtmf_inputs: list[DtmfEvent]) -> GetDtmfResult:
        raw = format_dtmf(dtmf_inputs)
        return cls(user_input=raw, verification_code=raw.replace(" ", ""))


class GetVerificationCodeDtmfTask(KoreanSpeechGuardMixin, AgentTask[GetDtmfResult]):
    """Collect a 6-digit verification_code over DTMF, then confirm it by voice.

    Completes with a `GetDtmfResult`; `confirmed` reports the caller's answer.
    """

    def __init__(
        self,
        *,
        num_digits: int,
        purpose: str | None = None,
        dtmf_input_timeout: float = 2.0,
        chat_ctx: NotGivenOr[ChatContext] = NOT_GIVEN,
        dtmf_source: str | None = None,
        dtmf_fallback_delay_ms: float | None = None,
        dtmf_dedup_window_ms: float | None = None,
    ) -> None:
        """
        Args:
            num_digits: The number of digits to collect.
            purpose: A reason-only sentence (e.g., "예약 신청을 위해 본인 확인이 필요합니다.").
            dtmf_input_timeout: The per-digit timeout.
            chat_ctx: The chat context to use.
            dtmf_source: sip, inband, or auto; defaults to auto; explicit code override only.
            dtmf_fallback_delay_ms: Auto-mode audio fallback delay in milliseconds.
            dtmf_dedup_window_ms: Auto-mode source correlation window in milliseconds.
        """
        if num_digits <= 0:
            raise ValueError("num_digits must be greater than 0")

        config = resolve_dtmf_config(
            dtmf_source,
            dtmf_fallback_delay_ms,
            dtmf_dedup_window_ms,
        )
        self._dtmf_source = config.source
        self._exited = False
        self._completed = False
        self._active_session = None
        self._room = None
        self._audio_reader: CallerDtmfAudio | None = None
        self._arbiter: DtmfInputArbiter | None = None
        self._reply_tasks: set[asyncio.Task] = set()
        self._cleanup_task: asyncio.Task | None = None
        if config.source == "auto":
            self._arbiter = DtmfInputArbiter(
                self._handle_digit,
                self._accepts_input,
                fallback_delay=config.fallback_delay,
                dedup_window=config.dedup_window,
                event_prefix="VERIFICATION_CODE_DTMF",
                log_digits=False,
                log_accepted_digits=True,
                preserve_order=True,
            )
        self._num_digits = num_digits
        self._curr_dtmf_inputs: list[DtmfEvent] = []
        self._dtmf_reply_running: bool = False
        self._confirming: bool = False
        self._collected_result: GetDtmfResult | None = None

        # 안내 멘트(announcement)는 on_enter 에서 통화 언어에 맞춰 구성한다.
        self._purpose = purpose
        self._lang: str | None = None
        self._announcement = ""

        super().__init__(
            instructions="Waiting for DTMF input of the 6-digit demo verification code. Do not speak.",
            chat_ctx=chat_ctx,
        )

        def _on_sip_dtmf_received(ev: rtc.SipDTMF) -> None:
            if not self._accepts_input():
                return
            if self._arbiter is not None:
                participant = getattr(ev, "participant", None)
                if getattr(participant, "identity", None) != self._caller_identity():
                    return
                self._arbiter.observe(ev.digit, "sip")
            else:
                self._handle_digit(ev.digit)

        @debounced(delay=dtmf_input_timeout)
        async def _generate_dtmf_reply() -> None:
            # An in-band candidate can arrive just before the per-digit timeout.
            # Keep collecting while SIP preference resolves; a new accepted digit
            # reschedules this debounce job without clearing the collected digits.
            while (
                len(self._curr_dtmf_inputs) < self._num_digits
                and self._arbiter is not None
                and self._arbiter.has_pending
            ):
                if not self._collection_open():
                    return
                await asyncio.sleep(0.01)
            if not self._collection_open():
                return
            self._dtmf_reply_running = True

            try:
                self._active_session.interrupt()

                dtmf_str = format_dtmf(self._curr_dtmf_inputs)
                logger.debug("Generating verification_code DTMF reply")

                # if input not fully received (i.e. timeout), re-prompt
                if len(self._curr_dtmf_inputs) != num_digits:
                    logger.debug("Incomplete verification_code input; requesting re-entry")
                    self._active_session.say(
                        t("dtmf_verification_code.reprompt.incomplete", self._lang),
                        # allow_interruptions=False,
                        add_to_chat_ctx=True,
                    )
                    return

                # Validate verification_code before completing
                raw_digits = dtmf_str.replace(" ", "")

                if not _is_valid_verification_code(raw_digits):
                    logger.debug("Invalid verification_code; requesting re-entry")
                    self._active_session.say(
                        t("dtmf_verification_code.reprompt.invalid", self._lang),
                        # allow_interruptions=False,
                        add_to_chat_ctx=True,
                    )
                    return

                self._active_session.history.add_message(
                    role="developer",
                    content="",
                    extra=agent_event_extra(
                        "VERIFICATION_CODE_DTMF_TASK",
                        status="INPUT_RECEIVED",
                        details={"digits": raw_digits},
                    ),
                )
                await self._start_confirmation(
                    GetDtmfResult.from_dtmf_inputs(self._curr_dtmf_inputs)
                )
            finally:
                self._dtmf_reply_running = False
                self._curr_dtmf_inputs.clear()

        def _on_agent_state_changed(ev: AgentStateChangedEvent) -> None:
            if not self._accepts_input():
                return

            if self.dtmf_reply_running():
                return

            if ev.new_state in ["speaking", "thinking"]:
                # clear any pending DTMF reply generation
                self._generate_dtmf_reply.cancel()
            elif len(self._curr_dtmf_inputs) != 0:
                # resume any previously cancelled DTMF reply generation after agent is back to non-speaking
                self._queue_reply()

        self._generate_dtmf_reply: Debounced[None] = _generate_dtmf_reply
        self._on_sip_dtmf_received: Callable[[rtc.SipDTMF], None] = (
            _on_sip_dtmf_received
        )
        self._on_agent_state_changed: Callable[[AgentStateChangedEvent], None] = (
            _on_agent_state_changed
        )

    def _collection_open(self) -> bool:
        return not (self._completed or self._exited or self.done() or self._confirming)

    def _accepts_input(self) -> bool:
        return (
            self._collection_open()
            and not self._dtmf_reply_running
            and len(self._curr_dtmf_inputs) < self._num_digits
        )

    def _caller_identity(self) -> str | None:
        userdata = getattr(self._active_session, "userdata", None)
        recipient = getattr(userdata, "recipient_data", None)
        return getattr(recipient, "sip_identity", None)

    def _on_audio_digit(self, digit: str) -> None:
        if self._arbiter is not None:
            self._arbiter.observe(digit, "inband")
        else:
            self._handle_digit(digit)

    def _handle_digit(self, digit: str) -> None:
        if not self._accepts_input():
            return
        self._active_session.interrupt()
        self._curr_dtmf_inputs.append(DtmfEvent(digit))
        self._queue_reply(immediate=len(self._curr_dtmf_inputs) == self._num_digits)

    def _queue_reply(self, *, immediate: bool = False) -> None:
        task = (
            self._generate_dtmf_reply()
            if immediate
            else self._generate_dtmf_reply.schedule()
        )
        self._reply_tasks.add(task)
        task.add_done_callback(self._reply_tasks.discard)

    def _on_participant_disconnected(self, participant: rtc.RemoteParticipant) -> None:
        identity = self._caller_identity()
        if identity and participant.identity == identity:
            userdata = getattr(self._active_session, "userdata", None)
            if userdata is not None:
                userdata.caller_disconnected = True
            self._on_room_disconnected()

    def _on_room_disconnected(self, *_args) -> None:
        if self._exited or self._completed or self.done():
            return
        self._completed = True
        userdata = getattr(self._active_session, "userdata", None)
        if userdata is not None:
            userdata.caller_disconnected = True
        self._begin_cleanup()
        self.cancel()

    async def _stop_dtmf_inputs(self) -> None:
        if self._arbiter is not None:
            await self._arbiter.aclose()
        if self._room is not None:
            try:
                self._room.off("sip_dtmf_received", self._on_sip_dtmf_received)
            except Exception:
                logger.exception("Failed to unregister verification_code SIP listener")
        if self._audio_reader is not None:
            try:
                await self._audio_reader.aclose()
            except Exception:
                logger.exception("Failed to close verification_code DTMF audio reader")

    def dtmf_reply_running(self) -> bool:
        return self._dtmf_reply_running

    async def _start_confirmation(self, result: GetDtmfResult) -> None:
        """DTMF 수집 완료 후 같은 Task 안에서 음성 확인 단계로 전환한다."""
        if self._confirming or self._exited or self._completed or self.done():
            return
        self._confirming = True
        self._collected_result = result

        # Fence input before any await; do not Debounced.cancel() the generator
        # currently awaiting this confirmation transition.
        for task in list(self._reply_tasks):
            if task is not asyncio.current_task() and not task.done():
                task.cancel()
        await self._stop_dtmf_inputs()
        if self._exited or self._completed or self.done():
            return
        self._active_session.input.set_audio_enabled(True)

        announcement = build_verification_code_confirmation_announcement(
            result.user_input.replace(" ", ""), self._lang
        )
        await self.update_instructions(
            t(
                "verification_code.confirm_instructions",
                self._lang,
                announcement=announcement,
            )
        )

        if self._exited or self._completed or self.done():
            return
        self._active_session.history.add_message(
            role="developer",
            content="",
            extra=agent_event_extra(
                "VERIFICATION_CODE_DTMF_TASK",
                status="CONFIRM",
                details={"verification_code": result.verification_code},
            ),
        )

        self._active_session.say(
            announcement,
            allow_interruptions=False,
            add_to_chat_ctx=True,
        )

    @function_tool
    async def confirm_information(self, ctx: RunContext_T) -> None:
        """고객이 데모 확인번호 6자리가 맞다고 확인할 때 호출하세요."""
        if self._completed or self._exited or self.done():
            return
        ctx.disallow_interruptions()

        result = self._collected_result
        if result is None:
            return

        self._completed = True
        self.complete(replace(result, confirmed=True))

    @function_tool
    async def reject_information(self, ctx: RunContext_T) -> None:
        """고객이 데모 확인번호 6자리가 틀리다고 하거나 수정을 요청할 때 호출하세요."""
        if self._completed or self._exited or self.done():
            return
        ctx.disallow_interruptions()

        result = self._collected_result
        if result is None:
            return

        self._completed = True
        self.complete(replace(result, confirmed=False))

    async def on_enter(self) -> None:
        try:
            await self._enter_collection()
        except BaseException:
            await self.on_exit()
            raise

    async def _enter_collection(self) -> None:
        ctx = get_job_context()
        self._room = ctx.room
        self._active_session = self.session
        ccd = getattr(
            getattr(self._active_session, "userdata", None), "call_config_data", None
        )
        self._lang = getattr(ccd, "language", None)
        instruction = t("dtmf_verification_code.announcement.instruction", self._lang)
        self._announcement = (
            f"{self._purpose} {instruction}" if self._purpose else instruction
        )
        self._active_session.history.add_message(
            role="developer",
            content="",
            extra=agent_event_extra("VERIFICATION_CODE_DTMF_TASK", status="ON_ENTER"),
        )
        if self._dtmf_source in {"sip", "auto"}:
            self._room.on("sip_dtmf_received", self._on_sip_dtmf_received)
        self._room.on("participant_disconnected", self._on_participant_disconnected)
        self._room.on("disconnected", self._on_room_disconnected)
        self._active_session.on("agent_state_changed", self._on_agent_state_changed)
        self._active_session.input.set_audio_enabled(False)
        if self._dtmf_source in {"inband", "auto"}:
            identity = self._caller_identity()
            if not identity:
                raise ValueError(
                    "VerificationCode audio DTMF requires an explicit caller identity"
                )
            self._audio_reader = CallerDtmfAudio(
                self._room,
                identity,
                self._on_audio_digit,
                self._accepts_input,
                event_prefix="VERIFICATION_CODE_DTMF",
                log_digits=False,
            )
            self._audio_reader.start()
        logger.bind(
            event="VERIFICATION_CODE_DTMF_SOURCE_STARTED", source=self._dtmf_source
        ).info("VerificationCode DTMF source started")
        userdata = getattr(self._active_session, "userdata", None)
        if getattr(userdata, "caller_disconnected", False):
            self._on_room_disconnected()
            return
        self._active_session.say(self._announcement, add_to_chat_ctx=True)

    def _begin_cleanup(self) -> None:
        self._exited = True
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup())

    async def on_exit(self) -> None:
        self._begin_cleanup()
        await await_through_cancellation(self._cleanup_task)

    async def _cleanup(self) -> None:
        await self._stop_dtmf_inputs()
        current = asyncio.current_task()
        workers = [task for task in self._reply_tasks if task is not current]
        for task in workers:
            if not task.done() and not task.cancelling():
                task.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self._curr_dtmf_inputs.clear()
        if self._room is not None:
            self._room.off(
                "participant_disconnected", self._on_participant_disconnected
            )
            self._room.off("disconnected", self._on_room_disconnected)
        if self._active_session is not None:
            self._active_session.off(
                "agent_state_changed", self._on_agent_state_changed
            )
            self._active_session.input.set_audio_enabled(True)
