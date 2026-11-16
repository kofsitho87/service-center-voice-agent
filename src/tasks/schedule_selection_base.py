from __future__ import annotations

import calendar
import datetime as dt
import re
from collections.abc import AsyncIterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from livekit.agents import AgentTask, FlushSentinel, ModelSettings, function_tool, llm
from livekit.agents.llm.chat_context import ChatContext
from loguru import logger

from data import RunContext_T
from tasks.booking.dynamic_dates import (
    _DATE_NEGATION_RE,
    CallerDateHint,
    caller_date_note,
    resolve_caller_dates,
)
from tasks.booking.exit_reasons import (
    BookingExitRouteTarget,
    BookingExitRouteTargetInput,
    ExitReasonCode,
    exit_reason_text,
    normalize_exit_reason_code,
    route_target_for_exit,
)
from utils.agent_event import agent_event_extra
from utils.i18n import t
from utils.korean_speech_guard import KoreanSpeechGuardMixin
from utils.spoken_datetime import (
    is_english,
    josa_ro,
    spoken_date,
    spoken_datetime,
    spoken_time,
)

# ko-KR 이외(영어 등)이면 True. (None은 한국어로 취급)
_is_en = is_english


def _tz_for_language(language: str | None) -> ZoneInfo:
    """Business timezone used for "today" (same rule as utils.common.load_prompt).

    The business is in Seoul, so every caller language uses Asia/Seoul and the
    prompt's "current time" matches the "today" computed here.
    """
    return ZoneInfo("Asia/Seoul")


def _schedule_lookup_range(
    lookup_days: int, language: str | None = None
) -> tuple[str, str]:
    """Return the exact inclusive date range used by prompts and schedule tools."""
    today = datetime.now(tz=_tz_for_language(language))
    start_date = today.strftime("%Y-%m-%d")
    end_date = (today + timedelta(days=lookup_days)).strftime("%Y-%m-%d")
    return start_date, end_date


def _day_name(weekday: int, language: str | None = None) -> str:
    """요일명. ko-KR: '토요일' / 그 외: 'Saturday'."""
    if _is_en(language):
        return calendar.day_name[weekday]
    return DAY_NAMES_KR[weekday]


DAY_NAMES_KR = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]
DEFAULT_LOOKUP_DAYS = 30
MAX_TIMES_TO_ENUMERATE = 4
MAX_TIMES_PER_DATE_IN_SEARCH = 3
MAX_DATES_TO_ENUMERATE_IN_SEARCH = 5
MAX_DATES_TO_ENUMERATE_IN_RANGE = 3

# Hour ranges for time-of-day filtering: [start_hour, end_hour)
PERIOD_HOUR_RANGES: dict[str, tuple[int, int]] = {
    "morning": (0, 12),
    "afternoon": (12, 17),
    "evening": (17, 24),
}
PERIOD_NAMES_KR: dict[str, str] = {
    "morning": "오전",
    "afternoon": "오후",
    "evening": "저녁",
}


@dataclass
class ScheduleSelectionResult:
    """User-selected schedule slot."""

    appointment_datetime: str | None = None
    advisor_id: int | None = None
    exited: bool = False
    exit_reason: str | None = None
    exit_reason_code: str | None = None
    route_target: BookingExitRouteTarget | None = None
    lookup_failed: bool = False
    lookup_failure_message: str | None = None


# Legacy names for the shared spoken formatters. Despite the suffix they are
# bilingual (ko-KR phrasing, English for every other language).
_format_spoken_datetime_korean = spoken_datetime
_format_spoken_date_korean = spoken_date
_format_spoken_time_korean = spoken_time


def _try_parse_yyyy_mm_dd(text: str) -> str | None:
    text = (text or "").strip()
    try:
        datetime.strptime(text, "%Y-%m-%d")
        return text
    except Exception:
        return None


def _relative_day_phrase(date_str: str, language: str | None = None) -> str:
    """'오늘 '/'내일 '/'모레 ' (en: 'today '/'tomorrow '/'the day after tomorrow ') prefix
    for nearby dates, '' otherwise. Makes announcements sound like a human agent."""
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        return ""
    today = datetime.now(tz=_tz_for_language(language)).date()
    diff = (target - today).days
    if _is_en(language):
        return {0: "today ", 1: "tomorrow ", 2: "the day after tomorrow "}.get(diff, "")
    return {0: "오늘 ", 1: "내일 ", 2: "모레 "}.get(diff, "")


def _spoken_date_with_day(date_str: str, language: str | None = None) -> str:
    """ko: '6월 13일 토요일' / en: 'June 13 (Saturday)'."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    spoken = _format_spoken_date_korean(date_str, language)
    day = _day_name(dt.weekday(), language)
    return f"{spoken} ({day})" if _is_en(language) else f"{spoken} {day}"


# Kept importable here: dynamic_booking_task imports it from this module.
_josa_ro = josa_ro


# Spoken-Korean time expression, e.g. "저녁 7시", "11시 반", "10시 5분 전",
# "오전 9시 30분이요". Trailing particles (요/이요/쯤/으로...) are simply left
# unmatched, so they don't break parsing.
_TIME_PATTERN = re.compile(
    r"(?:(?P<prefix>오전|오후|아침|저녁|새벽|밤|낮)\s*)?"
    r"(?P<hour>\d{1,2})\s*시"
    r"(?:\s*(?:(?P<minute>\d{1,2})\s*분|(?P<half>반)|(?P<sharp>정각)))?"
    r"(?P<before>\s*전(?:\s|$))?"
)

_AM_PREFIXES = ("오전", "아침", "새벽")
_PM_PREFIXES = ("오후", "저녁", "밤")
_AMPM_MARKERS = ("오전", "오후", "아침", "저녁", "새벽", "밤", "낮", "정오", "자정")


def _parse_time_to_hhmm(text: str) -> str | None:
    """
    Parse common spoken-Korean/number time expressions into 'HH:MM'.
    Accepts: '오전 9시', '오전9시30분', '저녁 7시', '아침 9시', '낮 12시',
    '15시', '15:30', '9:30', '11시 반', '10시 정각', '정오',
    '10시 5분 전' (-> 09:55), '10시요' (trailing particles ignored),
    '오후 17:00.' (trailing STT punctuation ignored, colon form included).
    Also accepts a redundant AM/PM marker on a 24h hour:
    '오후 18시' (-> 18:00), '오후 16:30' (-> 16:30), '저녁 19시' (-> 19:00).
    Rejects ambiguous/contradictory input: '10시 전', '4시에서 5시 사이',
    '13시 70분', '오전 16시' (AM marker + PM hour).
    """
    if not text:
        return None
    t = str(text).strip()
    # STT often appends sentence punctuation ("오후 17:00." → the colon branch's
    # `parts[1].isdigit()` saw "00." and rejected the whole utterance, causing a
    # re-request loop). Trailing punctuation never carries time meaning, so drop
    # it up front and every branch sees clean digits. The 시-form regex already
    # tolerated it; this brings the colon form to parity.
    t = t.rstrip(" .,!?…。·")
    if not t:
        return None
    t_nospace = t.replace(" ", "")

    if ":" in t_nospace:
        # Strip a leading 오전/오후-type marker so "오후 16:30" / "오후 4:30" parse.
        colon_body = t_nospace
        colon_marker = ""
        for marker in (*_PM_PREFIXES, *_AM_PREFIXES):
            if colon_body.startswith(marker):
                colon_marker = marker
                colon_body = colon_body[len(marker) :]
                break
        parts = colon_body.split(":")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            hh = int(parts[0])
            mm = int(parts[1])
            if not (0 <= mm <= 59):
                return None
            if colon_marker in _PM_PREFIXES:
                if 1 <= hh <= 11:
                    hh += 12  # "오후 4:30" -> 16:30
                elif not (12 <= hh <= 23):
                    return None  # "오후 0:30" 같은 비정상
                # 12~23 (예: "오후 16:30" 중복 마커, "오후 12:30" 정오대)은 그대로
            elif colon_marker in _AM_PREFIXES:
                if hh == 12:
                    hh = 0  # "오전 12:30" -> 00:30
                elif not (0 <= hh <= 11):
                    return None  # "오전 16:30" 모순
            if 0 <= hh <= 23:
                return f"{hh:02d}:{mm:02d}"
        return None

    if "정오" in t:
        return "12:00"
    if "자정" in t:
        return "00:00"

    matches = list(_TIME_PATTERN.finditer(t))
    if len(matches) != 1:
        # No time at all, or several ("4시에서 5시 사이") — ambiguous.
        return None
    m = matches[0]

    hh = int(m.group("hour"))
    if m.group("minute") is not None:
        mm = int(m.group("minute"))
    elif m.group("half"):
        mm = 30
    else:  # bare hour or 정각
        mm = 0
    if not (0 <= mm <= 59):
        return None

    is_before = bool(m.group("before"))
    if is_before and m.group("minute") is None:
        # "10시 전" = some time before 10 — too vague, re-ask.
        return None

    prefix = m.group("prefix")
    if prefix in _AM_PREFIXES or prefix in _PM_PREFIXES:
        if 13 <= hh <= 23:
            # 24시간제 오후 시각에 마커가 덧붙은 경우 ("오후 18시" = 18:00).
            # 오후 계열은 일관되므로 그대로 두고, "오전 18시"는 모순이라 거부.
            if prefix in _AM_PREFIXES:
                return None
        elif not (1 <= hh <= 12):
            return None
        else:
            if hh == 12:
                hh = 0
            if prefix in _PM_PREFIXES:
                hh += 12
    elif prefix == "낮":
        if 1 <= hh <= 6:
            hh += 12
        elif not (7 <= hh <= 12):
            return None
    else:
        if not (0 <= hh <= 23):
            return None

    if is_before:
        # "10시 5분 전" = 5 minutes BEFORE 10:00.
        total = hh * 60 - mm
        if total < 0:
            return None
        return f"{total // 60:02d}:{total % 60:02d}"

    return f"{hh:02d}:{mm:02d}"


def _ambiguous_pm_fallback(time_text: str, hhmm: str) -> str | None:
    """PM reading for bare-hour utterances. '3시' parses to 03:00, but in a
    service-center booking context the user almost always means 15:00 — when the AM reading
    matches no slot, callers retry with this value."""
    if any(marker in str(time_text or "") for marker in _AMPM_MARKERS):
        return None
    try:
        hh, mm = (int(x) for x in hhmm.split(":"))
    except Exception:
        return None
    if 1 <= hh <= 11:
        return f"{hh + 12:02d}:{mm:02d}"
    return None


def _get_raw_time(item: Any) -> str | None:
    raw_time = getattr(item, "time", None)
    if raw_time is None and isinstance(item, dict):
        raw_time = item.get("time")
    return raw_time


def _get_advisor_id(item: Any) -> int | None:
    advisor_id = getattr(item, "advisor_id", None)
    if advisor_id is None and isinstance(item, dict):
        advisor_id = item.get("advisor_id")
    if advisor_id is None:
        return None
    return int(advisor_id)


def _tool_response(
    *,
    status: str,
    response_type: str,
    user_message: str,
    next_action: str,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "status": status,
        "response_type": response_type,
        "user_message": user_message,
        "next_action": next_action,
        **extra,
    }


class BaseScheduleSelectionTask(
    KoreanSpeechGuardMixin, AgentTask[ScheduleSelectionResult]
):
    """Shared date->time->confirm scheduling flow over a prefetched snapshot.

    Subclasses provide only the data source (`_fetch_schedules`), their prompt
    `instructions`, and the on_enter developer-tag name (`_TASK_TAG`).
    """

    _TASK_TAG: str = "SCHEDULE_SELECTION_TASK"

    def __init__(
        self,
        *,
        instructions: str,
        reservation_data: dict[str, Any],
        action_kr: str,
        lookup_days: int,
        language_code: str = "ko-KR",
        chat_ctx: ChatContext | None = None,
        allow_interruptions: bool | None = None,
        search_start_date: str | None = None,
        search_end_date: str | None = None,
    ) -> None:
        self._reservation_data = reservation_data
        self._action_kr = action_kr
        self._language_code = language_code
        # 통화 언어용 액션 단어 ("변경"→change, "신청"→book). 템플릿/발화에 사용.
        self._action_word = t(
            "schedule.action.create"
            if action_kr == "신청"
            else "schedule.action.modify",
            language_code,
        )

        if search_start_date is None or search_end_date is None:
            search_start_date, search_end_date = _schedule_lookup_range(
                lookup_days, language_code
            )
        self._search_start_date = search_start_date
        self._search_end_date = search_end_date
        self._lookup_days: int = lookup_days

        self._schedules: list[Any] = []
        self._availability_loaded = True
        self._announced_dates: set[str] = set()
        self._stage: str = "show_dates"
        self._pending_pick: dict[str, Any] | None = None
        self._last_offered_slots: list[str] = []
        self._unclear_time_count: int = 0
        self._date_note_logged_message_id: str | None = None
        self._date_hint_checked_message_id: str | None = None

        super_kwargs: dict[str, Any] = {
            "instructions": instructions,
            "chat_ctx": chat_ctx,
        }
        if allow_interruptions is not None:
            super_kwargs["allow_interruptions"] = allow_interruptions
        super().__init__(**super_kwargs)

    def _date_horizon(self, today: dt.date) -> dt.date:
        try:
            return datetime.strptime(self._search_end_date, "%Y-%m-%d").date()
        except (AttributeError, TypeError, ValueError):
            return today + timedelta(days=DEFAULT_LOOKUP_DAYS)

    def _caller_date_hints(
        self, chat_ctx: ChatContext | None = None
    ) -> tuple[llm.ChatMessage | None, list[CallerDateHint]]:
        if chat_ctx is None:
            chat_ctx = self.chat_ctx
        latest = next(
            (
                item
                for item in reversed(chat_ctx.items)
                if getattr(item, "role", None) == "user"
                and (getattr(item, "text_content", None) or "").strip()
            ),
            None,
        )
        if latest is None:
            return None, []
        today = datetime.now(tz=_tz_for_language(self._language_code)).date()
        return latest, resolve_caller_dates(
            latest.text_content, today, self._date_horizon(today)
        )

    def _with_caller_date_note(self, chat_ctx: ChatContext) -> ChatContext:
        latest, hints = self._caller_date_hints(chat_ctx)
        if latest is None:
            return chat_ctx
        today = datetime.now(tz=_tz_for_language(self._language_code)).date()
        note = caller_date_note(
            hints,
            self._date_horizon(today),
            tool_wording="날짜 인자(start_date/end_date/date)",
        )
        if note is None:
            return chat_ctx
        if self._date_note_logged_message_id != latest.id:
            self._date_note_logged_message_id = latest.id
            logger.bind(
                task=self._TASK_TAG,
                stage="caller_date_note",
                hints=[(hint.phrase, hint.date, hint.status) for hint in hints],
            ).info("Resolved caller date expressions for this turn")
        noted = chat_ctx.copy()
        noted.insert(
            llm.ChatMessage(
                role="system", content=[note], created_at=latest.created_at + 1e-3
            )
        )
        return noted

    async def llm_node(
        self,
        chat_ctx: ChatContext,
        tools: list[llm.Tool],
        model_settings: ModelSettings,
    ) -> AsyncIterable[llm.ChatChunk | str | FlushSentinel]:
        # Text input skips on_user_turn_completed, so ground dates here too.
        async for chunk in super().llm_node(
            self._with_caller_date_note(chat_ctx), tools, model_settings
        ):
            yield chunk

    def _apply_caller_date_hint(
        self, start_date: str, end_date: str
    ) -> tuple[str, str, dict[str, Any] | None]:
        """Correct only the turn's first date-bearing call on one wrong day."""
        latest, hints = self._caller_date_hints()
        if latest is None or latest.id == self._date_hint_checked_message_id:
            return start_date, end_date, None
        self._date_hint_checked_message_id = latest.id
        if len(hints) != 1 or hints[0].status != "ok":
            return start_date, end_date, None
        hint = hints[0]
        if start_date != end_date or start_date == hint.date:
            return start_date, end_date, None
        if _DATE_NEGATION_RE.search(latest.text_content or ""):
            return start_date, end_date, None
        logger.bind(
            task=self._TASK_TAG,
            stage="caller_date_hint",
            phrase=hint.phrase,
            expected=hint.date,
            searched=f"{start_date}~{end_date}",
        ).warning("Corrected one-day request to the date the caller named")
        correction = {
            "from": start_date,
            "to": hint.date,
            "phrase": hint.phrase,
            "instruction": (
                f"고객이 말한 '{hint.phrase}'은 {hint.date}({hint.weekday}요일)이라 "
                f"그 날짜로 조회했습니다. {start_date}은 언급하지 말고 "
                "이 결과의 user_message로 안내하세요."
            ),
        }
        return hint.date, hint.date, correction

    async def _fetch_schedules(self, start_date: str, end_date: str) -> list[Any]:
        """Return normalized schedule items [{"time": "YYYY-MM-DD HH:MM:SS",
        "advisor_id": int}, ...]. Implemented per data source by subclasses."""
        raise NotImplementedError

    def _require_availability(self) -> dict[str, Any] | None:
        if self._availability_loaded:
            return None
        return _tool_response(
            status="lookup_required",
            response_type="lookup_required",
            user_message=(
                "먼저 원하시는 날짜나 요일, 시간대를 말씀해 주세요."
                if not _is_en(self._language_code)
                else "Please tell me your preferred date, weekday, or time first."
            ),
            next_action="search_preferred_schedules",
        )

    def _date_exists_in_cache(self, date_str: str) -> bool:
        return any(slot.strftime("%Y-%m-%d") == date_str for slot in self._slots())

    def _is_date_inside_cached_range(self, date_str: str) -> bool:
        return self._search_start_date <= date_str <= self._search_end_date

    def _slots(self) -> list[datetime]:
        slots: list[datetime] = []
        for item in self._schedules:
            raw_time = _get_raw_time(item)
            if not raw_time:
                continue
            try:
                slots.append(datetime.strptime(raw_time, "%Y-%m-%d %H:%M:%S"))
            except Exception:
                continue
        slots.sort()
        return slots

    def _slots_for_date(self, date_str: str) -> list[datetime]:
        seen: set[str] = set()
        slots: list[datetime] = []
        for slot in self._slots():
            if slot.strftime("%Y-%m-%d") != date_str:
                continue
            key = slot.strftime("%H:%M")
            if key in seen:
                continue
            seen.add(key)
            slots.append(slot)
        return slots

    def _available_dates(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        only_unannounced: bool = False,
    ) -> list[str]:
        date_set = {slot.strftime("%Y-%m-%d") for slot in self._slots()}
        if start_date is not None:
            date_set = {date for date in date_set if date >= start_date}
        if end_date is not None:
            date_set = {date for date in date_set if date <= end_date}
        if only_unannounced:
            date_set -= self._announced_dates
        return sorted(date_set)

    def _available_times_for_date(self, date_str: str) -> list[str]:
        return [
            _format_spoken_time_korean(slot, self._language_code)
            for slot in self._slots_for_date(date_str)
        ]

    def _get_earliest_slot(self) -> datetime | None:
        slots = self._slots()
        if not slots:
            return None
        return slots[0]

    def _render_earliest_date_text(self) -> str | None:
        earliest_dt = self._get_earliest_slot()
        if earliest_dt is None:
            return None

        date_str = earliest_dt.strftime("%Y-%m-%d")
        lang = self._language_code
        day_name = _day_name(earliest_dt.weekday(), lang)
        self._announced_dates.add(date_str)

        spoken_date = _format_spoken_date_korean(date_str, lang)
        rel = _relative_day_phrase(date_str, lang)
        return t(
            "schedule.earliest_date",
            lang,
            rel=rel,
            spoken_date=spoken_date,
            day_name=day_name,
        )

    def _render_available_dates_text(self, *, only_unannounced: bool = False) -> str:
        available_dates = self._available_dates(only_unannounced=only_unannounced)

        lang = self._language_code
        if not available_dates:
            return t("schedule.no_more_dates", lang, action=self._action_word)

        lines: list[str] = []
        for date_str in available_dates:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            lines.append(
                t(
                    "schedule.date_line",
                    lang,
                    spoken_date=_format_spoken_date_korean(date_str, lang),
                    day_name=_day_name(dt.weekday(), lang),
                )
            )

        self._announced_dates.update(available_dates)
        return t(
            "schedule.available_dates_header",
            lang,
            action=self._action_word,
            lines="\n".join(lines),
        )

    def _render_dates_for_range(self, start_date: str, end_date: str) -> str:
        available_dates = self._available_dates(
            start_date=start_date, end_date=end_date
        )

        lang = self._language_code
        start_spoken = _format_spoken_date_korean(start_date, lang)
        end_spoken = _format_spoken_date_korean(end_date, lang)
        is_single_day = start_date == end_date
        # Avoid awkward "6월 5일부터 6월 5일까지" when the range is a single day.
        if is_single_day:
            range_phrase = start_spoken
            range_topic = start_spoken if _is_en(lang) else f"{start_spoken}은"
        else:
            range_phrase = t("schedule.date_range", lang, a=start_spoken, b=end_spoken)
            range_topic = (
                range_phrase
                if _is_en(lang)
                else f"{start_spoken}부터 {end_spoken}까지는"
            )

        if not available_dates:
            return t(
                "schedule.range_no_dates",
                lang,
                range_topic=range_topic,
                action=self._action_word,
            )

        self._announced_dates.update(available_dates)

        names = ", ".join(
            _spoken_date_with_day(date_str, lang)
            for date_str in available_dates[:MAX_DATES_TO_ENUMERATE_IN_RANGE]
        )

        # Voice call: offer the nearest few dates and a question, the way a
        # human agent would — never read a month of dates aloud.
        if len(available_dates) > MAX_DATES_TO_ENUMERATE_IN_RANGE:
            last_spoken = _format_spoken_date_korean(available_dates[-1], lang)
            return t(
                "schedule.range_many",
                lang,
                range_phrase=range_phrase,
                names=names,
                last_spoken=last_spoken,
            )

        return t(
            "schedule.range_few",
            lang,
            range_phrase=range_phrase,
            action=self._action_word,
            names=names,
        )

    def _render_period_note(self, date_str: str) -> str:
        """One-line summary like '이 날은 오후 시간만 가능합니다.' so a customer
        who wants a morning slot can move on without hearing the full list."""
        slots = self._slots_for_date(date_str)
        if not slots:
            return ""
        if all(slot.hour >= 12 for slot in slots):
            return t("schedule.pm_only", self._language_code)
        if all(slot.hour < 12 for slot in slots):
            return t("schedule.am_only", self._language_code)
        return ""

    def _render_times_for_date(self, date_str: str) -> str:
        times = self._available_times_for_date(date_str)

        lang = self._language_code
        spoken_date = _format_spoken_date_korean(date_str, lang)
        if not times:
            return t(
                "schedule.no_times_for_date",
                lang,
                spoken_date=spoken_date,
                action=self._action_word,
            )

        period_note = self._render_period_note(date_str)

        if len(times) > MAX_TIMES_TO_ENUMERATE:
            windows = self._render_windows_for_date(date_str)
            if windows:
                return t(
                    "schedule.times_windows",
                    lang,
                    spoken_date=spoken_date,
                    windows=windows,
                    action=self._action_word,
                    period_note=period_note,
                )

        return t(
            "schedule.times_list",
            lang,
            spoken_date=spoken_date,
            action=self._action_word,
            period_note=period_note,
            times=", ".join(times),
        )

    def _times_for_date_response(self, date_str: str) -> dict[str, Any]:
        self._last_offered_slots = []
        return _tool_response(
            status="ok",
            response_type="times_for_date",
            user_message=self._render_times_for_date(date_str),
            next_action="ask_time",
            date=date_str,
            available_times=self._available_times_for_date(date_str),
        )

    def _commit_date(self, date_str: str) -> dict[str, Any]:
        """Lock in a validated, available date and return its times."""
        self._reservation_data["reservation_date"] = date_str
        self._stage = "select_time"
        self._announced_dates.add(date_str)
        self._unclear_time_count = 0
        return self._times_for_date_response(date_str)

    def _find_slot_for(self, date_str: str, hhmm: str) -> tuple[str | None, int | None]:
        """Find the raw schedule entry matching date+time. Returns (raw_time, advisor_id)."""
        for item in self._schedules:
            raw_time = _get_raw_time(item)
            if not raw_time:
                continue
            try:
                dt = datetime.strptime(raw_time, "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            if dt.strftime("%Y-%m-%d") != date_str:
                continue
            if dt.strftime("%H:%M") != hhmm:
                continue
            advisor_id = _get_advisor_id(item)
            if advisor_id is None:
                continue
            return raw_time, advisor_id
        return None, None

    def _find_nearest_slots(
        self, date_str: str, target_hhmm: str, limit: int = 2
    ) -> list[datetime]:
        slots = self._slots_for_date(date_str)
        if not slots:
            return []
        try:
            target_h, target_m = (int(x) for x in target_hhmm.split(":"))
        except Exception:
            return []
        target_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(
            hour=target_h, minute=target_m
        )
        slots.sort(key=lambda d: abs((d - target_dt).total_seconds()))
        return slots[:limit]

    def _render_windows_for_date(self, date_str: str) -> str:
        return self._render_windows(self._slots_for_date(date_str))

    def _render_windows(self, slots: list[datetime]) -> str:
        if not slots:
            return ""
        if len(slots) == 1:
            return _format_spoken_time_korean(slots[0], self._language_code)

        gaps = [
            int((slots[i + 1] - slots[i]).total_seconds() // 60)
            for i in range(len(slots) - 1)
        ]
        gaps_sorted = sorted(gaps)
        median = gaps_sorted[len(gaps_sorted) // 2] or 10
        threshold = max(median * 2, median + 30)

        windows: list[tuple[datetime, datetime]] = []
        win_start = slots[0]
        prev = slots[0]
        for cur in slots[1:]:
            if int((cur - prev).total_seconds() // 60) > threshold:
                windows.append((win_start, prev))
                win_start = cur
            prev = cur
        windows.append((win_start, prev))

        lang = self._language_code
        parts: list[str] = []
        for a, b in windows:
            if a == b:
                # A window collapses to one slot when a gap isolates it (e.g.
                # 13:30 alone after a 10:00-11:30 block) — "1시 30분부터
                # 1시 30분까지" is nonsense, so speak the bare time instead.
                parts.append(_format_spoken_time_korean(a, lang))
            else:
                parts.append(
                    t(
                        "schedule.window_range",
                        lang,
                        a=_format_spoken_time_korean(a, lang),
                        b=_format_spoken_time_korean(b, lang),
                    )
                )
        return ", ".join(parts)

    async def on_enter(self) -> None:
        logger.bind(task=self._TASK_TAG, stage="enter").debug(
            f"********** {self._TASK_TAG} on_enter **********"
        )

        self._schedules = await self._fetch_schedules(
            self._search_start_date, self._search_end_date
        )

        earliest_dt = self._get_earliest_slot()
        earliest_date: str | None = None
        if earliest_dt:
            earliest_date = earliest_dt.strftime("%Y-%m-%d")
            self._pending_pick = None
            self._stage = "show_dates"

        earliest_text = self._render_earliest_date_text()
        self.session.history.add_message(
            role="developer",
            content="",
            extra=agent_event_extra(
                self._TASK_TAG,
                status="ON_ENTER",
                details={
                    "start_date": self._search_start_date,
                    "end_date": self._search_end_date,
                    "lookup_days": self._lookup_days,
                    "count": len(self._schedules),
                    "earliest_date": earliest_date,
                    "earliest": earliest_text,
                },
            ),
        )

        lang = self._language_code
        if earliest_text:
            self.session.generate_reply(
                tool_choice="none",
                instructions=t(
                    "schedule.on_enter_instruction",
                    lang,
                    earliest_text=earliest_text,
                ),
                allow_interruptions=False,
            )
            return

        self.session.generate_reply(
            tool_choice="none",
            instructions=t(
                "schedule.on_enter_no_dates",
                lang,
                start=_format_spoken_date_korean(self._search_start_date, lang),
                end=_format_spoken_date_korean(self._search_end_date, lang),
                action=self._action_word,
            ),
            allow_interruptions=False,
        )

    @function_tool
    async def select_date(self, ctx: RunContext_T, date: str = "") -> dict[str, Any]:
        """Lock in the user's chosen date and return the available times
        for that date.

        When to call:
            - Call this only after the user has clearly committed to one
              date from the candidates surfaced by `load_more_dates`.

        How to fill the argument (LLM-internal only):
            - Resolve the user's natural-language date expression yourself,
              using the "Current time" given in your instructions as
              "today", and serialize it to YYYY-MM-DD.
            - YYYY-MM-DD is an internal wire format. NEVER expose this
              format to the user, and NEVER ask the user to reply in this
              format.

        Args:
            date: Reservation date chosen by the user. Internal wire
                format: YYYY-MM-DD.
        """
        if guard := self._require_availability():
            return guard
        # ctx.disallow_interruptions()

        resolved = _try_parse_yyyy_mm_dd(date)
        if not resolved:
            return _tool_response(
                status="invalid_date",
                response_type="invalid_date",
                user_message=t("schedule.invalid_date", self._language_code),
                next_action="ask_date",
                date=date,
            )

        if not self._is_date_inside_cached_range(resolved):
            lang = self._language_code
            return _tool_response(
                status="out_of_range",
                response_type="out_of_range",
                user_message=t(
                    "schedule.out_of_range",
                    lang,
                    date=_format_spoken_date_korean(resolved, lang),
                    start=_format_spoken_date_korean(self._search_start_date, lang),
                    end=_format_spoken_date_korean(self._search_end_date, lang),
                ),
                next_action="ask_date",
                date=resolved,
                # Hand the LLM the ground-truth weekday so it never fills a
                # confirmation like "혹시 X요일을 말씀하신 걸까요?" itself.
                day_name=_day_name(
                    datetime.strptime(resolved, "%Y-%m-%d").weekday(), lang
                ),
                spoken_date_with_day=_spoken_date_with_day(resolved, lang),
                start_date=self._search_start_date,
                end_date=self._search_end_date,
            )

        if not self._date_exists_in_cache(resolved):
            lang = self._language_code
            return _tool_response(
                status="unavailable",
                response_type="unavailable_date",
                user_message=t(
                    "schedule.unavailable_date",
                    lang,
                    date=_format_spoken_date_korean(resolved, lang),
                    action=self._action_word,
                ),
                next_action="ask_date",
                date=resolved,
                day_name=_day_name(
                    datetime.strptime(resolved, "%Y-%m-%d").weekday(), lang
                ),
                spoken_date_with_day=_spoken_date_with_day(resolved, lang),
            )

        return self._commit_date(resolved)

    @function_tool
    async def describe_date(self, ctx: RunContext_T, date: str = "") -> dict[str, Any]:
        """Return the day of week for a date. Read-only: does NOT select a date,
        does NOT advance the booking flow, and does NOT check availability.

        When to call:
            - The user asks a standalone weekday question unrelated to picking a
              date — e.g. "7월 10일이 무슨 요일이에요?".
            - You are about to speak a weekday for a date the user is
              selecting/confirming for booking. In THIS case you MUST also call
              load_more_dates or select_date for the SAME date in the same turn
              — never end the turn on describe_date alone, or the booking flow
              will not advance and the user will have to repeat the date.

        You MUST NOT compute a weekday yourself: date-to-weekday arithmetic is
        unreliable. Call this tool and speak the returned `day_name` verbatim.

        How to fill the argument (LLM-internal only):
            - Resolve the user's natural-language date expression yourself,
              using the "Current time" given in your instructions as "today",
              and serialize it to YYYY-MM-DD.
            - YYYY-MM-DD is an internal wire format. NEVER expose it to the user.

        Args:
            date: The date in question. Internal wire format: YYYY-MM-DD.
        """
        lang = self._language_code
        resolved = _try_parse_yyyy_mm_dd(date)
        if not resolved:
            return _tool_response(
                status="invalid_date",
                response_type="invalid_date",
                user_message=t("schedule.invalid_date", lang),
                next_action="ask_date",
                date=date,
            )

        spoken_with_day = _spoken_date_with_day(resolved, lang)
        return _tool_response(
            status="ok",
            response_type="date_weekday",
            user_message=t(
                "schedule.weekday_answer",
                lang,
                spoken_date_with_day=spoken_with_day,
            ),
            next_action="continue",
            date=resolved,
            day_name=_day_name(datetime.strptime(resolved, "%Y-%m-%d").weekday(), lang),
            spoken_date=_format_spoken_date_korean(resolved, lang),
            spoken_date_with_day=spoken_with_day,
        )

    @function_tool
    async def load_more_dates(
        self,
        ctx: RunContext_T,
        start_date: str,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """Check availability for a specific date or a date range from the
        cached one-month schedule snapshot.

        When to call:
            - The user mentioned a specific date (e.g. "4월 10일", "내일",
              "다음 주 화요일"). Pass only `start_date`.
            - The user mentioned a range (e.g. "다음 주", "이번 달 말",
              "4월 둘째 주"). Pass both `start_date` and `end_date`.

        How to fill the arguments (LLM-internal only):
            - Resolve the user's natural-language date expression yourself,
              using the "Current time" given in your instructions as
              "today", and serialize it to YYYY-MM-DD.
            - YYYY-MM-DD is an internal wire format. NEVER expose this
              format to the user, and NEVER ask the user to reply in this
              format.
            - If only a single date is given, leave `end_date` empty.

        How to talk to the user:
            - If the user has not given a date yet, ask again in natural
              spoken Korean WITHOUT mentioning any format example such as
              "YYYY-MM-DD" or "2026-04-10".
              (e.g. "원하시는 날짜를 말씀해 주세요.")
            - Do not proactively mention internal details such as the
              30-day lookup window or the date format.

        Args:
            start_date: Start date, or single target date. Internal wire
                format: YYYY-MM-DD.
            end_date: Optional end date for range queries. Internal wire
                format: YYYY-MM-DD.
        """
        if guard := self._require_availability():
            return guard
        self._last_offered_slots = []
        # ctx.disallow_interruptions()

        logger.bind(
            task="SCHEDULE_SELECTION_TASK",
            stage="load_more_dates",
            start_date=start_date,
            end_date=end_date,
        ).debug("********** load_more_dates from cache **********")

        resolved_start = _try_parse_yyyy_mm_dd(start_date)
        if not resolved_start:
            return _tool_response(
                status="invalid_date",
                response_type="invalid_date",
                user_message=t("schedule.invalid_date", self._language_code),
                next_action="ask_date",
                start_date=start_date,
            )

        resolved_end = _try_parse_yyyy_mm_dd(end_date or "") if end_date else None
        if end_date and not resolved_end:
            return _tool_response(
                status="invalid_date",
                response_type="invalid_date",
                user_message=t("schedule.invalid_end_date", self._language_code),
                next_action="ask_date",
                start_date=resolved_start,
                end_date=end_date,
            )

        if resolved_end and resolved_end < resolved_start:
            return _tool_response(
                status="invalid_range",
                response_type="invalid_range",
                user_message=t("schedule.invalid_range", self._language_code),
                next_action="ask_date",
                start_date=resolved_start,
                end_date=resolved_end,
            )

        # If a range collapses to a single day (or the LLM filled end_date with
        # the same value as start_date), treat it as a single-date request so we
        # skip the awkward "X부터 X까지" phrasing and go straight to time selection.
        if resolved_end and resolved_end == resolved_start:
            resolved_end = None

        if resolved_end:
            if (
                resolved_end < self._search_start_date
                or resolved_start > self._search_end_date
            ):
                lang = self._language_code
                return _tool_response(
                    status="out_of_range",
                    response_type="out_of_range",
                    user_message=t(
                        "schedule.range_out_of_range",
                        lang,
                        start=_format_spoken_date_korean(self._search_start_date, lang),
                        end=_format_spoken_date_korean(self._search_end_date, lang),
                    ),
                    next_action="ask_date",
                    start_date=resolved_start,
                    end_date=resolved_end,
                    cached_start_date=self._search_start_date,
                    cached_end_date=self._search_end_date,
                )

            range_start = max(resolved_start, self._search_start_date)
            range_end = min(resolved_end, self._search_end_date)
            available_dates = self._available_dates(
                start_date=range_start,
                end_date=range_end,
            )
            return _tool_response(
                status="ok" if available_dates else "unavailable",
                response_type="dates_for_range",
                user_message=self._render_dates_for_range(range_start, range_end),
                next_action="ask_date",
                start_date=range_start,
                end_date=range_end,
                available_dates=available_dates,
            )

        if not self._is_date_inside_cached_range(resolved_start):
            lang = self._language_code
            return _tool_response(
                status="out_of_range",
                response_type="out_of_range",
                user_message=t(
                    "schedule.out_of_range",
                    lang,
                    date=_format_spoken_date_korean(resolved_start, lang),
                    start=_format_spoken_date_korean(self._search_start_date, lang),
                    end=_format_spoken_date_korean(self._search_end_date, lang),
                ),
                next_action="ask_date",
                start_date=resolved_start,
                cached_start_date=self._search_start_date,
                cached_end_date=self._search_end_date,
            )

        if not self._date_exists_in_cache(resolved_start):
            return _tool_response(
                status="unavailable",
                response_type="unavailable_date",
                user_message=t(
                    "schedule.unavailable_date",
                    self._language_code,
                    date=_format_spoken_date_korean(
                        resolved_start, self._language_code
                    ),
                    action=self._action_word,
                ),
                next_action="ask_date",
                date=resolved_start,
            )

        return self._commit_date(resolved_start)

    def _slots_matching_time_condition(
        self,
        *,
        period: str | None = None,
        after_hhmm: str | None = None,
        before_hhmm: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[datetime]:
        slots = self._slots()
        if start_date:
            slots = [s for s in slots if s.strftime("%Y-%m-%d") >= start_date]
        if end_date:
            slots = [s for s in slots if s.strftime("%Y-%m-%d") <= end_date]
        if period in PERIOD_HOUR_RANGES:
            lo, hi = PERIOD_HOUR_RANGES[period]
            slots = [s for s in slots if lo <= s.hour < hi]
        if after_hhmm:
            slots = [s for s in slots if s.strftime("%H:%M") >= after_hhmm]
        if before_hhmm:
            slots = [s for s in slots if s.strftime("%H:%M") <= before_hhmm]
        return slots

    def _time_condition_label(
        self,
        period: str | None,
        after_hhmm: str | None,
        before_hhmm: str | None,
    ) -> str:
        lang = self._language_code

        def _spoken(hhmm: str) -> str:
            hh, mm = (int(x) for x in hhmm.split(":"))
            return _format_spoken_time_korean(datetime(2000, 1, 1, hh, mm), lang)

        parts: list[str] = []
        if period in PERIOD_NAMES_KR:
            parts.append(t(f"schedule.period.{period}", lang))
        if after_hhmm and before_hhmm:
            # Both bounds set. Equal bounds = an exact time; rendering them as
            # "after 9 / before 9" is contradictory. Different bounds read as a range.
            if after_hhmm == before_hhmm:
                parts.append(_spoken(after_hhmm))
            else:
                parts.append(
                    t(
                        "schedule.label.between",
                        lang,
                        after=_spoken(after_hhmm),
                        before=_spoken(before_hhmm),
                    )
                )
        elif after_hhmm:
            parts.append(t("schedule.label.after", lang, time=_spoken(after_hhmm)))
        elif before_hhmm:
            parts.append(t("schedule.label.before", lang, time=_spoken(before_hhmm)))
        return " ".join(parts) if parts else t("schedule.label.fallback", lang)

    @function_tool
    async def search_slots_by_time(
        self,
        ctx: RunContext_T,
        period: str = "",
        after_time: str = "",
        before_time: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> dict[str, Any]:
        """Find dates that have available slots matching a time-of-day
        condition, searching the whole cached one-month snapshot at once.

        When to call:
            - The user states a TIME preference instead of (or before) a
              date. 예: "오전 시간 되는 날짜로 해주세요", "오후 5시 이후만
              가능해요", "점심시간 전에 되는 날 있나요?", "저녁에 되는 날
              알려주세요".
            - Also call this when the user repeats a time-of-day request
              after being shown dates — never answer such a request with a
              plain date list.

        How to fill the arguments (LLM-internal only):
            - period: "morning" (before 12:00), "afternoon" (12:00-17:00),
              or "evening" (after 17:00). Leave empty if the user gave an
              explicit clock time instead.
            - after_time / before_time: 24h "HH:MM" bounds resolved from
              the user's words (예: "3시 이후" 낮 문맥 -> after_time="15:00").
            - start_date / end_date: optional YYYY-MM-DD range when the
              user combined a period with dates (예: "다음 주 오전").
              Leave empty to search the whole cached month.
            - All formats here are internal wire formats. NEVER expose
              them to the user.

        Args:
            period: One of "morning", "afternoon", "evening", or empty.
            after_time: Earliest acceptable time, internal format HH:MM.
            before_time: Latest acceptable time, internal format HH:MM.
            start_date: Optional range start, internal format YYYY-MM-DD.
            end_date: Optional range end, internal format YYYY-MM-DD.
        """
        if guard := self._require_availability():
            return guard
        self._last_offered_slots = []
        resolved_period = period if period in PERIOD_HOUR_RANGES else None
        after_hhmm = _parse_time_to_hhmm(after_time) if after_time else None
        before_hhmm = _parse_time_to_hhmm(before_time) if before_time else None
        resolved_start = _try_parse_yyyy_mm_dd(start_date) if start_date else None
        resolved_end = _try_parse_yyyy_mm_dd(end_date) if end_date else None

        logger.bind(
            task="SCHEDULE_SELECTION_TASK",
            stage="search_slots_by_time",
            period=resolved_period,
            after_time=after_hhmm,
            before_time=before_hhmm,
            start_date=resolved_start,
            end_date=resolved_end,
        ).debug("********** search_slots_by_time from cache **********")

        if not resolved_period and not after_hhmm and not before_hhmm:
            return _tool_response(
                status="invalid_condition",
                response_type="invalid_condition",
                user_message=t("schedule.invalid_time_condition", self._language_code),
                next_action="ask_time_condition",
            )

        matched = self._slots_matching_time_condition(
            period=resolved_period,
            after_hhmm=after_hhmm,
            before_hhmm=before_hhmm,
            start_date=resolved_start,
            end_date=resolved_end,
        )
        label = self._time_condition_label(resolved_period, after_hhmm, before_hhmm)

        if not matched:
            # Wanting an earlier/later slot than exists is the common case
            # (e.g. "오전이요" when the month only has afternoons) — suggest
            # the closest thing instead of leaving the customer to hunt.
            all_slots = self._slots_matching_time_condition(
                start_date=resolved_start, end_date=resolved_end
            )
            suggestion = ""
            if all_slots:
                wants_early = resolved_period == "morning" or (
                    before_hhmm and not after_hhmm
                )
                wants_late = resolved_period == "evening" or (
                    after_hhmm and not before_hhmm
                )
                nearest: datetime | None = None
                if wants_early:
                    nearest = min(all_slots, key=lambda s: (s.hour, s.minute))
                elif wants_late:
                    nearest = max(all_slots, key=lambda s: (s.hour, s.minute))
                if nearest is not None:
                    lang = self._language_code
                    suggestion = t(
                        "schedule.nearest_suggestion_early"
                        if wants_early
                        else "schedule.nearest_suggestion_late",
                        lang,
                        spoken_date=_format_spoken_date_korean(
                            nearest.strftime("%Y-%m-%d"), lang
                        ),
                        spoken_time=_format_spoken_time_korean(nearest, lang),
                    )
            return _tool_response(
                status="unavailable",
                response_type="no_slots_for_time_condition",
                user_message=t(
                    "schedule.no_slots_for_condition",
                    self._language_code,
                    label=label,
                    action=self._action_word,
                    suggestion=suggestion
                    or t("schedule.try_other_time", self._language_code),
                ),
                next_action="ask_time_condition",
                condition=label,
            )

        # Group matched slots by date so the customer hears date AND times
        # together — no more date->time->date round trips.
        by_date: dict[str, list[datetime]] = {}
        for slot in matched:
            by_date.setdefault(slot.strftime("%Y-%m-%d"), []).append(slot)

        sorted_dates = sorted(by_date)
        available: list[dict[str, Any]] = []
        for date_str in sorted_dates:
            available.append(
                {
                    "date": date_str,
                    "times": [
                        _format_spoken_time_korean(s, self._language_code)
                        for s in by_date[date_str]
                    ],
                }
            )
            self._announced_dates.add(date_str)

        if len(sorted_dates) > MAX_DATES_TO_ENUMERATE_IN_SEARCH:
            # Many matching dates: a human agent summarizes instead of
            # reading a long list aloud — lead with the earliest option.
            lang = self._language_code
            first_date = sorted_dates[0]
            first_slots = by_date[first_date]
            first_dt = datetime.strptime(first_date, "%Y-%m-%d")
            day_name = _day_name(first_dt.weekday(), lang)
            rel = _relative_day_phrase(first_date, lang)
            spoken_date = _format_spoken_date_korean(first_date, lang)
            earliest_time = _format_spoken_time_korean(first_slots[0], lang)
            windows = self._render_windows(first_slots)
            last_spoken = _format_spoken_date_korean(sorted_dates[-1], lang)
            user_message = t(
                "schedule.search_earliest",
                lang,
                rel=rel,
                spoken_date=spoken_date,
                day_name=day_name,
                earliest_time=earliest_time,
            )
            # Only describe "that day's window" when the earliest date actually
            # has more than one matching slot — otherwise it just repeats the
            # earliest time we already said.
            if len(first_slots) > 1:
                user_message += t("schedule.search_that_day", lang, windows=windows)
            user_message += t(
                "schedule.search_summary",
                lang,
                last_spoken=last_spoken,
                label=label,
                action=self._action_word,
                spoken_date=spoken_date,
            )
        else:
            lang = self._language_code
            entries: list[str] = []
            for date_str in sorted_dates:
                slots_for_date = by_date[date_str]
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                day_name = _day_name(dt.weekday(), lang)
                spoken_times = [
                    _format_spoken_time_korean(s, lang)
                    for s in slots_for_date[:MAX_TIMES_PER_DATE_IN_SEARCH]
                ]
                extra = len(slots_for_date) - len(spoken_times)
                times_text = ", ".join(spoken_times)
                if extra > 0:
                    times_text += t("schedule.times_extra", lang, extra=extra)
                entries.append(
                    t(
                        "schedule.search_entry",
                        lang,
                        spoken_date=_format_spoken_date_korean(date_str, lang),
                        day_name=day_name,
                        times_text=times_text,
                    )
                )
            # Separate each date with a period (not a newline/dash) so TTS
            # inserts a clear pause between entries — visual list markers are
            # read with no break and the dates run together. Times within one
            # date stay comma-joined, so date vs time boundaries stay distinct.
            user_message = t(
                "schedule.search_dates_times",
                lang,
                label=label,
                action=self._action_word,
                entries=". ".join(entries),
            )

        self._unclear_time_count = 0
        self._last_offered_slots = [
            slot.strftime("%Y-%m-%d %H:%M:%S") for slot in matched
        ]
        return _tool_response(
            status="ok",
            response_type="slots_for_time_condition",
            user_message=user_message,
            next_action="ask_date",
            condition=label,
            available_slots=available,
        )

    @function_tool
    async def check_time_availability(
        self,
        ctx: RunContext_T,
        time: str = "",
        date: str | None = None,
    ) -> dict[str, Any]:
        """Answer whether a specific time is still available WITHOUT
        selecting it. Never stages a pick and never changes the flow state.

        When to call:
            - The user is ASKING about availability, not committing.
              예: "10시도 가능해요?", "10시 아직 남아있나요?", "9시 30분이랑
              10시 둘 다 되나요?", "10시도 되는지 확인해 주세요".
            - Also call this for the question part of a compound utterance
              like "9시 30분으로 할게요, 근데 10시도 가능해요?" — answer the
              question FIRST; only call pick after the user settles on one
              final time.

        Args:
            time: The time the user asked about. Normalize to spoken Korean
                with an explicit 오전/오후 marker before passing
                (예: "11시 반도 있어요?" 오전 문맥 -> "오전 11시 30분").
            date: Optional date in internal YYYY-MM-DD format. Defaults to
                the date currently being discussed.
        """
        if guard := self._require_availability():
            return guard
        self._last_offered_slots = []
        explicit_date = _try_parse_yyyy_mm_dd(date or "")
        correction = None
        if explicit_date:
            explicit_date, _, correction = self._apply_caller_date_hint(
                explicit_date, explicit_date
            )
        correction_fields = {"date_corrected": correction} if correction else {}
        resolved_date = explicit_date or self._reservation_data.get("reservation_date")
        if not resolved_date:
            return _tool_response(
                status="missing_date",
                response_type="missing_date",
                user_message=t("schedule.missing_date", self._language_code),
                next_action="ask_date",
            )

        hhmm = _parse_time_to_hhmm(time)
        if not hhmm:
            return _tool_response(
                status="invalid_time",
                response_type="invalid_time",
                user_message=t("schedule.invalid_time_check", self._language_code),
                next_action="ask_time",
                date=resolved_date,
                **correction_fields,
            )

        lang = self._language_code
        slots = self._slots_for_date(resolved_date)
        is_available = any(slot.strftime("%H:%M") == hhmm for slot in slots)
        if not is_available:
            # Bare-hour utterance like "3시": try the PM reading too.
            pm_hhmm = _ambiguous_pm_fallback(time, hhmm)
            if pm_hhmm and any(slot.strftime("%H:%M") == pm_hhmm for slot in slots):
                hhmm = pm_hhmm
                is_available = True
        spoken_req = _format_spoken_time_korean(
            datetime.strptime(f"{resolved_date} {hhmm}", "%Y-%m-%d %H:%M"), lang
        )
        all_times = [_format_spoken_time_korean(slot, lang) for slot in slots]

        if is_available:
            # Remember the one time just confirmed as open, so a bare "네" that
            # reaches confirm_pick re-stages it instead of dead-ending.
            raw, advisor_id = self._find_slot_for(resolved_date, hhmm)
            if raw and advisor_id is not None:
                self._last_offered_slots = [raw]
            user_message = t(
                "schedule.time_available",
                lang,
                spoken_req=spoken_req,
                action=self._action_word,
            )
        else:
            user_message = t(
                "schedule.time_unavailable",
                lang,
                spoken_req=spoken_req,
                action=self._action_word,
            )

        # A parseable availability question means we heard them clearly.
        self._unclear_time_count = 0
        return _tool_response(
            status="ok",
            response_type="time_availability",
            user_message=user_message,
            next_action="ask_time",
            date=resolved_date,
            requested_time=hhmm,
            available=is_available,
            available_times=all_times,
            **correction_fields,
        )

    def _handle_unclear_datetime(self) -> dict[str, Any]:
        """Counter + loop-break for repeated UNCLEAR date/time utterances.

        STT mis-recognitions made the model emit the same "다시 말씀해
        주세요" re-request turn after turn. After the first
        retry, if a date with known slots is already in context, stop
        repeating the request and read that date's times so the user can
        just pick one — the escalation the STT prompt section describes.
        """
        self._unclear_time_count += 1
        date_str = self._reservation_data.get("reservation_date")
        if (
            self._unclear_time_count >= 2
            and date_str
            and self._slots_for_date(date_str)
        ):
            self._stage = "select_time"
            return self._times_for_date_response(date_str)
        return _tool_response(
            status="unclear",
            response_type="unclear_datetime",
            user_message=t("schedule.unclear_reask", self._language_code),
            next_action="ask_time",
            date=date_str,
        )

    @function_tool
    async def report_unclear_time(self, ctx: RunContext_T) -> dict[str, Any]:
        """Report that the user's date/time utterance was UNCLEAR and could
        not be parsed (empty/garbled fragment, implausible or contradictory
        number, or it does not answer the date/time question).

        Call this INSTEAD of speaking a re-request yourself, and INSTEAD of
        guessing with select_date/load_more_dates/pick. Read only the
        returned `user_message`. The tool tracks how many times this has
        happened in a row: on the first it asks the user to repeat, but once
        a date is already chosen it will instead read that date's available
        times so the user can pick one — so never keep asking them to repeat
        on your own.

        Do NOT call this for a clear-but-unavailable time (use pick) or an
        availability question (use check_time_availability). UNCLEAR means
        you genuinely could not make out what time they said.
        """
        return self._handle_unclear_datetime()

    @function_tool
    async def pick(
        self,
        ctx: RunContext_T,
        time: str = "",
        date: str | None = None,
    ) -> dict[str, Any] | None:
        """Stage the user's chosen time as pending confirmation.

        When the user names BOTH a date and a time in one utterance
        (예: "6월 13일 9시 반으로 할게요"), call this directly with both
        arguments — no separate select_date round trip is needed.

        Args:
            time: The chosen time. Normalize the user's words to spoken
                Korean with an explicit 오전/오후 marker before passing
                (예: "9시 반이요" 오전 문맥 -> "오전 9시 30분",
                "10시 5분 전" -> "오전 9시 55분").
            date: Optional date in internal YYYY-MM-DD format, when the
                user named it in the same utterance.
        """
        if guard := self._require_availability():
            return guard
        self._last_offered_slots = []
        # ctx.disallow_interruptions()

        explicit_date = _try_parse_yyyy_mm_dd(date or "")
        correction = None
        if explicit_date:
            explicit_date, _, correction = self._apply_caller_date_hint(
                explicit_date, explicit_date
            )
        correction_fields = {"date_corrected": correction} if correction else {}
        if self._stage not in ("select_time", "confirm") and not explicit_date:
            return _tool_response(
                status="invalid_stage",
                response_type="invalid_stage",
                user_message=t(
                    "schedule.pick_invalid_stage",
                    self._language_code,
                    action=self._action_word,
                ),
                next_action="ask_date",
            )

        if self._stage == "confirm":
            self._pending_pick = None
            self._stage = "select_time"

        resolved_date = explicit_date or self._reservation_data.get("reservation_date")
        if not resolved_date:
            return _tool_response(
                status="missing_date",
                response_type="missing_date",
                user_message=t("schedule.pick_missing_date", self._language_code),
                next_action="ask_date",
            )

        hhmm = _parse_time_to_hhmm(time)
        if not hhmm:
            return _tool_response(
                status="invalid_time",
                response_type="invalid_time",
                user_message=t("schedule.pick_invalid_time", self._language_code),
                next_action="ask_time",
                date=resolved_date,
                **correction_fields,
            )

        chosen_raw, chosen_advisor_id = self._find_slot_for(resolved_date, hhmm)

        if not chosen_raw:
            # Bare-hour utterance like "3시": the AM reading found nothing —
            # try the PM reading before reporting the time as unavailable.
            pm_hhmm = _ambiguous_pm_fallback(time, hhmm)
            if pm_hhmm:
                pm_raw, pm_advisor_id = self._find_slot_for(resolved_date, pm_hhmm)
                if pm_raw:
                    hhmm = pm_hhmm
                    chosen_raw, chosen_advisor_id = pm_raw, pm_advisor_id

        if not chosen_raw or chosen_advisor_id is None:
            lang = self._language_code
            spoken_req = _format_spoken_time_korean(
                datetime.strptime(f"{resolved_date} {hhmm}", "%Y-%m-%d %H:%M"), lang
            )
            nearest = self._find_nearest_slots(resolved_date, hhmm, limit=2)
            if nearest:
                suggestions = ", ".join(
                    _format_spoken_time_korean(d, lang) for d in nearest
                )
                return _tool_response(
                    status="unavailable",
                    response_type="unavailable_time",
                    user_message=t(
                        "schedule.pick_nearest",
                        lang,
                        spoken_req=spoken_req,
                        action=self._action_word,
                        suggestions=suggestions,
                    ),
                    next_action="ask_time",
                    date=resolved_date,
                    requested_time=hhmm,
                    suggested_times=[
                        _format_spoken_time_korean(slot, lang) for slot in nearest
                    ],
                    **correction_fields,
                )

            windows = self._render_windows_for_date(resolved_date)
            if windows:
                return _tool_response(
                    status="unavailable",
                    response_type="unavailable_time",
                    user_message=t(
                        "schedule.pick_windows",
                        lang,
                        spoken_req=spoken_req,
                        action=self._action_word,
                        windows=windows,
                    ),
                    next_action="ask_time",
                    date=resolved_date,
                    requested_time=hhmm,
                    available_window=windows,
                    **correction_fields,
                )

            return _tool_response(
                status="unavailable",
                response_type="unavailable_date",
                user_message=t(
                    "schedule.pick_no_times",
                    lang,
                    action=self._action_word,
                ),
                next_action="ask_date",
                date=resolved_date,
                requested_time=hhmm,
                **correction_fields,
            )

        response = self._stage_pending_pick(
            resolved_date, hhmm, chosen_raw, chosen_advisor_id
        )
        response.update(correction_fields)
        return response

    def _stage_pending_pick(
        self,
        resolved_date: str,
        hhmm: str,
        chosen_raw: str,
        chosen_advisor_id: int,
    ) -> dict[str, Any]:
        self._reservation_data["reservation_date"] = resolved_date
        self._announced_dates.add(resolved_date)
        self._unclear_time_count = 0
        self._pending_pick = {
            "appointment_datetime": chosen_raw,
            "advisor_id": chosen_advisor_id,
        }
        self._stage = "confirm"

        lang = self._language_code
        spoken_datetime = _format_spoken_datetime_korean(chosen_raw, lang)
        return _tool_response(
            status="ok",
            response_type="pending_confirmation",
            user_message=t(
                "schedule.pick_pending",
                lang,
                spoken_datetime=spoken_datetime,
                josa=_josa_ro(spoken_datetime),
                action=self._action_word,
            ),
            next_action="confirm_pick",
            date=resolved_date,
            requested_time=hhmm,
            appointment_datetime=chosen_raw,
            advisor_id=chosen_advisor_id,
        )

    @function_tool
    async def confirm_pick(self, ctx: RunContext_T) -> dict[str, Any] | None:
        """Finalize the time shown by the most recent pick. If no time is pending,
        nothing is finalized: read only the returned user_message and follow next_action.
        """
        # ctx.disallow_interruptions()

        if self._stage != "confirm" or not self._pending_pick:
            raw, advisor_id = None, None
            if len(self._last_offered_slots) == 1:
                offered = datetime.strptime(
                    self._last_offered_slots[0], "%Y-%m-%d %H:%M:%S"
                )
                date = offered.strftime("%Y-%m-%d")
                hhmm = offered.strftime("%H:%M")
                raw, advisor_id = self._find_slot_for(date, hhmm)
            recovered = raw is not None and advisor_id is not None
            logger.bind(
                task=self._TASK_TAG,
                stage="confirm_without_pick",
                offered_count=len(self._last_offered_slots),
                recovered=recovered,
            ).warning("Confirmation requested without a pending pick")
            if recovered:
                return self._stage_pending_pick(date, hhmm, raw, advisor_id)
            return _tool_response(
                status="no_pending_pick",
                response_type="no_pending_pick",
                user_message=t("schedule.confirm_no_pick", self._language_code),
                next_action="pick",
            )

        chosen_raw = self._pending_pick["appointment_datetime"]
        chosen_advisor_id = self._pending_pick["advisor_id"]

        result = ScheduleSelectionResult(
            appointment_datetime=chosen_raw,
            advisor_id=chosen_advisor_id,
        )
        self._reservation_data["appointment_datetime"] = chosen_raw
        self._reservation_data["advisor_id"] = chosen_advisor_id
        self._pending_pick = None

        # 예약신청(legacy)·예약변경이 공유하는 SLOT_SELECTED 단계.
        # 후보 제시(ON_ENTER)와 나눠야 '시간을 못 정하고 이탈' 이 퍼널에 남는다.
        self.session.history.add_message(
            role="developer",
            content="",
            extra=agent_event_extra(
                self._TASK_TAG,
                status="SELECTED",
                details={
                    "appointment_datetime": chosen_raw,
                    "advisor_id": chosen_advisor_id,
                },
            ),
        )

        self.complete(result)

        return None

    @function_tool
    async def exit_task(
        self,
        ctx: RunContext_T,
        reason_code: ExitReasonCode = "other",
        detail: str = "",
        route_target: BookingExitRouteTargetInput = "other",
    ) -> None:
        """Exit the schedule-selection step.

        Args:
            reason_code: Structured exit category. Use EXACTLY one of:
                - "change_advisor": the user wants a different advisor
                - "change_service": the user wants a different service
                - "staff_connection": the user asked to be connected to a human agent
                - "cancel": the user wants to stop the booking
                - "unrelated": a request unrelated to scheduling (e.g. service center info)
                - "other": anything else
            detail: (optional) The user's actual request, summarized from what they
                really said. Never fabricate; leave empty if there is nothing to add.
            route_target: For unrelated/other exits, classify the next request as
                booking_inquiry, booking_modify, booking_cancel, info_request,
                end_call, or other. Omit it for every other reason_code.
        """
        raw_reason_code = reason_code
        normalized_target = route_target_for_exit(raw_reason_code, route_target)
        reason_code = normalize_exit_reason_code(raw_reason_code)
        reason = exit_reason_text(reason_code, detail, self._language_code)

        logger.bind(
            task="SCHEDULE_SELECTION_TASK",
            stage="exit",
            reason=reason,
            reason_code=reason_code,
        ).debug("User exited schedule selection task")

        self.complete(
            ScheduleSelectionResult(
                exited=True,
                exit_reason=reason,
                exit_reason_code=reason_code,
                route_target=normalized_target,
            )
        )

        return None
