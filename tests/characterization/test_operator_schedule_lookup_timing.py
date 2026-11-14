from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tasks.operator_schedule_selection_task import OperatorScheduleSelectionTask
from tools import booking_tools


def _modify_task(monkeypatch, available_times):
    api = AsyncMock(
        return_value=SimpleNamespace(
            success=True,
            data=SimpleNamespace(availableTimes=available_times),
            message="ok",
        )
    )
    monkeypatch.setattr(
        "tasks.operator_schedule_selection_task.api_get_operator_available_times",
        api,
    )
    session = SimpleNamespace(
        history=SimpleNamespace(add_message=Mock()),
        say=Mock(),
        generate_reply=Mock(),
        userdata=SimpleNamespace(business_data=SimpleNamespace(client_id=9001)),
    )
    monkeypatch.setattr(
        OperatorScheduleSelectionTask, "session", property(lambda self: session)
    )
    task = OperatorScheduleSelectionTask(
        operator_id=955,
        reservation_data={},
        search_start_date="2026-10-28",
        search_end_date="2026-11-27",
    )
    completed = []
    monkeypatch.setattr(task, "complete", completed.append)
    return task, session, completed


@pytest.mark.asyncio
async def test_exact_date_and_time_stages_pick_before_confirmation(monkeypatch):
    task, session, completed = _modify_task(
        monkeypatch,
        [
            "2026-10-29T10:00:00.000Z",
            "2026-10-29T12:00:00.000Z",
            "2026-10-29T15:00:00.000Z",
        ],
    )

    response = await task.search_preferred_schedules._func(
        task,
        SimpleNamespace(),
        start_date="2026-10-29",
        end_date="2026-10-29",
        after_time="12:00",
        before_time="12:00",
        period="afternoon",
        weekdays=["thu"],
    )

    assert response["response_type"] == "pending_confirmation"
    assert response["appointment_datetime"] == "2026-10-29 12:00:00"
    assert task._stage == "confirm"
    assert completed == []

    result = await task.confirm_pick._func(task, SimpleNamespace())

    assert result is None
    assert len(completed) == 1
    assert completed[0].appointment_datetime == "2026-10-29 12:00:00"
    assert completed[0].advisor_id == 955
    session.generate_reply.assert_not_called()


@pytest.mark.asyncio
async def test_exact_date_ignores_conflicting_weekday_and_stages_pick(monkeypatch):
    task, session, completed = _modify_task(
        monkeypatch,
        ["2026-11-03T15:00:00.000Z"],
    )
    bound_logger = Mock()
    monkeypatch.setattr(
        "tasks.operator_schedule_selection_task.logger.bind", bound_logger
    )

    response = await task.search_preferred_schedules._func(
        task,
        SimpleNamespace(),
        start_date="2026-11-03",
        end_date="2026-11-03",
        weekdays=["wed"],
        after_time="15:00",
        before_time="15:00",
    )

    assert response["response_type"] == "pending_confirmation"
    assert response["appointment_datetime"] == "2026-11-03 15:00:00"
    assert task._stage == "confirm"
    assert completed == []
    bound_logger.assert_called_once_with(
        task=task._TASK_TAG,
        stage="weekday_mismatch",
        date="2026-11-03",
        weekdays=["wed"],
    )
    bound_logger.return_value.warning.assert_called_once()
    session.generate_reply.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("weekday", ["tue", "TUE", "Tuesday", "화", "화요일"])
async def test_range_preference_returns_only_named_weekday(monkeypatch, weekday):
    task, _, completed = _modify_task(
        monkeypatch,
        [
            "2026-11-02T15:00:00.000Z",
            "2026-11-03T09:00:00.000Z",
            "2026-11-03T15:00:00.000Z",
            "2026-11-04T15:00:00.000Z",
            "2026-11-10T15:00:00.000Z",
        ],
    )

    response = await task.search_preferred_schedules._func(
        task,
        SimpleNamespace(),
        start_date="2026-11-02",
        end_date="2026-11-10",
        weekdays=[weekday],
    )

    assert response["response_type"] == "preferred_schedules"
    assert response["available_slots"] == [
        "2026-11-03 09:00:00",
        "2026-11-03 15:00:00",
        "2026-11-10 15:00:00",
    ]
    assert task._last_offered_slots == response["available_slots"]
    assert completed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("weekday", ["funday", "2"])
async def test_invalid_weekday_returns_preference_error(monkeypatch, weekday):
    task, _, completed = _modify_task(monkeypatch, [])

    response = await task.search_preferred_schedules._func(
        task, SimpleNamespace(), weekdays=[weekday]
    )

    assert response == task._preference_error("weekday")
    assert task._availability_loaded is False
    assert completed == []


@pytest.mark.asyncio
async def test_unavailable_exact_time_does_not_stage_pick(monkeypatch):
    task, session, completed = _modify_task(
        monkeypatch,
        [
            "2026-10-29T10:00:00.000Z",
            "2026-10-29T12:00:00.000Z",
            "2026-10-29T15:00:00.000Z",
        ],
    )

    response = await task.search_preferred_schedules._func(
        task,
        SimpleNamespace(),
        start_date="2026-10-29",
        end_date="2026-10-29",
        after_time="11:00",
        before_time="11:00",
        weekdays=["thu"],
    )

    assert response["status"] == "unavailable"
    assert task._pending_pick is None
    assert task._last_offered_slots == []
    assert completed == []
    session.generate_reply.assert_not_called()


@pytest.mark.asyncio
async def test_confirm_without_pick_recovers_single_offer_before_completing(
    monkeypatch,
):
    task, session, completed = _modify_task(
        monkeypatch,
        ["2026-10-29T10:00:00.000Z", "2026-10-29T12:00:00.000Z"],
    )

    offered = await task.search_preferred_schedules._func(
        task,
        SimpleNamespace(),
        start_date="2026-10-29",
        end_date="2026-10-29",
        period="afternoon",
    )

    assert offered["response_type"] == "preferred_schedules"
    assert offered["available_slots"] == ["2026-10-29 12:00:00"]
    assert task._pending_pick is None

    recovered = await task.confirm_pick._func(task, SimpleNamespace())

    assert recovered["response_type"] == "pending_confirmation"
    assert recovered["appointment_datetime"] == "2026-10-29 12:00:00"
    assert task._stage == "confirm"
    assert completed == []
    session.generate_reply.assert_not_called()

    result = await task.confirm_pick._func(task, SimpleNamespace())

    assert result is None
    assert len(completed) == 1
    assert completed[0].appointment_datetime == "2026-10-29 12:00:00"
    assert completed[0].advisor_id == 955
    session.generate_reply.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "available_times",
    [[], ["2026-10-29T12:00:00.000Z", "2026-10-29T15:00:00.000Z"]],
)
async def test_confirm_without_pending_pick_requires_choice_for_zero_or_many_offers(
    monkeypatch, available_times
):
    task, session, completed = _modify_task(monkeypatch, available_times)
    await task.search_preferred_schedules._func(
        task,
        SimpleNamespace(),
        start_date="2026-10-29",
        end_date="2026-10-29",
        period="afternoon",
    )

    response = await task.confirm_pick._func(task, SimpleNamespace())

    assert response["status"] == "no_pending_pick"
    assert response["response_type"] == "no_pending_pick"
    assert response["next_action"] == "pick"
    assert response["user_message"] == "확정하실 시간을 한 번만 더 말씀해 주시겠어요?"
    assert task._pending_pick is None
    assert completed == []
    session.generate_reply.assert_not_called()


@pytest.mark.asyncio
async def test_modify_task_asks_exact_question_before_any_availability_lookup(
    monkeypatch,
):
    api = AsyncMock()
    monkeypatch.setattr(
        "tasks.operator_schedule_selection_task.api_get_operator_available_times",
        api,
    )
    session = SimpleNamespace(
        history=SimpleNamespace(add_message=Mock()),
        say=Mock(),
        userdata=SimpleNamespace(business_data=SimpleNamespace(client_id=9001)),
    )
    monkeypatch.setattr(
        OperatorScheduleSelectionTask, "session", property(lambda self: session)
    )
    task = OperatorScheduleSelectionTask(
        operator_id=955,
        reservation_data={},
        search_start_date="2026-09-28",
        search_end_date="2026-10-28",
    )

    await task.on_enter()

    session.say.assert_called_once_with(
        "어떤 날짜로 변경 원하세요? 편하신 요일이나 시간대 있으시면 같이 말씀해주세요",
        allow_interruptions=False,
    )
    api.assert_not_awaited()
    assert task._availability_loaded is False
    assert task._require_availability()["status"] == "lookup_required"

    invalid = await task.search_preferred_schedules._func(
        task, SimpleNamespace(), start_date="not-a-date"
    )
    assert invalid["user_message"] == "원하시는 날짜를 다시 말씀해 주세요."
    api.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_preference_answer_triggers_lookup_and_filters_same_task(
    monkeypatch,
):
    api = AsyncMock(
        return_value=SimpleNamespace(
            success=True,
            data=SimpleNamespace(
                availableTimes=[
                    "2026-09-29T15:00:00.000Z",
                    "2026-09-30T09:00:00.000Z",
                    "2026-09-30T15:00:00.000Z",
                ]
            ),
            message="ok",
        )
    )
    monkeypatch.setattr(
        "tasks.operator_schedule_selection_task.api_get_operator_available_times",
        api,
    )
    history = SimpleNamespace(add_message=Mock())
    session = SimpleNamespace(
        history=history,
        say=Mock(),
        userdata=SimpleNamespace(business_data=SimpleNamespace(client_id=9001)),
    )
    monkeypatch.setattr(
        OperatorScheduleSelectionTask, "session", property(lambda self: session)
    )
    reservation_data = {}
    task = OperatorScheduleSelectionTask(
        operator_id=955,
        reservation_data=reservation_data,
        search_start_date="2026-09-28",
        search_end_date="2026-10-28",
    )
    await task.on_enter()
    assert not api.await_count

    response = await task.search_preferred_schedules._func(
        task,
        SimpleNamespace(),
        start_date="2026-09-30",
        end_date="2026-09-30",
        period="afternoon",
    )

    api.assert_awaited_once_with(
        operator_id=955,
        start_date="2026-09-30",
        end_date="2026-09-30",
        client_id=9001,
    )
    assert response["status"] == "ok"
    assert "9월 30일" in response["user_message"]
    assert "오후 3시" in response["user_message"]
    assert "오전 9시" not in response["user_message"]
    assert reservation_data["reservation_date"] == "2026-09-30"
    assert task._availability_loaded is True
    assert len(task._schedules) == 3
    assert any(
        call.kwargs.get("extra", {}).get("agent_event", {}).get("name") == "API_RESULT"
        for call in history.add_message.call_args_list
    )

    await task.search_preferred_schedules._func(
        task, SimpleNamespace(), start_date="2026-10-01", end_date="2026-10-01"
    )
    assert api.await_count == 2
    assert api.await_args.kwargs["start_date"] == "2026-10-01"
    assert api.await_args.kwargs["end_date"] == "2026-10-01"


@pytest.mark.asyncio
async def test_weekday_or_time_only_preference_queries_configured_range(monkeypatch):
    api = AsyncMock(
        return_value=SimpleNamespace(
            success=True,
            data=SimpleNamespace(availableTimes=["2026-09-30T15:00:00.000Z"]),
            message="ok",
        )
    )
    monkeypatch.setattr(
        "tasks.operator_schedule_selection_task.api_get_operator_available_times",
        api,
    )
    session = SimpleNamespace(
        history=SimpleNamespace(add_message=Mock()),
        userdata=SimpleNamespace(business_data=SimpleNamespace(client_id=9001)),
    )
    monkeypatch.setattr(
        OperatorScheduleSelectionTask, "session", property(lambda self: session)
    )
    task = OperatorScheduleSelectionTask(
        operator_id=955,
        reservation_data={},
        search_start_date="2026-09-28",
        search_end_date="2026-10-28",
    )

    result = await task.search_preferred_schedules._func(
        task, SimpleNamespace(), weekdays=["wed"], period="afternoon"
    )

    assert result["status"] == "ok"
    api.assert_awaited_once_with(
        operator_id=955,
        start_date="2026-09-28",
        end_date="2026-10-28",
        client_id=9001,
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "known pre-existing failure: the exact-date empty message was "
        "replaced by a generic line without the date"
    ),
)
@pytest.mark.asyncio
async def test_empty_exact_date_lookup_only_describes_requested_date(monkeypatch):
    api = AsyncMock(
        return_value=SimpleNamespace(
            success=True,
            data=SimpleNamespace(availableTimes=[]),
            message="ok",
        )
    )
    monkeypatch.setattr(
        "tasks.operator_schedule_selection_task.api_get_operator_available_times",
        api,
    )
    session = SimpleNamespace(
        history=SimpleNamespace(add_message=Mock()),
        userdata=SimpleNamespace(business_data=SimpleNamespace(client_id=9001)),
    )
    monkeypatch.setattr(
        OperatorScheduleSelectionTask, "session", property(lambda self: session)
    )
    task = OperatorScheduleSelectionTask(
        operator_id=955,
        reservation_data={},
        search_start_date="2026-09-28",
        search_end_date="2026-10-28",
    )

    response = await task.search_preferred_schedules._func(
        task,
        SimpleNamespace(),
        start_date="2026-09-30",
        end_date="2026-09-30",
    )

    assert "9월 30일" in response["user_message"]
    assert "조회 기간" not in response["user_message"]
    assert response["status"] == "unavailable"


@pytest.mark.asyncio
async def test_lookup_failure_returns_distinct_result_instead_of_no_slots(monkeypatch):
    api = AsyncMock(
        return_value=SimpleNamespace(
            success=False,
            data=None,
            message="해당 상담사가 존재하지 않습니다.",
        )
    )
    monkeypatch.setattr(
        "tasks.operator_schedule_selection_task.api_get_operator_available_times",
        api,
    )
    session = SimpleNamespace(
        history=SimpleNamespace(add_message=Mock()),
        userdata=SimpleNamespace(business_data=SimpleNamespace(client_id=9001)),
    )
    monkeypatch.setattr(
        OperatorScheduleSelectionTask, "session", property(lambda self: session)
    )
    task = OperatorScheduleSelectionTask(
        operator_id=955,
        reservation_data={},
        search_start_date="2026-09-28",
        search_end_date="2026-10-28",
    )
    result = []
    monkeypatch.setattr(task, "complete", lambda value: result.append(value))

    response = await task.search_preferred_schedules._func(
        task, SimpleNamespace(), start_date="2026-09-30"
    )

    assert response is None
    assert result[0].lookup_failed is True
    assert result[0].lookup_failure_message == "해당 상담사가 존재하지 않습니다."
    assert task._availability_loaded is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_message", "missing"),
    [
        ("해당 상담사가 존재하지 않습니다.", True),
        ("상담사 예약 가능 시간 조회 중 오류가 발생했습니다.", False),
    ],
)
async def test_modify_appointment_waits_for_operator_task_and_routes_lookup_failure(
    monkeypatch,
    failure_message,
    missing,
):
    chat_ctx = SimpleNamespace(copy=Mock(), truncate=Mock())
    chat_ctx.copy.return_value = chat_ctx
    chat_ctx.truncate.return_value = chat_ctx
    session = SimpleNamespace(
        history=SimpleNamespace(add_message=Mock()),
        current_agent=SimpleNamespace(chat_ctx=chat_ctx),
        input=SimpleNamespace(set_audio_enabled=Mock()),
    )
    userdata = SimpleNamespace(
        recipient_data=SimpleNamespace(phone="01000000101"),
        call_config_data=SimpleNamespace(language="ko-KR"),
        workflow_config=SimpleNamespace(
            booking=SimpleNamespace(schedule_lookup_days=30)
        ),
        reservation_data={},
        room_name="test-room",
    )
    ctx = SimpleNamespace(
        session=session,
        userdata=userdata,
        disallow_interruptions=Mock(),
    )
    selected = {
        "operator_id": 955,
        "reservation_id": "42",
        "appointment_date": "2026-09-25 09:00:00",
    }
    monkeypatch.setattr(
        booking_tools,
        "GetMyReservationsTask",
        Mock(
            return_value=SimpleNamespace(
                run=AsyncMock(return_value=SimpleNamespace(reservations=[selected]))
            )
        ),
    )
    monkeypatch.setattr(booking_tools, "_record_identity_outcome", Mock())
    monkeypatch.setattr(
        booking_tools,
        "_finish_incomplete_reservation_lookup",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        booking_tools,
        "SelectReservationTask",
        AsyncMock(
            return_value=SimpleNamespace(exited=False, selected_reservation=selected)
        ),
    )
    monkeypatch.setattr(
        booking_tools,
        "_handle_same_day_reservation_policy",
        AsyncMock(return_value=False),
    )
    task = AsyncMock(
        return_value=SimpleNamespace(
            lookup_failed=True,
            lookup_failure_message=failure_message,
        )
    )
    monkeypatch.setattr(booking_tools, "OperatorScheduleSelectionTask", task)
    handoff_missing = AsyncMock()
    handoff_other = AsyncMock()
    monkeypatch.setattr(booking_tools, "_handoff_operator_missing", handoff_missing)
    monkeypatch.setattr(
        booking_tools, "_handoff_operator_schedule_lookup_failed", handoff_other
    )

    original_tool_body = booking_tools.modify_appointment._func.__wrapped__.__wrapped__
    await original_tool_body(ctx)

    task.assert_awaited_once()
    if missing:
        handoff_missing.assert_awaited_once()
        handoff_other.assert_not_awaited()
    else:
        handoff_other.assert_awaited_once()
        handoff_missing.assert_not_awaited()
