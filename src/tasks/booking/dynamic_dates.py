"""Dynamic booking date facts: caller date resolution, calendar block, speech forms.

Pure functions over explicit dates; nothing here reads the clock. Callers that
need "today" pass it in (tests patch tasks.dynamic_booking_task.now_kst).
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Literal

WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]
WEEKDAY_EN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEKDAY_EN_FULL = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]
MONTH_EN = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


def _is_whole_day(time_from: str | None, time_to: str | None) -> bool:
    """A window that excludes no clock time. Models often pass 00:00~23:59 for
    "내일" with no time preference; that is still a whole-day search."""
    return time_from in (None, "00:00") and time_to in (None, "23:59")


@dataclass(frozen=True)
class CallerDateHint:
    """One date expression in a caller utterance, resolved by code, not the LLM."""

    phrase: str
    date: str  # YYYY-MM-DD
    weekday: str  # 월..일
    status: Literal["ok", "past", "beyond_horizon"]


_WEEK_OFFSETS = {"이번": 0, "요번": 0, "다음": 1, "담": 1, "다다음": 2}
_RELATIVE_DAY_OFFSETS = {"내일모레": 2, "오늘": 0, "내일": 1, "모레": 2, "글피": 3}
_SKIPPED_WEEK_DAY_RE = re.compile(
    r"(지난|저번|전)\s*주\s*[월화수목금토일]\s*(?:요일|욜)"
)
_WEEK_DAY_RE = re.compile(
    r"(다다음|다음|담|이번|요번)\s*주\s*([월화수목금토일])\s*(?:요일|욜)"
)
_BARE_WEEKDAY_RE = re.compile(r"([월화수목금토일])\s*(?:요일|욜)")
_RELATIVE_DAY_RE = re.compile("|".join(_RELATIVE_DAY_OFFSETS))
_YEAR_DATE_RE = re.compile(r"\d{2,4}\s*년\s*\d{1,2}\s*월\s*\d{1,2}\s*일")
_MONTH_DAY_RE = re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_DAY_ONLY_RE = re.compile(
    r"(?<![\d.:])(\d{1,2})\s*일(?!\s*(?:뒤|후|이내|안에|동안|간|정도|째|씩|분|시))"
)
_DATE_NEGATION_RE = re.compile(r"말고|말구|빼고|아니라|아닌|제외|대신")


_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_HHMM_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")


def _spoken_condition_text(text: str | None, language: str | None) -> str | None:
    """A caller-facing condition with tool notation replaced by speech forms."""
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return None
    lang = "en" if language == "en-US" else "ko"

    def _date(match: re.Match[str]) -> str:
        try:
            return spoken_date(match.group(1), lang)
        except ValueError:
            return match.group(0)

    cleaned = _ISO_DATE_RE.sub(_date, cleaned)
    cleaned = _HHMM_RE.sub(
        lambda m: (
            _spoken_clock(f"{int(m.group(1)):02d}:{m.group(2)}", language) or m.group(0)
        ),
        cleaned,
    )
    return cleaned


def _blank(text: str, span: tuple[int, int]) -> str:
    start, end = span
    return text[:start] + " " * (end - start) + text[end:]


def resolve_caller_dates(
    text: str, today: dt.date, horizon: dt.date
) -> list[CallerDateHint]:
    """Resolve every date expression in a caller utterance deterministically.

    Gemma searched 10-20 or 10-26 for "다음주 월요일"(10-19) in 10/10 replays when
    the only help was a week-row calendar at the top of the system prompt. The
    model does not need to compute anything if the caller's own words are paired
    with the date right next to the utterance, so the code resolves them.

    Covered: 오늘/내일/모레/글피, (이번|다음|담|다다음)주 X요일, bare X요일 (next
    occurrence, today included), N월 N일, N일 (this month, else next). VerificationCode
    dates with a year ("87년 9월 4일") and 지난주 are consumed without a hint.
    Weeks start on Monday, matching build_calendar_block. Order follows the
    utterance; duplicates by date are dropped.
    """
    remaining = text
    found: list[tuple[int, str, dt.date]] = []

    for pattern in (_YEAR_DATE_RE, _SKIPPED_WEEK_DAY_RE):
        for match in pattern.finditer(remaining):
            remaining = _blank(remaining, match.span())

    monday = today - dt.timedelta(days=today.weekday())
    for match in _WEEK_DAY_RE.finditer(remaining):
        offset = _WEEK_OFFSETS[match.group(1)]
        weekday = WEEKDAY_KO.index(match.group(2))
        day = monday + dt.timedelta(days=7 * offset + weekday)
        found.append((match.start(), match.group(0), day))
    remaining = _WEEK_DAY_RE.sub(lambda m: " " * len(m.group(0)), remaining)

    for match in _BARE_WEEKDAY_RE.finditer(remaining):
        weekday = WEEKDAY_KO.index(match.group(1))
        day = today + dt.timedelta(days=(weekday - today.weekday()) % 7)
        found.append((match.start(), match.group(0), day))
    remaining = _BARE_WEEKDAY_RE.sub(lambda m: " " * len(m.group(0)), remaining)

    for match in _RELATIVE_DAY_RE.finditer(remaining):
        day = today + dt.timedelta(days=_RELATIVE_DAY_OFFSETS[match.group(0)])
        found.append((match.start(), match.group(0), day))
    remaining = _RELATIVE_DAY_RE.sub(lambda m: " " * len(m.group(0)), remaining)

    for match in _MONTH_DAY_RE.finditer(remaining):
        month, day_number = int(match.group(1)), int(match.group(2))
        year = today.year
        try:
            day = dt.date(year, month, day_number)
        except ValueError:
            continue
        if day < today - dt.timedelta(days=180):
            try:
                day = dt.date(year + 1, month, day_number)
            except ValueError:
                continue
        found.append((match.start(), match.group(0), day))
    remaining = _MONTH_DAY_RE.sub(lambda m: " " * len(m.group(0)), remaining)

    for match in _DAY_ONLY_RE.finditer(remaining):
        day_number = int(match.group(1))
        year, month = today.year, today.month
        if day_number < today.day:
            month += 1
            if month == 13:
                year, month = year + 1, 1
        try:
            day = dt.date(year, month, day_number)
        except ValueError:
            continue
        found.append((match.start(), match.group(0), day))

    hints: list[CallerDateHint] = []
    seen: set[dt.date] = set()
    for _, phrase, day in sorted(found, key=lambda item: item[0]):
        if day in seen:
            continue
        seen.add(day)
        if day < today:
            status = "past"
        elif day > horizon:
            status = "beyond_horizon"
        else:
            status = "ok"
        hints.append(
            CallerDateHint(
                phrase=phrase.strip(),
                date=day.isoformat(),
                weekday=WEEKDAY_KO[day.weekday()],
                status=status,
            )
        )
    return hints


def caller_date_note(
    hints: list[CallerDateHint],
    horizon: dt.date,
    tool_wording: str = "search_schedules의 start_date/end_date",
) -> str | None:
    """The per-turn system line that pairs each caller phrase with its date.

    Always Korean: runtime instructions are Korean templates regardless of the
    response language (tests/test_dynamic_booking_prompt_rendering.py).
    """
    if not hints:
        return None
    parts = []
    for hint in hints:
        pair = f"'{hint.phrase}' = {hint.date}({hint.weekday}요일)"
        if hint.status == "past":
            pair += "은 이미 지난 날짜라 조회할 수 없습니다. 어느 날짜인지 확인하세요"
        elif hint.status == "beyond_horizon":
            pair += (
                f"은 조회 가능 범위({horizon.isoformat()}까지) 밖이라 조회할 수 "
                "없습니다. 범위 안의 날짜를 안내하세요"
            )
        parts.append(pair)
    return (
        "[날짜 해석] 고객 발화의 "
        + ", ".join(parts)
        + f". {tool_wording}에는 이 날짜를 그대로 쓰고 "
        "직접 계산하지 마세요."
    )


def _minutes_apart(when: str, hhmm: str) -> int:
    """Minutes between a slot ("YYYY-MM-DD HH:MM") and a clock time ("HH:MM")."""
    slot = int(when[11:13]) * 60 + int(when[14:16])
    return abs(slot - (int(hhmm[:2]) * 60 + int(hhmm[3:5])))


def _spoken_clock(hhmm: str | None, language: str | None) -> str | None:
    """툴 표기 "HH:MM" 을 발화형으로 바꾼다. ko: "오전 11시 45분", en: "11:45 AM".

    메모 오프닝은 say() 로 그대로 읽히므로 숫자 표기를 넘기면 TTS 가 "십일 대
    사십오"처럼 읽는다. 형식이 아니면 원본을 돌려준다.
    """
    if not hhmm:
        return None
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", hhmm.strip())
    if not match:
        return hhmm
    hour, minute = int(match.group(1)), int(match.group(2))
    if language == "en-US":
        suffix = "AM" if hour < 12 else "PM"
        hour12 = hour % 12 or 12
        return f"{hour12}:{minute:02d} {suffix}"
    period = "오전" if hour < 12 else "오후"
    hour12 = hour % 12 or 12
    return f"{period} {hour12}시" + (f" {minute}분" if minute else "")


def build_calendar_block(
    today: dt.datetime, schedule_lookup_days: int, lang: str = "ko"
) -> str:
    """ "표현 = 날짜" 조회표: 오늘부터 horizon까지, 지난 날짜는 싣지 않는다. [D8]

    주 단위 행(`다음 주: 10-19(월), 10-20(화), ...`)은 Gemma가 칸·행 위치를
    밀려 읽어 "다음주 월요일"을 10-20·10-26으로 조회했다(10/10). 고객 표현과
    같은 문자열을 줄 머리에 두면 계산 대신 찾아 읽는다. 다다음 주까지는 한 줄에
    하루씩, 그 뒤는 상대 요일 표현이 드물어 주 단위 행으로 줄인다. 턴마다
    코드가 붙이는 [날짜 해석]이 1차 근거이고 이 표는 보조다.
    """
    current = today.date()
    horizon = current + dt.timedelta(days=schedule_lookup_days)
    if lang == "ko":
        lines = [
            f"오늘: {today:%Y-%m-%d} ({WEEKDAY_KO[today.weekday()]}요일)"
            f" 현재 시각 {today:%H:%M}",
            f"조회 가능 범위: {current:%Y-%m-%d}부터 {horizon:%Y-%m-%d}까지. "
            "범위 밖 날짜는 search_schedules에 전달하지 마세요.",
            "",
            "[달력] 고객 표현 = 날짜. 같은 줄의 날짜를 그대로 쓰고 직접 계산하지 "
            "마세요. 턴마다 오는 [날짜 해석]이 있으면 그 날짜가 우선합니다.",
        ]
        relative = ("오늘", "내일", "모레")
        week_labels = ("이번 주", "다음 주", "다다음 주")
        short = WEEKDAY_KO
        day_label = [f"{day}요일" for day in WEEKDAY_KO]
        far_label = "{n}주 뒤"
        today_marker = " (오늘)"
    else:
        lines = [
            f"Today: {today:%Y-%m-%d} ({WEEKDAY_EN_FULL[today.weekday()]}),"
            f" current time {today:%H:%M}",
            f"Lookup range: {current:%Y-%m-%d} through {horizon:%Y-%m-%d}. "
            "Never pass dates outside this range to search_schedules.",
            "",
            "[Calendar] caller phrase = date. Copy the date on the matching line; "
            "do not compute dates. A per-turn [날짜 해석] note takes precedence.",
        ]
        relative = ("Today", "Tomorrow", "Day after tomorrow")
        week_labels = ("This week", "Next week", "Week after next")
        short = WEEKDAY_EN
        day_label = list(WEEKDAY_EN_FULL)
        far_label = "In {n} weeks"
        today_marker = " (today)"
    lines.append(
        " / ".join(
            f"{label} = {current + dt.timedelta(days=offset):%Y-%m-%d}"
            f"({short[(current + dt.timedelta(days=offset)).weekday()]})"
            for offset, label in enumerate(relative)
            if current + dt.timedelta(days=offset) <= horizon
        )
    )
    monday = current - dt.timedelta(days=current.weekday())
    week = 0
    while monday + dt.timedelta(days=7 * week) <= horizon:
        days = [
            monday + dt.timedelta(days=7 * week + index)
            for index in range(7)
            if current <= monday + dt.timedelta(days=7 * week + index) <= horizon
        ]
        if week < len(week_labels):
            for day in days:
                lines.append(
                    f"{week_labels[week]} {day_label[day.weekday()]} = {day:%Y-%m-%d}"
                    + (today_marker if day == current else "")
                )
        else:
            cells = ", ".join(f"{day:%m-%d}({short[day.weekday()]})" for day in days)
            lines.append(f"{far_label.format(n=week)}: {cells}")
        week += 1
    return "\n".join(lines)


def spoken_date(day: str, lang: str = "ko") -> str:
    """Date-only speech form ("10월 19일 월요일" / "Monday, October 19")."""
    moment = dt.date.fromisoformat(day)
    if lang == "ko":
        return f"{moment.month}월 {moment.day}일 {WEEKDAY_KO[moment.weekday()]}요일"
    return (
        f"{WEEKDAY_EN_FULL[moment.weekday()]}, {MONTH_EN[moment.month - 1]}"
        f" {moment.day}"
    )


def spoken_date_range(start_date: str, end_date: str, lang: str = "ko") -> str:
    """Speech form of a searched range; a one-day range is just that day."""
    if start_date == end_date:
        return spoken_date(start_date, lang)
    if lang == "ko":
        return f"{spoken_date(start_date)}부터 {spoken_date(end_date)}까지"
    return f"{spoken_date(start_date, lang)} through {spoken_date(end_date, lang)}"


def spoken_datetime(when: str, lang: str = "ko") -> str:
    """음성 표기: 년도 생략, 요일 포함. ko '오전/오후 h시[ m분]', en 'h[:mm] AM/PM'."""
    moment = dt.datetime.strptime(when, "%Y-%m-%d %H:%M")
    hour12 = moment.hour % 12 or 12
    if lang == "ko":
        half = "오전" if moment.hour < 12 else "오후"
        minute = f" {moment.minute}분" if moment.minute else ""
        return (
            f"{moment.month}월 {moment.day}일 {WEEKDAY_KO[moment.weekday()]}요일"
            f" {half} {hour12}시{minute}"
        )
    half = "AM" if moment.hour < 12 else "PM"
    minute = f":{moment.minute:02d}" if moment.minute else ""
    return (
        f"{WEEKDAY_EN_FULL[moment.weekday()]}, {MONTH_EN[moment.month - 1]}"
        f" {moment.day} at {hour12}{minute} {half}"
    )
