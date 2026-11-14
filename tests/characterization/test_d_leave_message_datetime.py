"""Golden outputs for the leave-message `MM-DD HH:MM` validation and read-back."""

from datetime import datetime

import pytest

import tasks.leave_message_task as leave_message_module


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch):
    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 7, 1, 9, 0, tzinfo=tz)

    monkeypatch.setattr(leave_message_module, "datetime", _FrozenDateTime)


@pytest.mark.parametrize(
    ("value", "valid", "spoken_ko", "spoken_en"),
    [
        ("07-12 18:00", True, "7월 12일 일요일 오후 6시", "Sunday, July 12 at 6 PM"),
        (
            "07-08 10:30",
            True,
            "7월 8일 수요일 오전 10시 30분",
            "Wednesday, July 8 at 10:30 AM",
        ),
        # Already past this year, so the read-back resolves to next year.
        (
            "01-05 00:05",
            True,
            "1월 5일 화요일 오전 12시 5분",
            "Tuesday, January 5 at 12:05 AM",
        ),
        ("02-30 10:00", False, "02-30 10:00", "02-30 10:00"),
        # Validation rejects the hour, but the read-back only needs the shape.
        ("07-12 25:00", False, "7월 12일 일요일 오후 1시", "Sunday, July 12 at 1 PM"),
        ("7-12 18:00", False, "7-12 18:00", "7-12 18:00"),
        ("x", False, "x", "x"),
        ("", False, "", ""),
    ],
)
def test_mmdd_hhmm_validation_and_spoken_form(value, valid, spoken_ko, spoken_en):
    assert leave_message_module._is_valid_mmdd_hhmm(value) is valid
    assert leave_message_module._spoken_datetime(value, "ko-KR") == spoken_ko
    assert leave_message_module._spoken_datetime(value, "en-US") == spoken_en


def test_none_is_rejected_and_spoken_as_empty():
    assert leave_message_module._is_valid_mmdd_hhmm(None) is False
    assert leave_message_module._spoken_datetime(None, "ko-KR") == ""
