"""다이나믹 예약 접수 태스크 — 경계 하네스 설계.

대화는 LLM 소유, 코드는 사실(주입·조회)과 트랜잭션(스테이징·커밋)만 소유한다.
주석의 [D1]~[D15] 표기는 삭제된 설계 문서의 결정 항목이다:
`git show 0a1af1e^:docs/dynamic-booking-agent3-claude/2026-07-15-v3-design.md`.

진입점은 두 곳:
- SingleAgent용 툴 tools/dynamic_booking_tools.py (create_new_appointment)
- 실험 셸 agents/dynamic_booking_agent.py (agent-cli 의 dynamic_booking_agent)
로그는 event="DYNAMIC_BOOKING_TASK".

순수 헬퍼는 tasks/booking/dynamic_*.py 에 있고 이 모듈이 다시 내보낸다.
now_kst()·get_site_available_schedules 를 부르는 코드는 테스트가 이 모듈
경로로 patch 하므로 여기에 둔다.

서비스/상담사 참조 데이터는 태스크가 조회하지 않는다 — 호출자가
load_booking_reference()로 조회해 주입한다(통화 내 캐싱·재사용 가능).
종료 멘트(완료/취소)는 호출자 소유다. 태스크는 전체 예약 중단 확인 질문만
직접 발화하고, complete(result)로 종료 상태를 반환한다.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import uuid
from collections.abc import AsyncIterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, NamedTuple, cast
from zoneinfo import ZoneInfo

import yaml
from livekit.agents import (
    Agent,
    AgentTask,
    StopResponse,
    function_tool,
    llm,
)
from livekit.agents.voice import ModelSettings
from loguru import logger

from data import (
    BookingMemoContext,
    CallSessionData,
    DynamicBookingPending,
    DynamicBookingResumeContext,
    RunContext_T,
)
from external_apis.booking_api import get_site_available_schedules
from tasks.booking.dynamic_dates import (
    _DATE_NEGATION_RE,
    WEEKDAY_KO,
    CallerDateHint,
    _is_whole_day,
    _minutes_apart,
    _spoken_clock,
    _spoken_condition_text,
    build_calendar_block,
    caller_date_note,
    resolve_caller_dates,
    spoken_date,
    spoken_date_range,
    spoken_datetime,
)
from tasks.booking.dynamic_dialogue import (
    _has_condition_bearing_user_utterance,
    _has_prior_dialogue,
)
from tasks.booking.dynamic_reference import (
    BookingReference,
    _service_display_mode,
    _service_free_utterance,
    advisor_is_eligible,
    advisor_service_ids,
    is_operator_selection_enabled,
)
from tasks.booking.dynamic_reference import (
    load_booking_reference as load_booking_reference,
)
from tasks.booking.dynamic_slots import (
    MAX_SCHEDULE_OPTIONS_TO_PRESENT,
    add_earliest_only_truncation,
    add_latest_time_comparison,
    add_range_adjustment,
    add_time_constraint_facts,
    apply_strictly_after,
    error_result,
    filter_time_constraints,
    has_nearby_time_offer,
    limit_schedule_options,
    normalize_day_rows,
    normalize_slot_datetime,
    searched_time_window,
    validate_time_filters,
)
from tasks.booking.dynamic_types import (
    CANCEL_ANNOUNCEMENT as CANCEL_ANNOUNCEMENT,
)
from tasks.booking.dynamic_types import (
    DYNAMIC_BOOKING_EXIT_REASON_CODES,
    BookingApiError,
    DynamicBookingExitReasonCode,
    DynamicBookingResult,
    DynamicBookingToolExitReasonCode,
    EarliestOffer,
    NullableBookingRouteTarget,
    NullableToolBool,
    NullableToolInt,
    NullableToolStr,
    StagedBooking,
    _log_tool_io,
    _normalize_nullable_tool_value,
    normalize_optional_tool_text,
)
from tasks.booking.dynamic_types import (
    NO_PROGRESS_ANNOUNCEMENT as NO_PROGRESS_ANNOUNCEMENT,
)
from tasks.booking.dynamic_types import (
    REFERENCE_LOAD_FAILED_ANNOUNCEMENT as REFERENCE_LOAD_FAILED_ANNOUNCEMENT,
)
from tasks.booking.dynamic_types import (
    BookingOutcome as BookingOutcome,
)
from tasks.booking.exit_reasons import (
    normalize_exit_reason_code,
    route_target_for_exit,
)
from tasks.booking.handoff import (
    Blocker,
    HandoffOffer,
    HandoffReasonCode,
    NoMatchingSlotBlocker,
    ObstacleSubject,
    OfferReply,
    ServiceScopeBlocker,
    advisor_name_match,
    blocker_facts,
    normalize_label,
    offer_basis,
    reason_code_for,
)
from utils.agent_event import agent_event_extra, record_api_result
from utils.i18n import t
from utils.korean_speech_guard import guard_korean_speech

KST = ZoneInfo("Asia/Seoul")
SLOT_PAGE_SIZE = 100  # 응답 완전성 단정의 근거 [D4]
SCHEDULE_FETCH_CONCURRENCY = 5  # 범위 검증 시 일정 조회 동시성 상한
TIME_FILTER_SEARCH_DAYS = 7  # Return a bounded result before extending the search.
NEARBY_SLOT_WINDOW_MINUTES = 180
# No-slot help is offered after the caller declined this many alternatives
# (rejected candidates and re-searches after a no-match result). Exceptions that
# offer at once: a fixed date/time/advisor (report_booking_obstacle) and an empty
# search through the booking horizon. Plan:
# docs/plans/2026-09-29-booking-handoff-final-plan.md (revision v3).
DECLINED_ALTERNATIVES_BEFORE_OFFER = 3
TODAY_LEAD_MINUTES = 0  # 오늘 검색 시 now+lead 이전 슬롯 제외 [D10]
# 무진행(no-progress) 가드 임계값 — 실통화 2026-08-28(42분·336턴) 재발 방지.
#
# 그 통화에서 사용자 발화는 저신뢰 추임새와 무관한 제3자 대화 조각뿐이었고
# booking 툴은 한 번도 호출되지 않았다. 프롬프트 규칙(반복 금지·되묻기)도
# exit_task 도 발동하지 않았으며, 무음 워치독(USER_RESPONSE_DELAY)은 저신뢰
# 발화가 타이머를 리셋해 42분 뒤에야 동작했다. 발동이 보장되어야 하는
# 안전장치이므로 카운팅과 개입은 프롬프트가 아니라 코드가 소유한다.
#
# 연속 무진행 턴 수는 사용자 턴마다 1씩 늘고, booking 툴 호출(조회·스테이징·
# 커밋 시도)마다 0으로 리셋된다.
#
# 초기값 5/10 은 실통화 테스트(2026-08-31)에서 오탐 종료를 냈다 — 정상 예약
# 협의도 서비스 항목 선택이나 달력 블록 기반 날짜 문답("11월 되나요?",
# "언제까지 되는데?")처럼 툴 없이 답하는 구간이 이어지면 10턴을 채운다.
# 그 통화는 "오전이요"까지 순조롭게 진행 중이었는데 종료됐다. 병리적
# 통화(2026-08-28)는 336턴이었으므로 20/30 도 충분히 이르게 끊는다.
NO_PROGRESS_SOFT_TURNS = 20  # 예/아니오 강제 질문 지시문 주입
NO_PROGRESS_HARD_TURNS = 30  # LLM 응답 없이 태스크 종료(exit 사유 no_progress)


# The prompt YAML owns the speech/instruction text (English instructions under
# the `en` key); this module owns branch selection and injected values. It does
# not use utils.common.load_prompt because that loader silently falls back to
# another key on a missing key and to the unformatted text on a placeholder
# mismatch. This prompt carries booking safeguards (grounding, confirmation), so
# a silently degraded prompt is riskier than failing fast. Rendered output is
# pinned by the goldens in
# tests/characterization/test_dynamic_booking_prompt_rendering.py.
_PROMPT_FILE = (
    Path(__file__).resolve().parent.parent / "prompts" / "dynamic_booking_prompt.yaml"
)


@lru_cache(maxsize=1)
def _prompt_sections() -> dict[str, dict[str, str]]:
    with open(_PROMPT_FILE, encoding="utf-8") as file:
        return yaml.safe_load(file)


def _prompt(lang: str, key: str, **kwargs: object) -> str:
    """Strict render: a missing language/key or placeholder mismatch raises KeyError."""
    return _prompt_sections()[lang][key].format(**kwargs)


def _prompt_language(call_session_data: CallSessionData) -> tuple[str, str]:
    """Return the English template language and the configured response language.

    Instructions are English; {default_language} sets the language spoken to the
    caller (Korean when there is no call config). Quoted Korean examples in the
    templates are rendered naturally in that call language.
    """
    call_config = getattr(call_session_data, "call_config_data", None)
    default_language = (
        call_config.get_language_prompt_string() if call_config else "한국어"
    )
    return "en", default_language


def now_kst() -> dt.datetime:
    return dt.datetime.now(KST)


@dataclass(frozen=True)
class _SearchRequest:
    """One validated search_schedules call as the search strategies see it.

    end_date is the applied end (horizon clamp / time-filter window);
    requested_end_date is what the model asked for.
    """

    start_date: str
    end_date: str
    requested_end_date: str
    service_id: int | None
    operator_id: int | None
    time_from: str | None
    time_to: str | None
    target_time: str | None
    strictly_after: str | None
    mode: Literal["list", "earliest", "earliest_for_booking"]
    user_message_id: str | None
    moved_on: bool
    range_adjustment_reason: str

    @property
    def earliest_only(self) -> bool:
        return self.mode != "list"

    @property
    def offer_earliest(self) -> bool:
        return self.mode == "earliest_for_booking"

    @property
    def has_time_filter(self) -> bool:
        return any(
            value is not None
            for value in (self.time_from, self.time_to, self.target_time)
        )

    @property
    def filters(self) -> dict[str, str | None]:
        """Keyword arguments for DynamicBookingTask._filter_search_constraints."""
        return {
            "time_from": self.time_from,
            "time_to": self.time_to,
            "target_time": self.target_time,
            "strictly_after": self.strictly_after,
        }


class _SearchOutcome(NamedTuple):
    """A search strategy's payload plus what _finish_search needs to close it."""

    result: dict[str, Any]
    total_slot_count: int | None = None
    checked_end_date: str | None = None
    other_advisor_hint: tuple[str, bool] | None = None


class DynamicBookingTask(AgentTask[DynamicBookingResult]):
    """Create-booking task with grounded candidate and consent transitions."""

    _pending_earliest_offer: EarliestOffer | None = None
    # 연속 무진행 사용자 턴 수. 검증 로직만 돌리는 테스트가 __init__ 을
    # 우회하므로 클래스 기본값으로 둔다. [on_user_turn_completed]
    _no_progress_turns: int = 0
    # Caller message ids whose date expressions were already noted / checked
    # against the first search_schedules call. Keyed by message id, not by turn
    # hook, so the logic holds for every input path. [llm_node,
    # _apply_caller_date_hint]
    _date_note_logged_message_id: str | None = None
    _date_hint_checked_message_id: str | None = None
    # Handoff controller state. Refusal and "offer delivered" are session-owned
    # (DynamicBookingResumeContext); these fields only cover the live task.
    _safety_issue_evidence: dict[str, str] | None = None
    _offer_pending: HandoffOffer | None = None
    _request_revision: int = 0
    _schedule_blocker: NoMatchingSlotBlocker | None = None
    # Options presented by the last booking search: slot datetimes or dates
    # (empty: nothing found). A next search that covers none of them counts as
    # a declined alternative.
    _last_presented: frozenset[str] | None = None
    _declined_turn_id: str | None = None
    _resumed_after_cancel: bool = False
    # Avoid repeating name clarification for an off-list advisor report.
    _advisor_confirm_asked: frozenset[str] = frozenset()
    _searched_in_task: bool = False
    _entry_has_user_request: bool = False
    _pending_empty_search: dict[str, Any] | None = None
    _pending_empty_search_user_message_id: str | None = None
    _last_search_conditions: dict[str, Any] | None = None
    _empty_searched_ranges: list[str] | None = None
    _rejected_candidates: list[dict[str, Any]] | None = None
    _last_candidate: StagedBooking | None = None
    _cancel_question: str | None = None
    _cancel_boundary_ids: frozenset[str] = frozenset()
    # Template language key and the {default_language} value. Class defaults exist
    # for tests that bypass __init__. __init__ keeps the English template and takes
    # the spoken response language from the call config. [_prompt_language]
    _prompt_lang: str = "en"
    _default_language: str = "한국어"
    _center_context: str = ""

    def __init__(
        self,
        *,
        call_session_data: CallSessionData,
        reference: BookingReference,
        center_context: str = "",
        chat_ctx=None,
    ) -> None:
        self._call_session_data = call_session_data
        self._center_context = center_context.strip()
        self._services = reference.services
        self._advisors = reference.advisors
        self._service_reservation_setting = reference.service_reservation_setting
        self._advisors_by_id = {advisor["id"]: advisor for advisor in self._advisors}
        self._select_operator = is_operator_selection_enabled(call_session_data)
        self._prompt_lang, self._default_language = _prompt_language(call_session_data)
        self._schedule_lookup_days = (
            call_session_data.workflow_config.booking.schedule_lookup_days
        )
        # 날짜 -> 좁히지 않은 원본 행. 통화 단위 캐시. [_fetch_day]
        self._day_rows_cache: dict[str, list[Any]] = {}
        # (시작일, 종료일) -> 좁히지 않은 원본 행. 통화 단위 캐시.
        self._date_rows_cache: dict[tuple[str, str], list[Any]] = {}
        # `stage_booking` may use the earliest-offer wording only for the exact
        # candidate produced by the immediately preceding grounded search.
        self._pending_earliest_offer: EarliestOffer | None = None
        pending = getattr(call_session_data, "dynamic_booking_pending", None)
        self._staged: StagedBooking | None = None
        self._staged_requires_revalidation = False
        restored = self._staged_from_pending(pending)
        self._advisor_confirm_asked = frozenset()
        self._set_staged(
            restored,
            requires_revalidation=restored is not None,
        )
        self._mutation_lock = asyncio.Lock()
        self._no_progress_turns = 0
        self._safety_issue_evidence = None
        self._offer_pending = None
        self._request_revision = 0
        self._schedule_blocker = None
        self._last_presented = None
        self._declined_turn_id = None
        resume = getattr(call_session_data, "dynamic_booking_resume", None)
        self._resumed_after_cancel = bool(
            resume is not None and resume.last_exit_reason == "cancel"
        )
        self._searched_in_task = False
        self._last_search_conditions = (
            dict(resume.search_conditions) if resume is not None else None
        )
        self._rejected_candidates = (
            [dict(item) for item in resume.rejected_candidates]
            if resume is not None
            else []
        )
        self._last_candidate = None
        self._reset_cancel_confirmation()
        self._empty_searched_ranges = []
        # 인사말 분기는 코드가 정한다 — 근거는 _has_prior_dialogue 참고.
        self._entry_has_prior_dialogue = _has_prior_dialogue(chat_ctx)
        # 인사말 생략 여부와 조회 지시 여부는 별개 사실이다.
        # 근거는 _has_condition_bearing_user_utterance 참고.
        self._entry_has_user_request = _has_condition_bearing_user_utterance(chat_ctx)
        super().__init__(
            instructions=self._build_instructions(),
            chat_ctx=chat_ctx,
        )

    async def on_enter(self) -> None:
        self._record_task_entry()
        if (
            self._entry_greeting_branch() == "continuation_no_request"
            and len(self._services) > 1
        ):
            # 본인 확인 직후에는 고객 조건이 없어 LLM 생성 대신 고정 문구로 묻는다.
            call_config = getattr(self._call_session_data, "call_config_data", None)
            lang = getattr(call_config, "language", None)
            self.session.say(
                t("dynamic_booking.service_request", lang), add_to_chat_ctx=True
            )
        else:
            self.session.generate_reply(
                instructions=self._build_greeting_instructions(),
                allow_interruptions=True,
            )

    # ---------- 무진행(no-progress) 가드 ----------

    def _note_progress(self) -> None:
        """booking 툴 호출 = 예약 조건이 담긴 대화가 진행 중이라는 관측 사실.

        인자 검증에 실패한 호출도 리셋한다 — 조건을 뽑아 툴을 불렀다는 것
        자체가 무의미 발화 루프가 아니라는 증거다.
        """
        self._no_progress_turns = 0

    @property
    def _spoken_lang(self) -> str:
        """Language of the `spoken` date/time facts and the calendar block.

        Always "ko", independent of the (English) template language and of the
        call language: the caller-phrase calendar is keyed on Korean phrases and
        `spoken` facts are read verbatim. Whether en-US calls should get English
        facts is an open product decision, so it is named here instead of changed.
        """
        return "ko"

    def _prompt_text(self, key: str, **kwargs: object) -> str:
        """Render the selected template, filling {default_language} with the response language."""
        return _prompt(
            self._prompt_lang, key, default_language=self._default_language, **kwargs
        )

    def _no_progress_instruction(self) -> str:
        """SOFT 구간 주입 지시문 — 행동 지시만 담고 상황 설명은 담지 않는다.

        초기 버전은 "통화 상태가 나쁠 수 있다"는 배경 설명과 종료 예고를 매 턴
        지시했는데, 실통화 테스트(2026-08-31)에서 LLM 이 그 배경 설명을
        "통화가 잘 안 들리시면 다시 말씀해 주세요" 같은 발화로 그대로 읽어
        정상 진행 중인 고객에게 반복 안내했다. 그래서 통화 품질 언급은
        금지 항목으로 뒤집고, 종료 예고는 HARD 직전 두 턴에서만 허용한다.
        """
        base = self._prompt_text("no_progress_base", turns=self._no_progress_turns)
        if self._no_progress_turns >= NO_PROGRESS_HARD_TURNS - 2:
            return base + " " + self._prompt_text("no_progress_hard_suffix")
        return base

    async def on_user_turn_completed(self, turn_ctx: Any, new_message: Any) -> None:
        """코드 소유 무진행 카운터 — LLM 응답 생성 직전에 호출된다.

        판정을 LLM 에 맡기지 않는 근거는 NO_PROGRESS_SOFT_TURNS 주석 참고.
        발화 내용은 검사하지 않는다 — "조건을 담은 발화인지" 는 결국 다음
        턴의 툴 호출 여부로 코드가 관측하며, 내용 휴리스틱은 무관한 대화
        조각(예: 스피커폰 너머 제3자 대화)을 진행으로 오인한다.

        HARD 임계값의 마지막 턴이 우연히 유효한 답일 가능성은 감수한다 —
        그 앞의 모든 턴에서 툴 호출이 없던 통화에서 그 확률은 낮고, 종료 안내 후
        재발신이라는 복구 경로가 있다.
        """
        if self.done():
            return
        self._pending_empty_search = None
        self._no_progress_turns += 1
        turns = self._no_progress_turns
        if turns >= NO_PROGRESS_HARD_TURNS:
            logger.bind(
                event="DYNAMIC_BOOKING_TASK",
                stage="no_progress_hard_exit",
                turns=turns,
                room_name=self._call_session_data.room_name,
            ).warning("No-progress guard is ending the booking task")
            # 안내 발화·이벤트 기록·통화 종료는 호출자 소유(경계 하네스).
            self.complete(
                DynamicBookingResult(
                    outcome="exited",
                    exit_reason_code="no_progress",
                    detail=f"연속 {turns}턴 동안 예약 진행 없음",
                )
            )
            raise StopResponse
        if turns >= NO_PROGRESS_SOFT_TURNS:
            logger.bind(
                event="DYNAMIC_BOOKING_TASK",
                stage="no_progress_soft_intervention",
                turns=turns,
                room_name=self._call_session_data.room_name,
            ).info("No-progress guard is forcing a yes/no question")
            turn_ctx.add_message(role="system", content=self._no_progress_instruction())

    def _date_horizon(self, today: dt.date | None = None) -> dt.date:
        """The last bookable date: today (KST unless given) plus the lookup window."""
        if today is None:
            today = now_kst().date()
        # Tests that bypass __init__ (no-progress guard) have no lookup window.
        days = getattr(self, "_schedule_lookup_days", 30)
        return today + dt.timedelta(days=days)

    def _caller_date_hints(
        self, chat_ctx: llm.ChatContext | None = None
    ) -> tuple[Any | None, list[CallerDateHint]]:
        """The latest caller message and the date expressions code resolved in it."""
        if chat_ctx is None:
            items = self._dialogue_items()
        else:
            items = [
                item
                for item in chat_ctx.items
                if getattr(item, "role", None) == "user"
                and (getattr(item, "text_content", None) or "").strip()
            ]
        latest = next((item for item in reversed(items) if item.role == "user"), None)
        if latest is None:
            return None, []
        today = now_kst().date()
        hints = resolve_caller_dates(
            latest.text_content, today, self._date_horizon(today)
        )
        return latest, hints

    def _with_caller_date_note(self, chat_ctx: llm.ChatContext) -> llm.ChatContext:
        """Copy of chat_ctx with a [날짜 해석] line right after the latest caller message.

        The note lives only in this inference's context, never in the durable
        history, and sits where a small model follows it far more reliably than
        a calendar table at the top of the system prompt.
        """
        latest, hints = self._caller_date_hints(chat_ctx)
        if latest is None:
            return chat_ctx
        today = now_kst().date()
        note = caller_date_note(hints, self._date_horizon(today))
        if note is None:
            return chat_ctx
        note += self._partial_search_reminder(hints)
        if self._date_note_logged_message_id != latest.id:
            self._date_note_logged_message_id = latest.id
            logger.bind(
                event="DYNAMIC_BOOKING_TASK",
                stage="caller_date_note",
                hints=[(hint.phrase, hint.date, hint.status) for hint in hints],
                room_name=self._call_session_data.room_name,
            ).info("Resolved caller date expressions for this turn")
        noted = chat_ctx.copy()
        noted.insert(
            llm.ChatMessage(
                role="system",
                content=[note],
                created_at=latest.created_at + 1e-3,
            )
        )
        return noted

    def _partial_search_reminder(self, hints: list[CallerDateHint]) -> str:
        """Say so when the only search of the named day covered a time window.

        Gemma replays (2026-10-16): after "내일 오전" came back empty, "내일은
        가능한 시간이 언제에요?" was answered by repeating the morning result in
        4/4 runs instead of searching the whole day. The code knows the last
        search was partial, so it states that next to the date.
        """
        last = self._last_search_conditions or {}
        if len(hints) != 1 or hints[0].status != "ok":
            return ""
        day = hints[0].date
        if not (last.get("start_date") == day and last.get("end_date") == day):
            return ""
        window = self._searched_time_window(
            last.get("time_from"), last.get("time_to"), last.get("target_time")
        )
        if window is None:
            return ""
        return (
            f" 이 날짜는 {window['from']}~{window['to']}만 조회했습니다. 하루 전체나 "
            "다른 시간대를 물으면 시간 조건 없이 다시 조회한 뒤 답하세요."
        )

    async def llm_node(
        self,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        model_settings: ModelSettings,
    ) -> AsyncIterable[llm.ChatChunk | str]:
        """Ground caller dates before every inference, whatever the input path.

        on_user_turn_completed is not enough: text input (console, chat) goes
        through generate_reply(user_input=...) and skips that hook, which is why
        the first text-sim replays (2026-10-16) showed no note at all. llm_node
        runs for every generation of this task.
        """
        async for chunk in guard_korean_speech(
            self,
            Agent.default.llm_node(
                self, self._with_caller_date_note(chat_ctx), tools, model_settings
            ),
        ):
            yield chunk

    def _apply_caller_date_hint(
        self, start_date: str, end_date: str
    ) -> tuple[str, str, dict[str, Any] | None]:
        """Correct the turn's first one-day search to the date the caller named.

        Deterministic replacement instead of an error round trip: a silent
        retry costs another LLM call (1-2 s of voice latency) and trusts the
        same model again. Narrow by design: exactly one resolved expression in
        the utterance, a one-day search on a different date, no negation
        ("월요일 말고"), and only the first search after each caller message, so
        a follow-up search after an empty result is never rewritten.
        """
        latest, hints = self._caller_date_hints()
        if latest is None or latest.id == self._date_hint_checked_message_id:
            return start_date, end_date, None
        self._date_hint_checked_message_id = latest.id
        if len(hints) != 1 or hints[0].status != "ok":
            return start_date, end_date, None
        hint = hints[0]
        log = logger.bind(
            event="DYNAMIC_BOOKING_TASK",
            stage="caller_date_hint",
            phrase=hint.phrase,
            expected=hint.date,
            searched=f"{start_date}~{end_date}",
            room_name=self._call_session_data.room_name,
        )
        if start_date <= hint.date <= end_date:
            log.info("Search covers the date the caller named")
            return start_date, end_date, None
        if start_date != end_date:
            log.info("Range search skips the named date; left untouched")
            return start_date, end_date, None
        if _DATE_NEGATION_RE.search(latest.text_content or ""):
            log.info("Utterance negates the named date; left untouched")
            return start_date, end_date, None
        log.warning("Corrected one-day search to the date the caller named")
        correction = {
            "from": start_date,
            "to": hint.date,
            "phrase": hint.phrase,
            "instruction": (
                f"고객이 말한 '{hint.phrase}'은 {hint.date}({hint.weekday}요일)이라 "
                f"그 날짜로 조회했습니다. {start_date}은 언급하지 말고 이 결과의 "
                "spoken 문구로 안내하세요."
            ),
        }
        return hint.date, hint.date, correction

    def _record_task_entry(self) -> None:
        """이 태스크가 어떤 설정으로 동작했는지를 통화에 남긴다.

        같은 코드라도 `select_operator` 하나로 상담사 응대가 달라지고
        `lookup_days` 로 조회 범위가 달라진다. 통화 기록에 설정이 없으면
        사후 분석이 "왜 상담사를 안 물어봤나" 를 코드에서 재현할 수 없고,
        센터별 설정 차이를 에이전트 결함으로 오진하게 된다.

        주입 규모(서비스·상담사 수)도 함께 남긴다. LLM 이 볼 수 있는 선택지의
        크기가 곧 응대 폭이라, 0 이나 1 이면 되묻기 자체가 성립하지 않는다. [D8]
        """
        self.session.history.add_message(
            role="developer",
            content="",
            extra=agent_event_extra(
                "DYNAMIC_BOOKING_TASK",
                status="ON_ENTER",
                details={
                    "select_operator": self._select_operator,
                    "lookup_days": self._schedule_lookup_days,
                    "service_display_mode": _service_display_mode(
                        self._call_session_data
                    ),
                    "service_free_utterance": _service_free_utterance(
                        self._call_session_data
                    ),
                    "service_count": len(self._services),
                    "advisor_count": len(self._advisors),
                    "resumed_staged": self._staged is not None,
                    # 어느 인사말 지시문이 주입됐는지. 43902 분석은 이 값을
                    # 발화 문구에서 역추정해야 했다.
                    "entry_greeting": self._entry_greeting_branch(),
                },
            ),
        )

    @staticmethod
    def _staged_from_pending(
        pending: DynamicBookingPending | None,
    ) -> StagedBooking | None:
        if pending is None:
            return None
        return StagedBooking(
            service_id=pending.service_id,
            service_name=pending.service_name,
            operator_id=pending.operator_id,
            operator_name=pending.operator_name,
            datetime=pending.datetime,
        )

    def _set_staged(
        self,
        staged: StagedBooking | None,
        *,
        requires_revalidation: bool = False,
    ) -> None:
        self._staged = staged
        self._staged_requires_revalidation = bool(
            staged is not None and requires_revalidation
        )
        # Fresh-consent check: user turns that existed when this
        # candidate was staged. commit_booking accepts consent only from the
        # single user turn that follows the proposal.
        self._stage_user_message_ids = (
            self._user_message_ids() if staged is not None else None
        )
        self._call_session_data.dynamic_booking_pending = (
            DynamicBookingPending(
                service_id=staged.service_id,
                service_name=staged.service_name,
                operator_id=staged.operator_id,
                operator_name=staged.operator_name,
                datetime=staged.datetime,
            )
            if staged is not None
            else None
        )

    def _dialogue_items(self) -> list[Any]:
        chat_ctx = getattr(self, "chat_ctx", None)
        return [
            item
            for item in (chat_ctx.items if chat_ctx is not None else [])
            if getattr(item, "role", None) in {"assistant", "user"}
            and (getattr(item, "text_content", None) or "").strip()
        ]

    def _user_message_ids(self) -> frozenset[str]:
        chat_ctx = getattr(self, "chat_ctx", None)
        return frozenset(
            item.id
            for item in (chat_ctx.items if chat_ctx is not None else [])
            if getattr(item, "role", None) == "user"
        )

    def _consent_turns_since_stage(self) -> int | None:
        """User turns after the latest stage; None when no staging anchor exists."""
        anchor = getattr(self, "_stage_user_message_ids", None)
        if anchor is None:
            return None
        return len(self._user_message_ids() - anchor)

    def _latest_user_message_id(self) -> str | None:
        """The latest user item, including one without text.

        Text generate_reply can commit a user message without the audio hook,
        so this deliberately does not go through `_dialogue_items`.
        """
        chat_ctx = getattr(self, "chat_ctx", None)
        return next(
            (
                item.id
                for item in reversed(chat_ctx.items if chat_ctx is not None else [])
                if getattr(item, "role", None) == "user"
            ),
            None,
        )

    def _caller_quote_evidence(self, quote: str | None) -> dict[str, str] | None:
        """Ground an LLM-selected quote in recent caller text, never assistant text."""
        if quote is None:
            return None

        def normalize(value: str) -> str:
            quote_chars = "\"'\u201c\u201d\u2018\u2019"
            normalized = re.sub(r"\s+", " ", value.strip())
            normalized = normalized.strip(quote_chars)
            normalized = normalized.rstrip(".?!,").rstrip()
            return normalized.strip(quote_chars)

        sentences = [
            normalized
            for piece in re.split(r"[.?!]\s+", quote.strip())
            if (normalized := normalize(piece))
        ]
        if not sentences:
            return None
        recent_users = [item for item in self._dialogue_items() if item.role == "user"][
            -8:
        ]
        normalized_users = [
            (item, normalize(item.text_content)) for item in recent_users
        ]
        last_match = None
        for sentence in sentences:
            last_match = next(
                (item for item, text in reversed(normalized_users) if sentence in text),
                None,
            )
            if last_match is None:
                return None
        return {"quote": quote, "user_message_id": last_match.id}

    def _reset_cancel_confirmation(self) -> None:
        self._cancel_question = None
        self._cancel_boundary_ids = frozenset()

    def _invalidate_candidate(self) -> None:
        candidate = getattr(self, "_staged", None)
        if candidate is not None:
            self._last_candidate = candidate
            self._set_staged(None)

    def _cancel_response_received(self) -> bool:
        """Accept only the fresh answer to the exact, uninterrupted stop question."""
        fresh = [
            item
            for item in self._dialogue_items()
            if item.id not in self._cancel_boundary_ids
        ]
        return bool(
            self._cancel_question
            and len(fresh) == 2
            and fresh[0].role == "assistant"
            and not getattr(fresh[0], "interrupted", False)
            and fresh[0].text_content.strip() == self._cancel_question
            and fresh[1].role == "user"
        )

    async def _request_cancel_confirmation(self) -> None:
        config = getattr(self._call_session_data, "call_config_data", None)
        language = getattr(config, "language", "ko-KR").split("-")[0]
        # Exact localized wording makes delivery verifiable in both text and audio
        # history. Languages without a dedicated question use the English fallback.
        questions = {
            "ko": "신규 예약 신청 자체를 그만 진행할까요?",
            "en": "Would you like to stop this new booking request altogether?",
            "ja": "今回の新規予約の手続きを取りやめますか?",
        }
        self._cancel_question = questions.get(language, questions["en"])
        self._cancel_boundary_ids = frozenset(
            item.id for item in self._dialogue_items()
        )
        # Own this question's speech: an LLM paraphrase may ambiguously ask about
        # canceling an existing reservation and cannot prove whole-request consent.
        await self.session.say(self._cancel_question, allow_interruptions=True)
        raise StopResponse

    def _save_resume_context(self) -> None:
        resume = getattr(self._call_session_data, "dynamic_booking_resume", None)
        if resume is None:
            resume = DynamicBookingResumeContext()
            self._call_session_data.dynamic_booking_resume = resume
        resume.search_conditions = dict(self._last_search_conditions or {})
        resume.rejected_candidates = [
            dict(item) for item in (self._rejected_candidates or [])
        ]
        resume.excluded_dates = sorted(
            {
                item["datetime"][:10]
                for item in resume.rejected_candidates
                if item["scope"] == "date"
            }
        )

    def _candidate_is_rejected(self, when: str) -> bool:
        return any(
            (
                item["datetime"] == when
                if item["scope"] == "slot"
                else item["datetime"][:10] == when[:10]
            )
            and when not in item.get("reconsidered_datetimes", [])
            for item in (self._rejected_candidates or [])
        )

    def _filter_rejected_candidates(
        self, slots: dict[str, list[int]]
    ) -> dict[str, list[int]]:
        return {
            when: ids
            for when, ids in slots.items()
            if not self._candidate_is_rejected(when)
        }

    @function_tool
    @_log_tool_io
    async def reject_candidate(
        self, scope: Literal["slot", "date"] = "slot"
    ) -> dict[str, Any]:
        """Decline the pending candidate without stopping the booking task.

        Use slot for a simple no or a rejected time; date only when the caller
        rejects the whole day. This clears consent but retains service and time
        preferences. Then search with those preferences; use strictly_after only
        for a later/next slot. Never use exit_task(cancel) for a candidate refusal.
        A returned handoff_offer replaces the search: ask it instead.
        """
        async with self._mutation_lock:
            candidate = self._staged or self._last_candidate
            rejected = list(self._rejected_candidates or [])
            if candidate is None and not rejected:
                return self._error("nothing_staged", "There is no candidate to reject.")
            # A resumed task restores exclusions, never a candidate or consent.
            # Repeating the previous rejection must not make it forget appointment.
            when = (
                candidate.datetime
                if candidate is not None
                else rejected[-1]["datetime"]
            )
            matching = [
                item
                for item in rejected
                if item["datetime"][:10] == when[:10]
                and (item["scope"] == "date" or item["datetime"] == when)
            ]
            if any(
                item["scope"] == "date"
                and when not in item.get("reconsidered_datetimes", [])
                for item in matching
            ):
                scope = "date"
            already_rejected = any(
                item["scope"] == scope
                and when not in item.get("reconsidered_datetimes", [])
                for item in matching
            )
            self._note_progress()
            self._reset_cancel_confirmation()
            self._last_candidate = candidate
            self._pending_earliest_offer = None
            self._set_staged(None)
            if not already_rejected:
                rejected.append(
                    {
                        "datetime": when,
                        "scope": scope,
                        "user_message_id": self._latest_user_message_id(),
                        "reconsidered_datetimes": [],
                    }
                )
            self._rejected_candidates = rejected
            conditions = dict(self._last_search_conditions or {})
            if scope == "slot" and conditions.get("target_time") == when[11:16]:
                conditions.pop("target_time")
            self._last_search_conditions = conditions
            if not already_rejected:
                self._count_declined_alternative()
            self._save_resume_context()
            result = {
                "status": "already_rejected"
                if already_rejected
                else "candidate_rejected",
                "scope": scope,
                "datetime": when,
                "service": self._resume_service(),
                "search_conditions": dict(self._last_search_conditions or {}),
                "required_next_tool": "search_schedules",
                # "다른 시간은요?" 는 "다음 시간" 이 아니다. 거절 슬롯은 검색에서
                # 자동 제외되므로 같은 조건 재조회가 곧 "나머지 시간" 이다. 선호
                # 질문을 먼저 하면 조회 없이 되묻고, strictly_after 를 쓰면 "3시
                # 이후엔 없다" 처럼 고객이 묻지 않은 범위로 좁혀진다.
                "instruction": (
                    "Keep this booking task active and search again in this same "
                    "response. The saved service is already chosen; do not ask "
                    "it again. Keep the date and broader time_from/time_to "
                    "preferences, apply any new condition, and OMIT the declined "
                    "exact target_time. Declined slots are excluded automatically. "
                    "Use strictly_after only when the caller asks for a later or "
                    "next time; for another time without a new condition, search "
                    "the same date without strictly_after. Do not ask a preference "
                    "question before searching. Stage a new candidate before "
                    "asking for fresh consent."
                ),
            }
            if self._safety_offer_available():
                self._attach_offer(result, None, "staff_connection")
            elif self._alternatives_exhausted():
                self._attach_offer(result, self._schedule_blocker, "no_slot_memo")
        return result

    @function_tool
    @_log_tool_io
    async def record_caller_evidence(
        self,
        kind: Literal["safety_issue", "safety_issue_withdrawn", "handoff_refusal"],
        quote: str,
    ) -> dict[str, Any]:
        """Record the caller's own words that justify a staff offer or a refusal.

        Call before the next search, only when the caller actually said it. Copy one
        short phrase verbatim from a recent caller turn; never assistant text, a
        summary, a service name, or a third-party quote.
        safety_issue: the caller actually described a device safety issue
        (battery swelling, overheating, smoke, burning); an ordinary fault such as a
        cracked screen is NOT evidence. safety_issue_withdrawn: the caller denies or
        retracts that safety issue. handoff_refusal: an
        explicit refusal of staff/memo help, even before any offer.
        """
        async with self._mutation_lock:
            self._note_progress()
            # A null-sentinel quote and an ungrounded one get the same error.
            evidence = self._caller_quote_evidence(
                self._normalize_optional_tool_text(quote)
            )
            if evidence is None:
                return self._error(
                    "ungrounded_caller_evidence",
                    "Copy evidence verbatim from a recent USER message, never from "
                    "assistant/tool text or an invented summary. Omit unsupported quotes "
                    "and retry the same search. This invalid evidence does not change "
                    "handoff state.",
                    retry="silent",
                )
            if kind == "safety_issue":
                self._safety_issue_evidence = evidence
            if kind == "safety_issue_withdrawn":
                self._safety_issue_evidence = None
            result: dict[str, Any] = {
                "status": "recorded",
                "kind": kind,
                "evidence": evidence,
            }
            if kind == "handoff_refusal":
                self._record_handoff_refusal(evidence)
                if (offer := self._offer_pending) is not None:
                    # A refusal recorded in reply to the open offer declines it.
                    self._offer_pending = None
                    self._declined_offer(offer, result)
            self._save_resume_context()
            return result

    @function_tool
    @_log_tool_io
    async def reconsider_candidate(self, datetime: str) -> dict[str, Any]:
        """Reopen one previously rejected slot the caller explicitly reselected in a
        later turn, then search or stage it. Never use it to fill empty results or
        to reopen a whole rejected day.
        """
        async with self._mutation_lock:
            self._note_progress()
            reconsidered = normalize_slot_datetime(datetime)
            user_message_id = self._latest_user_message_id()
            relevant = [
                item
                for item in (self._rejected_candidates or [])
                if item["datetime"] == reconsidered
                or (
                    item["scope"] == "date"
                    and reconsidered is not None
                    and item["datetime"][:10] == reconsidered[:10]
                )
            ]
            if (
                reconsidered is None
                or not user_message_id
                or not relevant
                or any(item["user_message_id"] == user_message_id for item in relevant)
            ):
                return self._error(
                    "reselection_requires_new_turn",
                    "Only a later caller turn explicitly reselecting an excluded "
                    "slot can reconsider it; use its exact datetime.",
                    retry="silent",
                )
            for item in relevant:
                item["reconsidered_datetimes"] = [
                    *item.get("reconsidered_datetimes", []),
                    reconsidered,
                ]
            self._save_resume_context()
            return {"status": "reconsidered", "datetime": reconsidered}

    def _pending_context_block(self) -> str:
        staged = self._staged
        if staged is None:
            return self._prompt_text("pending_context_none")
        return self._prompt_text(
            "pending_context_present",
            service_id=staged.service_id,
            service_name=staged.service_name,
            operator_id=staged.operator_id,
            operator_name=staged.operator_name,
            datetime=staged.datetime,
        )

    def _entry_greeting_branch(self) -> str:
        """재개를 우선하고 단일 서비스 자동 지정·인사·조회 분기를 코드가 정한다.

        `continuation_no_request` 는 이어받을 대화는 있지만(= 인사말 생략) 고객이
        예약 조건을 말한 적은 없는 진입이다. DTMF 로 들어와 본인 확인만 마친
        경로가 여기 해당한다. `first_contact` 로 보내면 "예약 도와드리겠습니다"
        로 응대를 새로 열어 43902 의 재인사 문제가 되살아나고, `continuation`
        으로 보내면 읽을 조건이 없는 모델이 날짜를 지어내 조회한다(61402).
        """
        if self._staged is not None:
            return "resume_pending"
        if self._resume_service() is not None:
            return "resume_search"
        if len(self._services) == 1:
            return "single_service"
        if not self._entry_has_prior_dialogue:
            return "first_contact"
        if not self._entry_has_user_request:
            return "continuation_no_request"
        return "continuation"

    def _resume_service(self) -> dict[str, Any] | None:
        service_id = (self._last_search_conditions or {}).get("service_id")
        return next(
            (
                {"id": service["id"], "name": service["name"]}
                for service in self._services
                if service["id"] == service_id
            ),
            None,
        )

    def _build_greeting_instructions(self) -> str:
        branch = self._entry_greeting_branch()
        if branch == "resume_pending":
            return self._prompt_text(
                "greeting_resume_pending",
                pending_context_block=self._pending_context_block(),
            )
        if branch == "resume_search":
            return self._prompt_text(
                "greeting_resume_search",
                resume_context=json.dumps(
                    {
                        "service": self._resume_service(),
                        "search_conditions": self._last_search_conditions,
                        "rejected_candidates": self._rejected_candidates or [],
                    },
                    ensure_ascii=False,
                ),
            )
        if branch == "single_service":
            return self._single_service_greeting()
        if branch == "continuation":
            return self._prompt_text(
                "greeting_continuation",
                service_request=self._service_request_line(),
            )
        if branch == "continuation_no_request":
            return self._prompt_text(
                "greeting_continuation_no_request",
                service_request=self._service_request_line(),
            )
        return self._prompt_text("greeting_first_contact")

    def _eligible_service_advisors(self, service_id: int) -> list[dict[str, Any]]:
        """The injected advisors who cover the automatically assigned service."""
        return [
            advisor
            for advisor in self._advisors
            if service_id in advisor_service_ids(advisor)
        ]

    def _single_service_greeting(self) -> str:
        service = self._services[0]
        eligible = self._eligible_service_advisors(service["id"])
        today = now_kst().date()
        dates = {
            "start_date": today.isoformat(),
            "end_date": self._date_horizon(today).isoformat(),
        }
        if not self._select_operator or len(eligible) == 1:
            no_date_step = self._prompt_text(
                "single_service_step_search",
                service_id=service["id"],
                name=service["name"],
                **dates,
            )
        else:
            advisor_hint = (
                self._prompt_text(
                    "single_service_advisor_hint_named",
                    names=", ".join(
                        self._prompt_text("advisor_title", name=advisor["name"])
                        for advisor in eligible
                    ),
                )
                if 2 <= len(eligible) <= 3
                else self._prompt_text("single_service_advisor_hint_open")
            )
            no_date_step = self._prompt_text(
                "single_service_step_ask_advisor", advisor_hint=advisor_hint
            )
        return self._prompt_text(
            "greeting_single_service",
            opening=self._prompt_text(
                "single_service_opening_continuation"
                if self._entry_has_prior_dialogue
                else "single_service_opening_first_contact"
            ),
            fixed_service=self._prompt_text(
                "service_mode_block_single",
                name=service["name"],
                id=service["id"],
            ),
            request_handling=self._prompt_text(
                "single_service_with_request"
                if self._entry_has_user_request
                else "single_service_no_request",
                no_date_step=no_date_step,
                service_id=service["id"],
                **dates,
            ),
        )

    def _service_request_line(self) -> str:
        """Ask for the desired appointment when multiple services are available."""
        return self._prompt_text("service_request_multi")

    def _build_instructions(self) -> str:
        service_lines = "\n".join(
            self._prompt_text(
                "service_line_with_keywords",
                name=d["name"],
                id=d["id"],
                keywords=", ".join(d["keywords"]),
            )
            if d["keywords"]
            else self._prompt_text("service_line", name=d["name"], id=d["id"])
            for d in self._services
        )
        advisor_lines = "\n".join(
            self._prompt_text(
                "advisor_line",
                name=d["name"],
                id=d["id"],
                services=", ".join(
                    dep["name"]
                    for dep in self._services
                    if dep["id"] in advisor_service_ids(d)
                ),
            )
            for d in self._advisors
        )
        if self._select_operator:
            booking_goal = self._prompt_text("booking_goal_select_operator")
            operator_mode_block = ""
            operator_rule_6 = self._prompt_text("operator_rule_6_select")
        else:
            booking_goal = self._prompt_text("booking_goal_auto_assign")
            operator_mode_block = self._prompt_text("operator_mode_block_auto")
            operator_rule_6 = self._prompt_text("operator_rule_6_auto")

        service_mode_block = ""
        if len(self._services) == 1:
            service = self._services[0]
            service_mode_block = "\n\n" + self._prompt_text(
                "service_mode_block_single",
                name=service["name"],
                id=service["id"],
            )

        calendar_block = build_calendar_block(
            now_kst(),
            self._schedule_lookup_days,
            self._spoken_lang,
        )
        business_data = getattr(
            getattr(self, "_call_session_data", None), "business_data", None
        )
        call_identity_block = "\n".join(
            f"{label}: {getattr(business_data, field, None) or 'not available'}"
            for label, field in (
                ("Center name", "client_name"),
                ("Phone", "client_phone"),
                ("Address", "client_address"),
            )
        )

        return self._prompt_text(
            "instructions",
            booking_goal=booking_goal,
            call_identity_block=call_identity_block,
            service_lines=service_lines,
            advisor_lines=advisor_lines,
            center_context_block=(
                f"<service_center_knowledge>\n{self._center_context}\n</service_center_knowledge>"
                if self._center_context
                else "none"
            ),
            operator_mode_block=operator_mode_block,
            service_mode_block=service_mode_block,
            calendar_block=calendar_block,
            pending_context_block=self._pending_context_block(),
            operator_rule_6=operator_rule_6,
            handoff_channel_block=(
                "[Handoff channel]\n"
                f"handoff_channel: {self._handoff_channel()}\n"
                "The system itself speaks handoff questions on this channel."
            ),
        )

    def _handoff_channel(self) -> Literal["memo", "transfer"]:
        """Return the configured handoff channel without assuming concrete config types."""
        call_config = getattr(
            getattr(self, "_call_session_data", None), "call_config_data", None
        )
        if getattr(call_config, "transfer_mode", None) == "leave_memo" or not getattr(
            call_config, "transfer_to", None
        ):
            return "memo"
        return "transfer"

    async def _fetch_day(
        self,
        date_str: str,
        *,
        service_id: int | None = None,
        operator_id: int | None = None,
    ) -> dict[str, list[int]]:
        """하루 DATE_TIME 조회 — size=100 완전성 단정 + tripwire. [D4]

        조회는 날짜 단위로 한 번만 하고, 서비스·상담사 필터는 로컬에서 적용한다.
        eligible 판정은 이미 normalize_day_rows 가 주입된 상담사 매핑으로 하고
        있어(예약 케파는 서비스가 아니라 상담사 단위 [D6]) API 의 serviceId·
        operatorId 는 응답을 좁히는 역할뿐이었다. 좁히지 않고 받아 두면 그
        날짜의 어떤 (서비스, 상담사) 조합이든 재조회 없이 만들 수 있다 — search 가
        서비스 없이 조회한 뒤 stage 가 특정 서비스로 물어도 캐시 히트다.

        실통화 07-30_12:16 에서 조회 9건 중 4건이 같은 요청의 반복이었고,
        08-01 한 날짜만 5회 조회됐다.

        오늘의 지난 시각 제외는 캐시 뒤가 아니라 매 호출마다 적용한다. 캐시에
        첫 조회 시점의 바닥을 물려 두면 통화가 길어질 때 이미 지나간 슬롯을
        계속 살려 두게 된다.
        """
        rows = self._day_rows_cache.get(date_str)
        if rows is None:
            rows = await self._fetch_schedule_rows(
                date_str, date_str, schedule_type="DATE_TIME"
            )
            self._check_page_complete(rows, "slot", date=date_str)
            self._day_rows_cache[date_str] = rows
        slots = normalize_day_rows(
            rows,
            self._advisors_by_id,
            service_id=service_id,
            operator_id=operator_id,
        )
        return self._apply_today_floor(date_str, slots)

    @staticmethod
    def _apply_today_floor(
        date_str: str, slots: dict[str, list[int]]
    ) -> dict[str, list[int]]:
        """오늘 조회면 now+lead 이전 슬롯을 제외한다. [D10]"""
        today = now_kst()
        if date_str != f"{today:%Y-%m-%d}":
            return slots
        floor = today + dt.timedelta(minutes=TODAY_LEAD_MINUTES)
        return apply_strictly_after(slots, f"{floor:%Y-%m-%d %H:%M}")

    async def _fetch_available_dates(
        self,
        start_date: str,
        end_date: str,
        *,
        service_id: int | None = None,
        operator_id: int | None = None,
    ) -> list[str]:
        """범위 DATE probe — eligible 상담사가 있는 날짜만, 오름차순. [D5]

        조회는 범위 단위로 한 번만 하고, 서비스·상담사 필터는 로컬에서 적용한다.
        좁히지 않은 원본 행을 캐시하면 같은 범위의 어떤 (서비스, 상담사) 조합도
        재조회 없이 처리할 수 있다. 캐시의 신선도는 통화 시간으로 제한되며,
        이는 _day_rows_cache 와 같은 트레이드오프다.
        """
        cache_key = (start_date, end_date)
        rows = self._date_rows_cache.get(cache_key)
        if rows is None:
            rows = await self._fetch_schedule_rows(
                start_date, end_date, schedule_type="DATE"
            )
            logger.bind(
                event="DYNAMIC_BOOKING_TASK",
                stage="fetch_available_dates",
                start=start_date,
                end=end_date,
                service_id=service_id,
                operator_id=operator_id,
                schedules=rows,
            ).debug("fetching available dates")
            self._check_page_complete(rows, "date", start=start_date, end=end_date)
            self._date_rows_cache[cache_key] = rows
        dates: set[str] = set()
        for item in rows:
            date_part = str(item.time).strip()[:10]
            if not (start_date <= date_part <= end_date):
                continue
            for raw_id in item.operator_ids:
                advisor_id = int(raw_id)
                advisor = self._advisors_by_id.get(advisor_id)
                if advisor is not None and advisor_is_eligible(
                    advisor_id,
                    advisor,
                    service_id=service_id,
                    operator_id=operator_id,
                ):
                    dates.add(date_part)
                    break
        return sorted(dates)

    async def _fetch_schedule_rows(
        self,
        start_date: str,
        end_date: str,
        *,
        schedule_type: Literal["DATE_TIME", "DATE"],
    ) -> list[Any]:
        """Unfiltered availability rows for a range; a failed lookup raises. [D5]"""
        response = await get_site_available_schedules(
            self._call_session_data.business_data.site_id,
            start_date,
            end_date,
            service_reservation_setting=self._service_reservation_setting,
            schedule_type=schedule_type,
            size=SLOT_PAGE_SIZE,
            client_id=self._call_session_data.business_data.client_id,
        )
        record_api_result(
            self.session.history,
            "GET_SITE_AVAILABLE_SCHEDULES",
            response,
            elapsed_ms=getattr(response, "elapsed_ms", None),
        )
        if not response.success or response.data is None:
            raise BookingApiError(response.message or "schedule lookup failed")
        return response.data.schedules

    def _check_page_complete(
        self, rows: list[Any], kind: Literal["slot", "date"], **where: str
    ) -> None:
        """Tripwire: a full page may be truncated, so it is never trusted. [D4]"""
        if len(rows) >= SLOT_PAGE_SIZE:
            logger.bind(
                event="DYNAMIC_BOOKING_TASK",
                stage=f"{kind}_page_tripwire",
                **where,
                rows=len(rows),
                room_name=self._call_session_data.room_name,
            ).error(f"{kind} page assumed complete but hit size cap")
            raise BookingApiError(f"Incomplete schedule response: {kind} page size cap")

    # ---------- 결과 셰이핑 헬퍼 ----------

    # Stateless shaping helpers live in tasks.booking.dynamic_slots; the old
    # method names stay as aliases for self./cls.-style callers and tests.
    _limit_schedule_options = staticmethod(limit_schedule_options)
    _error = staticmethod(error_result)
    _add_earliest_only_truncation = staticmethod(add_earliest_only_truncation)
    _add_range_adjustment = staticmethod(add_range_adjustment)
    _validate_time_filters = staticmethod(validate_time_filters)
    _searched_time_window = staticmethod(searched_time_window)
    _filter_time_constraints = staticmethod(filter_time_constraints)
    _add_time_constraint_facts = staticmethod(add_time_constraint_facts)
    _has_nearby_time_offer = staticmethod(has_nearby_time_offer)
    _normalize_optional_tool_text = staticmethod(normalize_optional_tool_text)

    def _advisor_facts(self, advisor_ids: list[int]) -> list[dict[str, Any]]:
        return [
            {"id": advisor_id, "name": self._advisors_by_id[advisor_id]["name"]}
            for advisor_id in advisor_ids
        ]

    def _slot_facts(
        self,
        slots: dict[str, list[int]],
        *,
        include_services: bool = False,
    ) -> list[dict[str, Any]]:
        out = []
        for when in sorted(slots):
            day = dt.datetime.strptime(when[:10], "%Y-%m-%d")
            fact = {
                "datetime": when,
                "weekday": WEEKDAY_KO[day.weekday()],
                # Speak this, never a recomputed date (searched 10-26, said 10-20).
                "spoken": spoken_datetime(when, self._spoken_lang),
                "advisors": self._advisor_facts(slots[when]),
            }
            if include_services:
                fact["available_services"] = self._services_of_advisors(
                    slots[when]
                )
            out.append(fact)
        return out

    def _date_facts(self, dates: list[str]) -> list[dict[str, str]]:
        return [
            {
                "date": date,
                "weekday": WEEKDAY_KO[dt.datetime.strptime(date, "%Y-%m-%d").weekday()],
                "spoken": spoken_date(date, self._spoken_lang),
            }
            for date in dates
        ]

    def _validate_schedule_ids(
        self,
        service_id: int | None,
        operator_id: int | None,
    ) -> dict[str, Any] | None:
        if service_id is not None and not any(
            service["id"] == service_id for service in self._services
        ):
            return self._error(
                "unknown_service",
                "Use a service_id from the injected list for the selected "
                "service. If none is selected, use service_id=null or omit it. "
                "Never use 0 as an unspecified value. Preserve all other active "
                "conditions and retry the search.",
                retry="silent",
            )
        if operator_id is not None and operator_id not in self._advisors_by_id:
            return self._error(
                "unknown_advisor",
                "Use an operator_id from the injected list for the requested "
                "advisor. Omit it only if no advisor was requested. Never use 0 "
                "as an unspecified value. Preserve all other active conditions "
                "and retry the search.",
                retry="silent",
            )
        return None

    def _validate_range(self, start_date: str, end_date: str) -> dict[str, Any] | None:
        today = now_kst()
        today_str = f"{today:%Y-%m-%d}"
        horizon_str = self._date_horizon(today.date()).isoformat()
        try:
            dt.datetime.strptime(start_date, "%Y-%m-%d")
            dt.datetime.strptime(end_date, "%Y-%m-%d")
        except ValueError:
            return self._error(
                "invalid_date",
                "날짜는 YYYY-MM-DD 형식이어야 합니다.",
                retry="silent",
            )
        if not (today_str <= start_date <= end_date <= horizon_str):
            return self._error(
                "out_of_horizon",
                "조회 가능한 범위를 벗어났습니다.",
                allowed_range=f"{today_str} ~ {horizon_str}",
                retry="silent",
            )
        return None

    def _add_confirmation_gate(
        self,
        result: dict[str, Any],
        *,
        service_id: int | None,
        operator_id: int | None,
        total_slot_count: int | None = None,
        earliest_result: bool = False,
        offer_earliest: bool = False,
    ) -> dict[str, Any]:
        """Tell the model that search facts never authorize confirmation speech.

        `total_slot_count` 는 잘라내기 전 슬롯 수다. earliest_only 가 응답을
        1건으로 줄이면 게이트가 "그 날의 유일한 슬롯" 으로 착각해 stage 를
        강제했고, 시각만 물어본 고객에게 확인 질문이 나갔다(실통화
        07-30_12:16). 잘라낸 건수는 확정 강제의 근거가 될 수 없다. 단,
        `offer_earliest` 는 날짜 미지정 예약 신청에 최단 일정을 기본 제안하기로 한
        명시적 경로이므로 그 후보를 stage 하도록 요구한다. `earliest_result` 는
        자동 stage 여부와 별개로 해당 후보가 최단 검색 결과라는 근거를 보존한다.
        """
        gate: dict[str, Any] = {
            "confirmation_allowed": False,
            "instruction": (
                "search_schedules 결과만으로 확인 질문을 만들지 마세요. "
                "stage_booking 성공 응답의 confirmation 정보로만 통화 언어에 맞춰 "
                "확인 질문을 하세요. 예약 정보의 값을 바꾸거나 누락하지 마세요."
            ),
        }
        slots = result.get("slots")
        available_dates = result.get("available_dates")
        nearby_offer = self._has_nearby_time_offer(result)
        if (isinstance(slots, list) and not slots) or (
            isinstance(available_dates, list) and not available_dates
        ):
            self._add_no_availability_guidance(
                gate, result, offer_earliest=offer_earliest
            )
        elif nearby_offer:
            gate["wait_for_customer"] = True
            gate["handoff_available"] = self._proactive_offer_available()
            gate["instruction"] = (
                "searched_range에서 요청한 정확한 시각에는 자리가 없다고 안내하고, "
                "slots에 있는 그날의 가까운 시간만 가능한 대안으로 제시하세요. 고객이 이미 말한 시간이 "
                "slots에 있으면 그 시간을 우선 짚으세요. 다른 날짜를 먼저 제안하지 "
                "마세요. 고객이 그 시간만 가능하다고 명시했다면 같은 날 대안을 "
                "권하지 말고, handoff_available=true이면 같은 응답에서 "
                "report_booking_obstacle(subject='schedule')를 호출하고 아니면 "
                "다른 날짜를 찾을지 물으세요. 고객이 대안을 선택하기 "
                "전에는 stage_booking이나 "
                "예약 신청 확인 질문을 하지 말고 답변을 기다리세요."
            )
        self._apply_single_slot_rules(
            gate,
            slots,
            service_id=service_id,
            operator_id=operator_id,
            total_slot_count=total_slot_count,
            earliest_result=earliest_result,
            offer_earliest=offer_earliest,
            nearby_offer=nearby_offer,
        )
        if isinstance(slots, list) and slots and "stage_args" not in gate:
            gate["deferred_action"] = {
                "when": "after_customer_selects_exact_slot_and_service",
                "tool": "stage_booking",
                "instruction": (
                    "사용자가 정확한 일시와 서비스를 선택한 뒤 stage_booking을 "
                    "호출하세요. 선택 전에는 호출하지 마세요."
                ),
            }
        if isinstance(available_dates, list) and available_dates:
            gate["deferred_action"] = {
                "when": "after_customer_selects_date",
                "tool": "search_schedules",
                "instruction": (
                    "사용자가 날짜를 선택한 뒤 그 날짜의 정확한 시간을 "
                    "search_schedules로 조회하세요. 아직 stage_booking을 호출하지 마세요."
                ),
            }
        if result.get("latest_time_comparison"):
            gate["instruction"] += (
                " latest_time_comparison은 반환된 후보 날짜와 적용된 시간 조건 "
                "안에서 마지막 예약 가능 시각을 비교한 값입니다. 시간 조건 밖의 "
                "그날 최댓값, 영업 종료시간, 미조회 날짜의 최댓값이 아닙니다. "
                "가장 늦은 시각을 물으면 이 비교의 시각과 동률 날짜를 안내하세요. "
                "시각 비교 요청만으로 예약 확인 질문을 하거나 stage_booking을 "
                "호출하지 말고 고객이 날짜·시간을 선택할 때까지 기다리세요."
            )
        result["required_next_action"] = gate
        return result

    def _add_no_availability_guidance(
        self,
        gate: dict[str, Any],
        result: dict[str, Any],
        *,
        offer_earliest: bool,
    ) -> None:
        """Empty result: announce it, wait, and choose one adjustable condition."""
        gate["wait_for_customer"] = True
        gate["instruction"] = (
            "조회한 기간과 시간 조건에 예약 가능한 일정이 없다고 반드시 먼저 "
            "안내하세요. searched_range 밖의 날짜까지 없다고 단정하지 마세요. "
            "예약 가능하다고 안내하거나 stage_booking을 호출하지 마세요. "
            "같은 사용자 턴에 search_schedules를 다시 호출하지 말고 고객의 "
            "답변을 기다리세요. "
        )
        horizon_reached = result.get(
            "search_horizon_reached",
            offer_earliest and result.get("requested_range_complete", True),
        )
        gate["can_search_later_dates"] = not horizon_reached
        gate["handoff_available"] = self._proactive_offer_available()
        if horizon_reached:
            gate["instruction"] += (
                "조회 가능한 마지막 날짜까지 확인했습니다. 더 이후의 날짜를 "
                "검색하거나 제안하지 마세요. "
            )
        else:
            gate["instruction"] += (
                "target_time은 정확한 검색 시각일 뿐 시간 변경 불가의 근거가 "
                "아닙니다. '2시 반에 예약하고 싶어요'도 희망값이지 고정 조건이 "
                "아닙니다. 다음 순서로 하나를 고르세요. "
                "1. 고객이 '2시 반만 가능해요', '다른 시간은 안 돼요'처럼 "
                "조건 변경 불가를 이미 명시했고 handoff_available=true이면 대안을 "
                "묻지 말고 같은 응답에서 report_booking_obstacle(subject='schedule')"
                "를 호출하세요. handoff_available=false이면 시간을 유지해 다른 "
                "날짜를 찾을지 물으세요. "
            )
            time_filtered = (
                result.get("target_time")
                or not _is_whole_day(result.get("time_from"), result.get("time_to"))
                or (self._last_search_conditions or {}).get("strictly_after")
            )
            if not time_filtered:
                # 시간 조건 없이 하루 전체가 비었다. "같은 날 다른 시간도
                # 괜찮으세요?" 예시가 여기서 새면 방금 "그날은 없다" 고 한 뒤
                # 같은 날 다른 시간대를 제안하는 모순 발화가 된다.
                gate["instruction"] += (
                    "2. 시간 조건 없이 조회한 날짜 전체가 비었으므로 같은 날 "
                    "다른 시간·시간대를 제안하거나 묻지 말고, 다른 날짜를 "
                    "찾을지 물으세요. other_advisors_available=true이면 같은 "
                    "날 다른 상담사는 가능하다고 안내하고 그렇게 찾을지도 "
                    "함께 물으세요. "
                )
            else:
                gate["instruction"] += (
                    "2. 그런 명시적 제한 없이 상담사만 바꿨다면 '같은 날 다른 "
                    "시간도 괜찮으세요?'라고 물으세요. 날짜 변경을 먼저 "
                    "제안하지 마세요. "
                    "3. 그 외에는 고객이 밝힌 선호를 따르고, 불분명하면 조정 "
                    "가능한 조건 하나를 질문하세요. "
                )
            gate["instruction"] += (
                "답변을 받은 다음 턴에 동의한 조건만 바꿔 검색하세요. "
                "다른 날짜에 동의하면 next_start_date 또는 선택한 새 기간을 "
                "쓰고, 동의하지 않은 서비스·상담사·날짜·시간 조건은 유지하세요. "
            )

    def _apply_single_slot_rules(
        self,
        gate: dict[str, Any],
        slots: Any,
        *,
        service_id: int | None,
        operator_id: int | None,
        total_slot_count: int | None,
        earliest_result: bool,
        offer_earliest: bool,
        nearby_offer: bool,
    ) -> None:
        """One slot: remember an earliest offer, and require staging when it is
        the only candidate (or the explicit earliest-for-booking proposal).
        """
        available_slot_count = (
            total_slot_count
            if total_slot_count is not None
            else len(slots)
            if isinstance(slots, list)
            else 0
        )
        if (
            earliest_result
            and not nearby_offer
            and service_id is not None
            and isinstance(slots, list)
            and len(slots) == 1
        ):
            self._pending_earliest_offer = EarliestOffer(
                service_id=service_id,
                operator_id=operator_id,
                datetime=slots[0]["datetime"],
            )
        if (
            not nearby_offer
            and service_id is not None
            and isinstance(slots, list)
            and len(slots) == 1
            and (available_slot_count == 1 or offer_earliest)
        ):
            slot = slots[0]
            advisors = slot.get("advisors", [])
            single_candidate = (
                not self._select_operator
                or operator_id is not None
                or len(advisors) == 1
            )
            if single_candidate:
                args: dict[str, Any] = {
                    "service_id": service_id,
                    "datetime": slot["datetime"],
                }
                if operator_id is not None:
                    args["operator_id"] = operator_id
                gate["required_tool"] = "stage_booking"
                gate["stage_args"] = args
                gate["instruction"] = (
                    "지금 같은 응답에서 stage_args 그대로 stage_booking을 호출하세요. "
                    "호출 전 발화·새 동의 질문·턴 종료는 금지합니다. 성공 후 한 "
                    "발화로 답하세요. 고객이 가능 여부를 물었다면 '네, 가능합니다'"
                    "처럼 그 답을 먼저 짧게 말하고, 이어서 반환된 confirmation의 "
                    "전체 서비스명·날짜·시간과 제공된 상담사명으로 예약 신청 여부를 한 번 "
                    "묻고 고객의 새 명시적 동의를 기다리세요. "
                    "stage_booking은 예약 신청이 아니므로 commit_booking을 "
                    "연속 호출하지 마세요."
                )

    # ---------- 인계(handoff) 컨트롤러 ----------
    # 언제 제안할 수 있는지는 코드가 계산해 도구 결과의 handoff_offer로 알리고,
    # 문장과 말하기는 프롬프트가 맡는다. 코드는 동의 없는 인계 금지, 거절 시 예약
    # 유지, 세션 단위 거절·제안 이력만 소유한다. 근거:
    # docs/plans/2026-09-29-booking-handoff-final-plan.md (revision v3)

    def _handoff_session(self) -> DynamicBookingResumeContext:
        resume = getattr(self._call_session_data, "dynamic_booking_resume", None)
        if resume is None:
            resume = DynamicBookingResumeContext()
            self._call_session_data.dynamic_booking_resume = resume
        return resume

    def _handoff_refusal(self) -> dict[str, str] | None:
        resume = getattr(self._call_session_data, "dynamic_booking_resume", None)
        return getattr(resume, "handoff_declined_evidence", None)

    def _proactive_offer_available(self) -> bool:
        """One unsolicited offer per booking attempt, never after a refusal."""
        resume = getattr(self._call_session_data, "dynamic_booking_resume", None)
        return (
            self._offer_pending is None
            and getattr(resume, "handoff_declined_evidence", None) is None
            and not getattr(resume, "handoff_offer_delivered", False)
        )

    def _safety_offer_available(self) -> bool:
        return (
            self._safety_issue_evidence is not None
            and self._proactive_offer_available()
        )

    def _count_declined_alternative(self) -> None:
        """At most one per caller turn: chained searches are not new refusals."""
        turn = self._latest_user_message_id()
        if turn is not None and turn == self._declined_turn_id:
            return
        self._declined_turn_id = turn
        self._handoff_session().declined_alternatives += 1

    def _alternatives_exhausted(self) -> bool:
        declined = self._handoff_session().declined_alternatives
        return (
            declined >= DECLINED_ALTERNATIVES_BEFORE_OFFER
            and self._proactive_offer_available()
        )

    def _record_handoff_refusal(self, evidence: dict[str, str]) -> None:
        self._handoff_session().handoff_declined_evidence = dict(evidence)

    def _offer_pending_guard(self) -> dict[str, Any] | None:
        """A "yes" to a pending help question must never commit a booking."""
        if self._offer_pending is None:
            return None
        return self._error(
            "awaiting_handoff_decision",
            "A staff-help question is waiting. Report the caller's answer with "
            "answer_handoff_offer first.",
            required_next_tool="answer_handoff_offer",
            retry="silent",
        )

    def _record_handoff_event(
        self, status: str, offer: HandoffOffer, **details: Any
    ) -> None:
        payload = {
            "offer_id": offer.id,
            "reason_code": offer.reason_code,
            "handoff_channel": offer.channel,
            "blocker_kind": (
                offer.blocker.kind
                if offer.blocker is not None
                else offer_basis(None, offer.reason_code)
            ),
            **details,
        }
        self.session.history.add_message(
            role="developer",
            content="",
            extra=agent_event_extra("BOOKING_CREATE", status=status, details=payload),
        )
        logger.bind(
            event="DYNAMIC_BOOKING_TASK",
            stage=status.lower(),
            room_name=self._call_session_data.room_name,
            **payload,
        ).info("Booking handoff state changed")

    def _open_offer(
        self, blocker: Blocker | None, reason_code: HandoffReasonCode
    ) -> dict[str, Any] | None:
        """Open the single help offer the model asks next; None if not allowed."""
        if not self._proactive_offer_available():
            return None
        channel = self._handoff_channel()
        offer = HandoffOffer(
            id=uuid.uuid4().hex[:12],
            blocker=blocker,
            reason_code=reason_code,
            channel=channel,
            revision=self._request_revision,
            user_message_id=self._latest_user_message_id(),
        )
        self._offer_pending = offer
        self._handoff_session().handoff_offer_delivered = True
        self._record_handoff_event(
            "HANDOFF_OFFERED", offer, blocker=blocker_facts(blocker)
        )
        reason = {
            "blocker": "caller_condition_not_bookable",
            "issue": "safety_issue",
            "declined_alternatives": "alternatives_declined",
        }[offer_basis(blocker, reason_code)]
        return {
            "channel": channel,
            "reason": reason,
            "ask": (
                "Say in one short sentence what could not be matched, using only "
                "the checked facts, then ask one yes/no question whether to "
                + (
                    "leave a message for the staff."
                    if channel == "memo"
                    else "connect the caller to a staff member."
                )
                + " Ask nothing else in this turn, and wait for the answer."
            ),
        }

    def _attach_offer(
        self,
        result: dict[str, Any],
        blocker: Blocker | None,
        reason_code: HandoffReasonCode,
    ) -> None:
        if (offer := self._open_offer(blocker, reason_code)) is None:
            return
        result["handoff_offer"] = offer
        # The help question replaces the next search or alternative question.
        for key in ("required_next_tool", "instruction"):
            result.pop(key, None)
        gate = result.get("required_next_action")
        if isinstance(gate, dict) and "instruction" in gate:
            gate["instruction"] = (
                "Say what the checked range and filters found, then ask only the "
                "handoff_offer question instead of proposing another search."
            )

    def _offer_reply_is_fresh(self, offer: HandoffOffer) -> bool:
        """Only a later caller turn, with no new search since, can answer it."""
        return (
            offer.revision == self._request_revision
            and self._latest_user_message_id() != offer.user_message_id
        )

    def _latest_user_evidence(self) -> dict[str, str] | None:
        latest = next(
            (item for item in reversed(self._dialogue_items()) if item.role == "user"),
            None,
        )
        if latest is None:
            return None
        return {"quote": latest.text_content.strip(), "user_message_id": latest.id}

    def _handoff_detail(self, offer: HandoffOffer) -> str:
        blocker = offer.blocker
        if isinstance(blocker, ServiceScopeBlocker):
            kind = "서비스" if blocker.subject == "service" else "상담사"
            return f"전화 예약 목록 밖 {kind} 요청: {blocker.requested_label or '미상'}"
        if isinstance(blocker, NoMatchingSlotBlocker):
            start, end = blocker.searched_range
            span = start if start == end else f"{start}~{end}"
            return f"조회 범위 {span}에 요청 조건과 일치하는 예약 일정 없음"
        if offer.reason_code == "no_slot_memo":
            return "제안한 대안 일정을 여러 번 거절함"
        evidence = self._safety_issue_evidence or {}
        quote = evidence.get("quote")
        return f"기기 문제 호소: {quote}" if quote else "상담원 도움 동의"

    async def _complete_handoff(self, offer: HandoffOffer) -> None:
        async with self._mutation_lock:
            if self.done():
                return None
            blocker = offer.blocker
            if isinstance(blocker, NoMatchingSlotBlocker):
                span = f"{blocker.searched_range[0]}~{blocker.searched_range[1]}"
                ranges = list(self._empty_searched_ranges or [])
                if span not in ranges:
                    ranges.append(span)
                self._empty_searched_ranges = ranges
            detail = self._handoff_detail(offer)
            self._call_session_data.booking_memo_context = (
                self._build_booking_memo_context(
                    detail=detail,
                    exit_reason_code=offer.reason_code,
                    time_condition=None,
                )
            )
            self._set_staged(None)
            self._offer_pending = None
            logger.bind(
                event="DYNAMIC_BOOKING_TASK",
                stage="exited",
                room_name=self._call_session_data.room_name,
                exit_reason_code=offer.reason_code,
                detail=detail,
                handoff_accepted=True,
                handoff_channel=offer.channel,
            ).info("Caller accepted the booking handoff offer")
            self.complete(
                DynamicBookingResult(
                    outcome="exited",
                    exit_reason_code=offer.reason_code,
                    detail=detail,
                    handoff_accepted=True,
                    handoff_channel=offer.channel,
                )
            )
        return None

    def _booking_in_progress(self) -> bool:
        """A appointment, candidate or search exists that a stop would discard."""
        if self._staged is not None or self._last_candidate is not None:
            return True
        if self._rejected_candidates:
            return True
        if self._resumed_after_cancel and not self._searched_in_task:
            return False
        return bool((self._last_search_conditions or {}).get("service_id"))

    def _advisor_request_service(self, service_id: int | None) -> int | None:
        """The appointment an off-list advisor request is for, when it is decided."""
        if service_id is not None:
            return (
                service_id
                if any(d["id"] == service_id for d in self._services)
                else None
            )
        if self._staged is not None:
            return self._staged.service_id
        known = (self._last_search_conditions or {}).get("service_id")
        if known is not None:
            return known
        if len(self._services) == 1:
            return self._services[0]["id"]
        return None

    async def _verified_advisor_alternatives(
        self, service_id: int
    ) -> list[dict[str, Any]] | None:
        """Listed advisors of this appointment with a real bookable date.

        Uses the DATE availability rows for the whole lookup window (one cached
        request). None means availability could not be verified.
        """
        start = now_kst().date()
        end = self._date_horizon(start)
        candidates = [
            advisor
            for advisor in self._advisors
            if service_id in advisor_service_ids(advisor)
        ]
        verified: list[dict[str, Any]] = []
        try:
            for advisor in candidates:
                dates = await self._fetch_available_dates(
                    start.isoformat(),
                    end.isoformat(),
                    service_id=service_id,
                    operator_id=advisor["id"],
                )
                if dates:
                    verified.append(
                        {
                            "id": advisor["id"],
                            "name": advisor["name"],
                            "earliest_date": dates[0],
                        }
                    )
        except BookingApiError:
            return None
        return verified

    async def _offer_advisor_alternative(
        self,
        blocker: ServiceScopeBlocker,
        service_id: int | None,
        caller_fixed_advisor: bool,
    ) -> dict[str, Any] | None:
        """Present a verified other-advisor alternative once, or return None.

        None means: no alternative step applies, continue with the handoff offer.
        """
        resume = self._handoff_session()
        if (
            caller_fixed_advisor
            or resume.advisor_alternative_presented
            or not self._proactive_offer_available()
        ):
            return None
        service = self._advisor_request_service(service_id)
        if service is None:
            return {
                "status": "service_required",
                "blocker": blocker_facts(blocker),
                "message": "First explain that this phone booking channel cannot "
                "book the requested advisor. Do not claim the advisor is absent, "
                "not working, or does not offer the service. Then ask "
                "which listed service the caller wants, without tying the "
                "question to that advisor or implying they can be booked. After "
                "the answer, call report_booking_obstacle again with service_id.",
            }
        alternatives = await self._verified_advisor_alternatives(service)
        if not alternatives:
            return None
        resume.advisor_alternative_presented = True
        service_name = next(
            (d["name"] for d in self._services if d["id"] == service), None
        )
        self.session.history.add_message(
            role="developer",
            content="",
            extra=agent_event_extra(
                "BOOKING_CREATE",
                status="ALTERNATIVE_OFFERED",
                details={
                    "blocker_kind": blocker.kind,
                    "subject": blocker.subject,
                    "service_id": service,
                    "alternative_count": len(alternatives),
                },
            ),
        )
        result: dict[str, Any] = {
            "status": "alternative_available",
            "blocker": blocker_facts(blocker),
            "service": {"id": service, "name": service_name},
            "wait_for_customer": True,
        }
        if self._select_operator:
            result["alternative_advisors"] = alternatives[
                :MAX_SCHEDULE_OPTIONS_TO_PRESENT
            ]
        else:
            result["alternative"] = {
                "kind": "auto_assign",
                "earliest_date": min(item["earliest_date"] for item in alternatives),
            }
        return result

    async def _report_obstacle(
        self,
        subject: ObstacleSubject,
        requested_label: str | None,
        *,
        service_id: int | None = None,
        caller_fixed_advisor: bool = False,
    ) -> dict[str, Any]:
        self._note_progress()
        label = self._normalize_optional_tool_text(requested_label)
        blocker: Blocker
        if subject == "schedule":
            if self._schedule_blocker is None:
                return self._error(
                    "no_schedule_blocker",
                    "Use subject=schedule only after search_schedules found no exact "
                    "match for the caller's condition. Search that condition first.",
                    retry="silent",
                )
            blocker = self._schedule_blocker
        else:
            if label is not None and subject == "advisor":
                matches = {
                    advisor["id"]: advisor_name_match(label, advisor["name"])
                    for advisor in self._advisors
                }
                exact = next((i for i, m in matches.items() if m == "exact"), None)
                if exact is not None:
                    return self._error(
                        "listed_in_catalog",
                        "This advisor is in the injected list; continue booking with it.",
                        id=exact,
                        retry="silent",
                    )
                close = [
                    {"id": i, "name": self._advisors_by_id[i]["name"]}
                    for i, m in matches.items()
                    if m in {"partial", "similar"}
                ]
                key = normalize_label(label)
                if close and key not in self._advisor_confirm_asked:
                    self._advisor_confirm_asked = self._advisor_confirm_asked | {key}
                    return {
                        "status": "advisor_confirmation_required",
                        "requested_label": label,
                        "candidates": close[:MAX_SCHEDULE_OPTIONS_TO_PRESENT],
                        "message": "Ask one short question whether the caller means "
                        "one of these listed advisors. If they say it is someone "
                        "else, call report_booking_obstacle again.",
                    }
            if label is not None:
                catalog = self._services if subject == "service" else self._advisors
                listed = next(
                    (
                        item
                        for item in catalog
                        if normalize_label(item["name"]) == normalize_label(label)
                    ),
                    None,
                )
                if listed is not None:
                    return self._error(
                        "listed_in_catalog",
                        "This item is in the injected list; continue booking with it.",
                        id=listed["id"],
                        retry="silent",
                    )
            blocker = ServiceScopeBlocker(subject=subject, requested_label=label)
            if subject == "advisor" and (
                alternative := await self._offer_advisor_alternative(
                    blocker, service_id, caller_fixed_advisor
                )
            ):
                return alternative
        offer = self._open_offer(blocker, reason_code_for(blocker))
        if offer is None:
            return {
                "status": "handoff_suppressed",
                "reason": (
                    "caller_declined_handoff"
                    if self._handoff_refusal() is not None
                    else "already_offered"
                ),
                "blocker": blocker_facts(blocker),
            }
        return {
            "status": "handoff_offer",
            "blocker": blocker_facts(blocker),
            "handoff_offer": offer,
        }

    @function_tool
    @_log_tool_io
    async def report_booking_obstacle(
        self,
        subject: ObstacleSubject,
        requested_label: NullableToolStr = None,
        service_id: NullableToolInt = None,
        caller_fixed_advisor: bool = False,
    ) -> dict[str, Any]:
        """Report that phone booking cannot meet the caller's current request.

        service/advisor: the caller clearly wants a service or advisor (subject
        advisor) that no injected list item matches — a first request, a refusal of
        listed items, or restating the original ("데이터 복구로 해야 돼", "저번에
        데이터 복구 받았는데 이걸로 해줘요"). Pass the caller's own name for it as
        requested_label. Call this instead of saying it cannot be booked; do not
        offer unrelated items first. Mentioning a past inspection is not a records
        request. If the meaning is unclear, ask instead. schedule: after a search
        with no exact match, the caller says that date/time/advisor cannot change
        ("그날 그 시간밖에 안 돼요", "그 상담사님한테 받아야 해요").
        handoff_offer: ask it as instructed and wait. handoff_suppressed: say only
        that it cannot be booked by phone and continue booking.
        advisor: pass service_id when the service is known.
        caller_fixed_advisor=true only when the caller explicitly rules out every
        other advisor ("그 상담사님 아니면 안 돼요", "다른 상담사는 싫어요"); naming,
        requesting or preferring an advisor is false. alternative_available: offer
        the returned verified alternative once, in one question, with no times; if
        the caller refuses it or keeps the original advisor, call this again.
        service_required: explain only the phone booking scope limitation,
        then ask the service without implying the requested advisor is bookable;
        call this again with service_id after the answer.
        advisor_confirmation_required: a listed advisor has a close name;
        ask whether the caller means them, and
        call this again only if not.
        """
        return await self._report_obstacle(
            subject,
            requested_label,
            service_id=_normalize_nullable_tool_value(service_id),
            caller_fixed_advisor=caller_fixed_advisor,
        )

    @function_tool
    @_log_tool_io
    async def answer_handoff_offer(self, reply: OfferReply) -> dict[str, Any] | None:
        """Report the caller's answer to the staff-help question you just asked.

        accept: an explicit yes to the offered connection or message ("네
        남겨주세요", "연결해 주세요"); restating a condition is not consent.
        decline: refuses the help ("연결은 싫고 예약만 할게요", "괜찮아요"), also
        together with new conditions; then search those conditions. other: new
        booking conditions only, or anything else; continue with it. Output
        nothing after accept; the parent handles it.
        """
        self._note_progress()
        offer = self._offer_pending
        if reply == "accept":
            if offer is None or not self._offer_reply_is_fresh(offer):
                return self._error(
                    "consent_not_fresh",
                    "No staff-help question is waiting for this answer. Continue "
                    "booking; ask the help question first if it is still offered.",
                    retry="silent",
                )
            return await self._complete_handoff(offer)
        self._offer_pending = None
        result: dict[str, Any] = {"status": "booking_continues"}
        if reply == "decline":
            # Recorded even if a search already replaced the question: an explicit
            # refusal is what suppresses further offers in this booking attempt.
            evidence = self._latest_user_evidence() or {
                "quote": "",
                "user_message_id": "",
            }
            self._record_handoff_refusal(evidence)
            self._declined_offer(offer, result)
        self._save_resume_context()
        return result

    def _declined_offer(self, offer: HandoffOffer | None, result: dict[str, Any]):
        """Close a declined offer and tell the model how the booking continues."""
        if offer is not None:
            self._record_handoff_event("HANDOFF_DECLINED", offer)
        result["handoff"] = "declined"
        if not self._booking_in_progress():
            # Nothing bookable is pending: one question, and a "no" ends the
            # booking without a cancel confirmation (see exit_task).
            result["next_step"] = "ask_if_another_booking"
            result["if_caller_wants_nothing_else"] = "exit_task:cancel"

    def _nearby_slots(
        self,
        day_slots: dict[str, list[int]],
        *,
        target_time: str,
        time_from: str | None,
        time_to: str | None,
        strictly_after: str | None,
    ) -> dict[str, list[int]]:
        """Slots within NEARBY_SLOT_WINDOW_MINUTES of target_time, nearest first."""
        others = self._filter_search_constraints(
            day_slots,
            time_from=time_from,
            time_to=time_to,
            target_time=None,
            strictly_after=strictly_after,
        )
        return {
            when: others[when]
            for when in sorted(
                others, key=lambda when: (_minutes_apart(when, target_time), when)
            )
            if _minutes_apart(when, target_time) <= NEARBY_SLOT_WINDOW_MINUTES
        }

    async def _nearby_over_dates(
        self,
        dates: list[str],
        *,
        service_id: int | None,
        operator_id: int | None,
        target_time: str,
        time_from: str | None,
        time_to: str | None,
        strictly_after: str | None,
        fetched: dict[str, dict[str, list[int]]],
    ) -> dict[str, Any] | None:
        """A multi-day exact-time miss: nearby times on the first date with any.

        Reuses the day rows the exact search already fetched (``fetched``).
        """
        for date in dates:
            day_slots = fetched.get(date)
            if day_slots is None:
                day_slots = await self._fetch_day(
                    date, service_id=service_id, operator_id=operator_id
                )
            slots = self._nearby_slots(
                day_slots,
                target_time=target_time,
                time_from=time_from,
                time_to=time_to,
                strictly_after=strictly_after,
            )
            if not slots:
                continue
            facts = self._slot_facts(slots, include_services=service_id is None)
            facts.sort(
                key=lambda fact: (
                    _minutes_apart(fact["datetime"], target_time),
                    fact["datetime"],
                )
            )
            result: dict[str, Any] = {
                "status": "ok",
                "date": date,
                "slots": facts,
                "exact_time_match": False,
            }
            self._add_time_constraint_facts(
                result, time_from=time_from, time_to=time_to, target_time=target_time
            )
            self._limit_schedule_options(result, option_key="slots")
            return result
        return None

    def _filter_search_constraints(
        self,
        slots: dict[str, list[int]],
        *,
        time_from: str | None,
        time_to: str | None,
        target_time: str | None,
        strictly_after: str | None,
    ) -> dict[str, list[int]]:
        slots = self._filter_time_constraints(
            slots,
            time_from=time_from,
            time_to=time_to,
            target_time=target_time,
        )
        return self._filter_rejected_candidates(
            apply_strictly_after(slots, strictly_after)
        )

    def _services_of_slots(
        self, slots: dict[str, list[int]]
    ) -> list[dict[str, Any]]:
        """서비스 미지정 하루 조회의 가능 서비스 집계. [D6]

        예약 케파는 서비스가 아닌 상담사 단위이므로, 가용 상담사의 모든 담당
        서비스는 해당 상담사의 슬롯에 공통으로 가용하다.
        """
        available_advisor_ids: set[int] = set()
        for advisor_ids in slots.values():
            available_advisor_ids.update(advisor_ids)
        return self._services_of_advisors(list(available_advisor_ids))

    def _services_of_advisors(self, advisor_ids: list[int]) -> list[dict[str, Any]]:
        """가용 상담사 슬롯의 서비스 항목을 주입 순서로 반환한다. [D6]

        도메인 계약: 예약 케파는 상담사 단위이며 서비스별로 분리되지 않는다.
        따라서 상담사가 특정 시각에 가용하면 정적 매핑된 모든 담당 서비스 항목이
        그 시각에 가용하다. 이 합집합은 낙관적 추정이 아닌 확정적 슬롯 사실이다.
        """
        service_ids: set[int] = set()
        for advisor_id in advisor_ids:
            service_ids |= advisor_service_ids(self._advisors_by_id[advisor_id])
        return [
            {"id": d["id"], "name": d["name"]}
            for d in self._services
            if d["id"] in service_ids
        ]

    def _awaiting_customer_after_no_availability(self) -> dict[str, Any] | None:
        if self._latest_user_message_id() != self._pending_empty_search_user_message_id:
            self._pending_empty_search = None
        if self._pending_empty_search is None:
            return None
        if self._has_nearby_time_offer(self._pending_empty_search):
            message = (
                "먼저 previous_search의 요청 시각에는 자리가 없고 slots의 가까운 "
                "시간은 가능한 대안이라고 안내하세요. 고객의 선택을 기다리고, "
                "같은 턴에 검색하거나 후보를 검증하지 마세요."
            )
        else:
            message = (
                "먼저 previous_search의 기간과 시간 조건에 일정이 없다고 "
                "안내하세요. 이전 required_next_action을 따라 대안을 물은 뒤 "
                "고객의 다음 답변을 기다리세요. 검색이나 후보 검증을 반복하지 마세요."
            )
        return {
            "status": "awaiting_customer",
            "code": "awaiting_customer_after_no_availability",
            "message": message,
            "previous_search": self._pending_empty_search,
        }

    # ---------- 툴 1: 조회 ----------

    async def _search_schedules_impl(
        self,
        start_date: str,
        end_date: str,
        service_id: NullableToolInt = None,
        operator_id: NullableToolInt = None,
        time_from: NullableToolStr = None,
        time_to: NullableToolStr = None,
        target_time: NullableToolStr = None,
        strictly_after: NullableToolStr = None,
        mode: Literal["list", "earliest", "earliest_for_booking"] = "list",
    ) -> dict[str, Any]:
        """Search implementation; see search_schedules for the tool contract.

        Validates and resets per-search state here, runs one search strategy,
        then applies every shared side effect in `_finish_search`.
        """
        earliest_only = mode != "list"
        self._note_progress()
        self._reset_cancel_confirmation()
        self._invalidate_candidate()
        service_id = _normalize_nullable_tool_value(service_id)
        operator_id = _normalize_nullable_tool_value(operator_id)
        time_from = self._normalize_optional_tool_text(time_from)
        time_to = self._normalize_optional_tool_text(time_to)
        target_time = self._normalize_optional_tool_text(target_time)
        strictly_after = self._normalize_optional_tool_text(strictly_after)
        if self._empty_searched_ranges is None:
            self._empty_searched_ranges = []
        user_message_id = self._latest_user_message_id()
        if waiting := self._awaiting_customer_after_no_availability():
            return waiting
        # A new search is a new request revision: an earlier no-match can no
        # longer justify an offer, even if this search fails before finishing.
        self._request_revision += 1
        self._schedule_blocker = None
        self._searched_in_task = True
        self._pending_earliest_offer = None
        # A new search replaces an unanswered help question.
        self._offer_pending = None
        moved_on = mode != "earliest" and not self._picks_presented(
            start_date, end_date, target_time, time_from, time_to
        )
        if mode == "earliest_for_booking" and service_id is None:
            return self._error(
                "invalid_arguments",
                "mode=earliest_for_booking requires service_id.",
                retry="silent",
            )
        requested_end_date = end_date
        if earliest_only:
            try:
                parsed_start = dt.datetime.strptime(start_date, "%Y-%m-%d").date()
                parsed_end = dt.datetime.strptime(end_date, "%Y-%m-%d").date()
            except ValueError:
                pass
            else:
                horizon = self._date_horizon()
                if parsed_start <= horizon < parsed_end:
                    end_date = horizon.isoformat()
                    logger.bind(
                        event="DYNAMIC_BOOKING_TASK",
                        stage="search_range_clamped",
                        start=start_date,
                        requested_end=requested_end_date,
                        applied_end=end_date,
                    ).info("capping earliest schedule search at booking horizon")

        if invalid := self._validate_schedule_ids(service_id, operator_id):
            return invalid
        if invalid := self._validate_range(start_date, end_date):
            return invalid
        if invalid := self._validate_time_filters(
            time_from=time_from,
            time_to=time_to,
            target_time=target_time,
        ):
            return invalid

        has_time_filter = any(
            value is not None for value in (time_from, time_to, target_time)
        )
        range_adjustment_reason = "booking_horizon"
        if has_time_filter:
            window_end = (
                dt.date.fromisoformat(start_date)
                + dt.timedelta(days=TIME_FILTER_SEARCH_DAYS - 1)
            ).isoformat()
            if end_date > window_end:
                end_date = window_end
                range_adjustment_reason = "time_search_window"

        request = _SearchRequest(
            start_date=start_date,
            end_date=end_date,
            requested_end_date=requested_end_date,
            service_id=service_id,
            operator_id=operator_id,
            time_from=time_from,
            time_to=time_to,
            target_time=target_time,
            strictly_after=strictly_after,
            mode=mode,
            user_message_id=user_message_id,
            moved_on=moved_on,
            range_adjustment_reason=range_adjustment_reason,
        )
        try:
            return self._finish_search(request, await self._run_search(request))
        except BookingApiError as error:
            return self._error("schedule_lookup_failed", str(error))

    async def _run_search(self, request: _SearchRequest) -> _SearchOutcome:
        """Pick the search strategy: one day, earliest, verified dates or dates."""
        if request.start_date == request.end_date:
            return await self._search_one_day(request)

        dates = await self._fetch_available_dates(
            request.start_date,
            request.end_date,
            service_id=request.service_id,
            operator_id=request.operator_id,
        )
        # Multi-day advisor-filtered misses: whether other advisors have dates in
        # the same cached DATE rows. With time filters this is only a date-level
        # hint, never a claim about the requested time.
        other_advisor_hint: tuple[str, bool] | None = None
        if request.operator_id is not None and not dates:
            other_advisor_hint = await self._other_advisor_hint(request)
        fetched: dict[str, dict[str, list[int]]] = {}
        if request.earliest_only:
            outcome = await self._search_earliest_over_dates(request, dates, fetched)
        elif (
            request.has_time_filter
            or request.strictly_after is not None
            or self._rejected_candidates
        ):
            outcome = await self._search_verified_dates(request, dates, fetched)
        else:
            result = {
                "status": "ok",
                "available_dates": self._date_facts(dates),
            }
            self._limit_schedule_options(result, option_key="available_dates")
            outcome = _SearchOutcome(result)
        return outcome._replace(other_advisor_hint=other_advisor_hint)

    async def _search_one_day(self, request: _SearchRequest) -> _SearchOutcome:
        """One date: exact slots, or nearby slots for a list-mode exact-time miss."""
        start_date = request.start_date
        service_id = request.service_id
        operator_id = request.operator_id
        target_time = request.target_time
        day_slots = await self._fetch_day(
            start_date,
            service_id=service_id,
            operator_id=operator_id,
        )
        slots = self._filter_search_constraints(day_slots, **request.filters)
        nearby_offer = bool(target_time and not slots and request.mode == "list")
        if nearby_offer:
            slots = self._nearby_slots(
                day_slots,
                target_time=target_time,
                time_from=request.time_from,
                time_to=request.time_to,
                strictly_after=request.strictly_after,
            )
        facts = self._slot_facts(slots, include_services=service_id is None)
        if nearby_offer and facts:
            facts.sort(
                key=lambda fact: (
                    _minutes_apart(fact["datetime"], target_time),
                    fact["datetime"],
                )
            )
        total_slot_count = len(facts)
        if request.earliest_only:
            facts = facts[:1]
        result: dict[str, Any] = {
            "status": "ok",
            "date": start_date,
            "date_spoken": spoken_date(start_date, self._spoken_lang),
            "slots": facts,
        }
        self._add_time_constraint_facts(
            result,
            time_from=request.time_from,
            time_to=request.time_to,
            target_time=target_time,
        )
        if request.earliest_only:
            self._add_earliest_only_truncation(
                result, total_slot_count=total_slot_count
            )
        else:
            self._limit_schedule_options(result, option_key="slots")
        if service_id is None and slots:
            result["available_services"] = self._services_of_slots(slots)
        if operator_id is not None and not facts:
            # Day rows are cached unfiltered, so this adds no API call.
            others = self._filter_search_constraints(
                await self._fetch_day(start_date, service_id=service_id),
                **request.filters,
            )
            result["other_advisors_available"] = any(
                advisor != operator_id
                for advisors in others.values()
                for advisor in advisors
            )
        return _SearchOutcome(result, total_slot_count=total_slot_count)

    async def _other_advisor_hint(self, request: _SearchRequest) -> tuple[str, bool]:
        """Whether advisors other than the requested one have dates in the range."""
        any_advisor_dates = await self._fetch_available_dates(
            request.start_date, request.end_date, service_id=request.service_id
        )
        # DATE rows prove another advisor's date only without time,
        # strictly_after or earliest filters. Rejections matter per date:
        # a rejected whole day is excluded and a day with a rejected slot
        # is only a hint until its times are checked.
        rejected = self._rejected_candidates or []
        rejected_days = {
            item["datetime"][:10] for item in rejected if item["scope"] == "date"
        }
        slot_days = {
            item["datetime"][:10] for item in rejected if item["scope"] == "slot"
        }
        candidates = [d for d in any_advisor_dates if d not in rejected_days]
        clean = [d for d in candidates if d not in slot_days]
        if request.has_time_filter or request.strictly_after or request.earliest_only:
            return ("other_advisors_may_be_available", bool(candidates))
        if clean:
            return ("other_advisors_available", True)
        return ("other_advisors_may_be_available", bool(candidates))

    async def _search_earliest_over_dates(
        self,
        request: _SearchRequest,
        dates: list[str],
        fetched: dict[str, dict[str, list[int]]],
    ) -> _SearchOutcome:
        """The first date with a matching slot, else nearby times, else empty."""
        service_id = request.service_id
        for date in dates:
            slots = await self._fetch_day(
                date,
                service_id=service_id,
                operator_id=request.operator_id,
            )
            fetched[date] = slots
            slots = self._filter_search_constraints(slots, **request.filters)
            if slots:
                facts = self._slot_facts(
                    slots, include_services=service_id is None
                )
                total_slot_count = len(facts)
                result = {"status": "ok", "slots": facts[:1]}
                result = self._add_time_constraint_facts(
                    result,
                    time_from=request.time_from,
                    time_to=request.time_to,
                    target_time=request.target_time,
                )
                self._add_earliest_only_truncation(
                    result, total_slot_count=total_slot_count
                )
                return _SearchOutcome(
                    result,
                    total_slot_count=total_slot_count,
                    checked_end_date=date,
                )
        if request.target_time is not None and (
            nearby := await self._nearby_over_dates(
                dates,
                service_id=service_id,
                operator_id=request.operator_id,
                target_time=request.target_time,
                time_from=request.time_from,
                time_to=request.time_to,
                strictly_after=request.strictly_after,
                fetched=fetched,
            )
        ):
            return _SearchOutcome(nearby)
        result = {"status": "ok", "slots": []}
        result = self._add_time_constraint_facts(
            result,
            time_from=request.time_from,
            time_to=request.time_to,
            target_time=request.target_time,
        )
        return _SearchOutcome(result)

    async def _search_verified_dates(
        self,
        request: _SearchRequest,
        dates: list[str],
        fetched: dict[str, dict[str, list[int]]],
    ) -> _SearchOutcome:
        """Dates whose slots pass the time/after/rejection filters, checked by day."""
        verified, latest_times, checked_end_date = await self._verify_dates(
            request, dates, fetched
        )
        if (
            not verified
            and request.target_time is not None
            and (
                nearby := await self._nearby_over_dates(
                    dates,
                    service_id=request.service_id,
                    operator_id=request.operator_id,
                    target_time=request.target_time,
                    time_from=request.time_from,
                    time_to=request.time_to,
                    strictly_after=request.strictly_after,
                    fetched=fetched,
                )
            )
        ):
            return _SearchOutcome(nearby, checked_end_date=checked_end_date)
        result = {
            "status": "ok",
            "available_dates": self._date_facts(verified),
        }
        self._limit_schedule_options(result, option_key="available_dates")
        add_latest_time_comparison(result, latest_times)
        result = self._add_time_constraint_facts(
            result,
            time_from=request.time_from,
            time_to=request.time_to,
            target_time=request.target_time,
        )
        return _SearchOutcome(result, checked_end_date=checked_end_date)

    async def _verify_dates(
        self,
        request: _SearchRequest,
        dates: list[str],
        fetched: dict[str, dict[str, list[int]]],
    ) -> tuple[list[str], dict[str, str], str]:
        """Fetch days concurrently in chunks until one more than can be shown pass.

        Returns the verified dates, each one's latest passing time, and the last
        date counted as checked (the end of the chunk that hit the limit).
        """
        verified: list[str] = []
        latest_times: dict[str, str] = {}
        checked_end_date = request.end_date
        verification_limit = MAX_SCHEDULE_OPTIONS_TO_PRESENT + 1
        for chunk_start in range(0, len(dates), SCHEDULE_FETCH_CONCURRENCY):
            chunk = dates[chunk_start : chunk_start + SCHEDULE_FETCH_CONCURRENCY]
            chunk_results = await asyncio.gather(
                *(
                    self._fetch_day(
                        date,
                        service_id=request.service_id,
                        operator_id=request.operator_id,
                    )
                    for date in chunk
                ),
                return_exceptions=True,
            )
            errors = [
                result for result in chunk_results if isinstance(result, BaseException)
            ]
            if errors:
                booking_error = next(
                    (error for error in errors if isinstance(error, BookingApiError)),
                    None,
                )
                if booking_error is not None:
                    raise booking_error
                raise errors[0]
            for date, slots in zip(chunk, chunk_results, strict=True):
                fetched[date] = slots
                filtered_slots = self._filter_search_constraints(
                    slots, **request.filters
                )
                if filtered_slots:
                    verified.append(date)
                    latest_times[date] = max(filtered_slots)[11:16]
                    if len(verified) == verification_limit:
                        break
            if len(verified) == verification_limit:
                checked_end_date = chunk[-1]
                break
        return verified, latest_times, checked_end_date

    def _finish_search(
        self, request: _SearchRequest, outcome: _SearchOutcome
    ) -> dict[str, Any]:
        """Shared search epilogue: range facts, saved conditions, gate, offers.

        Every successful strategy ends here; the order of the state updates
        below is part of the contract (resume context before the gate, the gate
        before the pending-empty wait, the blocker and offer last).
        """
        result = outcome.result
        start_date = request.start_date
        end_date = request.end_date
        requested_end_date = request.requested_end_date
        checked_end_date = outcome.checked_end_date or end_date
        other_advisor_hint = outcome.other_advisor_hint
        if other_advisor_hint is not None and not (
            result.get("slots") or result.get("available_dates")
        ):
            result[other_advisor_hint[0]] = other_advisor_hint[1]
        result["searched_range"] = {
            "start_date": start_date,
            "end_date": checked_end_date,
            "spoken": spoken_date_range(
                start_date, checked_end_date, self._spoken_lang
            ),
        }
        result["requested_range_complete"] = checked_end_date == requested_end_date
        if window := self._searched_time_window(
            request.time_from, request.time_to, request.target_time
        ):
            result["searched_time_window"] = window
        horizon = self._date_horizon().isoformat()
        is_empty = not (result.get("slots") or result.get("available_dates"))
        if is_empty:
            self._empty_searched_ranges.append(f"{start_date}~{checked_end_date}")
            result["availability"] = "none_in_searched_range"
            result["search_horizon_reached"] = checked_end_date >= horizon
            if checked_end_date < min(requested_end_date, horizon):
                result["next_start_date"] = (
                    dt.date.fromisoformat(checked_end_date) + dt.timedelta(days=1)
                ).isoformat()
        else:
            self._empty_searched_ranges = []
        self._last_search_conditions = {
            "service_id": request.service_id,
            "operator_id": request.operator_id,
            "start_date": start_date,
            "end_date": checked_end_date,
            "time_from": request.time_from,
            "time_to": request.time_to,
            "target_time": request.target_time,
            "strictly_after": request.strictly_after,
        }
        self._save_resume_context()
        self._add_range_adjustment(
            result,
            requested_end_date=requested_end_date,
            applied_end_date=end_date,
            reason=request.range_adjustment_reason,
        )
        self._add_confirmation_gate(
            result,
            service_id=request.service_id,
            operator_id=request.operator_id,
            total_slot_count=outcome.total_slot_count,
            earliest_result=request.earliest_only,
            offer_earliest=request.offer_earliest,
        )
        nearby = self._has_nearby_time_offer(result)
        if is_empty or nearby:
            self._pending_empty_search = result
            self._pending_empty_search_user_message_id = request.user_message_id
        if request.moved_on:
            # The caller went past the options presented last time.
            self._count_declined_alternative()
        if request.mode != "earliest":
            self._last_presented = frozenset(
                slot["datetime"]
                for slot in (result.get("slots") or [])
                if isinstance(slot, dict) and isinstance(slot.get("datetime"), str)
            ) | frozenset(
                item["date"]
                for item in (result.get("available_dates") or [])
                if isinstance(item, dict) and isinstance(item.get("date"), str)
            )
        if offer := self._update_schedule_blocker(
            result,
            is_empty=is_empty,
            nearby=nearby,
            availability_only=request.mode == "earliest",
        ):
            self._attach_offer(result, *offer)
        return self._record_slots_searched(
            result,
            service_id=request.service_id,
            operator_id=request.operator_id,
        )

    @function_tool
    @_log_tool_io
    async def search_schedules(
        self,
        start_date: str,
        end_date: str,
        service_id: NullableToolInt = None,
        operator_id: NullableToolInt = None,
        time_from: NullableToolStr = None,
        time_to: NullableToolStr = None,
        target_time: NullableToolStr = None,
        strictly_after: NullableToolStr = None,
        mode: Literal["list", "earliest", "earliest_for_booking"] = "list",
    ) -> dict[str, Any]:
        """Search grounded availability; pass every active condition.

        Dates are date-only ``YYYY-MM-DD``. Omit unused args or use JSON null;
        never invent IDs or times as placeholders. One day returns slots; multiple days
        return dates. In list mode, a one-day exact-time miss may return up to
        three verified slots within three hours, nearest first. Time-filtered ranges
        check at most seven days. Empty applies only to searched_range and
        filters: announce it and wait.

        service_id and operator_id must come from the injected lists. Omit
        service_id only when no service is selected and operator_id only
        when no advisor is requested; never use 0 as an unspecified ID.
        Available-service questions use service_id=null before asking for
        the device issue or service. "Other services" clears that filter; preserve
        the caller's date/time and requested advisor.

        status=error with code schedule_lookup_failed is a system failure to
        announce. retry=silent means fix arguments and retry without speaking;
        retry=clarify means ask the caller before changing conflicting constraints.

        Times use local HH:MM. No requested window means time_from/time_to=null,
        never invent a range. "오전 11시" is target_time only; "오전만, 가능하면 11시"
        keeps both. "오전만+13시" conflicts: ask before searching; never invent or
        change bounds. Inclusive bounds also constrain nearby alternatives.
        A window alone uses target_time=null.
        오전/아침 00:00..11:59, 오후 12:00..23:59, 저녁 17:00..23:59.
        Use strictly_after only for an explicit next/later
        request ("다음", "이후"), never for an exact-time question. Consent to
        "다른 시간" removes target_time but does not mean later: do not add
        strictly_after or a new lower time bound without that request.

        mode=list is the default. mode=earliest returns the earliest slot for an
        availability-only question. mode=earliest_for_booking is for booking intent
        without date/time, requires service_id, and follows its stage_booking
        action. Search results alone never authorize a booking-consent question.

        Speak dates only via spoken/date_spoken/searched_range.spoken; never
        recompute them. date_corrected means the search ran on the date the
        caller named: present that date and do not mention the replaced one.
        Present at most three choices; honor presentation.has_more.
        latest_time_comparison covers only returned dates and filters. Without
        service_id, name available_services and wait for a choice.
        A returned handoff_offer replaces the alternative question: ask it.
        """
        operator_id = _normalize_nullable_tool_value(operator_id)
        # Search-only compatibility: zero means no advisor preference.
        if operator_id == 0 or (
            isinstance(operator_id, str) and operator_id.strip() == "0"
        ):
            operator_id = None
        start_date, end_date, correction = self._apply_caller_date_hint(
            start_date, end_date
        )
        result = await self._search_schedules_impl(
            start_date=start_date,
            end_date=end_date,
            service_id=service_id,
            operator_id=operator_id,
            time_from=time_from,
            time_to=time_to,
            target_time=target_time,
            strictly_after=strictly_after,
            mode=mode,
        )
        if correction is not None and isinstance(result, dict):
            result["date_corrected"] = correction
        return result

    def _picks_presented(
        self,
        start_date: str,
        end_date: str,
        target_time: str | None,
        time_from: str | None,
        time_to: str | None,
    ) -> bool:
        """Whether a new search stays with an option the last search presented.

        True when there was no earlier booking search. Narrowing within the
        presented dates or choosing a presented time is not a decline; a search
        after an empty result is (the caller had to change the request).
        """
        presented = self._last_presented
        if presented is None:
            return True
        for option in presented:
            day, clock = option[:10], option[11:16]
            if not start_date <= day <= end_date:
                continue
            if not clock:
                return True
            if target_time is not None:
                if clock == target_time:
                    return True
            elif (time_from or "00:00") <= clock <= (time_to or "23:59"):
                return True
        return False

    def _update_schedule_blocker(
        self,
        result: dict[str, Any],
        *,
        is_empty: bool,
        nearby: bool,
        availability_only: bool,
    ) -> tuple[NoMatchingSlotBlocker, HandoffReasonCode] | None:
        """Keep the exact-request failure and return a help offer when it is due.

        Due: physical-issue evidence, an empty search through the booking
        horizon (nothing to propose), or enough declined alternatives.
        """
        if availability_only or not (is_empty or nearby):
            self._schedule_blocker = None
            return None
        conditions = self._last_search_conditions or {}
        searched = result.get("searched_range") or {}
        alternatives = len(result.get("slots") or []) if nearby else 0
        # Only verified alternatives count. other_advisors_may_be_available is a
        # date-level hint for grounding and never defers the offer.
        if result.get("other_advisors_available"):
            alternatives += 1
        blocker = NoMatchingSlotBlocker(
            searched_range=(searched.get("start_date"), searched.get("end_date")),
            filters={
                key: value
                for key, value in conditions.items()
                if value is not None and key not in {"start_date", "end_date"}
            },
            alternative_slot_count=alternatives,
            range_complete=bool(result.get("requested_range_complete", True)),
            horizon_reached=bool(result.get("search_horizon_reached", False)),
        )
        self._schedule_blocker = blocker
        result["request_match"] = {
            "matching_slot_count": 0,
            "alternative_slot_count": blocker.alternative_slot_count,
        }
        if self._safety_offer_available():
            return blocker, "staff_connection"
        # Nothing to propose: a multi-day range through the booking horizon.
        nothing_left = (
            is_empty
            and blocker.horizon_reached
            and blocker.searched_range[0] != blocker.searched_range[1]
        )
        if nothing_left or self._alternatives_exhausted():
            return blocker, "no_slot_memo"
        return None

    def _record_slots_searched(
        self,
        result: dict[str, Any],
        *,
        service_id: int | None,
        operator_id: int | None,
    ) -> dict[str, Any]:
        """일정 조회 결과를 통화 후 분석용 이벤트로 남기고 결과를 그대로 돌려준다.

        `count=0` 이 `NO_AVAILABILITY / CENTER_OPS` 귀인의 직접 근거다.
        이 이벤트가 없으면 예약 퍼널이 '보험 선택 → 블랙박스 → 완료' 가 되어
        어느 단계에서 깨졌는지 알 수 없다(설계 문서 §6.2 P0-A).
        """
        presentation = result.get("presentation")
        total_available = (
            presentation.get("total_available")
            if isinstance(presentation, dict)
            else None
        )
        slots = result.get("slots")
        target_time = result.get("target_time")
        if isinstance(target_time, str) and isinstance(slots, list):
            # Nearby options are visible to the caller, but the requested exact
            # time still had zero supply for the post-call diagnosis.
            count = sum(
                slot.get("datetime", "")[11:16] == target_time for slot in slots
            )
        else:
            count = (
                total_available
                if isinstance(total_available, int)
                else len(slots or result.get("available_dates") or [])
            )
        self.session.history.add_message(
            role="developer",
            content="",
            extra=agent_event_extra(
                "BOOKING_CREATE",
                status="SLOTS_SEARCHED",
                details={
                    "count": count,
                    "service_id": service_id,
                    "operator_id": operator_id,
                },
            ),
        )
        return result

    def _consume_earliest_offer(
        self,
        *,
        service_id: int,
        operator_id: int | None,
        datetime: str,
    ) -> bool:
        offer = self._pending_earliest_offer
        self._pending_earliest_offer = None
        return bool(
            offer is not None
            and offer.service_id == service_id
            and offer.datetime == datetime
            and (offer.operator_id is None or offer.operator_id == operator_id)
        )

    def _confirmation_facts(
        self,
        staged: StagedBooking,
        *,
        earliest_offer: bool = False,
    ) -> dict[str, Any]:
        """Ground the confirmation in staged facts, independent of prompt language."""
        call_config = getattr(self._call_session_data, "call_config_data", None)
        moment = dt.datetime.strptime(staged.datetime, "%Y-%m-%d %H:%M")
        facts: dict[str, Any] = {
            "language": call_config.language if call_config else "ko-KR",
            "service_name": staged.service_name,
            "datetime": staged.datetime,
            "date": moment.date().isoformat(),
            "time": moment.strftime("%H:%M"),
            "weekday_iso": moment.isoweekday(),
            "is_earliest": earliest_offer,
        }
        # Auto-assignment must not become a proactive advisor announcement.
        if self._select_operator:
            facts["operator_name"] = staged.operator_name
        return facts

    @function_tool
    @_log_tool_io
    async def stage_booking(
        self,
        ctx: RunContext_T,
        service_id: int,
        datetime: str,
        operator_id: NullableToolInt = None,
    ) -> dict[str, Any]:
        """Validate and stage one exact candidate before asking for consent.

        ``datetime`` must be grounded and formatted ``YYYY-MM-DD HH:MM``. Pass
        operator_id when the caller named an advisor; otherwise omit it. On success,
        ask one natural question using every value in ``confirmation``: full
        service, date, time, and operator_name only when present. Preserve
        the full service label, even repeated parent/child names and qualifiers.
        Localize weekday_iso without recalculating. If is_earliest is true,
        say "earliest"; otherwise do not. This is pending, not completed.
        Wait for explicit agreement, then commit confirmation.datetime.
        """
        ctx.disallow_interruptions()
        if waiting := self._awaiting_customer_after_no_availability():
            return waiting
        self._note_progress()
        self._reset_cancel_confirmation()
        self._invalidate_candidate()

        service = next(
            (d for d in self._services if d["id"] == service_id), None
        )
        if service is None:
            return self._error(
                "unknown_service", "주입된 서비스 목록의 id만 사용할 수 있습니다."
            )
        if operator_id is not None and operator_id not in self._advisors_by_id:
            return self._error(
                "unknown_advisor", "주입된 상담사 목록의 id만 사용할 수 있습니다."
            )

        when = normalize_slot_datetime(datetime)
        if when is None:
            return self._error(
                "invalid_datetime", "일시는 'YYYY-MM-DD HH:MM' 형식입니다."
            )
        today = now_kst()
        floor = today + dt.timedelta(minutes=TODAY_LEAD_MINUTES)
        horizon_str = self._date_horizon(today.date()).isoformat()
        if when <= f"{floor:%Y-%m-%d %H:%M}" or when[:10] > horizon_str:
            return self._error(
                "out_of_horizon",
                "지나갔거나 조회 범위 밖의 일시입니다.",
                allowed_range=f"{floor:%Y-%m-%d %H:%M} ~ {horizon_str}",
            )

        async with self._mutation_lock:
            if self._candidate_is_rejected(when):
                self._set_staged(None)
                return self._error(
                    "candidate_rejected",
                    "The caller declined this candidate/date. Search for another "
                    "candidate. Only a later explicit reselection may reconsider it.",
                )
            try:
                slots = await self._fetch_day(
                    when[:10], service_id=service_id, operator_id=operator_id
                )
            except BookingApiError as error:
                return self._error("schedule_lookup_failed", str(error))
            slots = self._filter_rejected_candidates(slots)
            advisors = slots.get(when, [])
            if not advisors or (operator_id is not None and operator_id not in advisors):
                self._pending_earliest_offer = None
                self._set_staged(None)
                result = self._add_confirmation_gate(
                    {
                        "status": "unavailable",
                        "requested": spoken_datetime(when, self._spoken_lang),
                        "slots": [],
                        "target_time": when[11:16],
                        "searched_range": {
                            "start_date": when[:10],
                            "end_date": when[:10],
                            "spoken": spoken_date(when[:10], self._spoken_lang),
                        },
                        "search_horizon_reached": when[:10] >= horizon_str,
                    },
                    service_id=service_id,
                    operator_id=operator_id,
                )
                # Alternatives outside the attempted time need caller permission.
                # Reuse the search wait gate so another tool cannot bypass it.
                self._pending_empty_search = result
                self._pending_empty_search_user_message_id = (
                    self._latest_user_message_id()
                )
                return result
            # select_operator=true 는 "상담사를 고객이 고른다" 는 센터 설정이다.
            # search 경로의 gate 는 이미 다중 후보에서 자동 staging 을 막고 있는데
            # ([D7] 배정이 stage 안에 있어) 여기엔 같은 조건이 없었다. 그래서 LLM 이
            # 다음 턴에 stage_booking 을 직접 부르면 설정을 우회해 배정이 확정됐다.
            # 두 경로의 정책을 일치시킨다.
            if self._select_operator and operator_id is None and len(advisors) > 1:
                return {
                    "status": "operator_choice_required",
                    "requested": spoken_datetime(when, self._spoken_lang),
                    "available_operators": self._advisor_facts(advisors),
                }
            assigned = operator_id if operator_id is not None else advisors[0]  # [D7]
            earliest_offer = self._consume_earliest_offer(
                service_id=service_id,
                operator_id=operator_id,
                datetime=when,
            )
            self._set_staged(
                StagedBooking(
                    service_id=service_id,
                    service_name=service["name"],
                    operator_id=assigned,
                    operator_name=self._advisors_by_id[assigned]["name"],
                    datetime=when,
                )
            )
            # A caller may choose an exact candidate before any search tool ran.
            # Retain its appointment/explicit advisor across interruption, but never
            # turn an offered candidate's timestamp into a customer time preference.
            conditions = dict(self._last_search_conditions or {})
            conditions["service_id"] = service_id
            if operator_id is not None:
                conditions["operator_id"] = operator_id
            self._last_search_conditions = conditions
            self._save_resume_context()
            staged = self._staged
            assert staged is not None
            logger.bind(
                event="DYNAMIC_BOOKING_TASK",
                stage="staged",
                room_name=self._call_session_data.room_name,
                assigned=(operator_id is None),
                **staged.__dict__,
            ).info("Booking staged")
            # 서비스·상담사·일시가 한 번에 확정되는 지점. 통화 후 퍼널의
            # STAGED 칸이며, 여기까지 왔다가 확정 못 한 이탈이 보이게 된다.
            self.session.history.add_message(
                role="developer",
                content="",
                extra=agent_event_extra(
                    "BOOKING_CREATE",
                    status="STAGED",
                    details={
                        "service_id": staged.service_id,
                        "operator_id": staged.operator_id,
                        "datetime": staged.datetime,
                        "operator_assigned": operator_id is None,
                    },
                ),
            )
            # A verified candidate resolves the previous no-match blocker.
            self._schedule_blocker = None
            self._last_presented = None
            self._offer_pending = None
            self._request_revision += 1
            result: dict[str, Any] = {
                "status": "staged",
                "confirmation": self._confirmation_facts(
                    staged,
                    earliest_offer=earliest_offer,
                ),
                "required_next_action": "ask_booking_confirmation",
                "staged": dict(staged.__dict__),
            }
            if earliest_offer:
                result["confirmation_context"] = "earliest_offer"
            # 배정 근거는 select_operator=true 에서만 싣는다.
            #
            # false 는 "상담사를 먼저 묻지도 제안하지도 않는다" 는 모드다. 이 두
            # 필드를 주면 LLM 이 곧바로 "어느 선생님으로 하시겠어요?" 를 꺼낸다 —
            # available_operators 없이 operator_assigned 만 줘도 재현됐고,
            # 프롬프트로 금지하는 시도는 세 번 모두 실패했다. 고객에게 줄 수 없는
            # 선택지는 애초에 모델에게 보이지 않게 한다.
            #
            # true 모드에서는 이 값들이 필요하다. 없으면 LLM 이 받은 상담사 이름이
            # 유일한 선택지인지 [D7] 로 임의 배정된 것인지 구분할 수 없어, 임의
            # 배정을 "이 시간은 A 선생님만 가능" 처럼 배타적으로 서술하고 고객이
            # 되물으면 근거 없이 번복한다(통화 40966).
            if self._select_operator:
                result["operator_assigned"] = operator_id is None
                result["available_operators"] = self._advisor_facts(advisors)
            return result

    @function_tool
    @_log_tool_io
    async def commit_booking(self, confirmed_datetime: str) -> dict[str, Any] | None:
        """Commit only the candidate the caller just explicitly accepted.

        Copy ``confirmed_datetime`` from that confirmation. "네?", "예?" and
        "뭐라고요?" request repetition and are not consent. On any error, follow
        its recovery action; never force a match by retrying commit. Success is
        terminal for this task: output nothing, because the parent announces the
        result and ends the call.
        """
        self._note_progress()
        if waiting := self._offer_pending_guard():
            return waiting
        async with self._mutation_lock:
            if self._cancel_question is not None:
                return self._error(
                    "cancellation_pending",
                    "Resolve the stop-booking question before staging and asking "
                    "for fresh booking consent. No prior consent may be committed.",
                )
            staged = self._staged
            if staged is None:
                return self._error(
                    "nothing_staged",
                    "확정할 스테이징이 없습니다. 확인 질문을 말하기 전에 선택한 "
                    "후보로 stage_booking을 호출하고 반환된 confirmation 정보로 "
                    "통화 언어에 맞춰 확인 질문을 한 뒤 새 동의를 받으세요.",
                    required_next_tool="stage_booking",
                )
            if self._staged_requires_revalidation:
                return self._error(
                    "staging_requires_revalidation",
                    "이 후보는 이전 Task에서 저장되어 현재 가용성이 확인되지 "
                    "않았습니다. 말하기 전에 required_next_call대로 stage_booking을 "
                    "호출하고 새 confirmation 정보로 통화 언어에 맞춰 확인 질문을 "
                    "한 뒤 새 명시적 동의를 받으세요.",
                    required_next_call={
                        "tool": "stage_booking",
                        "args": {
                            "service_id": staged.service_id,
                            "datetime": staged.datetime,
                            "operator_id": staged.operator_id,
                        },
                    },
                )
            confirmed = normalize_slot_datetime(confirmed_datetime)
            if confirmed is None:
                return self._error(
                    "invalid_datetime", "일시는 'YYYY-MM-DD HH:MM' 형식입니다."
                )
            if confirmed != staged.datetime:
                logger.bind(
                    event="DYNAMIC_BOOKING_TASK",
                    stage="commit_mismatch",
                    room_name=self._call_session_data.room_name,
                    confirmed=confirmed,
                    **staged.__dict__,
                ).warning("Commit blocked: agreed datetime differs from staging")
                return self._error(
                    "staging_mismatch",
                    "커밋되지 않았습니다. 동의받은 일시가 스테이징된 일시와 "
                    "다릅니다. 지금 이 응답에서 말하기 전에 required_next_call "
                    "대로 stage_booking을 먼저 호출하고(고객이 상담사를 지정했다면 "
                    "operator_id 포함), 반환된 confirmation 정보로만 통화 언어에 "
                    "맞춰 확인 질문을 하고 새 명시적 동의를 받으세요. "
                    "stage_booking 없이 확인 질문을 반복하거나 commit_booking을 "
                    "재시도하지 마세요.",
                    staged=dict(staged.__dict__),
                    confirmed_datetime=confirmed,
                    required_next_call={
                        "tool": "stage_booking",
                        "args": {
                            "service_id": staged.service_id,
                            "datetime": confirmed,
                        },
                    },
                )
            # Fresh-consent check (scenario S07). Consent must be the first
            # and only user turn after the proposal. Zero turns means nobody
            # agreed; two or more means something else came up (an FAQ, a
            # question, an interruption) after the proposal, so it must be
            # presented again before consent counts.
            turns = self._consent_turns_since_stage()
            if turns is not None and turns != 1:
                logger.bind(
                    event="DYNAMIC_BOOKING_TASK",
                    stage="commit_stale_consent",
                    room_name=self._call_session_data.room_name,
                    user_turns_since_stage=turns,
                ).warning("Commit blocked: consent is not the turn right after the proposal")
                return self._error(
                    "consent_not_fresh",
                    "커밋되지 않았습니다. 동의는 확인 질문 바로 다음 고객 발화여야 "
                    "합니다. 확인 질문 이후 다른 대화가 있었거나 아직 답을 듣지 "
                    "않았습니다. 지금 이 응답에서 말하기 전에 required_next_call대로 "
                    "stage_booking을 다시 호출하고, 반환된 confirmation 정보로 확인 "
                    "질문을 한 뒤 새 명시적 동의를 받으세요.",
                    user_turns_since_stage=turns,
                    required_next_call={
                        "tool": "stage_booking",
                        "args": {
                            "service_id": staged.service_id,
                            "datetime": staged.datetime,
                            "operator_id": staged.operator_id,
                        },
                    },
                )
            # 가용성 재조회는 하지 않는다. stage_booking 이 같은 술어를 이미
            # 확인했고, 여기서 다시 물어도 닫히지 않는 레이스를 조금 좁힐 뿐이다 —
            # 통화 중에는 예약을 쓰는 API 자체가 없고(booking_api 는 전부 GET),
            # 중복 신청의 최종 판정은 센터의 사후 확정 단계가 한다.
            self._call_session_data.reservation_info = {
                "type": "NEW_RESERVATION",
                "service_id": staged.service_id,
                "operator_id": staged.operator_id,
                "reservation_request_date_time": f"{staged.datetime}:00",
            }
            self._set_staged(None)
            self._call_session_data.booking_memo_context = None
            logger.bind(
                event="DYNAMIC_BOOKING_TASK",
                stage="completed",
                room_name=self._call_session_data.room_name,
                **staged.__dict__,
            ).info("Dynamic booking committed")
            # 상태만 반환하고 종료 — 종료 멘트·오디오 제어·shutdown은 호출자 소유.
            self.complete(DynamicBookingResult(outcome="completed", reservation=staged))
            return None

    @function_tool
    @_log_tool_io
    async def exit_task(
        self,
        reason_code: DynamicBookingToolExitReasonCode = "other",
        detail: str = "",
        time_condition: str | None = None,
        route_target: NullableBookingRouteTarget = None,
        cancel_confirmed: NullableToolBool = None,
    ) -> dict[str, Any] | None:
        """Leave create-booking only for a clear stop, staff request, or new intent.

        For a whole-booking stop, call cancel; do not merely acknowledge it. This
        tool speaks the stop question; only a fresh explicit yes permits
        cancel_confirmed=true, false continues booking. Candidate refusal uses
        reject_candidate, never cancel. staff_connection: the caller explicitly
        asks for a person, or reports a device safety issue (battery swelling,
        overheating, smoke, burning smell). unrelated: existing-appointment
        requests; for unrelated/other choose route_target booking_inquiry,
        booking_modify, booking_cancel, end_call, other. Never use transfer;
        staff_connection omits route_target. Answer service center information here
        without exiting. Unbookable requests use report_booking_obstacle and
        replies to a handoff question use answer_handoff_offer, never this tool.
        Ground detail/time_condition in caller words. After completion output
        nothing; the parent handles it.
        """
        offer = self._offer_pending
        if (
            reason_code == "staff_connection"
            and offer is not None
            and self._offer_reply_is_fresh(offer)
        ):
            # Asking for a person in direct reply to the delivered offer is its
            # acceptance: keep the grounded reason so the caller is not asked for
            # it again (observed in sim: reason question after "네 연결해 주세요").
            return await self._complete_handoff(offer)
        async with self._mutation_lock:
            raw_reason_code = reason_code
            normalized_target = route_target_for_exit(raw_reason_code, route_target)
            normalized = normalize_exit_reason_code(raw_reason_code)
            if normalized not in DYNAMIC_BOOKING_EXIT_REASON_CODES:
                normalized = "other"
            normalized_code = cast(DynamicBookingExitReasonCode, normalized)
            # Models echo "null"/"None" for omitted text; the memo opening speaks
            # time_condition verbatim, so a sentinel must not survive here.
            detail = self._normalize_optional_tool_text(detail) or ""
            time_condition = self._normalize_optional_tool_text(time_condition)
            if normalized_code in {"no_slot_memo", "service_not_offered"}:
                # Offers and consent belong to the handoff controller; a model
                # exit can no longer be read as consent (formerly inferred).
                return self._error(
                    "use_handoff_tools",
                    "Answer a pending handoff question with answer_handoff_offer; "
                    "report an unbookable request with report_booking_obstacle. "
                    "Use staff_connection only for an explicit request for a person.",
                    required_next_tool=(
                        "answer_handoff_offer"
                        if self._offer_pending is not None
                        else "report_booking_obstacle"
                    ),
                    retry="silent",
                )
            if normalized_code == "cancel" and not self._booking_in_progress():
                # Nothing was chosen or verified (e.g. the caller declined help for
                # an unbookable request and wants nothing else): stopping needs no
                # confirmation question (sim create_hard_02 regression).
                self._note_progress()
                self._reset_cancel_confirmation()
                skip_confirmation = True
            else:
                skip_confirmation = False
            if normalized_code == "cancel" and not skip_confirmation:
                self._note_progress()
                if self._staged is not None:
                    self._last_candidate = self._staged
                self._pending_earliest_offer = None
                self._set_staged(None)
                if cancel_confirmed is False and self._cancel_response_received():
                    self._reset_cancel_confirmation()
                    return {
                        "status": "booking_continues",
                        "reason": "cancel_declined",
                        "next_tool_for_candidate_refusal": "reject_candidate",
                    }
                if not (cancel_confirmed is True and self._cancel_response_received()):
                    await self._request_cancel_confirmation()
                self._reset_cancel_confirmation()
            if normalized_code == "staff_connection":
                self._call_session_data.booking_memo_context = (
                    self._build_booking_memo_context(
                        detail=detail,
                        exit_reason_code=normalized_code,
                        time_condition=time_condition,
                    )
                )
            if normalized_code != "unrelated":
                self._set_staged(None)
            self._offer_pending = None
            logger.bind(
                event="DYNAMIC_BOOKING_TASK",
                stage="exited",
                room_name=self._call_session_data.room_name,
                exit_reason_code=normalized_code,
                detail=detail,
                route_target=normalized_target,
            ).info("Customer exited dynamic booking task")
            self.complete(
                DynamicBookingResult(
                    outcome="exited",
                    exit_reason_code=normalized_code,
                    detail=detail,
                    route_target=normalized_target,
                    stopped_before_selection=skip_confirmation,
                )
            )
            return None

    def _build_booking_memo_context(
        self,
        *,
        detail: str,
        exit_reason_code: str,
        time_condition: str | None,
    ) -> BookingMemoContext:
        """Create memo facts from grounded search state, never chat history."""
        conditions = getattr(self, "_last_search_conditions", None) or {}
        if exit_reason_code == "service_not_offered":
            return BookingMemoContext(
                detail=detail,
                exit_reason_code=exit_reason_code,
            )
        service_id = conditions.get("service_id")
        operator_id = conditions.get("operator_id")
        service_name = next(
            (
                service["name"]
                for service in self._services
                if service["id"] == service_id
            ),
            None,
        )
        operator = self._advisors_by_id.get(operator_id)
        language = getattr(
            getattr(self._call_session_data, "call_config_data", None),
            "language",
            None,
        )
        # 오프닝 문구는 say() 로 그대로 읽히므로 "2026-10-19"·"11:45" 같은 툴
        # 표기가 아니라 발화형("10월 19일 월요일"·"오전 11시 45분")으로 만든다.
        # Gemma 재현(2026-10-16): exit_task(time_condition="2026-10-19")를 그대로
        # 읽어 "2026-10-19 조건으로 요청"이 발화됐다.
        derived_condition = _spoken_condition_text(time_condition, language)
        if derived_condition is None:
            target_time = _spoken_clock(conditions.get("target_time"), language)
            time_from = _spoken_clock(conditions.get("time_from"), language)
            time_to = _spoken_clock(conditions.get("time_to"), language)
            english = language == "en-US"
            window = (conditions.get("time_from"), conditions.get("time_to"))
            if _is_whole_day(*window):
                # 하루 전체 조회는 시간 조건이 아니다("오전 12시부터 오후 11시
                # 59분 사이"로 읽히던 것을 막는다).
                time_from = time_to = None
            if target_time:
                derived_condition = target_time
            elif window == ("00:00", "11:59"):
                derived_condition = "morning" if english else "오전"
            elif window == ("12:00", "23:59"):
                derived_condition = "afternoon" if english else "오후"
            elif time_from and time_to:
                derived_condition = (
                    f"between {time_from} and {time_to}"
                    if english
                    else f"{time_from}부터 {time_to} 사이"
                )
            elif time_from:
                derived_condition = (
                    f"after {time_from}" if english else f"{time_from} 이후"
                )
            elif time_to:
                derived_condition = (
                    f"before {time_to}" if english else f"{time_to} 이전"
                )
        return BookingMemoContext(
            service_name=service_name,
            operator_name=operator["name"] if operator else None,
            time_condition=derived_condition,
            searched_ranges=list(getattr(self, "_empty_searched_ranges", None) or []),
            detail=detail,
            exit_reason_code=exit_reason_code,
        )
