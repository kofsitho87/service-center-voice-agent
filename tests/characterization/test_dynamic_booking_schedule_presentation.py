import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import pytest

from tasks.dynamic_booking_task import DynamicBookingTask

KST = ZoneInfo("Asia/Seoul")
FIXED_NOW = dt.datetime(2026, 8, 26, 18, 0, tzinfo=KST)


def _task() -> DynamicBookingTask:
    task = object.__new__(DynamicBookingTask)
    task._select_operator = False
    task._services = [
        {"id": 3590, "name": "노트북 상담", "keywords": ["노트북"]},
    ]
    task._advisors = [
        {
            "id": 805,
            "name": "김테스트",
            "service_ids": [3590],
        }
    ]
    task._advisors_by_id = {805: task._advisors[0]}
    task._schedule_lookup_days = 30
    task._staged = None
    task._staged_requires_revalidation = False
    task._pending_earliest_offer = None
    task._call_session_data = SimpleNamespace(
        room_name="synthetic-schedule-presentation",
        dynamic_booking_pending=None,
        reservation_info={},
    )
    return task


def _session():
    return SimpleNamespace(history=SimpleNamespace(add_message=Mock()))


def _recorded_result_count(session) -> int:
    message = session.history.add_message.call_args.kwargs
    return message["extra"]["agent_event"]["details"]["count"]


def test_prompt_limits_date_and_time_choices_to_three_total():
    task = _task()

    with patch(
        "tasks.dynamic_booking_task.now_kst",
        return_value=FIXED_NOW,
    ):
        instructions = task._build_instructions()

    assert "Offer at most 3 date/time options in total" in instructions
    assert "never list 3 dates and 3 times separately" in instructions
    assert "presentation.has_more=true" in instructions


@pytest.mark.asyncio
async def test_multi_day_search_exposes_only_three_dates_but_records_full_count():
    task = _task()
    task._fetch_available_dates = AsyncMock(
        return_value=[
            "2026-08-31",
            "2026-09-01",
            "2026-09-02",
            "2026-09-03",
            "2026-09-04",
        ]
    )
    session = _session()

    with (
        patch.object(DynamicBookingTask, "session", session),
        patch(
            "tasks.dynamic_booking_task.now_kst",
            return_value=FIXED_NOW,
        ),
    ):
        result = await DynamicBookingTask.search_schedules._func(
            task,
            start_date="2026-08-26",
            end_date="2026-09-25",
        )

    assert [item["date"] for item in result["available_dates"]] == [
        "2026-08-31",
        "2026-09-01",
        "2026-09-02",
    ]
    assert result["presentation"] == {
        "option_kind": "date",
        "max_options": 3,
        "returned": 3,
        "total_available": 5,
        "has_more": True,
        "remaining_count": 2,
        "instruction": (
            "available_dates에 담긴 선택지만 안내하세요. 날짜와 시각 선택지는 "
            "합계 최대 3개입니다. has_more가 true이면 다른 구체적인 일정은 말하지 "
            "말고 추가 일정이 있다고만 요약한 뒤 선호 요일, 주 또는 시간대를 "
            "물으세요."
        ),
    }
    assert "2026-09-03" not in str(result)
    assert "2026-09-04" not in str(result)
    assert _recorded_result_count(session) == 5


@pytest.mark.asyncio
async def test_single_day_search_exposes_only_three_times_but_records_full_count():
    task = _task()
    task._fetch_day = AsyncMock(
        return_value={
            "2026-08-31 09:00": [805],
            "2026-08-31 10:00": [805],
            "2026-08-31 11:00": [805],
            "2026-08-31 14:00": [805],
            "2026-08-31 15:00": [805],
        }
    )
    session = _session()

    with (
        patch.object(DynamicBookingTask, "session", session),
        patch(
            "tasks.dynamic_booking_task.now_kst",
            return_value=FIXED_NOW,
        ),
    ):
        result = await DynamicBookingTask.search_schedules._func(
            task,
            start_date="2026-08-31",
            end_date="2026-08-31",
            service_id=3590,
        )

    assert [item["datetime"] for item in result["slots"]] == [
        "2026-08-31 09:00",
        "2026-08-31 10:00",
        "2026-08-31 11:00",
    ]
    assert result["presentation"]["option_kind"] == "datetime"
    assert result["presentation"]["max_options"] == 3
    assert result["presentation"]["returned"] == 3
    assert result["presentation"]["total_available"] == 5
    assert result["presentation"]["has_more"] is True
    assert result["presentation"]["remaining_count"] == 2
    assert "2026-08-31 14:00" not in str(result)
    assert "2026-08-31 15:00" not in str(result)
    assert _recorded_result_count(session) == 5


@pytest.mark.asyncio
async def test_available_appointments_only_use_slots_in_requested_time_window():
    task = _task()
    task._services.append({"id": 3591, "name": "액정 교체", "keywords": []})
    task._advisors.append({"id": 806, "name": "이테스트", "service_ids": [3591]})
    task._advisors_by_id[806] = task._advisors[-1]
    task._last_search_conditions = {"service_id": 3591}
    task._fetch_day = AsyncMock(
        return_value={
            "2026-08-31 09:00": [805],
            "2026-08-31 14:00": [806],
        }
    )

    with (
        patch.object(DynamicBookingTask, "session", _session()),
        patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW),
    ):
        result = await DynamicBookingTask.search_schedules._func(
            task,
            start_date="2026-08-31",
            end_date="2026-08-31",
            time_from="00:00",
            time_to="11:59",
        )

    task._fetch_day.assert_awaited_once_with(
        "2026-08-31", service_id=None, operator_id=None
    )
    assert result["available_services"] == [{"id": 3590, "name": "노트북 상담"}]
    assert (
        result["slots"][0]["available_services"] == result["available_services"]
    )
    assert [slot["datetime"] for slot in result["slots"]] == ["2026-08-31 09:00"]
    assert task._last_search_conditions["service_id"] is None
    assert task._staged is None
