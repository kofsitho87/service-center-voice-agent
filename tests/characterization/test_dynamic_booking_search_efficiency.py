import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import pytest
from livekit.agents import llm

from data import DynamicBookingResumeContext
from tasks.dynamic_booking_task import (
    SCHEDULE_FETCH_CONCURRENCY,
    BookingApiError,
    DynamicBookingTask,
)

KST = ZoneInfo("Asia/Seoul")
FIXED_NOW = dt.datetime(2026, 8, 27, 9, 0, tzinfo=KST)
TARGET_TIME = "09:30"


def _task() -> DynamicBookingTask:
    task = object.__new__(DynamicBookingTask)
    task._chat_ctx = llm.ChatContext()
    task._chat_ctx.add_message(role="user", content="예약하려고요")
    task._select_operator = False
    task._services = [
        {"id": 3590, "name": "노트북 상담", "keywords": ["노트북"]},
        {"id": 4720, "name": "휴대폰 점검", "keywords": ["휴대폰"]},
    ]
    task._advisors = [
        {
            "id": 805,
            "name": "김테스트",
            "service_ids": [3590],
        },
        {
            "id": 806,
            "name": "이테스트",
            "service_ids": [4720],
        },
    ]
    task._advisors_by_id = {advisor["id"]: advisor for advisor in task._advisors}
    task._schedule_lookup_days = 30
    task._staged = None
    task._staged_requires_revalidation = False
    task._pending_earliest_offer = None
    task._day_rows_cache = {}
    task._date_rows_cache = {}
    task._service_reservation_setting = "OPERATOR"
    task._call_session_data = SimpleNamespace(
        room_name="synthetic-search-efficiency",
        dynamic_booking_pending=None,
        reservation_info={},
        business_data=SimpleNamespace(site_id="site-1", client_id=1),
        # The attempt's single proactive handoff offer is already used, so these
        # tests observe search facts only (offers: test_dynamic_booking_no_slot_handoff).
        dynamic_booking_resume=DynamicBookingResumeContext(
            handoff_offer_delivered=True
        ),
    )
    return task


def _session():
    return SimpleNamespace(history=SimpleNamespace(add_message=Mock()))


def _recorded_result_count(session) -> int:
    message = session.history.add_message.call_args.kwargs
    return message["extra"]["agent_event"]["details"]["count"]


def _candidate_dates(count: int) -> list[str]:
    start = dt.date(2026, 8, 28)
    return [(start + dt.timedelta(days=offset)).isoformat() for offset in range(count)]


def _slots(date: str, *, matches: bool) -> dict[str, list[int]]:
    time = TARGET_TIME if matches else "10:00"
    return {f"{date} {time}": [805]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ids", "end_date", "mode", "error_code"),
    [
        ({"service_id": 0}, "2026-08-28", "list", "unknown_service"),
        ({"service_id": "0"}, "2026-08-28", "list", "unknown_service"),
        (
            {"service_id": 999999},
            "2026-08-30",
            "earliest",
            "unknown_service",
        ),
        ({"operator_id": 999999}, "2026-08-30", "list", "unknown_advisor"),
        (
            {"service_id": 3590, "operator_id": 999999},
            "2026-08-30",
            "earliest_for_booking",
            "unknown_advisor",
        ),
    ],
)
async def test_unknown_schedule_ids_fail_before_lookup(ids, end_date, mode, error_code):
    task = _task()
    task._fetch_day = AsyncMock(return_value={})
    task._fetch_available_dates = AsyncMock(return_value=[])

    with (
        patch.object(DynamicBookingTask, "session", _session()),
        patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW),
        patch(
            "tasks.dynamic_booking_task.get_site_available_schedules",
            new_callable=AsyncMock,
        ) as get_schedules,
    ):
        result = await DynamicBookingTask.search_schedules._func(
            task,
            start_date="2026-08-28",
            end_date=end_date,
            mode=mode,
            **ids,
        )

    assert result["status"] == "error"
    assert result["code"] == error_code
    assert result["retry"] == "silent"
    assert "availability" not in result
    assert "handoff_offer" not in result
    assert task._pending_empty_search is None
    assert task._schedule_blocker is None
    task._fetch_day.assert_not_awaited()
    task._fetch_available_dates.assert_not_awaited()
    get_schedules.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("invalid_ids", "corrected_ids"),
    [
        ({"service_id": "0", "operator_id": "0"}, {}),
        (
            {"service_id": 3590, "operator_id": "999999"},
            {"service_id": 3590},
        ),
    ],
)
async def test_invalid_schedule_ids_allow_same_turn_correction(
    invalid_ids, corrected_ids
):
    task = _task()
    calls = []
    with (
        patch.object(DynamicBookingTask, "session", _session()),
        patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW),
        patch(
            "tasks.dynamic_booking_task.get_site_available_schedules",
            new=_schedule_api(calls),
        ),
    ):
        invalid = await DynamicBookingTask.search_schedules._func(
            task,
            start_date="2026-08-28",
            end_date="2026-08-30",
            **invalid_ids,
        )
        assert invalid["status"] == "error"
        assert calls == []
        assert task._pending_empty_search is None

        corrected = await DynamicBookingTask.search_schedules._func(
            task,
            start_date="2026-08-28",
            end_date="2026-08-30",
            **corrected_ids,
        )

    assert corrected["status"] == "ok"
    assert [item["date"] for item in corrected["available_dates"]] == _candidate_dates(
        3
    )
    assert len(calls) == 1
    assert "availability" not in corrected
    assert "handoff_offer" not in corrected
    assert task._pending_empty_search is None
    assert task._last_search_conditions["service_id"] == corrected_ids.get(
        "service_id"
    )


@pytest.mark.asyncio
async def test_invalid_schedule_ids_clear_previous_request_blocker():
    task = _task()
    task._fetch_day = AsyncMock(return_value={})
    with (
        patch.object(DynamicBookingTask, "session", _session()),
        patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW),
    ):
        empty = await DynamicBookingTask.search_schedules._func(
            task,
            start_date="2026-08-28",
            end_date="2026-08-28",
            service_id=3590,
        )
        assert empty["availability"] == "none_in_searched_range"
        assert task._schedule_blocker is not None
        previous_revision = task._request_revision
        task._chat_ctx.add_message(role="user", content="다시 확인해 주세요")

        invalid = await DynamicBookingTask.search_schedules._func(
            task,
            start_date="2026-08-28",
            end_date="2026-08-28",
            service_id=0,
        )

    assert invalid["code"] == "unknown_service"
    assert task._request_revision == previous_revision + 1
    assert task._schedule_blocker is None
    assert task._pending_empty_search is None
    assert "availability" not in invalid
    assert "handoff_offer" not in invalid
    task._fetch_day.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("operator_id", [None, "None", 0, "0"])
@pytest.mark.parametrize("zero_advisor_exists", [False, True])
async def test_search_normalizes_unspecified_advisor_before_guard_and_lookup(
    operator_id, zero_advisor_exists
):
    task = _task()
    if zero_advisor_exists:
        advisor = {"id": 0, "name": "박테스트", "service_ids": [4720]}
        task._advisors.append(advisor)
        task._advisors_by_id[0] = advisor
    task._fetch_day = AsyncMock(return_value={"2026-08-28 10:00": [806]})
    session = _session()

    with (
        patch.object(DynamicBookingTask, "session", session),
        patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW),
    ):
        result = await DynamicBookingTask.search_schedules._func(
            task,
            start_date="2026-08-28",
            end_date="2026-08-28",
            service_id=4720,
            operator_id=operator_id,
            time_from="None",
            time_to="None",
            target_time="None",
            strictly_after="None",
        )

    assert result["status"] == "ok"
    assert result["slots"][0]["datetime"] == "2026-08-28 10:00"
    assert task._last_search_conditions["operator_id"] is None
    task._fetch_day.assert_awaited_once_with(
        "2026-08-28",
        service_id=4720,
        operator_id=None,
    )


@pytest.mark.asyncio
async def test_exact_time_miss_offers_verified_nearby_slots_before_other_dates():
    task = _task()
    task._chat_ctx = llm.ChatContext()
    task._chat_ctx.add_message(role="user", content="오늘 6시로 해주세요")
    task._fetch_day = AsyncMock(
        return_value={
            "2026-08-28 14:30": [805],
            "2026-08-28 15:00": [805],
            "2026-08-28 17:30": [805],
            "2026-08-28 17:45": [805],
            "2026-08-28 18:15": [805],
            "2026-08-28 18:30": [805],
        }
    )
    session = _session()

    with (
        patch.object(DynamicBookingTask, "session", session),
        patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW),
    ):
        result = await DynamicBookingTask.search_schedules._func(
            task,
            start_date="2026-08-28",
            end_date="2026-08-28",
            service_id=3590,
            target_time="18:00",
        )
        blocked = await DynamicBookingTask.stage_booking._func(
            task,
            SimpleNamespace(disallow_interruptions=Mock()),
            service_id=3590,
            datetime="2026-08-28 17:45",
        )

    assert [slot["datetime"] for slot in result["slots"]] == [
        "2026-08-28 17:45",
        "2026-08-28 18:15",
        "2026-08-28 17:30",
    ]
    assert result["presentation"]["total_available"] == 5
    assert result["required_next_action"]["wait_for_customer"] is True
    assert "required_tool" not in result["required_next_action"]
    assert blocked["status"] == "awaiting_customer"
    assert _recorded_result_count(session) == 0
    task._fetch_day.assert_awaited_once()


@pytest.mark.asyncio
async def test_exact_time_match_keeps_existing_single_slot_stage_gate():
    task = _task()
    task._fetch_day = AsyncMock(
        return_value={
            "2026-08-28 17:30": [805],
            "2026-08-28 18:00": [805],
        }
    )
    session = _session()

    with (
        patch.object(DynamicBookingTask, "session", session),
        patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW),
    ):
        result = await DynamicBookingTask.search_schedules._func(
            task,
            start_date="2026-08-28",
            end_date="2026-08-28",
            service_id=3590,
            target_time="18:00",
        )

    assert [slot["datetime"] for slot in result["slots"]] == ["2026-08-28 18:00"]
    assert result["required_next_action"]["required_tool"] == "stage_booking"
    assert _recorded_result_count(session) == 1


@pytest.mark.asyncio
async def test_single_nearby_slot_requires_customer_choice_before_staging():
    task = _task()
    task._chat_ctx = llm.ChatContext()
    task._chat_ctx.add_message(role="user", content="6시요")
    task._fetch_day = AsyncMock(return_value={"2026-08-28 17:30": [805]})
    task._mutation_lock = asyncio.Lock()
    task._call_session_data.dynamic_booking_resume = None
    session = _session()
    context = SimpleNamespace(disallow_interruptions=Mock())

    with (
        patch.object(DynamicBookingTask, "session", session),
        patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW),
    ):
        result = await DynamicBookingTask.search_schedules._func(
            task,
            start_date="2026-08-28",
            end_date="2026-08-28",
            service_id=3590,
            target_time="18:00",
        )
        blocked = await DynamicBookingTask.stage_booking._func(
            task, context, service_id=3590, datetime="2026-08-28 17:30"
        )
        task._chat_ctx.add_message(role="user", content="5시 30분으로 해주세요")
        staged = await DynamicBookingTask.stage_booking._func(
            task, context, service_id=3590, datetime="2026-08-28 17:30"
        )

    assert [slot["datetime"] for slot in result["slots"]] == ["2026-08-28 17:30"]
    assert "required_tool" not in result["required_next_action"]
    assert blocked["status"] == "awaiting_customer"
    assert staged["status"] == "staged"
    assert staged["confirmation"]["datetime"] == "2026-08-28 17:30"


@pytest.mark.asyncio
async def test_time_constrained_search_stops_after_proving_has_more():
    task = _task()
    dates = _candidate_dates(7)
    task._fetch_available_dates = AsyncMock(return_value=dates)
    task._fetch_day = AsyncMock(
        side_effect=lambda date, **_kwargs: _slots(date, matches=True)
    )
    session = _session()

    with (
        patch.object(DynamicBookingTask, "session", session),
        patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW),
    ):
        result = await DynamicBookingTask.search_schedules._func(
            task,
            start_date=dates[0],
            end_date=dates[-1],
            service_id=3590,
            target_time=TARGET_TIME,
        )

    assert [item["date"] for item in result["available_dates"]] == dates[:3]
    assert result["presentation"]["has_more"] is True
    assert result["presentation"]["total_available"] == 4
    assert task._fetch_day.await_count < len(dates)


@pytest.mark.asyncio
async def test_time_constrained_search_keeps_exact_counts_below_threshold():
    dates = _candidate_dates(6)
    matching_dates = {dates[1], dates[4]}
    task = _task()
    task._fetch_available_dates = AsyncMock(return_value=dates)
    task._fetch_day = AsyncMock(
        side_effect=lambda date, **_kwargs: _slots(date, matches=date in matching_dates)
    )
    session = _session()

    with (
        patch.object(DynamicBookingTask, "session", session),
        patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW),
    ):
        result = await DynamicBookingTask.search_schedules._func(
            task,
            start_date=dates[0],
            end_date=dates[-1],
            service_id=3590,
            target_time=TARGET_TIME,
        )

    assert task._fetch_day.await_count == len(dates)
    assert result["presentation"]["total_available"] == 2
    assert [item["date"] for item in result["available_dates"]] == sorted(
        matching_dates
    )

    zero_task = _task()
    zero_task._fetch_available_dates = AsyncMock(return_value=dates)
    # 15:00 is outside the nearby window around 09:30: a true zero result.
    zero_task._fetch_day = AsyncMock(
        side_effect=lambda date, **_kwargs: {f"{date} 15:00": [805]}
    )
    zero_session = _session()

    with (
        patch.object(DynamicBookingTask, "session", zero_session),
        patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW),
    ):
        zero_result = await DynamicBookingTask.search_schedules._func(
            zero_task,
            start_date=dates[0],
            end_date=dates[-1],
            service_id=3590,
            target_time=TARGET_TIME,
        )

    assert zero_result["available_dates"] == []
    assert zero_task._fetch_day.await_count == len(dates)
    assert _recorded_result_count(zero_session) == 0


@pytest.mark.asyncio
async def test_multi_day_exact_time_miss_returns_nearby_times_of_the_first_date():
    """events_20261001_113317: 11:29 matched nothing although 11:30 existed."""
    dates = _candidate_dates(3)
    task = _task()
    task._fetch_available_dates = AsyncMock(return_value=dates)
    task._fetch_day = AsyncMock(
        side_effect=lambda date, **_kwargs: _slots(date, matches=False)
    )
    with (
        patch.object(DynamicBookingTask, "session", _session()),
        patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW),
    ):
        for mode in ("list", "earliest_for_booking"):
            result = await DynamicBookingTask.search_schedules._func(
                task,
                start_date=dates[0],
                end_date=dates[-1],
                service_id=3590,
                target_time=TARGET_TIME,
                mode=mode,
            )
            assert result["exact_time_match"] is False
            assert [slot["datetime"] for slot in result["slots"]] == [
                f"{dates[0]} 10:00"
            ]
            assert "availability" not in result
            # A nearby time is an alternative to choose, never an auto-staged offer.
            assert result["required_next_action"]["wait_for_customer"] is True
            assert task._pending_earliest_offer is None
            task._pending_empty_search = None


@pytest.mark.asyncio
async def test_time_constrained_search_bounds_concurrency_and_preserves_order():
    task = _task()
    dates = _candidate_dates(10)
    matching_dates = {dates[1], dates[4], dates[5], dates[8], dates[9]}
    task._fetch_available_dates = AsyncMock(return_value=dates)
    active = 0
    max_active = 0

    async def fetch_day(date: str, **_kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            delay = (len(dates) - dates.index(date)) / 1000
            await asyncio.sleep(delay)
            return _slots(date, matches=date in matching_dates)
        finally:
            active -= 1

    task._fetch_day = AsyncMock(side_effect=fetch_day)
    session = _session()

    with (
        patch.object(DynamicBookingTask, "session", session),
        patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW),
    ):
        result = await DynamicBookingTask.search_schedules._func(
            task,
            start_date=dates[0],
            end_date=dates[-1],
            service_id=3590,
            target_time=TARGET_TIME,
        )

    serial_expectation = [date for date in dates if date in matching_dates][:3]
    assert max_active <= SCHEDULE_FETCH_CONCURRENCY
    assert [item["date"] for item in result["available_dates"]] == serial_expectation


@pytest.mark.asyncio
async def test_time_constrained_search_maps_booking_api_error():
    task = _task()
    dates = _candidate_dates(6)
    task._fetch_available_dates = AsyncMock(return_value=dates)

    async def fetch_day(date: str, **_kwargs):
        if date == dates[2]:
            raise BookingApiError("lookup failed")
        return _slots(date, matches=True)

    task._fetch_day = AsyncMock(side_effect=fetch_day)
    session = _session()

    with (
        patch.object(DynamicBookingTask, "session", session),
        patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW),
    ):
        result = await DynamicBookingTask.search_schedules._func(
            task,
            start_date=dates[0],
            end_date=dates[-1],
            service_id=3590,
            target_time=TARGET_TIME,
        )

    assert result["status"] == "error"
    assert result["code"] == "schedule_lookup_failed"


@pytest.mark.asyncio
async def test_available_date_cache_reuses_unfiltered_rows_for_local_filters():
    task = _task()
    session = _session()
    response = SimpleNamespace(
        success=True,
        data=SimpleNamespace(
            schedules=[
                SimpleNamespace(time="2026-08-28", operator_ids=[805]),
                SimpleNamespace(time="2026-08-29", operator_ids=[806]),
            ]
        ),
        message="",
        elapsed_ms=12,
    )

    with (
        patch.object(DynamicBookingTask, "session", session),
        patch(
            "tasks.dynamic_booking_task.get_site_available_schedules",
            new=AsyncMock(return_value=response),
        ) as get_schedules,
    ):
        first = await task._fetch_available_dates(
            "2026-08-28",
            "2026-08-29",
            service_id=3590,
        )
        second = await task._fetch_available_dates(
            "2026-08-28",
            "2026-08-29",
            service_id=4720,
        )

    assert first == ["2026-08-28"]
    assert second == ["2026-08-29"]
    get_schedules.assert_awaited_once()
    kwargs = get_schedules.await_args.kwargs
    assert kwargs.get("service_id") is None
    assert kwargs.get("operator_id") is None


def _schedule_api(calls: list[dict], *, saturated: bool = False):
    async def fetch(_site_id, start_date, end_date, **kwargs):
        calls.append({"start": start_date, "end": end_date, **kwargs})
        start = dt.date.fromisoformat(start_date)
        end = dt.date.fromisoformat(end_date)
        dates = [
            (start + dt.timedelta(days=offset)).isoformat()
            for offset in range((end - start).days + 1)
        ]
        rows = [
            SimpleNamespace(
                time=date if kwargs["schedule_type"] == "DATE" else f"{date} 17:00",
                operator_ids=[805],
            )
            for date in dates
        ]
        if saturated:
            rows = rows[:1] * 100
        return SimpleNamespace(
            success=True,
            data=SimpleNamespace(schedules=rows),
            message="",
            elapsed_ms=1,
        )

    return fetch


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["list", "earliest"])
async def test_empty_evening_search_bounds_api_calls_and_reports_verified_scope(
    mode,
):
    task = _task()
    task._schedule_lookup_days = 90
    calls = []
    with (
        patch.object(DynamicBookingTask, "session", _session()),
        patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW),
        patch(
            "tasks.dynamic_booking_task.get_site_available_schedules",
            new=_schedule_api(calls),
        ),
    ):
        result = await DynamicBookingTask.search_schedules._func(
            task,
            start_date="2026-08-28",
            end_date="2026-11-25",
            service_id=3590,
            time_from="18:00",
            mode=mode,
        )

    assert len(calls) <= 8, "One DATE probe and at most seven daily lookups"
    assert all(call["end"] <= "2026-09-03" for call in calls)
    assert result["status"] == "ok"
    assert result["availability"] == "none_in_searched_range"
    assert result["searched_range"] == {
        "start_date": "2026-08-28",
        "end_date": "2026-09-03",
        "spoken": "8월 28일 금요일부터 9월 3일 목요일까지",
    }
    assert result["requested_range_complete"] is False
    assert result["next_start_date"] == "2026-09-04"
    assert result["time_from"] == "18:00"
    assert result["required_next_action"]["wait_for_customer"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("multi_day", [False, True])
async def test_empty_result_blocks_same_turn_search_and_resumes_on_user_turn(multi_day):
    task = _task()
    task.done = lambda: False
    calls = []
    with (
        patch.object(DynamicBookingTask, "session", _session()),
        patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW),
        patch(
            "tasks.dynamic_booking_task.get_site_available_schedules",
            new=_schedule_api(calls),
        ),
    ):
        first = await DynamicBookingTask.search_schedules._func(
            task,
            start_date="2026-08-28",
            end_date="2026-08-30" if multi_day else "2026-08-28",
            service_id=3590,
            time_from="18:00",
        )
        initial_count = len(calls)
        blocked = await DynamicBookingTask.search_schedules._func(
            task,
            start_date="2026-09-04",
            end_date="2026-09-10",
            service_id=3590,
            time_from="17:00",
        )
        assert len(calls) == initial_count
        assert blocked["status"] == "awaiting_customer"
        assert blocked["code"] == "awaiting_customer_after_no_availability"
        assert blocked["previous_search"] == first

        await task.on_user_turn_completed(
            SimpleNamespace(add_message=Mock()),
            SimpleNamespace(text_content="다음 주는 5시도 괜찮아요"),
        )
        resumed = await DynamicBookingTask.search_schedules._func(
            task,
            start_date="2026-09-04",
            end_date="2026-09-10",
            service_id=3590,
            time_from="17:00",
        )

    assert len(calls) > initial_count
    assert resumed["available_dates"]
    assert first["availability"] == "none_in_searched_range"
    assert first["requested_range_complete"] is True
    assert "next_start_date" not in first


@pytest.mark.asyncio
@pytest.mark.parametrize("multi_day", [False, True])
async def test_saturated_schedule_page_is_not_reported_as_no_availability(multi_day):
    task = _task()
    with (
        patch.object(DynamicBookingTask, "session", _session()),
        patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW),
        patch(
            "tasks.dynamic_booking_task.get_site_available_schedules",
            new=_schedule_api([], saturated=True),
        ),
    ):
        result = await DynamicBookingTask.search_schedules._func(
            task,
            start_date="2026-08-28",
            end_date="2026-08-30" if multi_day else "2026-08-28",
            service_id=3590,
            time_from="18:00",
        )

    assert result["status"] == "error"
    assert result["code"] == "schedule_lookup_failed"
    assert "availability" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    # target_time misses return nearby times instead (see the nearby test).
    "filters",
    [{"time_from": "18:00"}, {"time_to": "09:00"}],
)
async def test_match_outside_window_requires_next_customer_turn(filters):
    task = _task()
    task.done = lambda: False
    calls = []
    base_fetch = _schedule_api(calls)

    async def fetch(site_id, start_date, end_date, **kwargs):
        response = await base_fetch(site_id, start_date, end_date, **kwargs)
        if kwargs["schedule_type"] == "DATE_TIME" and start_date == "2026-09-04":
            response.data.schedules[0].time = (
                "2026-09-04 09:00" if "time_to" in filters else "2026-09-04 18:00"
            )
        return response

    with (
        patch.object(DynamicBookingTask, "session", _session()),
        patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW),
        patch("tasks.dynamic_booking_task.get_site_available_schedules", new=fetch),
    ):
        first = await DynamicBookingTask.search_schedules._func(
            task,
            start_date="2026-08-28",
            end_date="2026-09-04",
            service_id=3590,
            **filters,
        )
        assert first["available_dates"] == []
        assert first["requested_range_complete"] is False
        assert first["next_start_date"] == "2026-09-04"
        assert len(calls) == 8

        await task.on_user_turn_completed(
            SimpleNamespace(add_message=Mock()), SimpleNamespace(text_content="네")
        )
        second = await DynamicBookingTask.search_schedules._func(
            task,
            start_date=first["next_start_date"],
            end_date="2026-09-04",
            service_id=3590,
            **filters,
        )

    assert second["slots"][0]["datetime"] == (
        "2026-09-04 09:00" if "time_to" in filters else "2026-09-04 18:00"
    )
    assert len(calls) == 9


@pytest.mark.asyncio
async def test_exact_seven_day_empty_window_is_complete():
    task = _task()
    calls = []
    with (
        patch.object(DynamicBookingTask, "session", _session()),
        patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW),
        patch(
            "tasks.dynamic_booking_task.get_site_available_schedules",
            new=_schedule_api(calls),
        ),
    ):
        result = await DynamicBookingTask.search_schedules._func(
            task,
            start_date="2026-08-28",
            end_date="2026-09-03",
            service_id=3590,
            time_from="18:00",
        )

    assert len(calls) == 8
    assert result["requested_range_complete"] is True
    assert result["searched_range"]["end_date"] == "2026-09-03"
    assert "next_start_date" not in result


@pytest.mark.asyncio
async def test_empty_clamped_horizon_has_no_next_period_to_offer():
    task = _task()
    task._schedule_lookup_days = 7
    # Handoff offers at the horizon are covered by the handoff controller tests;
    # this test only checks the returned range facts.
    task._proactive_offer_available = lambda: False
    with (
        patch.object(DynamicBookingTask, "session", _session()),
        patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW),
        patch(
            "tasks.dynamic_booking_task.get_site_available_schedules",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    success=True,
                    data=SimpleNamespace(schedules=[]),
                    message="",
                    elapsed_ms=1,
                )
            ),
        ),
    ):
        result = await DynamicBookingTask.search_schedules._func(
            task,
            start_date="2026-08-28",
            end_date="2026-11-25",
            service_id=3590,
            mode="earliest_for_booking",
        )

    assert result["search_horizon_reached"] is True
    assert result["requested_range_complete"] is False
    assert "next_start_date" not in result
    assert result["required_next_action"]["can_search_later_dates"] is False


@pytest.mark.asyncio
async def test_matching_dates_do_not_offer_continuation_past_hidden_options():
    task = _task()
    with (
        patch.object(DynamicBookingTask, "session", _session()),
        patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW),
        patch(
            "tasks.dynamic_booking_task.get_site_available_schedules",
            new=_schedule_api([]),
        ),
    ):
        result = await DynamicBookingTask.search_schedules._func(
            task,
            start_date="2026-08-28",
            end_date="2026-09-10",
            service_id=3590,
            time_from="17:00",
        )

    assert result["presentation"]["has_more"] is True
    assert "next_start_date" not in result


@pytest.mark.asyncio
async def test_text_user_message_resumes_search_without_audio_turn_hook():
    from livekit.agents.llm import ChatContext

    task = _task()
    task._chat_ctx = ChatContext()
    task._chat_ctx.add_message(role="user", content="다음 주 저녁도 확인해 주세요")
    calls = []
    with (
        patch.object(DynamicBookingTask, "session", _session()),
        patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW),
        patch(
            "tasks.dynamic_booking_task.get_site_available_schedules",
            new=_schedule_api(calls),
        ),
    ):
        first = await DynamicBookingTask.search_schedules._func(
            task,
            start_date="2026-08-28",
            end_date="2026-09-03",
            service_id=3590,
            time_from="18:00",
        )
        initial_count = len(calls)
        task._chat_ctx.add_message(role="assistant", content="해당 기간에는 없습니다")
        blocked = await DynamicBookingTask.search_schedules._func(
            task,
            start_date="2026-09-04",
            end_date="2026-09-10",
            service_id=3590,
            time_from="18:00",
        )
        assert blocked["status"] == "awaiting_customer"
        assert len(calls) == initial_count

        # Text generate_reply adds a new message but does not call the audio hook.
        # Even identical wording is a distinct user turn because its ID changes.
        task._chat_ctx.add_message(role="user", content="다음 주도 확인해 주세요")
        resumed = await DynamicBookingTask.search_schedules._func(
            task,
            start_date="2026-09-04",
            end_date="2026-09-10",
            service_id=3590,
            time_from="18:00",
        )

    assert first["availability"] == "none_in_searched_range"
    assert resumed["status"] == "ok"
    assert resumed["searched_range"]["start_date"] == "2026-09-04"
    assert len(calls) == initial_count + 8


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("daily_times", "filters", "expected_latest", "expected_dates"),
    [
        (
            [["17:00"], ["17:30"], ["17:30"]],
            {"time_from": "17:00"},
            "17:30",
            ["2026-08-29", "2026-08-30"],
        ),
        (
            [["19:00"], ["18:00"], ["17:00"]],
            {"time_from": "17:00"},
            "19:00",
            ["2026-08-28"],
        ),
        (
            [["17:00", "17:30"], ["18:00"], ["17:00"], ["21:00"]],
            {"time_from": "17:00"},
            "18:00",
            ["2026-08-29"],
        ),
        (
            [["17:00", "18:00", "20:00"], ["18:30", "19:30"]],
            {"time_from": "18:00", "time_to": "19:00"},
            "18:30",
            ["2026-08-29"],
        ),
    ],
    ids=["ties", "date-order-is-not-time-order", "hidden-later-date", "filtered-times"],
)
async def test_latest_time_comparison_uses_only_returned_verified_candidates(
    daily_times, filters, expected_latest, expected_dates
):
    task = _task()
    dates = _candidate_dates(len(daily_times))
    calls = []
    base_fetch = _schedule_api(calls)

    async def fetch(site_id, start_date, end_date, **kwargs):
        response = await base_fetch(site_id, start_date, end_date, **kwargs)
        if kwargs["schedule_type"] == "DATE_TIME":
            response.data.schedules = [
                SimpleNamespace(time=f"{start_date} {time}", operator_ids=[805])
                for time in daily_times[dates.index(start_date)]
            ]
        return response

    with (
        patch.object(DynamicBookingTask, "session", _session()),
        patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW),
        patch("tasks.dynamic_booking_task.get_site_available_schedules", new=fetch),
    ):
        result = await DynamicBookingTask.search_schedules._func(
            task,
            start_date=dates[0],
            end_date=dates[-1],
            service_id=3590,
            **filters,
        )

    comparison = result["latest_time_comparison"]
    assert comparison["scope"] == "returned_dates"
    assert comparison["time"] == expected_latest
    assert comparison["dates"] == expected_dates
    assert len(result["available_dates"]) <= 3
    assert (
        max(item["latest_appointment_time"] for item in result["available_dates"])
        == expected_latest
    )
    assert len(calls) == len(daily_times) + 1, "Comparison must reuse the daily lookup"
    assert result["required_next_action"]["confirmation_allowed"] is False
    assert "stage_args" not in result["required_next_action"]
    if len(daily_times) > 3:
        assert result["presentation"]["has_more"] is True
        assert dates[3] not in comparison["dates"]
        assert all(item["date"] != dates[3] for item in result["available_dates"])


@pytest.mark.asyncio
async def test_date_only_search_does_not_invent_latest_appointment_times():
    task = _task()
    calls = []
    with (
        patch.object(DynamicBookingTask, "session", _session()),
        patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW),
        patch(
            "tasks.dynamic_booking_task.get_site_available_schedules",
            new=_schedule_api(calls),
        ),
    ):
        result = await DynamicBookingTask.search_schedules._func(
            task,
            start_date="2026-08-28",
            end_date="2026-08-30",
            service_id=3590,
        )

    assert "latest_time_comparison" not in result
    assert all(
        "latest_appointment_time" not in day for day in result["available_dates"]
    )
    assert len(calls) == 1


# ---------- explicit time filters and input validation ----------


def _caller(task, *texts):
    task._chat_ctx = llm.ChatContext()
    for text in texts:
        task._chat_ctx.add_message(role="user", content=text)
    return task


async def _search_with(task, **filters):
    with (
        patch.object(DynamicBookingTask, "session", _session()),
        patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW),
    ):
        return await DynamicBookingTask.search_schedules._func(
            task,
            start_date="2026-08-28",
            end_date="2026-08-28",
            service_id=3590,
            **filters,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "utterances",
    [(), ("예약신청", "휴대폰이요"), ("오늘 오후 5시에 되는 거 있어요?", "네")],
    ids=["no_dialogue", "no_time_mentioned", "different_time_mentioned"],
)
@pytest.mark.parametrize(
    ("filters", "expected_times"),
    [
        ({"target_time": "11:29"}, ["11:29"]),
        ({"time_from": "12:00", "time_to": "18:00"}, ["12:00", "18:00"]),
        ({"time_from": "18:00"}, ["18:00", "19:00"]),
        ({"time_to": "11:30"}, ["11:29", "11:30"]),
    ],
)
async def test_time_filters_apply_without_dialogue_evidence(
    utterances, filters, expected_times
):
    task = _caller(_task(), *utterances)
    task._fetch_day = AsyncMock(
        return_value={
            f"2026-08-28 {time}": [805]
            for time in ["11:29", "11:30", "12:00", "18:00", "19:00"]
        }
    )
    result = await _search_with(task, **filters)
    assert result["status"] == "ok"
    assert [slot["datetime"] for slot in result["slots"]] == [
        f"2026-08-28 {time}" for time in expected_times
    ]
    for name, value in filters.items():
        assert result[name] == value
        assert task._last_search_conditions[name] == value
    task._fetch_day.assert_awaited_once_with(
        "2026-08-28", service_id=3590, operator_id=None
    )


@pytest.mark.asyncio
async def test_full_day_time_filter_remains_explicit_and_keeps_seven_day_window():
    task = _caller(_task(), "휴대폰 점검 예약하려고요")
    calls = []
    base_fetch = _schedule_api(calls)

    async def fetch(site_id, start_date, end_date, **kwargs):
        response = await base_fetch(site_id, start_date, end_date, **kwargs)
        if kwargs["schedule_type"] == "DATE_TIME" and start_date != "2026-09-03":
            response.data.schedules = []
        return response

    with (
        patch.object(DynamicBookingTask, "session", _session()),
        patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW),
        patch(
            "tasks.dynamic_booking_task.get_site_available_schedules",
            new=fetch,
        ),
    ):
        result = await DynamicBookingTask.search_schedules._func(
            task,
            start_date="2026-08-28",
            end_date="2026-09-10",
            service_id=3590,
            time_from="00:00",
            time_to="23:59",
        )

    assert result["status"] == "ok"
    assert result["available_dates"]
    assert result["time_from"] == "00:00"
    assert result["time_to"] == "23:59"
    assert task._last_search_conditions["time_from"] == "00:00"
    assert task._last_search_conditions["time_to"] == "23:59"
    assert result["searched_range"] == {
        "start_date": "2026-08-28",
        "end_date": "2026-09-03",
        "spoken": "8월 28일 금요일부터 9월 3일 목요일까지",
    }
    assert result["range_adjustment"] == {
        "reason": "time_search_window",
        "requested_end_date": "2026-09-10",
        "applied_end_date": "2026-09-03",
    }
    assert result["requested_range_complete"] is False
    assert len(calls) == 8  # One date lookup plus seven daily time lookups.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filters", "error_code"),
    [
        ({"target_time": "24:00"}, "invalid_time"),
        (
            {"target_time": "12:00", "time_from": "00:00", "time_to": "11:59"},
            "conflicting_time_filters",
        ),
        ({"target_time": "09:00", "time_from": "10:00"}, "conflicting_time_filters"),
        ({"target_time": "12:00", "time_to": "11:59"}, "conflicting_time_filters"),
        ({"time_from": "9:00"}, "invalid_time"),
        ({"time_to": "12:60"}, "invalid_time"),
        ({"time_from": "18:00", "time_to": "12:00"}, "invalid_time_range"),
        (
            {"time_from": "18:00", "time_to": "12:00", "target_time": "15:00"},
            "invalid_time_range",
        ),
    ],
)
async def test_invalid_time_filters_fail_before_lookup(filters, error_code):
    task = _caller(_task(), "예약하려고요")
    previous = {"time_from": "00:00", "time_to": "11:59", "target_time": "11:30"}
    task._last_search_conditions = dict(previous)
    task._call_session_data.dynamic_booking_resume.search_conditions = dict(previous)
    task._fetch_day = AsyncMock(return_value={})
    task._fetch_available_dates = AsyncMock(return_value=[])
    result = await _search_with(task, **filters)
    assert result["status"] == "error"
    assert result["code"] == error_code
    assert result["retry"] == ("silent" if error_code == "invalid_time" else "clarify")
    assert task._last_search_conditions == previous
    assert task._call_session_data.dynamic_booking_resume.search_conditions == previous
    task._fetch_day.assert_not_awaited()
    task._fetch_available_dates.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bounds",
    [
        {"time_from": "00:00", "time_to": "11:59"},
        {"time_from": "11:30"},
        {"time_to": "11:30"},
        {"time_from": "11:30", "time_to": "11:30"},
    ],
)
async def test_exact_target_with_inclusive_window_keeps_resume_conditions(bounds):
    task = _task()
    task._fetch_day = AsyncMock(
        return_value={
            f"2026-08-28 {time}": [805] for time in ["10:00", "11:30", "13:00"]
        }
    )
    filters = {**bounds, "target_time": "11:30"}
    result = await _search_with(task, **filters)
    assert result["status"] == "ok"
    assert [slot["datetime"] for slot in result["slots"]] == ["2026-08-28 11:30"]
    for key, value in filters.items():
        assert result[key] == value
        assert task._last_search_conditions[key] == value
        assert (
            task._call_session_data.dynamic_booking_resume.search_conditions[key]
            == value
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["list", "earliest", "earliest_for_booking"])
@pytest.mark.parametrize("multi_day", [False, True])
@pytest.mark.parametrize("case", ["exact", "nearby", "outside_only"])
async def test_bounded_target_never_offers_outside_window(mode, multi_day, case):
    task = _task()
    dates = ["2026-08-28", "2026-08-29"] if multi_day else ["2026-08-28"]
    task._fetch_available_dates = AsyncMock(return_value=dates)
    times = {
        "exact": ["11:30", "13:00"],
        "nearby": ["11:00", "13:00"],
        "outside_only": ["13:00"],
    }[case]
    task._fetch_day = AsyncMock(
        side_effect=lambda date, **kwargs: {f"{date} {time}": [805] for time in times}
    )
    with (
        patch.object(DynamicBookingTask, "session", _session()),
        patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW),
    ):
        result = await DynamicBookingTask.search_schedules._func(
            task,
            start_date=dates[0],
            end_date=dates[-1],
            service_id=3590,
            time_from="00:00",
            time_to="11:59",
            target_time="11:30",
            mode=mode,
        )
    assert result["status"] == "ok"
    assert {key: result[key] for key in ("time_from", "time_to", "target_time")} == {
        "time_from": "00:00",
        "time_to": "11:59",
        "target_time": "11:30",
    }
    # Keep existing mode semantics: one-day earliest modes do not offer nearby slots.
    has_results = case == "exact" or (
        case == "nearby" and (multi_day or mode == "list")
    )
    assert bool(result.get("slots") or result.get("available_dates")) is has_results
    for slot in result.get("slots", []):
        assert slot["datetime"][11:16] == ("11:30" if case == "exact" else "11:00")
    for date in result.get("available_dates", []):
        assert date["latest_appointment_time"] == "11:30"
    if not has_results:
        assert result["availability"] == "none_in_searched_range"
    assert (
        task._call_session_data.dynamic_booking_resume.search_conditions["time_to"]
        == "11:59"
    )


@pytest.mark.parametrize(
    ("bounds", "expected"),
    [
        ({"time_from": "11:30"}, ["12:00", "13:00"]),
        ({"time_to": "11:30"}, ["11:00", "10:00"]),
        ({"time_from": "11:30", "time_to": "11:30"}, []),
    ],
)
def test_nearby_alternatives_respect_one_sided_or_equal_bounds(bounds, expected):
    slots = {
        f"2026-08-28 {time}": [805] for time in ["10:00", "11:00", "12:00", "13:00"]
    }
    result = _task()._nearby_slots(
        slots,
        target_time="11:30",
        time_from=bounds.get("time_from"),
        time_to=bounds.get("time_to"),
        strictly_after=None,
    )
    assert [when[11:16] for when in result] == expected


def test_time_filter_intersects_exact_target_and_window():
    # Defense in depth: even bypassing argument validation cannot leak an outside slot.
    result = DynamicBookingTask._filter_time_constraints(
        {"2026-08-28 13:00": [805]},
        time_from="00:00",
        time_to="11:59",
        target_time="13:00",
    )
    assert result == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"time_from": "12:00", "time_to": "23:59"}, ("12:00", "23:59")),
        ({"time_to": "11:59"}, ("00:00", "11:59")),
        ({"target_time": "14:00"}, ("14:00", "14:00")),
        ({}, None),
        ({"time_from": "00:00", "time_to": "23:59"}, None),
    ],
)
async def test_non_empty_result_reports_the_verified_time_window(filters, expected):
    task = _task()
    task._fetch_day = AsyncMock(
        return_value={
            "2026-08-28 10:00": [805],
            "2026-08-28 13:00": [805],
            "2026-08-28 14:00": [805],
            "2026-08-28 16:00": [805],
        }
    )

    with (
        patch.object(DynamicBookingTask, "session", _session()),
        patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW),
    ):
        result = await DynamicBookingTask.search_schedules._func(
            task,
            start_date="2026-08-28",
            end_date="2026-08-28",
            service_id=3590,
            **filters,
        )

    assert result["status"] == "ok"
    assert result["slots"]
    window = result.get("searched_time_window")
    if expected is None:
        assert window is None
    else:
        assert (window["from"], window["to"]) == expected
        assert "search_schedules" in window["instruction"]
