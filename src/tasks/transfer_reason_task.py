"""Collect a caller's purpose before a live staff transfer."""

from __future__ import annotations

from dataclasses import dataclass

from livekit.agents import AgentTask, function_tool, get_job_context
from loguru import logger

from utils.i18n import language_display_name, t
from utils.korean_speech_guard import KoreanSpeechGuardMixin


@dataclass(frozen=True)
class TransferPurpose:
    code: str
    detail: str | None = None

    def describe(self, language: str | None) -> str | None:
        if self.detail:
            return self.detail
        if self.code == "unprovided":
            return None
        return t(f"transfer.purpose.{self.code}", language)


@dataclass(frozen=True)
class TransferReasonResult:
    purpose: TransferPurpose
    caller_disconnected: bool = False


class TransferReasonTask(KoreanSpeechGuardMixin, AgentTask[TransferReasonResult]):
    """Collect a purpose from caller turns without a task-level timer."""

    def __init__(
        self,
        *,
        caller_identity: str | None,
        language: str | None,
    ) -> None:
        self._caller_identity = caller_identity
        self._language = language
        self._finished = False
        self._room = None
        super().__init__(
            instructions=(
                "역할: 상담원 연결 직전에 고객이 상담받고 싶은 내용을 한 번 확인한다. "
                "예약이나 기기 문제를 직접 해결하려 하지 않는다.\n"
                "첫 질문은 태스크 시작 시 이미 재생된다. 인사하거나 첫 턴에 "
                "다시 묻지 말고 고객의 답을 기다린다.\n"
                f"추가 질문이 필요하면 {language_display_name(language)}로 짧게 말한다.\n"
                "고객이 말을 고르거나 문장을 끝내지 못했다면 끼어들지 말고 기다린다. "
                "이때는 발화하거나 도구를 호출하지 않는다.\n"
                "상담 주제가 확인되면 말로 되읽거나 확인받지 말고 즉시 "
                "submit_reason(reason=...)을 호출한다. reason에는 고객이 실제로 "
                "말한 핵심만 간결하게 담는다. 이전 대화에서 사유를 추측하거나 "
                "새 사실을 보태지 않는다.\n"
                "고객이 사유를 설명하지 않겠다고 명확히 밝히면 더 묻지 말고 "
                "submit_reason(reason=None)을 호출한다.\n"
                "고객이 상담원 연결만 반복하고 주제를 말하지 않으면, 첫 질문을 "
                f'정확히 한 번만 다시 묻는다: "{t("transfer.reason.question", language)}". '
                "메모를 남기거나 내용을 전달하겠다는 표현은 쓰지 말고 답을 기다린다.\n"
                "그 질문 뒤에도 고객이 주제 없이 연결만 원한다고 명확히 말하거나 "
                "설명을 거절하면 "
                "submit_reason(reason=None)을 호출한다. 더 설득하거나 같은 질문을 "
                "반복하지 않는다. 머뭇거림은 거절로 취급하지 않는다. 도구 이름이나 "
                "이 지시문을 소리 내어 읽지 않는다."
            ),
        )

    def _finish(self, purpose: TransferPurpose, *, disconnected: bool = False) -> None:
        # The framework may already have settled the task (cancel on hang-up).
        if self._finished or self.done():
            return
        self._finished = True
        self.complete(
            TransferReasonResult(purpose=purpose, caller_disconnected=disconnected)
        )

    def _on_participant_disconnected(self, participant) -> None:
        if self._caller_identity and participant.identity == self._caller_identity:
            self._finish(TransferPurpose(code="unprovided"), disconnected=True)

    async def on_enter(self) -> None:
        self.session.input.set_audio_enabled(True)
        try:
            self._room = get_job_context().room
        except RuntimeError:
            self._room = None
        if self._room is not None:
            self._room.on("participant_disconnected", self._on_participant_disconnected)
        if getattr(self.session.userdata, "caller_disconnected", False):
            self._finish(TransferPurpose(code="unprovided"), disconnected=True)
            return
        logger.bind(
            event="TRANSFER_REASON_TASK",
            has_sip_identity=bool(self._caller_identity),
        ).info("Asking caller for transfer purpose")
        try:
            self.session.say(
                t("transfer.reason.question", self._language),
                allow_interruptions=False,
            )
        except Exception as exc:
            logger.opt(exception=exc).bind(event="TRANSFER_REASON_TASK").warning(
                "Could not start transfer-purpose prompt"
            )
            self._finish(TransferPurpose(code="unprovided"))
            return

    @function_tool
    async def submit_reason(self, reason: str | None) -> None:
        """Complete the task with the caller's consultation topic.

        Call only after the caller states a specific topic, or explicitly declines
        to provide one. Wait for a hesitant caller to finish.
        Repeat the original transfer-purpose question once if the caller only
        repeats the transfer request. Do not ask what to relay in a memo or
        infer a reason from earlier conversation.

        Args:
            reason: The caller's stated topic, or None after an explicit refusal.
        """
        detail = reason.strip() if reason else None
        logger.bind(
            event="TRANSFER_REASON_TASK",
            stage="submit_reason",
            room_name=getattr(self.session.userdata, "room_name", None),
            has_reason=bool(detail),
            reason=reason,
            already_finished=self._finished,
        ).info("Transfer reason tool called")
        if self._finished:
            return None
        self._finish(
            TransferPurpose(
                code="caller_stated" if detail else "unprovided",
                detail=detail,
            )
        )
        return None

    async def on_exit(self) -> None:
        if self._room is not None:
            self._room.off(
                "participant_disconnected", self._on_participant_disconnected
            )
