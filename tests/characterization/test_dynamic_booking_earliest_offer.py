import asyncio
import datetime as dt
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import pytest
from livekit.agents import function_tool, llm
from livekit.agents.llm.utils import prepare_function_arguments

from data import CallConfigData
from tasks.dynamic_booking_task import (
    DynamicBookingTask,
    EarliestOffer,
    StagedBooking,
)

KST = ZoneInfo("Asia/Seoul")


def _task(*, select_operator: bool = False) -> DynamicBookingTask:
    task = object.__new__(DynamicBookingTask)
    task._select_operator = select_operator
    task._services = [
        {"id": 3590, "name": "휴대폰 점검", "keywords": ["휴대폰"]},
    ]
    task._advisors = [
        {
            "id": 805,
            "name": "김테스트",
            "service_ids": [3590],
        }
    ]
    task._advisors_by_id = {805: task._advisors[0]}
    task._schedule_lookup_days = 90
    task._staged = None
    task._staged_requires_revalidation = False
    task._pending_earliest_offer = None
    task._mutation_lock = asyncio.Lock()
    task._call_session_data = SimpleNamespace(
        room_name="synthetic-earliest-offer",
        dynamic_booking_pending=None,
        reservation_info={},
    )
    return task


def _slot(when: str = "2026-08-21 09:30") -> dict:
    return {
        "datetime": when,
        "weekday": "금",
        "advisors": [{"id": 805, "name": "김테스트"}],
    }


def _add_gate(
    task,
    result,
    *,
    service_id,
    operator_id,
    total_slot_count=None,
    earliest_result=False,
    booking_offer=False,
):
    return task._add_confirmation_gate(
        result,
        service_id=service_id,
        operator_id=operator_id,
        total_slot_count=total_slot_count,
        earliest_result=earliest_result,
        **{"offer" + "_earliest": booking_offer},
    )


def test_prompt_routes_date_unspecified_booking_to_earliest_offer():
    task = _task()

    with patch(
        "tasks.dynamic_booking_task.now_kst",
        return_value=dt.datetime(2026, 8, 19, 16, 0, tzinfo=KST),
    ):
        instructions = task._build_instructions()

    assert "do not ask for a date or search only today" in instructions
    assert 'mode="earliest_for_booking"' in instructions
    assert "only wants schedule info" in instructions
    assert 'mode="earliest"' in instructions


def test_earliest_offer_requires_staging_even_when_later_slots_exist():
    task = _task()

    result = _add_gate(
        task,
        {"status": "ok", "slots": [_slot()]},
        service_id=3590,
        operator_id=None,
        total_slot_count=3,
        earliest_result=True,
        booking_offer=True,
    )

    action = result["required_next_action"]
    assert action["confirmation_allowed"] is False
    assert action["required_tool"] == "stage_booking"
    assert action["stage_args"] == {
        "service_id": 3590,
        "datetime": "2026-08-21 09:30",
    }
    assert "deferred_action" not in action
    assert "같은 응답에서 stage_args 그대로 stage_booking" in action["instruction"]
    assert "호출 전 발화·새 동의 질문·턴 종료는 금지" in action["instruction"]
    assert "고객의 새 명시적 동의를 기다리세요" in action["instruction"]
    assert task._pending_earliest_offer == EarliestOffer(
        service_id=3590,
        operator_id=None,
        datetime="2026-08-21 09:30",
    )


def test_pure_earliest_availability_question_waits_but_keeps_provenance():
    task = _task()

    result = _add_gate(
        task,
        {"status": "ok", "slots": [_slot()]},
        service_id=3590,
        operator_id=None,
        total_slot_count=3,
        earliest_result=True,
    )

    action = result["required_next_action"]
    assert "required_tool" not in action
    assert "stage_args 그대로" not in action["instruction"]
    assert action["deferred_action"]["tool"] == "stage_booking"
    assert task._pending_earliest_offer == EarliestOffer(
        service_id=3590,
        operator_id=None,
        datetime="2026-08-21 09:30",
    )


def test_earliest_result_keeps_context_when_offer_flag_is_missing():
    task = _task()

    result = _add_gate(
        task,
        {"status": "ok", "slots": [_slot()]},
        service_id=3590,
        operator_id=None,
        total_slot_count=1,
        earliest_result=True,
    )

    action = result["required_next_action"]
    assert action["required_tool"] == "stage_booking"
    assert task._pending_earliest_offer == EarliestOffer(
        service_id=3590,
        operator_id=None,
        datetime="2026-08-21 09:30",
    )


def test_explicit_date_empty_result_waits_for_flexible_condition():
    task = _task()

    result = _add_gate(
        task,
        {"status": "ok", "slots": []},
        service_id=3590,
        operator_id=None,
    )

    action = result["required_next_action"]
    assert action["wait_for_customer"] is True
    assert action["confirmation_allowed"] is False
    assert action["can_search_later_dates"] is True
    assert "required_tool" not in action
    # A whole day without time filters was checked: no same-day alternative.
    assert "같은 날 다른 시간·시간대를 제안하거나 묻지 말고" in action["instruction"]
    assert "같은 날 다른 시간도 괜찮으세요" not in action["instruction"]


def test_explicit_whole_day_window_is_not_a_time_filter():
    task = _task()

    result = _add_gate(
        task,
        {"status": "ok", "slots": [], "time_from": "00:00", "time_to": "23:59"},
        service_id=3590,
        operator_id=None,
    )

    instruction = result["required_next_action"]["instruction"]
    assert "같은 날 다른 시간·시간대를 제안하거나 묻지 말고" in instruction
    assert "같은 날 다른 시간도 괜찮으세요" not in instruction


def test_time_filtered_empty_result_keeps_same_day_adjustment_options():
    task = _task()

    result = _add_gate(
        task,
        {"status": "ok", "slots": [], "time_from": "18:00", "time_to": None},
        service_id=3590,
        operator_id=None,
    )

    instruction = result["required_next_action"]["instruction"]
    assert "조정 가능한 조건 하나를 질문하세요" in instruction
    assert "같은 날 다른 시간도 괜찮으세요" in instruction
    assert "같은 날 다른 시간·시간대를 제안하거나 묻지 말고" not in instruction


def test_full_horizon_empty_result_does_not_offer_the_same_search_again():
    task = _task()

    result = _add_gate(
        task,
        {"status": "ok", "slots": []},
        service_id=3590,
        operator_id=None,
        booking_offer=True,
    )

    instruction = result["required_next_action"]["instruction"]
    assert "조회 가능한 마지막 날짜까지 확인했습니다" in instruction
    assert "가장 빠른 다른 일정을 찾아드릴지" not in instruction


def test_search_mode_tool_model_rejects_unknown_literal_and_accepts_earliest():
    task = _task()
    bound = DynamicBookingTask.search_schedules._func.__get__(task, DynamicBookingTask)
    tool = function_tool(bound, name="search_schedules", description="test")
    arguments = {
        "start_date": "2026-08-21",
        "end_date": "2026-08-21",
        "mode": "fastest",
    }

    with pytest.raises(llm.ToolError):
        prepare_function_arguments(fnc=tool, json_arguments=arguments)

    arguments["mode"] = "earliest"
    args, kwargs = prepare_function_arguments(fnc=tool, json_arguments=arguments)
    values = inspect.signature(bound).bind(*args, **kwargs).arguments
    assert values["mode"] == "earliest"


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["ko-KR", "en-US"])
@pytest.mark.parametrize("select_operator", [False, True])
async def test_stage_booking_returns_language_adaptive_confirmation_facts(
    language, select_operator
):
    task = _task(select_operator=select_operator)
    task._prompt_lang = "en"
    task._call_session_data.call_config_data = CallConfigData(language=language)
    task._services[0]["name"] = "휴대폰 점검 - 배터리 점검"
    task._pending_earliest_offer = EarliestOffer(
        service_id=3590,
        operator_id=None,
        datetime="2026-08-21 09:30",
    )
    task._fetch_day = AsyncMock(
        return_value={
            "2026-08-21 09:30": [805],
            "2026-08-21 10:00": [805],
        }
    )
    run_context = SimpleNamespace(disallow_interruptions=Mock())
    session = SimpleNamespace(history=SimpleNamespace(add_message=Mock()))

    with (
        patch.object(DynamicBookingTask, "session", session),
        patch(
            "tasks.dynamic_booking_task.now_kst",
            return_value=dt.datetime(2026, 8, 19, 16, 0, tzinfo=KST),
        ),
    ):
        result = await DynamicBookingTask.stage_booking._func(
            task,
            run_context,
            service_id=3590,
            datetime="2026-08-21 09:30",
            operator_id=None,
        )

    run_context.disallow_interruptions.assert_called_once_with()
    assert result["confirmation_context"] == "earliest_offer"
    assert result["required_next_action"] == "ask_booking_confirmation"
    assert "confirmation_question" not in result
    expected = {
        "language": language,
        "service_name": "휴대폰 점검 - 배터리 점검",
        "datetime": "2026-08-21 09:30",
        "date": "2026-08-21",
        "time": "09:30",
        "weekday_iso": 5,
        "is_earliest": True,
    }
    if select_operator:
        expected["operator_name"] = "김테스트"
    assert result["confirmation"] == expected
    assert result["staged"] == {
        "service_id": 3590,
        "service_name": "휴대폰 점검 - 배터리 점검",
        "operator_id": 805,
        "operator_name": "김테스트",
        "datetime": "2026-08-21 09:30",
    }
    assert task._pending_earliest_offer is None


def test_standard_confirmation_defaults_to_korean_without_earliest_claim():
    task = _task()
    staged = StagedBooking(
        service_id=3590,
        service_name="휴대폰 점검",
        operator_id=805,
        operator_name="김테스트",
        datetime="2026-08-21 09:30",
    )

    facts = task._confirmation_facts(staged)
    assert facts["language"] == "ko-KR"
    assert facts["is_earliest"] is False
    assert "operator_name" not in facts


def test_standard_confirmation_uses_full_configured_service_name():
    task = _task()
    staged = StagedBooking(
        service_id=3590,
        service_name="노트북 상담 - 방문 상담",
        operator_id=805,
        operator_name="김테스트",
        datetime="2026-08-29 18:00",
    )

    facts = task._confirmation_facts(staged)
    assert facts["service_name"] == "노트북 상담 - 방문 상담"
    assert facts["weekday_iso"] == 6
    assert facts["time"] == "18:00"


@pytest.mark.parametrize(
    "candidate",
    [
        {"service_id": 9999, "operator_id": 805, "datetime": "2026-08-21 09:30"},
        {"service_id": 3590, "operator_id": 806, "datetime": "2026-08-21 09:30"},
        {"service_id": 3590, "operator_id": 805, "datetime": "2026-08-21 10:00"},
    ],
)
def test_earliest_provenance_cannot_transfer_to_a_different_candidate(candidate):
    task = _task()
    task._pending_earliest_offer = EarliestOffer(
        service_id=3590, operator_id=805, datetime="2026-08-21 09:30"
    )

    assert task._consume_earliest_offer(**candidate) is False
    assert task._pending_earliest_offer is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("has_staged", "requires_revalidation", "confirmed_datetime", "expected_code"),
    [
        (False, False, "2026-08-21 09:30", "nothing_staged"),
        (True, True, "2026-08-21 09:30", "staging_requires_revalidation"),
        (True, False, "2026-08-21 10:00", "staging_mismatch"),
    ],
)
async def test_commit_rejects_unstaged_stale_or_mismatched_candidate(
    has_staged, requires_revalidation, confirmed_datetime, expected_code
):
    task = _task()
    if has_staged:
        task._staged = StagedBooking(
            service_id=3590,
            service_name="휴대폰 점검",
            operator_id=805,
            operator_name="김테스트",
            datetime="2026-08-21 09:30",
        )
    task._staged_requires_revalidation = requires_revalidation

    result = await DynamicBookingTask.commit_booking._func(task, confirmed_datetime)

    assert result["code"] == expected_code
    assert task._call_session_data.reservation_info == {}
