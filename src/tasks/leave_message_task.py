import calendar
import re
import textwrap
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from livekit.agents import AgentTask, function_tool
from loguru import logger

from data import BookingMemoContext, RunContext_T
from tasks.transfer_reason_task import TransferPurpose
from utils.common import load_prompt
from utils.i18n import t
from utils.korean_speech_guard import KoreanSpeechGuardMixin

# request_label / progress_notice 가 registry 에 정의된 action_key 집합
# (registry: leave_message.request_label.<action_key>, leave_message.progress_notice.<action_key>)
_KNOWN_ACTION_KEYS = frozenset(
    {
        "booking_create",
        "booking_modify",
        "booking_cancel",
        "booking_inquiry",
    }
)


def _spoken_date(d: date, language: Optional[str]) -> str:
    """날짜를 통화 언어에 맞는 표기로 변환한다. (ko: 'M월 D일', 그 외: 'March 1')"""
    if language == "ko-KR":
        return f"{d.month}월 {d.day}일"
    return f"{calendar.month_name[d.month]} {d.day}"


_MMDD_HHMM_RE = re.compile(r"(\d{2})-(\d{2}) (\d{2}):(\d{2})")


def _split_mmdd_hhmm(value: str | None) -> tuple[int, int, int, int] | None:
    """'MM-DD HH:MM' 형태면 (월, 일, 시, 분)을, 아니면 None 을 반환한다(범위 검사 없음)."""
    match = _MMDD_HHMM_RE.fullmatch(value or "")
    if not match:
        return None
    month, day, hour, minute = (int(group) for group in match.groups())
    return month, day, hour, minute


def _speak_timing_confirmation(ctx: RunContext_T, question: str) -> None:
    """확인 질문을 툴에서 직접 발화하고 LLM 후속 응답은 요청하지 않는다.

    툴 결과를 LLM이 읽게 두면 모델이 빈 응답을 내는 경우 통화가 무음으로
    멈춘다. 문구가 고정 템플릿이므로 결정론적으로 말한다.
    """
    ctx.session.say(question)
    return None


def _is_valid_mmdd_hhmm(value: str) -> bool:
    parts = _split_mmdd_hhmm(value)
    if parts is None:
        return False

    month, day, hour, minute = parts
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return False

    try:
        date(2000, month, day)
    except ValueError:
        return False

    return True


_WEEKDAY_KO = ("월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일")
_WEEKDAY_EN = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def _spoken_datetime(mmdd_hhmm: str, language: Optional[str]) -> str:
    """'MM-DD HH:MM' 을 통화 언어의 자연 발화로 변환한다. 요일을 함께 읽어준다.

    ko: '7월 12일 일요일 오후 6시' / '7월 8일 목요일 오전 10시'
    그 외: 'Sunday, July 12 at 6 PM' / 'Thursday, July 8 at 10 AM'

    요일은 다가오는 가장 가까운 해(올해, 이미 지났으면 내년)를 기준으로 계산한다.
    사용자가 요일 표현("이번 주 토요일" 등)을 말했을 때, LLM 이 날짜를 잘못
    계산했더라도 되읽기에서 실제 요일이 드러나 사용자가 정정할 수 있게 한다.
    유효한 포맷이 아니면 원본을 그대로 반환한다(호출부에서 이미 검증됨).
    """
    parts = _split_mmdd_hhmm(mmdd_hhmm)
    if parts is None:
        return mmdd_hhmm or ""

    month, day, hour, minute = parts
    # Business timezone (Asia/Seoul) for every caller language.
    tz = "Asia/Seoul"
    today = datetime.now(tz=ZoneInfo(tz)).date()
    try:
        resolved = date(today.year, month, day)
        if resolved < today:
            resolved = date(today.year + 1, month, day)
    except ValueError:
        return mmdd_hhmm

    spoken_date = _spoken_date(resolved, language)
    hour12 = hour % 12 or 12
    if language == "ko-KR":
        weekday = _WEEKDAY_KO[resolved.weekday()]
        period = "오전" if hour < 12 else "오후"
        minute_part = "" if minute == 0 else f" {minute}분"
        return f"{spoken_date} {weekday} {period} {hour12}시{minute_part}"

    weekday = _WEEKDAY_EN[resolved.weekday()]
    ampm = "AM" if hour < 12 else "PM"
    minute_part = "" if minute == 0 else f":{minute:02d}"
    return f"{weekday}, {spoken_date} at {hour12}{minute_part} {ampm}"


def _build_progress_notice(
    action_key: Optional[str], language: Optional[str] = None
) -> str:
    if action_key in _KNOWN_ACTION_KEYS:
        return t(f"leave_message.progress_notice.{action_key}", language)
    return t("leave_message.progress_notice.default", language)


# Why the memo flow started. Drives whether the opener may mention a failed
# connection and whether that failure was already announced.
# - memo_only: no live transfer was attempted (leave_memo / no transfer_to).
# - preflight_failure: warm transfer could not start; nothing announced yet.
# - final_failure: warm transfer failed and ``transfer.failed`` was spoken.
# - caller_exit: the caller chose to leave a message while waiting.
LeaveMessageEntryReason = Literal[
    "memo_only", "preflight_failure", "final_failure", "caller_exit"
]

# Where the transfer purpose came from.
# - caller: the caller stated it in TransferReasonTask.
# - system: code filled it in (routing reason); the caller never said it.
# - none: no describable purpose.
PurposeSource = Literal["caller", "system", "none"]


def _purpose_source(
    purpose: TransferPurpose | None, language: Optional[str]
) -> PurposeSource:
    if purpose is None or not purpose.describe(language):
        return "none"
    return "caller" if purpose.code == "caller_stated" else "system"


def _build_opening_notice(
    *,
    action_key: Optional[str],
    entry_reason: LeaveMessageEntryReason,
    purpose_source: PurposeSource,
    language: Optional[str],
) -> str:
    """Choose the memo opener from action key, purpose origin, and entry reason.

    Only a caller-stated purpose may be acknowledged as something the caller
    said. A failure apology is added only when nothing announced it yet.
    """
    if action_key in _KNOWN_ACTION_KEYS:
        notice = _build_progress_notice(action_key, language)
    elif entry_reason == "caller_exit":
        key = "caller_stated" if purpose_source == "caller" else "default"
        notice = t(f"leave_message.progress_notice.caller_exit.{key}", language)
    elif purpose_source == "caller":
        notice = t("leave_message.progress_notice.caller_stated", language)
    else:
        notice = _build_progress_notice(None, language)

    # The default notice already says a live connection is not possible right
    # now, so prefixing the apology would repeat it.
    uses_default_notice = action_key not in _KNOWN_ACTION_KEYS and (
        purpose_source != "caller"
    )
    if entry_reason == "preflight_failure" and not uses_default_notice:
        prefix = t("leave_message.transfer_failure_prefix", language)
        notice = f"{prefix} {notice}"
    return notice


def _format_additional_instructions(text: Optional[str]) -> str:
    """값이 있을 때만 ADDITIONAL INSTRUCTIONS 블록을 만든다.

    YAML 블록 스칼라는 기본 들여쓰기를 이미 제거하므로 주입 지점은 0열이다.
    설정 문자열에 딸려 온 공통 들여쓰기만 dedent 로 벗겨 첫 줄과 나머지 줄의
    기준을 맞춘다. 비어 있으면 제목까지 포함해 블록 전체를 생략한다.
    """
    if not (text or "").strip():
        return ""

    heading = (
        "ADDITIONAL INSTRUCTIONS (call-specific; these OVERRIDE the rules above "
        "on conflict):"
    )
    return f"{heading}\n{textwrap.dedent(text).strip()}"


def _build_opening_instructions(opening_line: str, opening_extra: str = "") -> str:
    """첫 턴 generate_reply 에 넘길 지시문을 만든다.

    say() 와 달리 오프너가 모델을 거치므로, 승인된 문구는 verbatim 앵커로 고정하고
    추가로 물을 항목만 뒤에 덧붙이게 범위를 좁힌다. 첫 턴 툴 호출 차단은 지시문이
    아니라 on_enter 의 tool_choice="none" 이 담당한다 — 지시만으로는 새는 턴이 있다.

    opening_extra 는 additional_instructions(Task 전체 규칙)를 그대로 받는다.
    거기에는 툴 인자·포맷 규칙이 섞여 있으므로, 발화로 새지 않도록 "물어야 할 것만
    골라서 묻고 규칙 자체는 말하지 말라"는 메타 지시를 반드시 함께 준다.
    """
    lines = [
        "This is the FIRST turn of this task.",
        "1. Start by speaking the following sentence VERBATIM. Do not paraphrase, "
        "shorten, translate, or add any greeting or apology before it:",
        f'"{opening_line}"',
    ]

    extra = textwrap.dedent(opening_extra or "").strip()
    if extra:
        lines += [
            "2. In the SAME turn, right after that sentence, ask the user for whatever "
            "the task rules below say you must still collect from them.",
            "The rules below are task-wide instructions, NOT a script: ask ONLY the "
            "things that need asking, and NEVER read a tool, argument, or formatting "
            "rule out loud. Skip anything that belongs to a later turn.",
            extra,
        ]

    lines += ["Constraints for this turn:", "- Do NOT call any tool in this turn."]
    if extra:
        # 이 제약은 2번에만 건다. 전체에 걸면 모델이 1번의 verbatim 문장까지
        # 줄이려 들어 승인 문구가 훼손된다(예: 취소 사유 보기를 나열하는 오프너).
        lines.append(
            "- Phrase the step 2 request as ONE short, natural spoken question — "
            "never a list. Do not alter step 1 to fit it."
        )
    lines.append("- Say nothing else, then WAIT for the user's reply.")
    return "\n".join(lines)


@dataclass
class TransferFailureResult:
    """상담원 연결 실패 시 결과."""

    scenario: str  # "booking_contact_info_collected" or "leave_message_collected"
    desired_datetime: Optional[str] = None
    desired_condition: Optional[str] = None
    message: Optional[str] = None


def _format_booking_context(context: BookingMemoContext | None) -> str:
    if context is None:
        return ""
    return "\n".join(
        (
            f"service_name: {context.service_name or ''}",
            f"operator_name: {context.operator_name or ''}",
            f"time_condition: {context.time_condition or ''}",
            f"searched_ranges: {', '.join(context.searched_ranges)}",
            f"detail: {context.detail}",
            f"exit_reason_code: {context.exit_reason_code}",
        )
    )


def _booking_context_subject(
    context: BookingMemoContext, language: Optional[str]
) -> str:
    """오프닝 문구의 주어. 상담사·서비스 이름이 없어도 문장이 깨지지 않게 조립한다."""
    operator = (context.operator_name or "").strip()
    service = (context.service_name or "").strip()
    if language == "en-US":
        parts = [
            part
            for part in (service, f"with {operator}" if operator else "")
            if part
        ]
        return " ".join(parts) or "your appointment"
    parts = [
        part for part in (f"{operator} 상담사" if operator else "", service) if part
    ]
    return " ".join(parts) or "요청하신 서비스"


def _booking_context_summary(context: BookingMemoContext | None) -> str | None:
    if context is None:
        return None
    parts = [
        value
        for value in (
            context.operator_name,
            context.service_name,
            context.time_condition,
            "조회한 기간에 가능 일정 없음" if context.searched_ranges else None,
        )
        if value
    ]
    return ", ".join(parts) or None


class LeaveMessageTask(KoreanSpeechGuardMixin, AgentTask[TransferFailureResult]):
    """
    상담원 연결이 불가능할 때 실행되는 Task.

    목적:
    1. 사용자에게 상담원 연결 불가 안내
    2. 사용자가 상담원에게 남길 메세지를 명시적으로 수집
    3. 수집된 정보를 반환하여 후속 처리
    """

    def __init__(
        self,
        chat_ctx=None,
        *,
        is_after_transfer_failure: bool,
        action_key: Optional[str] = None,
        collect_desired_datetime: bool = False,
        language: Optional[str] = None,
        additional_instructions: Optional[str] = None,
        booking_context: BookingMemoContext | None = None,
        transfer_purpose: TransferPurpose | None = None,
        entry_reason: LeaveMessageEntryReason | None = None,
    ):
        self.additional_instructions = additional_instructions or ""
        self.is_after_transfer_failure = is_after_transfer_failure
        # 명시되지 않으면 기존 플래그로 추정한다. 실패 후 진입인데 원인을 모르면
        # 실패 안내가 한 번도 나가지 않는 쪽보다 중복되는 쪽이 안전하다.
        self.entry_reason: LeaveMessageEntryReason = entry_reason or (
            "preflight_failure" if is_after_transfer_failure else "memo_only"
        )
        self.action_key = action_key
        self.collect_desired_datetime = collect_desired_datetime
        self._language = language
        self.booking_context = booking_context
        self.transfer_purpose = transfer_purpose
        self.purpose_source = _purpose_source(transfer_purpose, language)
        purpose_text = (
            transfer_purpose.describe(language) if transfer_purpose else None
        ) or ""
        # 날짜 수집 모드(collect_desired_datetime=True)의 2-step 흐름에서
        # set_desired_datetime 이 확정한 값을 임시 보관 → submit_booking_contact 에서 사용.
        self._pending_datetime: Optional[str] = None
        self._pending_condition: Optional[str] = None
        # 시스템이 채운 사유는 고객 요청 라벨로 쓰지 않는다. 라벨로 쓰면 LLM 이
        # 고객이 말한 요청처럼 요약하고 메모 내용을 묻지 않을 수 있다.
        self.request_label = (
            t(f"leave_message.request_label.{action_key}", language)
            if action_key in _KNOWN_ACTION_KEYS
            else purpose_text
            if self.purpose_source == "caller"
            else None
        )
        self.progress_notice = _build_opening_notice(
            action_key=action_key,
            entry_reason=self.entry_reason,
            purpose_source=self.purpose_source,
            language=language,
        )

        is_empty_booking_memo = (
            action_key in {"booking_modify", "booking_cancel"}
            and booking_context is not None
            and booking_context.exit_reason_code == "no_reservations"
            and self.entry_reason == "memo_only"
        )

        # 첫 발화 오프너 결정.
        # - 날짜 수집 모드(booking_create/modify): 날짜/시간을 먼저 묻는 전용 오프너.
        #   아직 실패 안내가 나가지 않은 사전 점검 실패일 때만 사과 문구를 붙인다.
        # - 그 외(message_only 등): progress_notice 를 그대로 사용.
        if is_empty_booking_memo:
            self.opening_line = t(
                f"booking.{action_key.removeprefix('booking_')}.no_reservations_handoff.memo",
                language,
            )
        elif collect_desired_datetime and action_key in {
            "booking_create",
            "booking_modify",
        }:
            if booking_context and booking_context.time_condition:
                opening = t(
                    "leave_message.opening_datetime.booking_context",
                    language,
                    subject=_booking_context_subject(booking_context, language),
                    time_condition=booking_context.time_condition,
                )
            else:
                opening = t(f"leave_message.opening_datetime.{action_key}", language)
            if self.entry_reason == "preflight_failure":
                prefix = t("leave_message.transfer_failure_prefix", language)
                opening = f"{prefix} {opening}"
            self.opening_line = opening
        else:
            self.opening_line = self.progress_notice

        # 첫 턴에도 additional_instructions 를 참조시킬지 결정한다.
        # 날짜 수집 모드의 첫 턴은 이미 날짜/시간을 묻고 있어 인적사항까지 얹으면
        # 한 턴에 네 가지를 묻게 된다. 그 경로는 STEP 2(날짜 확인) 이후에 묻게 두고,
        # 첫 턴은 기존처럼 확정 문구(say)로 내보낸다.
        self.opening_extra = (
            ""
            if collect_desired_datetime or is_empty_booking_memo
            else self.additional_instructions
        )
        self.opening_instructions = _build_opening_instructions(
            self.opening_line, self.opening_extra
        )

        if action_key and self.request_label is None:
            logger.bind(
                event="LEAVE_MESSAGE_TASK_UNKNOWN_ACTION_KEY",
                action_key=action_key,
            ).warning(
                "Unknown action_key for leave message task; falling back to chat_ctx"
            )
        request_source = (
            "action_key"
            if action_key in _KNOWN_ACTION_KEYS
            else "transfer_purpose"
            if self.purpose_source == "caller"
            else "chat_ctx"
        )

        # NOTE: 오프너는 on_enter(say 또는 generate_reply)가 전담하므로 시스템
        # 프롬프트에는 주입하지 않는다. 프롬프트 변수로 주면 LLM 이 이미 발화한
        # 오프너를 다시 말하거나 STEP 3 도입부로 새어 들어온다.
        instructions = load_prompt(
            "leave_message_task.yaml",
            language_code=language,
            is_after_transfer_failure=is_after_transfer_failure,
            entry_reason=self.entry_reason,
            request_label=self.request_label or "",
            request_source=request_source,
            collect_desired_datetime=collect_desired_datetime,
            additional_instructions=_format_additional_instructions(
                self.additional_instructions
            ),
            booking_context=_format_booking_context(booking_context),
            transfer_purpose=purpose_text,
            transfer_purpose_source=self.purpose_source,
        )

        if collect_desired_datetime:

            @function_tool
            async def set_desired_datetime(
                ctx: RunContext_T,
                desired_datetime: str,
            ) -> Optional[str]:
                """
                STEP 1 of booking contact collection. Store the desired appointment
                date/time. Resolve the caller's natural-language date/time yourself
                (using CURRENT_TIME as the reference) into `MM-DD HH:MM` format before
                calling this. Only call this once you have a CONCRETE hour — never for a
                vague time-of-day like "저녁"/"아침"/"evening"/"morning". This does NOT
                end the task. On success the tool itself speaks the confirmation
                question to the user — do NOT repeat or paraphrase it; just wait for
                their answer. An error text return means fix the value or ask the user
                for what is missing. On confirmation,
                call submit_booking_contact without asking for an optional message,
                once any required call-specific details have been collected.

                Args:
                    desired_datetime: The resolved desired appointment or change date and time, formatted as %m-%d %H:%M.
                """
                if not _is_valid_mmdd_hhmm(desired_datetime):
                    return t("leave_message.invalid_datetime_format", self._language)

                self._pending_datetime = desired_datetime
                self._pending_condition = None
                spoken = _spoken_datetime(desired_datetime, self._language)

                logger.bind(
                    event="LEAVE_MESSAGE_TASK_SET_DESIRED_DATETIME",
                    desired_datetime=desired_datetime,
                ).debug("set_desired_datetime")
                return _speak_timing_confirmation(
                    ctx,
                    t(
                        "leave_message.datetime_set_confirm",
                        self._language,
                        datetime=spoken,
                    ),
                )

            @function_tool
            async def set_desired_condition(
                ctx: RunContext_T,
                desired_condition: str,
            ) -> Optional[str]:
                """Store a caller-provided flexible appointment condition.

                Use this instead of forcing an exact date/hour when the caller clearly
                wants the request relayed as a condition, such as "tomorrow morning",
                "any weekday afternoon", or "whenever advisor Kim is available".
                Preserve the caller's meaning and never add a date or hour they did
                not provide. This does not end the task. On success the tool itself
                speaks the confirmation question — do NOT repeat or paraphrase it;
                wait for the caller's answer. If they confirm, call
                confirm_desired_condition, then submit_booking_contact in the same
                turn once any required call-specific details have been collected.
                Do not ask for an optional additional message.

                Args:
                    desired_condition: A concise natural-language condition grounded
                        in the caller's words.
                """
                condition = " ".join(desired_condition.split())
                if not condition:
                    return t("leave_message.condition_not_set", self._language)

                self._pending_datetime = None
                self._pending_condition = condition
                logger.bind(
                    event="LEAVE_MESSAGE_TASK_SET_DESIRED_CONDITION",
                    desired_condition=condition,
                ).debug("set_desired_condition")
                return _speak_timing_confirmation(
                    ctx,
                    t(
                        "leave_message.condition_set_confirm",
                        self._language,
                        condition=condition,
                    ),
                )

            @function_tool
            async def confirm_desired_condition(ctx: RunContext_T) -> str:
                """Confirm the current flexible appointment condition.

                Use only after the caller explicitly confirms the condition stored
                by set_desired_condition, or BOOKING_CONTEXT.time_condition when no
                newer date/time or condition has been set. Preserve the current
                request without inventing a concrete date. Its return value is an
                internal instruction: do not read it aloud. Call submit_booking_contact
                in the same turn once any required call-specific details have been
                collected, without asking for an optional additional message. Do not
                use this to confirm a concrete date/time.
                """
                condition = self._pending_condition
                if (
                    not condition
                    and not self._pending_datetime
                    and self.booking_context
                ):
                    condition = " ".join(
                        (self.booking_context.time_condition or "").split()
                    )
                if not condition:
                    return t("leave_message.condition_not_set", self._language)
                self._pending_condition = condition
                return t("leave_message.condition_confirm", self._language)

            @function_tool
            async def submit_booking_contact(
                ctx: RunContext_T,
                message: Optional[str] = None,
            ) -> Optional[str]:
                """
                STEP 3 (final) of booking contact collection. Complete the task with the
                date/time or flexible condition captured by the matching setter and an
                OPTIONAL message. Call this ONLY after the user has CONFIRMED that value
                and any required call-specific details have been collected. Submit in
                the same turn as confirmation without asking for an optional message.
                If the user rejected or corrected it, call the matching setter again
                and wait for confirmation first.

                Args:
                    message: OPTIONAL. Any extra request or note the user wants to relay to the
                        representative(상담원) beyond the desired date/time. Leave as None (omit) if
                        no extra content was volunteered. Never ask for optional content. Include
                        any required call-specific details already collected.
                """
                # set_desired_datetime 을 건너뛰고 호출된 경우 완료하지 않고 날짜부터 요청.
                if not self._pending_datetime and not self._pending_condition:
                    return t("leave_message.datetime_not_set", self._language)

                # message 는 선택 사항. 비어 있으면 intent 라벨(request_label)로 폴백해
                # 상담원에게 남는 기록이 비지 않도록 한다. (booking_create/modify 경로에서는
                # request_label 이 항상 존재한다.)
                resolved_message = (
                    (message or "").strip()
                    or _booking_context_summary(self.booking_context)
                    or self.request_label
                )

                logger.bind(
                    event="LEAVE_MESSAGE_TASK_SUBMIT_BOOKING_CONTACT",
                    desired_datetime=self._pending_datetime,
                    desired_condition=self._pending_condition,
                    message=resolved_message,
                ).debug("submit_booking_contact")
                result = TransferFailureResult(
                    scenario="booking_contact_info_collected",
                    desired_datetime=self._pending_datetime,
                    desired_condition=self._pending_condition,
                    message=resolved_message,
                )
                await self._play_closing_message(ctx)
                self._complete_if_pending(result)
                return None

            tools = [
                set_desired_datetime,
                set_desired_condition,
                confirm_desired_condition,
                submit_booking_contact,
            ]
        else:

            @function_tool
            async def collect_leave_message(ctx: RunContext_T, message: str) -> None:
                """
                Collect the message to relay to the representative(상담원) and complete the task.

                Call this ONLY after the user has provided or confirmed what to relay, or
                clearly declined to leave anything.
                - If the user's intent is already clear from context (e.g. booking change),
                  summarize it concisely (e.g. "예약 변경 요청입니다.").
                - If the user provided additional details, include them verbatim.
                - If the user clearly declined, summarize that there is nothing additional
                  to relay.
                - Never call this with an empty string or a placeholder.

                Do NOT call this for filler, an incomplete or context-dependent reply,
                or a question about why a live connection is not continuing. Respond
                naturally to the caller's latest utterance, answer process confusion
                first when needed, and wait for meaningful content or a clear refusal.

                Args:
                    message: The complete message to relay to the representative(상담원).
                             Must be a non-empty sentence summarising the user's request
                             or explicit refusal.
                """

                ctx.disallow_interruptions()

                logger.bind(
                    event="LEAVE_MESSAGE_TASK_COLLECT_LEAVE_MESSAGE",
                    message=message,
                ).debug("collect_leave_message")
                result = TransferFailureResult(
                    scenario="leave_message_collected",
                    message=message,
                )
                await self._play_closing_message(ctx)
                self._complete_if_pending(result)

            tools = [collect_leave_message]

        logger.bind(
            event="LEAVE_MESSAGE_TASK_INITIALIZED",
            is_after_transfer_failure=is_after_transfer_failure,
            entry_reason=self.entry_reason,
            transfer_purpose_source=self.purpose_source,
            action_key=action_key,
            collect_desired_datetime=collect_desired_datetime,
            request_label=self.request_label,
            progress_notice=self.progress_notice,
            opening_uses_generate_reply=bool(self.opening_extra),
        ).debug("Leave message task initialized")

        super().__init__(
            instructions=instructions,
            chat_ctx=chat_ctx,
            tools=tools,
        )

    def _complete_if_pending(self, result: TransferFailureResult) -> None:
        """Complete unless the framework already settled the task.

        LiveKit cancels a task by completing it first, so a tool finishing its
        closing playout after a hang-up must not complete it a second time.
        """
        try:
            self.complete(result)
        except RuntimeError as e:
            logger.bind(
                event="LEAVE_MESSAGE_TASK_ALREADY_DONE",
                scenario=result.scenario,
            ).opt(exception=e).debug("Leave message task already completed")

    async def _play_closing_message(self, ctx: RunContext_T) -> None:
        """완료 문구를 끝까지 재생한 뒤 Task 완료를 허용한다."""
        closing_key = (
            "leave_message.collected_cancel"
            if self.action_key == "booking_cancel"
            else "leave_message.collected_default"
            if self.collect_desired_datetime
            else "leave_message.collected_message_only"
        )
        try:
            closing_handle = ctx.session.say(
                t(closing_key, self._language),
                allow_interruptions=False,
            )
            await closing_handle.wait_for_playout()
        except Exception as e:
            logger.bind(
                event="LEAVE_MESSAGE_TASK_CLOSING",
                stage="playout_error",
                action_key=self.action_key,
            ).opt(exception=e).error("Error waiting for leave-message closing playout")

    async def on_enter(self) -> None:
        """Task 진입 시 첫 응답 생성."""

        self.session.input.set_audio_enabled(True)

        # 첫 턴은 오프너 발화 + (설정 시) 추가 정보 질문까지만 하고 사용자 답을 기다린다.
        #
        # 추가 질문이 없으면 예전처럼 say() 로 오프너를 확정 발화한다. generate_reply 는
        # 모델을 한 번 거치므로 첫 발화 지연이 붙고, 고지성 오프너("…상담원 확인을 통해
        # 진행됩니다")의 verbatim 보장이 모델 준수로 내려간다. 추가 질문을 설정하지 않은
        # 통화까지 그 비용을 물릴 이유가 없다.
        #
        # 추가 질문이 있을 때만 generate_reply 로 올린다. 고정 문자열로는 설정으로 주입한
        # 질문을 오프너와 한 문장으로 합쳐 물을 수 없기 때문이다.
        # IMPORTANT: 이때 tool_choice="none" 은 필수다. 없으면 모델이 이 턴에 바로
        # collect_leave_message 를 호출해 사용자가 말하기도 전에 통화가 끝난다.
        if not self.opening_extra:
            self.session.say(self.opening_line, allow_interruptions=False)
            return

        self.session.generate_reply(
            instructions=self.opening_instructions,
            tool_choice="none",
            allow_interruptions=False,
        )
