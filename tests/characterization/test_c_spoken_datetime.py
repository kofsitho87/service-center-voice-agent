"""Golden outputs for the spoken (TTS) date/time formatters.

These strings are read aloud, so every formatter — the shared module and the
legacy aliases kept in older modules — must stay byte-identical.
"""

from datetime import datetime

import pytest

from tasks import schedule_selection_base
from tasks.booking import select_reservation_task
from tools import booking_tools

DATETIME_CASES = [
    ("2030-01-07 09:00:00", "ko-KR", "1월 7일 오전 9시"),
    ("2030-01-07 15:30:00", "ko-KR", "1월 7일 오후 3시 30분"),
    ("2030-03-01 12:00:00", None, "3월 1일 오후 12시"),
    ("2030-12-31 00:05:00", "ko-KR", "12월 31일 오전 12시 5분"),
    ("2030-01-07 09:00:00", "en-US", "January 7 at 9 AM"),
    ("2030-01-07 15:30:00", "en-US", "January 7 at 3:30 PM"),
    ("2030-03-01 12:00:00", "fr-FR", "March 1 at 12 PM"),
    ("2030-12-31 00:05:00", "en-US", "December 31 at 12:05 AM"),
]

DATE_CASES = [
    ("2030-01-07", "ko-KR", "1월 7일"),
    ("2030-12-31", None, "12월 31일"),
    ("2030-01-07", "en-US", "January 7"),
    ("2030-12-31", "fr-FR", "December 31"),
]

TIME_CASES = [
    (9, 0, "ko-KR", "오전 9시"),
    (15, 30, None, "오후 3시 30분"),
    (0, 5, "ko-KR", "오전 12시 5분"),
    (12, 0, "ko-KR", "오후 12시"),
    (9, 0, "en-US", "9 AM"),
    (15, 30, "en-US", "3:30 PM"),
    (0, 5, "en-US", "12:05 AM"),
    (12, 0, "fr-FR", "12 PM"),
]

JOSA_CASES = [
    ("9시", "로"),
    ("30분", "으로"),
    ("1월 7일 오후 3시 30분", "으로"),
    ("서울", "로"),
    ("", "로"),
    ("3 PM", "로"),
]

DATETIME_FORMATTERS = {
    "booking_tools._format_spoken_datetime": booking_tools._format_spoken_datetime,
    "schedule_selection_base._format_spoken_datetime_korean": (
        schedule_selection_base._format_spoken_datetime_korean
    ),
    "select_reservation_task._format_spoken_date": (
        select_reservation_task._format_spoken_date
    ),
}


@pytest.mark.parametrize("formatter", DATETIME_FORMATTERS, ids=str)
@pytest.mark.parametrize(("raw", "language", "expected"), DATETIME_CASES)
def test_spoken_datetime(formatter, raw, language, expected):
    assert DATETIME_FORMATTERS[formatter](raw, language) == expected


@pytest.mark.parametrize(
    "formatter",
    [
        schedule_selection_base._format_spoken_date_korean,
        select_reservation_task._format_spoken_date,
    ],
)
@pytest.mark.parametrize(("raw", "language", "expected"), DATE_CASES)
def test_spoken_date(formatter, raw, language, expected):
    assert formatter(raw, language) == expected


@pytest.mark.parametrize(("hour", "minute", "language", "expected"), TIME_CASES)
def test_spoken_time(hour, minute, language, expected):
    dt = datetime(2030, 1, 7, hour, minute)
    assert schedule_selection_base._format_spoken_time_korean(dt, language) == expected
    assert select_reservation_task._spoken_time(hour, minute, language) == expected


@pytest.mark.parametrize(("word", "expected"), JOSA_CASES)
def test_josa_ro(word, expected):
    assert schedule_selection_base._josa_ro(word) == expected


def test_reservation_date_formatter_falls_back_to_raw_text():
    assert select_reservation_task._format_spoken_date("미정", "ko-KR") == "미정"
    assert select_reservation_task._format_spoken_date("2030-01-07 09:00", None) == (
        "2030-01-07 09:00"
    )


@pytest.mark.parametrize("raw", ["2030-01-07", "미정", "2030-01-07 09:00"])
def test_strict_datetime_formatters_reject_other_formats(raw):
    with pytest.raises(ValueError):
        booking_tools._format_spoken_datetime(raw, "ko-KR")
    with pytest.raises(ValueError):
        schedule_selection_base._format_spoken_datetime_korean(raw, "ko-KR")
