from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable

from livekit import rtc
from livekit.agents import function_tool
from livekit.agents.job import get_job_context
from livekit.agents.llm.chat_context import ChatContext
from livekit.agents.llm.tool_context import ToolError
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.agents.voice.agent import AgentTask
from livekit.agents.voice.events import AgentStateChangedEvent, UserStateChangedEvent
from loguru import logger

from utils.agent_event import agent_event_extra
from utils.async_utils import await_through_cancellation
from utils.dtmf_config import resolve_dtmf_config
from utils.dtmf_input_arbiter import DtmfInputArbiter
from utils.greeting_dtmf_audio import CallerDtmfAudio
from utils.i18n import t
from utils.korean_speech_guard import KoreanSpeechGuardMixin


@dataclass
class GreetingDtmfResult:
    """Result of a greeting DTMF selection task."""

    selected_key: str | None  # "1", "2", etc. or None on timeout
    timed_out: bool = False
    interrupted: bool = False

    @classmethod
    def from_key(cls, key: str) -> GreetingDtmfResult:
        return cls(selected_key=key, timed_out=False)

    @classmethod
    def timeout(cls) -> GreetingDtmfResult:
        return cls(selected_key=None, timed_out=True)

    @classmethod
    def interrupted_playout(cls) -> GreetingDtmfResult:
        return cls(selected_key=None, interrupted=True)


class GreetingDtmfTask(KoreanSpeechGuardMixin, AgentTask[GreetingDtmfResult]):
    """A task to collect a single DTMF key press for greeting menu navigation.

    Unlike GetVerificationCodeDtmfTask which collects N digits, this task:
    - Waits for a single key press
    - Validates against a set of allowed keys (dtmf_options)
    - Returns immediately on valid input
    - Returns None on timeout (caller handles fallback to node.next)
    """

    def __init__(
        self,
        *,
        message: str,
        dtmf_options: dict[str, str],
        replay_message: str | None = None,
        replay_keys: set[str] | None = None,
        timeout: float = 10.0,
        replay_on_invalid: bool = True,
        max_invalid_attempts: int = 2,
        chat_ctx: NotGivenOr[ChatContext] = NOT_GIVEN,
        dtmf_source: str | None = None,
        dtmf_fallback_delay_ms: float | None = None,
        dtmf_dedup_window_ms: float | None = None,
    ) -> None:
        """
        Args:
            message: The first full greeting message to play via TTS.
            dtmf_options: Valid keys and their labels (e.g., {"1": "booking", "2": "info"}).
            replay_message: The greeting to replay on 다시듣기/invalid/timeout.
                Falls back to ``message`` when not provided.
            replay_keys: Keys (e.g., {"*"}) that replay the menu in-task instead of
                completing the task. Played via ``replay_message`` without a notice
                and without counting as an invalid attempt.
            timeout: How long to wait for input after greeting completes.
            replay_on_invalid: Whether to re-prompt on invalid key press.
            max_invalid_attempts: Max invalid attempts before timeout.
            chat_ctx: The chat context to use.
            dtmf_source: "sip", "inband", or "auto"; defaults to auto; explicit code override only.
        """
        config = resolve_dtmf_config(
            dtmf_source, dtmf_fallback_delay_ms, dtmf_dedup_window_ms
        )
        self._dtmf_source = config.source
        self._exited = False
        self._room = None
        self._audio_reader: CallerDtmfAudio | None = None
        self._arbiter: DtmfInputArbiter | None = None
        if self._dtmf_source == "auto":
            self._arbiter = DtmfInputArbiter(
                self._handle_digit,
                self._accepts_input,
                fallback_delay=config.fallback_delay,
                dedup_window=config.dedup_window,
            )
        self._history_tasks: set[asyncio.Task] = set()
        self._cleanup_task: asyncio.Task | None = None
        self._message = message
        self._replay_message = replay_message or message
        self._dtmf_options = dtmf_options
        self._valid_keys = set(dtmf_options.keys())
        self._replay_keys = set(replay_keys or ())
        self._timeout_duration = timeout
        self._replay_on_invalid = replay_on_invalid
        self._max_invalid_attempts = max_invalid_attempts

        self._completed = False
        self._invalid_attempt_count = 0
        self._timeout_task: asyncio.Task | None = None

        # on_enter에서 유효한 session을 캐시해 둔다.
        # `self.session` property는 activity context가 사라지면 RuntimeError를 던지므로,
        # 조기 끊김 race에서 on_exit cleanup을 안전하게 수행하기 위해 캐시 참조를 사용한다.
        self._active_session = None

        # 루프 제어용 이벤트/플래그
        self._wakeup_event = asyncio.Event()
        self._needs_replay = False
        self._replay_notice_message: str | None = None
        self._playout_interruption_reason: str | None = None
        self._active_speech_handle = None
        self._greeting_skip_recorded = False

        @function_tool
        async def select_option(selected_key: str | None) -> None:
            """Record the user's menu selection.

            Call this when the user has selected a valid option via DTMF keypress
            or by speaking the number.

            Args:
                selected_key: The key the user selected (e.g., "1", "2") or None if the user did not select any option
            """

            logger.bind(
                event="SELECT_OPTION_FUNCTION_TOOL_CALLED",
                selected_key=selected_key,
            ).debug("select_option function tool called")

            if not self._accepts_input():
                logger.debug(
                    "select_option called but task already completed, ignoring"
                )
                return

            if selected_key and selected_key not in self._valid_keys:
                raise ToolError(
                    f"Invalid key: {selected_key}. Valid options: {list(self._valid_keys)}"
                )
            self._finish(
                GreetingDtmfResult.from_key(selected_key),
                reason="DTMF_SELECTED" if selected_key else None,
            )

        super().__init__(
            # 실제 지시문은 on_enter 에서 통화 언어에 맞춰 설정한다.
            instructions="",
            chat_ctx=chat_ctx,
            tools=[select_option],
        )

        def _on_sip_dtmf_received(ev: rtc.SipDTMF) -> None:
            if self._arbiter is not None:
                recipient = getattr(
                    self._active_session.userdata, "recipient_data", None
                )
                identity = getattr(recipient, "sip_identity", None)
                participant = getattr(ev, "participant", None)
                if not identity or getattr(participant, "identity", None) != identity:
                    return
                self._arbiter.observe(ev.digit, "sip")
            else:
                self._handle_digit(ev.digit)

        def _on_participant_disconnected(
            participant: rtc.RemoteParticipant,
        ) -> None:
            if not self._accepts_input():
                return

            active_session = self._active_session
            userdata = getattr(active_session, "userdata", None)
            recipient_data = getattr(userdata, "recipient_data", None)
            caller_identity = getattr(recipient_data, "sip_identity", None)
            participant_identity = getattr(participant, "identity", None)
            if caller_identity is None or participant_identity != caller_identity:
                return

            # 전역 participant_disconnected handler보다 먼저 실행되는 경우에도
            # caller 상태를 즉시 노출해 후속 분기가 timeout으로 오인하지 않게 한다.
            userdata.caller_disconnected = True
            self._cancel_timeout()
            self._playout_interruption_reason = "PLAYOUT_INTERRUPTED"
            self._wakeup_event.set()
            try:
                active_session.interrupt()
            except Exception as e:
                logger.opt(exception=e).debug(
                    "Failed to interrupt greeting after caller disconnected"
                )
            self._safe_complete(GreetingDtmfResult.interrupted_playout())

        def _on_greeting_playout_done(speech_handle) -> None:
            # interrupt 후 곧바로 replay가 시작되면 이전 SpeechHandle의 done
            # callback이 늦게 도착할 수 있다. 현재 handle만 완료로 인정한다.
            if speech_handle is not self._active_speech_handle:
                return
            self._wakeup_event.set()

        def _on_user_state_changed(ev: UserStateChangedEvent) -> None:
            if not self._accepts_input():
                return
            # Pause timeout while user is speaking
            if ev.new_state == "speaking":
                self._pause_timeout()
            elif ev.new_state == "listening":
                self._resume_timeout()

        def _on_agent_state_changed(ev: AgentStateChangedEvent) -> None:
            if not self._accepts_input():
                return
            # Pause timeout while agent is speaking
            if ev.new_state in ["speaking", "thinking"]:
                self._pause_timeout()
            elif ev.new_state == "listening":
                self._resume_timeout()

        self._on_sip_dtmf_received: Callable[[rtc.SipDTMF], None] = (
            _on_sip_dtmf_received
        )
        self._on_user_state_changed: Callable[[UserStateChangedEvent], None] = (
            _on_user_state_changed
        )
        self._on_agent_state_changed: Callable[[AgentStateChangedEvent], None] = (
            _on_agent_state_changed
        )
        self._on_participant_disconnected: Callable[[rtc.RemoteParticipant], None] = (
            _on_participant_disconnected
        )
        self._on_greeting_playout_done = _on_greeting_playout_done
        self._options_str = ""
        self._timeout_paused = False
        self._lang: str | None = None

    def _accepts_input(self) -> bool:
        return not self._completed and not self._exited and not self.done()

    def _on_audio_digit(self, digit: str) -> None:
        if self._arbiter is not None:
            self._arbiter.observe(digit, "inband")
        else:
            self._handle_digit(digit)

    def _handle_digit(self, digit: str) -> None:
        if not self._accepts_input():
            return

        logger.debug(f"DTMF received: {digit}")

        # 다시듣기 키 (예: "*") → task 종료/라우팅 대신 in-task replay
        if digit in self._replay_keys:
            logger.debug(f"Replay DTMF key received: {digit}; replaying menu")
            self._needs_replay = True
            self._replay_notice_message = None  # 안내 멘트 없이 바로 재생
            self._cancel_timeout()
            self._wakeup_event.set()
            self._active_session.interrupt()
            return

        # Check if valid option
        if digit in self._valid_keys:
            logger.debug(f"Valid DTMF key received: {digit}")
            self._finish(
                GreetingDtmfResult.from_key(digit),
                reason="DTMF_SELECTED",
                interrupt=True,
            )
        else:
            # Invalid key
            self._invalid_attempt_count += 1
            logger.debug(
                f"Invalid DTMF key: {digit}, attempt {self._invalid_attempt_count}"
            )

            self._schedule_invalid_dtmf_history_note(digit)

            if self._invalid_attempt_count > self._max_invalid_attempts:
                # Too many invalid attempts, timeout
                logger.debug("Max invalid attempts reached, timing out")
                self._finish(
                    GreetingDtmfResult.timeout(),
                    reason="MAX_INVALID_DTMF",
                    interrupt=True,
                )
            elif self._replay_on_invalid:
                # 인사말 재생 요청 → on_enter 루프에서 처리
                self._needs_replay = True
                self._replay_notice_message = t(
                    "greeting_dtmf.replay.invalid", self._lang
                )
                self._cancel_timeout()
                self._wakeup_event.set()
                self._active_session.interrupt()

    def _schedule_invalid_dtmf_history_note(self, digit: str) -> None:
        async def _delayed() -> None:
            await asyncio.sleep(0.3)
            if not self._accepts_input():
                return
            self._active_session.history.add_message(
                role="developer",
                content="",
                extra=agent_event_extra(
                    "GREETING_DTMF_TASK",
                    status="INVALID",
                    details={"selected_key": digit},
                ),
            )

        try:
            task = asyncio.get_running_loop().create_task(_delayed())
            self._history_tasks.add(task)
            task.add_done_callback(self._history_tasks.discard)
        except RuntimeError:
            logger.warning(
                "No running event loop; skipping delayed invalid DTMF history note"
            )

    def _record_greeting_skipped(self, reason: str) -> None:
        if self._greeting_skip_recorded:
            return
        self._active_session.history.add_message(
            role="developer",
            content="",
            extra=agent_event_extra(
                "GREETING", status="SKIPPED", details={"reason": reason}
            ),
        )
        self._greeting_skip_recorded = True

    def _safe_complete(self, result: GreetingDtmfResult | Exception) -> None:
        """task를 최대 한 번만 완료시킨다.

        LiveKit의 AgentTask는 내부 future가 이미 done이면 complete() 호출 시
        ``RuntimeError: ... is already done``을 던진다. 통화가 인사말 재생 중/직후에
        조기 종료되면 프레임워크가 cancel()을 통해 future를 먼저 완료시키는데,
        이때 on_enter 루프나 DTMF 리스너 콜백이 self.complete()를 다시 호출하면
        예외가 발생한다. 자체 플래그(``_completed``)만으로는 프레임워크가 먼저
        완료시킨 경우를 막지 못하므로, 실제 LiveKit 상태(``self.done()``)도 함께
        확인하여 중복 호출을 방어한다.
        """
        self._completed = True
        if self.done():
            return
        # complete() resolves the task future before the playout loop resumes.
        # Record interruption while the active greeting is still observable.
        if self._active_speech_handle is not None and self._playout_interruption_reason:
            self._record_greeting_skipped(self._playout_interruption_reason)
        self.complete(result)

    def _finish(
        self,
        result: GreetingDtmfResult,
        *,
        reason: str | None = None,
        interrupt: bool = False,
    ) -> None:
        """Stop the timer, wake the playout loop, then complete exactly once.

        ``_completed`` is set before interrupting so callbacks fired by the
        interruption already see the task as closed to input.
        """
        self._cancel_timeout()
        if reason is not None:
            self._playout_interruption_reason = reason
        self._completed = True
        self._wakeup_event.set()
        if interrupt:
            self._active_session.interrupt()
        self._safe_complete(result)

    def _on_room_disconnected(self, *_args) -> None:
        if not self._accepts_input():
            return
        self._finish(GreetingDtmfResult.interrupted_playout())

    def _is_caller_disconnected(self) -> bool:
        """session.userdata(CallSessionData)에 set된 caller_disconnected 플래그 확인."""
        try:
            userdata = self.session.userdata
        except Exception:
            return False
        return bool(getattr(userdata, "caller_disconnected", False))

    async def _start_timeout(self) -> None:
        """Start the input timeout timer."""
        try:
            remaining = self._timeout_duration
            while remaining > 0 and self._accepts_input():
                # caller가 이미 끊은 경우 타임아웃 대기를 즉시 중단한다.
                if self._is_caller_disconnected():
                    logger.debug(
                        "Caller disconnected during timeout wait; aborting greeting task"
                    )
                    self._completed = True
                    self._wakeup_event.set()
                    self._safe_complete(GreetingDtmfResult.interrupted_playout())
                    return

                if self._timeout_counting():
                    await asyncio.sleep(0.1)
                    # An audio observation may arrive during this last timer tick.
                    if self._timeout_counting():
                        remaining -= 0.1
                else:
                    await asyncio.sleep(0.1)

            if self._accepts_input():
                self._invalid_attempt_count += 1
                logger.debug(
                    f"Greeting DTMF timeout - no input received, "
                    f"attempt {self._invalid_attempt_count}"
                )

                if self._invalid_attempt_count > self._max_invalid_attempts:
                    logger.debug("Max timeout attempts reached, timing out")
                    self._completed = True
                    self._wakeup_event.set()
                    self._safe_complete(GreetingDtmfResult.timeout())
                else:
                    # 인사말 재생 요청
                    self._needs_replay = True
                    self._replay_notice_message = t(
                        "greeting_dtmf.replay.timeout", self._lang
                    )
                    self._wakeup_event.set()
        except asyncio.CancelledError:
            pass

    def _timeout_counting(self) -> bool:
        """Whether the input timeout should advance (not paused, no pending DTMF)."""
        return not self._timeout_paused and not (
            self._arbiter is not None and self._arbiter.has_pending
        )

    def _cancel_timeout(self) -> None:
        """Cancel the timeout if user provides input."""
        if self._timeout_task and not self._timeout_task.done():
            self._timeout_task.cancel()

    def _pause_timeout(self) -> None:
        """Pause timeout countdown."""
        self._timeout_paused = True

    def _resume_timeout(self) -> None:
        """Resume timeout countdown."""
        self._timeout_paused = False

    async def on_enter(self) -> None:
        try:
            await self._run_greeting()
        finally:
            await self.on_exit()

    async def _run_greeting(self) -> None:
        ctx = get_job_context()
        self._room = ctx.room

        # 진입 시점에 유효한 session 참조를 캐시한다.
        # on_exit이 호출될 때는 activity context가 이미 사라져 self.session property가
        # RuntimeError를 던질 수 있으므로, 캐시된 참조로 listener를 해제해야 한다.
        self._active_session = self.session

        # 통화 언어 해석 후 지시문/옵션 문자열을 언어에 맞춰 구성한다.
        ccd = getattr(getattr(self.session, "userdata", None), "call_config_data", None)
        self._lang = getattr(ccd, "language", None)
        self._options_str = ", ".join(
            t("greeting_dtmf.option_item", self._lang, digit=k, label=v)
            for k, v in self._dtmf_options.items()
        )
        await self.update_instructions(
            t(
                "greeting_dtmf.base_instructions",
                self._lang,
                options_str=self._options_str,
            )
        )

        # ! 오디오 입력 비활성화
        self.session.input.set_audio_enabled(False)

        logger.bind(
            event="GREETING_DTMF_SOURCE_STARTED", source=self._dtmf_source
        ).info("Greeting DTMF source started")

        # Register DTMF listener
        if self._dtmf_source in {"sip", "auto"}:
            ctx.room.on("sip_dtmf_received", self._on_sip_dtmf_received)
        if self._dtmf_source in {"inband", "auto"}:
            recipient = getattr(self._active_session.userdata, "recipient_data", None)
            identity = getattr(recipient, "sip_identity", None)
            if not identity:
                raise ValueError("In-band DTMF requires an explicit caller identity")
            self._audio_reader = CallerDtmfAudio(
                ctx.room, identity, self._on_audio_digit, self._accepts_input
            )
            self._audio_reader.start()
        ctx.room.on("disconnected", self._on_room_disconnected)
        ctx.room.on(
            "participant_disconnected",
            self._on_participant_disconnected,
        )

        # Register state change listeners
        self.session.on("user_state_changed", self._on_user_state_changed)
        self.session.on("agent_state_changed", self._on_agent_state_changed)

        # task 진입 시 caller가 이미 끊었으면 인사말을 발화하지 않고 즉시 종료한다.
        if self._is_caller_disconnected():
            logger.debug("Caller already disconnected at task entry; aborting greeting")
            self._completed = True
            self._safe_complete(GreetingDtmfResult.interrupted_playout())
            return

        first_play = True

        while self._accepts_input():
            if self._replay_notice_message:
                logger.debug("Playing replay notice before greeting retry")
                self.session.say(
                    self._replay_notice_message,
                    # allow_interruptions=False,
                )

                if self._completed:
                    return

                self._replay_notice_message = None

            # 인사말 재생 (첫 재생은 full 인사말, 이후는 다시듣기용 인사말)
            greeting_text = self._message if first_play else self._replay_message
            if greeting_text:
                logger.debug(f"Playing greeting message: {greeting_text[:50]}...")

                # 인사말 시작 태그 기록
                self._greeting_skip_recorded = False
                self.session.history.add_message(
                    role="developer",
                    content="",
                    extra=agent_event_extra("GREETING", status="STARTED"),
                )

                # AgentTask.on_enter에서는 playout을 직접 await하지 않는다. 대신
                # SpeechHandle done callback이 동일한 wakeup event를 깨우게 하여
                # DTMF/replay/disconnect와 실제 재생 완료를 한 상태 머신에서 처리한다.
                self._wakeup_event.clear()
                speech_handle = self.session.say(
                    greeting_text,
                    # allow_interruptions=False,
                    add_to_chat_ctx=first_play,
                )
                self._active_speech_handle = speech_handle
                speech_handle.add_done_callback(self._on_greeting_playout_done)

                # 실제 playout 완료, DTMF, replay 또는 disconnect 중 하나를 기다린다.
                await self._wakeup_event.wait()
                speech_handle.remove_done_callback(self._on_greeting_playout_done)
                if self._active_speech_handle is speech_handle:
                    self._active_speech_handle = None

                if self._exited or self.done():
                    return
                if self._completed:
                    if self._playout_interruption_reason:
                        self._record_greeting_skipped(self._playout_interruption_reason)
                    return

                # 재생 중 다시듣기/잘못된 키가 들어오면 interrupt된 현재 발화를
                # COMPLETED로 기록하지 않고, 요청 상태를 보존한 채 즉시 재생한다.
                if self._needs_replay:
                    self._needs_replay = False
                    self._wakeup_event.clear()
                    first_play = False
                    continue

                playout_error = speech_handle.exception()
                if playout_error is not None:
                    logger.bind(
                        event="GREETING_DTMF_TASK",
                        greeting_text=greeting_text,
                    ).opt(exception=playout_error).warning(
                        "Greeting playout finished with an error"
                    )
                    self._playout_interruption_reason = "PLAYOUT_INTERRUPTED"
                    self._completed = True
                    self._record_greeting_skipped("PLAYOUT_INTERRUPTED")
                    self._safe_complete(GreetingDtmfResult.interrupted_playout())
                    return

                # caller가 인사말 재생 중/직후 끊은 경우 COMPLETED 로 기록하지 않고 종료
                if self._is_caller_disconnected() or speech_handle.interrupted:
                    logger.debug(
                        "Caller disconnected during/after greeting playout; aborting"
                    )
                    self._playout_interruption_reason = "PLAYOUT_INTERRUPTED"
                    self._completed = True
                    self._record_greeting_skipped("PLAYOUT_INTERRUPTED")
                    self._safe_complete(GreetingDtmfResult.interrupted_playout())
                    return

                first_play = False

            # caller가 연결된 상태에서 인사말 TTS 재생 완료
            self.session.history.add_message(
                role="developer",
                content="",
                extra=agent_event_extra("GREETING", status="COMPLETED"),
            )

            # 타임아웃 대기
            self._wakeup_event.clear()
            self._needs_replay = False
            self._timeout_task = asyncio.create_task(self._start_timeout())

            # DTMF 입력 또는 타임아웃 대기
            await self._wakeup_event.wait()

            if not self._accepts_input():
                break

            if self._needs_replay:
                # 잘못된 키 입력 → 루프 재시작하여 인사말 재생
                self._needs_replay = False
                self._wakeup_event.clear()
                logger.debug("Replaying greeting after invalid DTMF input")
                continue

    async def on_exit(self) -> None:
        # Share a shielded cleanup job across framework exit and on_enter finally.
        self._exited = True
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup())
        await await_through_cancellation(self._cleanup_task)

    async def _cleanup(self) -> None:
        # caller 조기 끊김 race에서는 activity context가 이미 정리된 상태로 on_exit이
        # 호출될 수 있다. self.session/get_job_context() property/함수가 이 상황에서
        # RuntimeError를 던지므로, 모든 cleanup 단계를 defensive하게 수행한다.

        self._exited = True
        self._wakeup_event.set()
        if self._arbiter is not None:
            await self._arbiter.aclose()
        if self._room is not None:
            for event, callback in (
                ("sip_dtmf_received", self._on_sip_dtmf_received),
                ("participant_disconnected", self._on_participant_disconnected),
                ("disconnected", self._on_room_disconnected),
            ):
                try:
                    self._room.off(event, callback)
                except Exception:
                    logger.exception("Failed to unregister greeting room listener")
        if self._audio_reader is not None:
            try:
                await self._audio_reader.aclose()
            except Exception:
                logger.exception("Failed to close greeting DTMF audio reader")

        history_tasks = list(self._history_tasks)
        for task in history_tasks:
            task.cancel()
        if history_tasks:
            await asyncio.gather(*history_tasks, return_exceptions=True)

        # Session listener는 on_enter에서 캐시한 참조로 해제 (property 재진입 회피)
        active_session = self._active_session
        if active_session is not None:
            try:
                active_session.off("user_state_changed", self._on_user_state_changed)
                active_session.off("agent_state_changed", self._on_agent_state_changed)
            except Exception as e:
                logger.opt(exception=e).debug(
                    "Failed to unregister session listeners; "
                    "session may already be closed"
                )

        # Cancel timeout
        self._cancel_timeout()
        if self._timeout_task is not None:
            await asyncio.gather(self._timeout_task, return_exceptions=True)

        speech_handle = self._active_speech_handle
        if speech_handle is not None:
            try:
                speech_handle.remove_done_callback(self._on_greeting_playout_done)
            except Exception as e:
                logger.opt(exception=e).debug(
                    "Failed to unregister greeting playout callback"
                )
            self._active_speech_handle = None
