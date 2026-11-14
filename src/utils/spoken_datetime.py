"""Spoken (TTS) renderings of booking dates and times.

ko-KR (and a missing language) uses Korean phrasing; every other language uses
English phrasing. The year is never spoken: bookings are always near-term.
"""

from __future__ import annotations

import calendar
from datetime import datetime


def is_english(language: str | None) -> bool:
    """True for any language other than ko-KR (None counts as Korean)."""
    return bool(language and language != "ko-KR")


def spoken_time(dt: datetime, language: str | None = None) -> str:
    """ko: '오전 9시' / '오후 3시 30분'; en: '9 AM' / '3:30 PM'."""
    hour12 = dt.hour % 12 or 12
    if is_english(language):
        meridiem = "AM" if dt.hour < 12 else "PM"
        if dt.minute == 0:
            return f"{hour12} {meridiem}"
        return f"{hour12}:{dt.minute:02d} {meridiem}"
    meridiem = "오전" if dt.hour < 12 else "오후"
    if dt.minute == 0:
        return f"{meridiem} {hour12}시"
    return f"{meridiem} {hour12}시 {dt.minute}분"


def _date_part(dt: datetime, language: str | None) -> str:
    if is_english(language):
        return f"{calendar.month_name[dt.month]} {dt.day}"
    return f"{dt.month}월 {dt.day}일"


def _join_date_time(dt: datetime, language: str | None) -> str:
    separator = " at " if is_english(language) else " "
    return f"{_date_part(dt, language)}{separator}{spoken_time(dt, language)}"


def spoken_date(date_str: str, language: str | None = None) -> str:
    """'YYYY-MM-DD' -> ko: '6월 13일' / en: 'June 13'. Raises ValueError."""
    return _date_part(datetime.strptime(date_str, "%Y-%m-%d"), language)


def spoken_datetime(raw: str, language: str | None = None) -> str:
    """'YYYY-MM-DD HH:MM:SS' -> ko: '6월 13일 오전 9시' / en: 'June 13 at 9 AM'.

    Raises ValueError for any other format.
    """
    return _join_date_time(datetime.strptime(raw, "%Y-%m-%d %H:%M:%S"), language)


def spoken_date_or_datetime(text: str, language: str | None = None) -> str:
    """Lenient variant for RECORD reservation dates.

    Accepts 'YYYY-MM-DD HH:MM:SS' or 'YYYY-MM-DD'; anything else is returned
    unchanged.
    """
    try:
        return spoken_datetime(text, language)
    except ValueError:
        pass
    try:
        return spoken_date(text, language)
    except ValueError:
        return text


def josa_ro(word: str) -> str:
    """Pick '로' or '으로' for the preceding word (e.g. '9시로' vs '9시 30분으로')."""
    if not word:
        return "로"
    code = ord(word[-1]) - 0xAC00
    if 0 <= code < 11172:
        jong = code % 28
        if jong in (0, 8):  # no final consonant, or ㄹ
            return "로"
        return "으로"
    return "로"
