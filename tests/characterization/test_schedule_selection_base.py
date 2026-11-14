"""Characterization tests for the shared schedule-selection tool responses."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tasks.operator_schedule_selection_task import OperatorScheduleSelectionTask

# Far-future snapshot so relative-day phrases ("오늘"/"내일") never apply.
_SCHEDULES = [
    {"time": "2030-01-07 09:00:00", "advisor_id": 7},
    {"time": "2030-01-07 15:00:00", "advisor_id": 7},
    {"time": "2030-01-08 15:30:00", "advisor_id": 7},
]


def _task(monkeypatch, *, language_code="ko-KR", reservation_data=None):
    session = SimpleNamespace(
        history=SimpleNamespace(add_message=Mock()),
        say=Mock(),
        generate_reply=Mock(),
        userdata=SimpleNamespace(business_data=SimpleNamespace(client_id=1)),
    )
    monkeypatch.setattr(
        OperatorScheduleSelectionTask, "session", property(lambda self: session)
    )
    # action="create" keeps the base-class flow (availability already loaded).
    task = OperatorScheduleSelectionTask(
        operator_id=7,
        reservation_data={} if reservation_data is None else reservation_data,
        action="create",
        language_code=language_code,
        search_start_date="2030-01-01",
        search_end_date="2030-01-31",
    )
    task._schedules = list(_SCHEDULES)
    return task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language_code", "expected_times"),
    [("ko-KR", ["오후 3시"]), ("en-US", ["3 PM"])],
)
async def test_search_slots_by_time_formats_slot_times_in_call_language(
    monkeypatch, language_code, expected_times
):
    task = _task(monkeypatch, language_code=language_code)

    response = await task.search_slots_by_time._func(
        task,
        SimpleNamespace(),
        period="afternoon",
        start_date="2030-01-07",
        end_date="2030-01-07",
    )

    assert response["status"] == "ok"
    assert response["available_slots"] == [
        {"date": "2030-01-07", "times": expected_times}
    ]
    assert task._last_offered_slots == ["2030-01-07 15:00:00"]


@pytest.mark.asyncio
@pytest.mark.parametrize("date_count", [1, 6])
async def test_time_search_retains_all_raw_matches_for_confirmation(
    monkeypatch, date_count
):
    task = _task(monkeypatch)
    task._schedules = [
        {"time": f"2030-01-{day:02d} {hour:02d}:00:00", "advisor_id": 7}
        for day in range(7, 7 + date_count)
        for hour in range(12, 17)
    ]

    response = await task.search_slots_by_time._func(
        task, SimpleNamespace(), period="afternoon"
    )

    assert response["status"] == "ok"
    assert response["response_type"] == "slots_for_time_condition"
    assert len(response["available_slots"]) == date_count
    assert all(len(item["times"]) == 5 for item in response["available_slots"])
    assert task._last_offered_slots == [item["time"] for item in task._schedules]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "kwargs"),
    [
        ("pick", {"time": "오전 9시", "date": "2030-01-07"}),
        ("check_time_availability", {"time": "오전 10시", "date": "2030-01-07"}),
        ("load_more_dates", {"start_date": "2030-01-07"}),
        ("select_date", {"date": "2030-01-07"}),
        ("search_slots_by_time", {}),
        ("search_slots_by_time", {"period": "evening"}),
    ],
)
async def test_new_schedule_response_clears_previous_single_offer(
    monkeypatch, tool_name, kwargs
):
    task = _task(monkeypatch)
    task._last_offered_slots = ["2030-01-07 15:00:00"]

    await getattr(task, tool_name)._func(task, SimpleNamespace(), **kwargs)

    assert task._last_offered_slots == []


_TIMES_FOR_JAN_7 = {
    "status": "ok",
    "response_type": "times_for_date",
    "next_action": "ask_time",
    "date": "2030-01-07",
    "available_times": ["오전 9시", "오후 3시"],
}


@pytest.mark.asyncio
async def test_select_date_commits_date_and_returns_times(monkeypatch):
    reservation_data = {}
    task = _task(monkeypatch, reservation_data=reservation_data)
    task._unclear_time_count = 3

    response = await task.select_date._func(task, SimpleNamespace(), date="2030-01-07")

    assert {k: v for k, v in response.items() if k != "user_message"} == (
        _TIMES_FOR_JAN_7
    )
    assert "1월 7일" in response["user_message"]
    assert reservation_data["reservation_date"] == "2030-01-07"
    assert task._stage == "select_time"
    assert "2030-01-07" in task._announced_dates
    assert task._unclear_time_count == 0


@pytest.mark.asyncio
async def test_load_more_dates_single_date_matches_select_date(monkeypatch):
    selected = _task(monkeypatch)
    loaded = _task(monkeypatch)
    loaded._unclear_time_count = 2

    select_response = await selected.select_date._func(
        selected, SimpleNamespace(), date="2030-01-07"
    )
    load_response = await loaded.load_more_dates._func(
        loaded, SimpleNamespace(), start_date="2030-01-07"
    )

    assert load_response == select_response
    assert loaded._reservation_data["reservation_date"] == "2030-01-07"
    assert loaded._stage == "select_time"
    assert "2030-01-07" in loaded._announced_dates
    assert loaded._unclear_time_count == 0


@pytest.mark.asyncio
async def test_unclear_time_reads_known_date_times_on_second_report(monkeypatch):
    task = _task(monkeypatch, reservation_data={"reservation_date": "2030-01-07"})
    reference = _task(monkeypatch)
    expected = await reference.select_date._func(
        reference, SimpleNamespace(), date="2030-01-07"
    )

    first = await task.report_unclear_time._func(task, SimpleNamespace())
    second = await task.report_unclear_time._func(task, SimpleNamespace())

    assert first["status"] == "unclear"
    assert first["response_type"] == "unclear_datetime"
    assert first["date"] == "2030-01-07"
    assert second == expected
    assert task._stage == "select_time"
    # The loop-break read-out does not reset the unclear counter.
    assert task._unclear_time_count == 2


@pytest.mark.asyncio
async def test_pick_then_confirm_completes_with_selected_slot(monkeypatch):
    reservation_data = {}
    task = _task(monkeypatch, reservation_data=reservation_data)
    completed = []
    monkeypatch.setattr(task, "complete", completed.append)

    staged = await task.pick._func(
        task, SimpleNamespace(), time="오후 3시", date="2030-01-07"
    )
    await task.confirm_pick._func(task, SimpleNamespace())

    assert staged["status"] == "ok"
    assert staged["appointment_datetime"] == "2030-01-07 15:00:00"
    assert reservation_data["reservation_date"] == "2030-01-07"
    assert reservation_data["appointment_datetime"] == "2030-01-07 15:00:00"
    assert completed[0].appointment_datetime == "2030-01-07 15:00:00"
    assert completed[0].advisor_id == 7


@pytest.mark.asyncio
async def test_available_time_check_offers_that_single_slot(monkeypatch):
    task = _task(monkeypatch)

    response = await task.check_time_availability._func(
        task, SimpleNamespace(), time="오전 9시", date="2030-01-07"
    )

    assert response["available"] is True
    assert task._last_offered_slots == ["2030-01-07 09:00:00"]
    assert task._pending_pick is None


@pytest.mark.asyncio
async def test_confirm_after_available_time_check_reconfirms_then_completes(
    monkeypatch,
):
    task = _task(monkeypatch)
    completed = []
    monkeypatch.setattr(task, "complete", completed.append)

    await task.check_time_availability._func(
        task, SimpleNamespace(), time="오후 3시", date="2030-01-07"
    )
    recovered = await task.confirm_pick._func(task, SimpleNamespace())

    assert recovered["response_type"] == "pending_confirmation"
    assert recovered["appointment_datetime"] == "2030-01-07 15:00:00"
    assert completed == []

    await task.confirm_pick._func(task, SimpleNamespace())

    assert completed[0].appointment_datetime == "2030-01-07 15:00:00"
    task.session.generate_reply.assert_not_called()
