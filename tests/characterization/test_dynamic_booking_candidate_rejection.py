"""Candidate refusal and whole-booking cancellation are separate transitions."""

import asyncio
import datetime as dt
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from livekit.agents import RunContext, StopResponse, function_tool, llm
from livekit.agents.llm.utils import (
    function_arguments_to_pydantic_model,
    prepare_function_arguments,
)

from data import DynamicBookingResumeContext
from tasks.dynamic_booking_task import KST, DynamicBookingTask, StagedBooking

FIRST = "2026-09-29 10:00"
LATER = "2026-09-29 11:00"
NEXT_DAY = "2026-09-30 10:00"


# The caller states the times these tests filter by (time-filter grounding).
CALLER_TIMES = (
    "오전이나 오후, " + ", ".join(f"{h}시" for h in range(1, 13)) + " 다 괜찮아요"
)


@pytest.fixture
def task(monkeypatch):
    task = object.__new__(DynamicBookingTask)
    task._services = [{"id": 10, "name": "Screen Repair", "keywords": []}]
    task._advisors = [{"id": 100, "name": "Advisor", "service_ids": [10]}]
    task._advisors_by_id = {100: task._advisors[0]}
    task._select_operator = False
    task._schedule_lookup_days = 30
    task._mutation_lock = asyncio.Lock()
    task._call_session_data = SimpleNamespace(
        room_name="synthetic-candidate-rejection",
        dynamic_booking_pending=None,
        # The attempt's single proactive handoff offer is already used, so these
        # tests observe search facts only (offers: test_dynamic_booking_no_slot_handoff).
        dynamic_booking_resume=DynamicBookingResumeContext(
            handoff_offer_delivered=True
        ),
        reservation_info={},
    )
    task._chat_ctx = llm.ChatContext()
    task._chat_ctx.add_message(role="user", content=CALLER_TIMES)
    task._chat_ctx.add_message(role="user", content="No, that time does not work.")
    task._set_staged(
        StagedBooking(
            service_id=10,
            service_name="Screen Repair",
            operator_id=100,
            operator_name="Advisor",
            datetime=FIRST,
        )
    )
    task._last_search_conditions = {
        "service_id": 10,
        "operator_id": None,
        "start_date": "2026-09-22",
        "end_date": "2026-10-22",
        "time_to": "11:59",
    }
    task._fetch_day = AsyncMock(
        side_effect=lambda day, **kwargs: {
            when: [100] for when in (FIRST, LATER, NEXT_DAY) if when[:10] == day
        }
    )
    task._fetch_available_dates = AsyncMock(return_value=["2026-09-29", "2026-09-30"])
    task.complete = Mock()
    monkeypatch.setattr(
        DynamicBookingTask,
        "session",
        SimpleNamespace(history=SimpleNamespace(add_message=Mock()), say=AsyncMock()),
    )
    monkeypatch.setattr(
        "tasks.dynamic_booking_task.now_kst",
        lambda: dt.datetime(2026, 9, 22, 9, 0, tzinfo=KST),
    )
    return task


async def search(task, **kwargs):
    return await DynamicBookingTask.search_schedules._func(
        task,
        start_date="2026-09-29",
        end_date=kwargs.pop("end_date", "2026-09-29"),
        service_id=10,
        **kwargs,
    )


async def stage(task, when=FIRST):
    return await DynamicBookingTask.stage_booking._func(
        task, SimpleNamespace(disallow_interruptions=Mock()), 10, when
    )


async def cancel(task, **kwargs):
    try:
        return await DynamicBookingTask.exit_task._func(
            task, reason_code="cancel", **kwargs
        )
    except StopResponse:
        return task._cancel_question


def answer_cancel_question(
    task, answer="Yes, stop this booking.", *, interrupted=False
):
    task._chat_ctx.add_message(
        role="assistant", content=task._cancel_question, interrupted=interrupted
    )
    task._chat_ctx.add_message(role="user", content=answer)


@pytest.mark.asyncio
async def test_slot_rejection_keeps_preferences_and_blocks_stale_commit(task):
    result = await DynamicBookingTask.reject_candidate._func(task)

    assert result["status"] == "candidate_rejected"
    assert result["scope"] == "slot"
    assert result["search_conditions"]["service_id"] == 10
    assert result["search_conditions"]["time_to"] == "11:59"
    assert task._staged is None
    assert task._call_session_data.dynamic_booking_pending is None
    task.complete.assert_not_called()
    committed = await DynamicBookingTask.commit_booking._func(task, FIRST)
    assert committed["code"] == "nothing_staged"
    assert task._call_session_data.reservation_info == {}

    result = await search(task)
    assert [slot["datetime"] for slot in result["slots"]] == [LATER]
    assert (await stage(task))["code"] == "candidate_rejected"
    assert (await stage(task, LATER))["status"] == "staged"


@pytest.mark.asyncio
async def test_date_rejection_removes_all_slots_and_date_summary(task):
    await DynamicBookingTask.reject_candidate._func(task, scope="date")

    result = await search(task, end_date="2026-09-30")
    assert [item["date"] for item in result["available_dates"]] == ["2026-09-30"]
    assert (await stage(task, LATER))["code"] == "candidate_rejected"
    assert task._call_session_data.dynamic_booking_resume.excluded_dates == [
        "2026-09-29"
    ]


@pytest.mark.asyncio
async def test_unavailable_stage_waits_before_searching_non_rejected_alternatives(task):
    await DynamicBookingTask.reject_candidate._func(task)
    result = await stage(task, "2026-09-29 12:00")
    assert result["status"] == "unavailable"
    assert "same_day_alternatives" not in result
    assert result["required_next_action"]["wait_for_customer"] is True
    assert (await search(task))["status"] == "awaiting_customer"

    task._chat_ctx.add_message(role="user", content="Other times that day are okay.")
    result = await search(task)
    assert [item["datetime"] for item in result["slots"]] == [LATER]
    assert (await stage(task, FIRST))["code"] == "candidate_rejected"
    assert (await stage(task, LATER))["status"] == "staged"


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_source", ["search", "stage"])
@pytest.mark.parametrize("new_turn", ["chat_message", "audio_hook"])
async def test_no_availability_blocks_both_tools_until_customer_reply(
    task, empty_source, new_turn
):
    if empty_source == "search":
        # 11:00 is outside the three-hour nearby window around 15:00.
        result = await search(task, target_time="15:00")
    else:
        result = await stage(task, "2026-09-29 12:00")
    assert result["slots"] == []
    assert result["target_time"] == ("15:00" if empty_source == "search" else "12:00")
    assert result["searched_range"] == {
        "start_date": "2026-09-29",
        "end_date": "2026-09-29",
        "spoken": "9월 29일 화요일",
    }
    assert result["required_next_action"]["confirmation_allowed"] is False
    initial_fetches = task._fetch_day.await_count

    # A different tool or an otherwise valid alternative cannot bypass the wait.
    for blocked in (await stage(task, LATER), await search(task)):
        assert blocked["status"] == "awaiting_customer"
        assert blocked["previous_search"] is result
    assert task._fetch_day.await_count == initial_fetches
    assert task._staged is None
    assert (await DynamicBookingTask.commit_booking._func(task, LATER))["code"] == (
        "nothing_staged"
    )
    assert task._call_session_data.reservation_info == {}
    task.complete.assert_not_called()

    if new_turn == "chat_message":
        task._chat_ctx.add_message(role="user", content="11 AM is okay, please use it.")
    else:
        task.done = lambda: False
        await task.on_user_turn_completed(
            SimpleNamespace(add_message=Mock()),
            SimpleNamespace(text_content="11 AM is okay, please use it."),
        )
    allowed = await stage(task, LATER)
    assert allowed["status"] == "staged"
    assert allowed["confirmation"]["datetime"] == LATER
    assert task._fetch_day.await_count == initial_fetches + 1


@pytest.mark.asyncio
async def test_next_slot_search_preserves_strictly_after_in_resume_context(task):
    await DynamicBookingTask.reject_candidate._func(task)
    result = await search(task, strictly_after=FIRST, time_to="11:59")
    assert result["slots"][0]["datetime"] == LATER
    resume = task._call_session_data.dynamic_booking_resume
    assert resume.search_conditions["strictly_after"] == FIRST
    assert resume.search_conditions["time_to"] == "11:59"


@pytest.mark.asyncio
async def test_date_reselection_requires_new_turn_and_only_reopens_exact_slot(task):
    await DynamicBookingTask.reject_candidate._func(task, scope="date")
    invalid = await DynamicBookingTask.reconsider_candidate._func(task, datetime=FIRST)
    assert invalid["code"] == "reselection_requires_new_turn"
    assert invalid["retry"] == "silent"
    assert task._candidate_is_rejected(FIRST)

    task._chat_ctx.add_message(
        role="user", content="Actually, September 29 at 10 works."
    )
    reconsidered = await DynamicBookingTask.reconsider_candidate._func(
        task, datetime=FIRST
    )
    assert reconsidered == {"status": "reconsidered", "datetime": FIRST}
    result = await search(task)
    assert [slot["datetime"] for slot in result["slots"]] == [FIRST]
    assert task._candidate_is_rejected(LATER)
    assert (await stage(task))["status"] == "staged"
    assert task._call_session_data.reservation_info == {}


@pytest.mark.asyncio
async def test_same_turn_cancel_bool_cannot_complete(task):
    instruction = await cancel(task, cancel_confirmed=True)
    assert task._cancel_question in instruction
    assert task._staged is None
    await cancel(task, cancel_confirmed=True)
    task.complete.assert_not_called()
    result = await DynamicBookingTask.commit_booking._func(task, FIRST)
    assert result["code"] == "cancellation_pending"


@pytest.mark.asyncio
async def test_initial_false_cannot_skip_code_owned_cancellation_question(task):
    await cancel(task, cancel_confirmed=False)
    task.session.say.assert_awaited_once_with(
        "신규 예약 신청 자체를 그만 진행할까요?", allow_interruptions=True
    )
    assert task._cancel_question is not None
    assert task._staged is None
    task.complete.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_completes_only_after_exact_question_and_fresh_agreement(task):
    await cancel(task)
    answer_cancel_question(task)
    assert await cancel(task, cancel_confirmed=True) is None
    task.complete.assert_called_once()
    assert task.complete.call_args.args[0].exit_reason_code == "cancel"


@pytest.mark.asyncio
@pytest.mark.parametrize("operator_id", [None, 100])
async def test_direct_staging_preserves_appointment_without_inventing_time_preference(
    task, operator_id
):
    task._last_search_conditions = None
    result = await DynamicBookingTask.stage_booking._func(
        task,
        SimpleNamespace(disallow_interruptions=Mock()),
        10,
        FIRST,
        operator_id=operator_id,
    )
    assert result["status"] == "staged"
    await cancel(task)
    answer_cancel_question(task)
    await cancel(task, cancel_confirmed=True)
    conditions = task._call_session_data.dynamic_booking_resume.search_conditions
    assert conditions == (
        {"service_id": 10}
        if operator_id is None
        else {"service_id": 10, "operator_id": 100}
    )


@pytest.mark.asyncio
async def test_restored_exclusions_work_without_a_restored_candidate(task):
    await DynamicBookingTask.reject_candidate._func(task, scope="date")
    resume = task._call_session_data.dynamic_booking_resume
    task._last_candidate = None
    task._rejected_candidates = [dict(item) for item in resume.rejected_candidates]
    task._last_search_conditions = dict(resume.search_conditions)
    result = await search(task, end_date="2026-09-30", mode="earliest")
    assert result["slots"][0]["datetime"] == NEXT_DAY
    assert (await stage(task, LATER))["code"] == "candidate_rejected"
    assert (await stage(task, NEXT_DAY))["status"] == "staged"


@pytest.mark.asyncio
async def test_resumed_duplicate_rejection_is_idempotent_and_preserves_appointment(task):
    await DynamicBookingTask.reject_candidate._func(task)
    original_records = list(task._rejected_candidates)
    task._last_candidate = None
    result = await DynamicBookingTask.reject_candidate._func(task)
    assert result["status"] == "already_rejected"
    assert result["datetime"] == FIRST
    assert result["service"] == {"id": 10, "name": "Screen Repair"}
    assert result["search_conditions"]["service_id"] == 10
    assert result["required_next_tool"] == "search_schedules"
    assert task._rejected_candidates == original_records
    assert task._staged is None
    assert task._last_candidate is None
    task.complete.assert_not_called()


@pytest.mark.asyncio
async def test_resumed_slot_rejection_can_upgrade_only_its_grounded_date(task):
    await DynamicBookingTask.reject_candidate._func(task)
    task._last_candidate = None
    result = await DynamicBookingTask.reject_candidate._func(task, scope="date")
    assert result["status"] == "candidate_rejected"
    assert result["scope"] == "date"
    assert result["datetime"] == FIRST
    assert task._candidate_is_rejected(LATER)
    assert not task._candidate_is_rejected(NEXT_DAY)
    assert task._staged is None
    repeated = await DynamicBookingTask.reject_candidate._func(task)
    assert repeated["status"] == "already_rejected"
    assert repeated["scope"] == "date"
    assert len(task._rejected_candidates) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted", [False, True])
async def test_cancel_cannot_use_unrelated_or_interrupted_assistant_turn(
    task, interrupted
):
    await cancel(task)
    if interrupted:
        answer_cancel_question(task, interrupted=True)
    else:
        task._chat_ctx.add_message(
            role="assistant", content="Do you prefer the morning?"
        )
        task._chat_ctx.add_message(role="user", content="Yes")
    assert await cancel(task, cancel_confirmed=True) is not None
    task.complete.assert_not_called()


@pytest.mark.asyncio
async def test_question_then_competing_question_then_yes_cannot_cancel(task):
    await cancel(task)
    answer_cancel_question(task, answer="What is your address?")
    task._chat_ctx.add_message(role="assistant", content="Do you need directions?")
    task._chat_ctx.add_message(role="user", content="Yes")
    await cancel(task, cancel_confirmed=True)
    task.complete.assert_not_called()


@pytest.mark.asyncio
async def test_fresh_user_without_delivered_question_cannot_cancel(task):
    await cancel(task)
    task._chat_ctx.add_message(role="user", content="Yes")
    await cancel(task, cancel_confirmed=True)
    task.complete.assert_not_called()


@pytest.mark.asyncio
async def test_no_to_whole_booking_stop_retains_task_without_candidate_consent(task):
    await cancel(task)
    answer_cancel_question(task, answer="No, I want to keep booking.")
    assert await cancel(task, cancel_confirmed=False) is not None
    assert task._cancel_question is None
    task.complete.assert_not_called()
    assert (await DynamicBookingTask.commit_booking._func(task, FIRST))["code"] == (
        "nothing_staged"
    )


@pytest.mark.asyncio
async def test_correction_after_wrong_cancel_rejects_day_and_continues(task):
    await cancel(task)
    answer_cancel_question(task, answer="I only meant that date does not work.")
    result = await DynamicBookingTask.reject_candidate._func(task, scope="date")
    assert result["datetime"] == FIRST
    assert task._cancel_question is None
    result = await search(task, end_date="2026-09-30", mode="earliest")
    assert result["slots"][0]["datetime"] == NEXT_DAY
    assert (await stage(task, NEXT_DAY))["status"] == "staged"
    task.complete.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["search", "stage"])
async def test_competing_booking_operation_invalidates_cancel_confirmation(
    task, operation
):
    await cancel(task)
    answer_cancel_question(task)
    await (search(task) if operation == "search" else stage(task, LATER))
    assert task._cancel_question is None
    await cancel(task, cancel_confirmed=True)
    task.complete.assert_not_called()


@pytest.mark.asyncio
async def test_new_search_invalidates_staged_candidate(task):
    await search(task, target_time="11:00")
    assert task._staged is None
    assert (await DynamicBookingTask.commit_booking._func(task, FIRST))["code"] == (
        "nothing_staged"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["search", "stage"])
async def test_out_of_horizon_request_cannot_leave_previous_candidate_committable(
    task, operation
):
    if operation == "search":
        result = await DynamicBookingTask.search_schedules._func(
            task,
            start_date="2026-11-07",
            end_date="2026-11-07",
            service_id=10,
        )
    else:
        result = await stage(task, "2026-11-07 10:00")
    assert result["status"] == "error"
    assert task._staged is None
    assert (await DynamicBookingTask.commit_booking._func(task, FIRST))["code"] == (
        "nothing_staged"
    )
    assert task._call_session_data.reservation_info == {}


@pytest.mark.asyncio
async def test_explicit_hangup_bypasses_booking_stop_confirmation(task):
    await DynamicBookingTask.exit_task._func(
        task, reason_code="other", route_target="end_call", detail="Please hang up."
    )
    task.complete.assert_called_once()
    result = task.complete.call_args.args[0]
    assert result.route_target == "end_call"
    assert task._cancel_question is None


@pytest.mark.asyncio
async def test_reject_without_grounded_candidate_cannot_create_exclusion(task):
    task._set_staged(None)
    result = await DynamicBookingTask.reject_candidate._func(task, scope="date")
    assert result["code"] == "nothing_staged"
    assert not task._rejected_candidates


@pytest.mark.asyncio
async def test_slot_refusal_drops_only_declined_exact_time_preference(task):
    task._last_search_conditions.update(target_time="10:00", time_from="09:00")
    result = await DynamicBookingTask.reject_candidate._func(task)
    conditions = result["search_conditions"]
    assert "target_time" not in conditions
    assert conditions["time_from"] == "09:00"
    assert conditions["time_to"] == "11:59"
    assert "OMIT the declined" in result["instruction"]
    result = await search(
        task, strictly_after=FIRST, time_from="09:00", time_to="11:59"
    )
    assert result["slots"][0]["datetime"] == LATER


@pytest.mark.asyncio
async def test_another_time_request_searches_same_date_without_preference_question(
    task,
):
    # "Another time?" after declining 11:00 must surface the earlier 10:00 too;
    # strictly_after would narrow it to "nothing after 11" instead.
    task._set_staged(
        StagedBooking(
            service_id=10,
            service_name="Screen Repair",
            operator_id=100,
            operator_name="Advisor",
            datetime=LATER,
        )
    )
    result = await DynamicBookingTask.reject_candidate._func(task)
    instruction = result["instruction"]
    assert result["required_next_tool"] == "search_schedules"
    assert "without strictly_after" in instruction
    assert "Do not ask a preference question before searching" in instruction
    result = await search(task, time_to="11:59")
    assert [slot["datetime"] for slot in result["slots"]] == [FIRST]


@pytest.mark.asyncio
async def test_date_refusal_preserves_exact_time_for_another_day(task):
    task._last_search_conditions["target_time"] = "10:00"
    result = await DynamicBookingTask.reject_candidate._func(task, scope="date")
    assert result["search_conditions"]["target_time"] == "10:00"


@pytest.mark.asyncio
@pytest.mark.parametrize("sentinel", [None, "None", "null"])
async def test_reconsider_null_sentinel_does_not_change_search(task, sentinel):
    reconsidered = await DynamicBookingTask.reconsider_candidate._func(
        task, datetime=sentinel
    )
    assert reconsidered["code"] == "reselection_requires_new_turn"
    result = await search(task)
    assert result["status"] == "ok"
    assert result["slots"][0]["datetime"] == FIRST


@pytest.mark.asyncio
async def test_cancel_question_is_code_owned_and_stops_llm_rewriting(task):
    with pytest.raises(StopResponse):
        await DynamicBookingTask.exit_task._func(task, reason_code="cancel")
    task.session.say.assert_awaited_once_with(
        "신규 예약 신청 자체를 그만 진행할까요?", allow_interruptions=True
    )
    task.complete.assert_not_called()


@pytest.mark.parametrize("sentinel", [None, "None", "null"])
def test_exit_schema_normalizes_null_sentinels_before_literal_bool_validation(
    task, sentinel
):
    bound = DynamicBookingTask.exit_task._func.__get__(task, DynamicBookingTask)
    tool = function_tool(bound, name="exit_task", description="test")
    args, kwargs = prepare_function_arguments(
        fnc=tool,
        json_arguments={
            "reason_code": "cancel",
            "detail": "Caller explicitly agreed to stop the new booking.",
            "route_target": sentinel,
            "time_condition": sentinel,
            "cancel_confirmed": True,
        },
    )
    values = inspect.signature(bound).bind(*args, **kwargs).arguments
    assert "handoff_accepted" not in values
    assert values["route_target"] is None
    assert values["cancel_confirmed"] is True
    args, kwargs = prepare_function_arguments(
        fnc=tool,
        json_arguments={"reason_code": "cancel", "cancel_confirmed": sentinel},
    )
    assert (
        inspect.signature(bound).bind(*args, **kwargs).arguments["cancel_confirmed"]
        is None
    )


@pytest.mark.parametrize("sentinel", [None, "None", "null"])
def test_stage_schema_normalizes_nullable_operator_without_broadening_ids(
    task, sentinel
):
    bound = DynamicBookingTask.stage_booking._func.__get__(task, DynamicBookingTask)
    tool = function_tool(bound, name="stage_booking", description="test")
    args, kwargs = prepare_function_arguments(
        fnc=tool,
        call_ctx=object.__new__(RunContext),
        json_arguments={
            "service_id": 10,
            "datetime": FIRST,
            "operator_id": sentinel,
        },
    )
    values = inspect.signature(bound).bind(*args, **kwargs).arguments
    assert values["operator_id"] is None


@pytest.mark.parametrize(
    "invalid",
    [
        {"route_target": "invented_route"},
        {"route_target": "info_request"},
        {"cancel_confirmed": "perhaps"},
    ],
)
def test_exit_schema_still_rejects_non_sentinel_invalid_arguments(task, invalid):
    bound = DynamicBookingTask.exit_task._func.__get__(task, DynamicBookingTask)
    tool = function_tool(bound, name="exit_task", description="test")
    with pytest.raises(llm.ToolError):
        prepare_function_arguments(fnc=tool, json_arguments=invalid)


def test_nullable_argument_json_schema_keeps_bool_and_enum_contract(task):
    bound = DynamicBookingTask.exit_task._func.__get__(task, DynamicBookingTask)
    schema = function_arguments_to_pydantic_model(bound).model_json_schema()
    definitions = schema["$defs"]
    assert definitions["NullableToolBool"]["anyOf"] == [
        {"type": "boolean"},
        {"type": "null"},
    ]
    route_types = definitions["NullableBookingRouteTarget"]["anyOf"]
    assert route_types[1] == {"type": "null"}
    assert "booking_modify" in route_types[0]["enum"]
    assert "info_request" not in route_types[0]["enum"]
    assert "end_call" in route_types[0]["enum"]
    assert "transfer" in route_types[0]["enum"]
    assert "staff_connection" in route_types[0]["enum"]
    assert "None" not in route_types[0]["enum"]
