from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum

from livekit import api, rtc
from livekit.agents import AgentTask, get_job_context
from loguru import logger

from utils.agent_event import agent_event_extra
from utils.common import load_prompt
from utils.i18n import language_display_name, t
from utils.korean_speech_guard import KoreanSpeechGuardMixin


class ReconfirmationAction(str, Enum):
    CONTINUE_WAITING = "continue_waiting"
    EXIT_TO_LEAVE_MESSAGE = "exit_to_leave_message"
    TERMINATE_CALL = "terminate_call"
    CALLER_DISCONNECTED = "caller_disconnected"


_DTMF_ACTIONS = {
    "1": ReconfirmationAction.CONTINUE_WAITING,
    "2": ReconfirmationAction.EXIT_TO_LEAVE_MESSAGE,
}


@dataclass(eq=True)
class CallerWaitReconfirmationResult:
    action: ReconfirmationAction
    attempt_count: int


class CallerWaitReconfirmationTask(
    KoreanSpeechGuardMixin, AgentTask[CallerWaitReconfirmationResult]
):
    def __init__(
        self,
        *,
        caller_identity: str,
        message: str | None = None,
        timeout: float = 10.0,
        max_attempts: int = 3,
        language_code: str = "ko-KR",
    ) -> None:
        self._caller_identity = caller_identity
        self._message = message
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._language_code = language_code
        self._attempt_count = 0
        self._completed = False
        self._needs_replay = False
        self._wakeup_event = asyncio.Event()
        self._timeout_task: asyncio.Task | None = None
        super().__init__(
            instructions=load_prompt(
                "caller_wait_reconfirmation_prompt.yaml",
                language_code=language_code,
                default_language=language_display_name(language_code),
            ),
            chat_ctx=None,
        )

    def _get_session(self):
        injected_session = getattr(self, "_session", None)
        if injected_session is not None:
            return injected_session
        return self.session

    def _complete_once(self, result: CallerWaitReconfirmationResult) -> None:
        if self._completed:
            return
        self._completed = True
        self._needs_replay = False
        self._wakeup_event.set()
        self.complete(result)

    def _finish(self, action: ReconfirmationAction) -> None:
        self._cancel_timeout()
        self._complete_once(
            CallerWaitReconfirmationResult(
                action=action,
                attempt_count=self._attempt_count,
            )
        )

    def _register_failed_attempt(self) -> None:
        self._attempt_count += 1
        if self._attempt_count >= self._max_attempts:
            self._finish(ReconfirmationAction.TERMINATE_CALL)
            return

        self._needs_replay = True
        self._wakeup_event.set()

    def _is_caller_event(self, ev: rtc.SipDTMF) -> bool:
        participant_identity = getattr(
            getattr(ev, "participant", None),
            "identity",
            None,
        )
        return participant_identity == self._caller_identity

    def _record_dtmf_received(self, digit: str, action: ReconfirmationAction) -> None:
        attempt_count = self._attempt_count + 1
        history = self._get_session().history

        def _add_dev_message() -> None:
            history.add_message(
                role="developer",
                content="",
                extra=agent_event_extra(
                    "WT_RECONFIRMATION_DTMF",
                    details={"digit": digit, "action": action.value},
                ),
            )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _add_dev_message()
        else:
            loop.call_later(0.5, _add_dev_message)

        logger.bind(
            event="WT_RECONFIRMATION_DTMF",
            caller_identity=self._caller_identity,
            digit=digit,
            action=action.value,
            attempt_count=attempt_count,
        ).debug("Caller wait reconfirmation DTMF received")

    def _on_sip_dtmf_received(self, ev: rtc.SipDTMF) -> None:
        if self._completed or not self._is_caller_event(ev):
            return

        digit = ev.digit
        self._get_session().interrupt()

        action = _DTMF_ACTIONS.get(digit)
        if action is not None:
            self._record_dtmf_received(digit, action)
            self._attempt_count += 1
            self._finish(action)
            return

        # The prompt's timer must not count this same attempt a second time.
        self._cancel_timeout()
        self._register_failed_attempt()

    def _on_participant_disconnected(self, participant) -> None:
        if self._completed:
            return
        if participant.identity != self._caller_identity:
            return
        self._finish(ReconfirmationAction.CALLER_DISCONNECTED)

    async def _restore_caller_audio_for_prompt(self, ctx) -> None:
        room = getattr(ctx, "room", None)
        local_participant = getattr(room, "local_participant", None)

        try:
            if local_participant is not None:
                local_participant.set_track_subscription_permissions(
                    allow_all_participants=True,
                )

            api_room = getattr(getattr(ctx, "api", None), "room", None)
            caller = getattr(room, "remote_participants", {}).get(self._caller_identity)
            if api_room is None or caller is None:
                return

            for pub in caller.track_publications.values():
                if pub.kind != rtc.TrackKind.KIND_AUDIO or not pub.sid:
                    continue
                await api_room.mute_published_track(
                    api.MuteRoomTrackRequest(
                        room=room.name,
                        identity=self._caller_identity,
                        track_sid=pub.sid,
                        muted=False,
                    )
                )
        except Exception as exc:
            logger.opt(exception=exc).warning(
                "Failed to restore caller audio before wait reconfirmation prompt"
            )

    async def _start_timeout(self) -> None:
        try:
            await asyncio.sleep(self._timeout)
            if not self._completed:
                self._register_failed_attempt()
        except asyncio.CancelledError:
            pass

    def _cancel_timeout(self) -> None:
        if self._timeout_task is not None and not self._timeout_task.done():
            self._timeout_task.cancel()

    async def on_enter(self) -> None:
        ctx = get_job_context()
        session = self._get_session()
        message = self._message or t(
            "caller_wait.reconfirm.prompt", self._language_code
        )
        await self._restore_caller_audio_for_prompt(ctx)
        session.input.set_audio_enabled(False)
        ctx.room.on("sip_dtmf_received", self._on_sip_dtmf_received)
        ctx.room.on("participant_disconnected", self._on_participant_disconnected)

        while not self._completed:
            session.say(
                message,
                allow_interruptions=True,
                add_to_chat_ctx=False,
            )

            if self._completed:
                break

            self._wakeup_event.clear()
            self._needs_replay = False
            self._timeout_task = asyncio.create_task(self._start_timeout())
            await self._wakeup_event.wait()

            if self._completed:
                break

            if self._needs_replay:
                self._needs_replay = False
                continue

    async def on_exit(self) -> None:
        ctx = get_job_context()
        ctx.room.off("sip_dtmf_received", self._on_sip_dtmf_received)
        ctx.room.off("participant_disconnected", self._on_participant_disconnected)
        self._cancel_timeout()
