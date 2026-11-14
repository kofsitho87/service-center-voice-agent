"""Caller-evidence grounding and search argument validation regressions."""

import asyncio
import datetime as dt
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from livekit.agents import function_tool, llm
from livekit.agents.llm.utils import prepare_function_arguments

from data import DynamicBookingResumeContext
from tasks.dynamic_booking_task import BookingApiError, DynamicBookingTask

FIXED_NOW = dt.datetime(2026, 9, 11, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))


@pytest.fixture
def task(monkeypatch):
    task = object.__new__(DynamicBookingTask)
    task._select_operator = False
    task._services = [{"id": 10, "name": "Checkup", "keywords": []}]
    task._advisors = [{"id": 100, "name": "Provider", "service_ids": [10]}]
    task._advisors_by_id = {100: task._advisors[0]}
    task._schedule_lookup_days = 30
    task._staged = None
    task._staged_requires_revalidation = False
    task._mutation_lock = asyncio.Lock()
    task._fetch_day = AsyncMock(return_value={})
    task._fetch_available_dates = AsyncMock(return_value=[])
    task._call_session_data = SimpleNamespace(
        room_name="synthetic-caller-evidence",
        dynamic_booking_pending=None,
        reservation_info={},
        # The attempt's single proactive handoff offer is already used, so these
        # tests observe search facts only (offers: test_dynamic_booking_no_slot_handoff).
        dynamic_booking_resume=DynamicBookingResumeContext(
            handoff_offer_delivered=True
        ),
    )
    task._chat_ctx = llm.ChatContext()
    task.complete = Mock()
    monkeypatch.setattr(
        DynamicBookingTask,
        "session",
        SimpleNamespace(history=SimpleNamespace(add_message=Mock())),
    )
    monkeypatch.setattr("tasks.dynamic_booking_task.now_kst", lambda: FIXED_NOW)
    return task


async def _search(task, *, day="2026-09-12", new_turn=True, **kwargs):
    if new_turn:
        task._chat_ctx.add_message(role="user", content="Please check this date")
    return await DynamicBookingTask.search_schedules._func(
        task, start_date=day, end_date=day, service_id=10, **kwargs
    )


async def _record(task, *, kind, quote):
    return await DynamicBookingTask.record_caller_evidence._func(
        task, kind=kind, quote=quote
    )


@pytest.mark.asyncio
async def test_multi_sentence_quote_can_span_recent_user_turns(task):
    task._chat_ctx.add_message(role="user", content="배터리가 부풀었어요. 배터리가 부풀었어요.")
    last_message = task._chat_ctx.add_message(role="user", content="뜨거워요.")
    quote = "배터리가 부풀었어요. 뜨거워요."

    evidence = task._caller_quote_evidence(quote)

    assert evidence == {"quote": quote, "user_message_id": last_message.id}
    result = await _record(task, kind="safety_issue", quote=quote)
    assert result == {
        "status": "recorded",
        "kind": "safety_issue",
        "evidence": {"quote": quote, "user_message_id": last_message.id},
    }


def test_quote_normalization_accepts_spacing_punctuation_and_quotes(task):
    message = task._chat_ctx.add_message(role="user", content="  배터리가   부풀었어요  ")
    quote = '  "배터리가 부풀었어요."  '

    assert task._caller_quote_evidence(quote) == {
        "quote": quote,
        "user_message_id": message.id,
    }


@pytest.mark.asyncio
async def test_assistant_quote_is_rejected_with_silent_retry(task):
    quote = "화면이 심하게 깜빡여요"
    task._chat_ctx.add_message(role="assistant", content=quote)

    assert task._caller_quote_evidence(quote) is None
    result = await _record(task, kind="safety_issue", quote=quote)

    assert result["code"] == "ungrounded_caller_evidence"
    assert result["retry"] == "silent"
    assert task._safety_issue_evidence is None
    search_result = await _search(task)
    assert search_result["status"] == "ok"
    task._fetch_day.assert_awaited_once()


def test_quote_with_one_invented_sentence_is_rejected(task):
    task._chat_ctx.add_message(role="user", content="배터리가 부풀었어요.")

    assert task._caller_quote_evidence("배터리가 부풀었어요. 연기가 나요.") is None


@pytest.mark.asyncio
async def test_search_tool_model_normalizes_nullable_arguments(task):
    bound = DynamicBookingTask.search_schedules._func.__get__(task, DynamicBookingTask)
    tool = function_tool(bound, name="search_schedules", description="test")

    args, kwargs = prepare_function_arguments(
        fnc=tool,
        json_arguments={
            "start_date": "2026-09-12",
            "end_date": "2026-09-12",
            "service_id": "None",
            "operator_id": "null",
            "time_from": "None",
        },
    )
    values = inspect.signature(bound).bind(*args, **kwargs).arguments

    assert values["service_id"] is None
    assert values["operator_id"] is None
    assert values["time_from"] is None

    result = await tool(*args, **kwargs)
    assert result["status"] == "ok"
    task._fetch_day.assert_awaited_once_with(
        "2026-09-12", service_id=None, operator_id=None
    )
    assert task._last_search_conditions["time_from"] is None
    assert task._safety_issue_evidence is None


@pytest.mark.asyncio
async def test_record_evidence_tool_model_accepts_multi_turn_quote(task):
    task._chat_ctx.add_message(role="user", content="배터리가 부풀었어요.")
    last_message = task._chat_ctx.add_message(role="user", content="뜨거워요.")
    quote = "배터리가 부풀었어요. 뜨거워요."
    bound = DynamicBookingTask.record_caller_evidence._func.__get__(
        task, DynamicBookingTask
    )
    tool = function_tool(bound, name="record_caller_evidence", description="test")

    args, kwargs = prepare_function_arguments(
        fnc=tool,
        json_arguments={"kind": "safety_issue", "quote": quote},
    )
    result = await tool(*args, **kwargs)

    assert result == {
        "status": "recorded",
        "kind": "safety_issue",
        "evidence": {"quote": quote, "user_message_id": last_message.id},
    }


@pytest.mark.asyncio
async def test_recording_both_evidence_kinds_preserves_both_and_suppresses_offer(task):
    issue = "화면이 깜빡여요"
    issue_message = task._chat_ctx.add_message(role="user", content=issue)
    refusal = "상담원 연결이나 메모는 원하지 않아요"
    refusal_message = task._chat_ctx.add_message(role="user", content=refusal)

    issue_result = await _record(task, kind="safety_issue", quote=issue)
    refusal_result = await _record(task, kind="handoff_refusal", quote=refusal)

    assert task._safety_issue_evidence == {
        "quote": issue,
        "user_message_id": issue_message.id,
    }
    refusal_evidence = {
        "quote": refusal,
        "user_message_id": refusal_message.id,
    }
    # Refusal is session-owned so a re-entered booking task keeps honoring it.
    resume = task._call_session_data.dynamic_booking_resume
    assert resume.handoff_declined_evidence == refusal_evidence
    assert issue_result["status"] == "recorded"
    assert refusal_result["status"] == "recorded"

    resume.handoff_offer_delivered = False  # the refusal alone must suppress it
    result = await _search(task)
    gate = result["required_next_action"]
    # Issue evidence alone would offer staff help on an empty result, but the
    # recorded refusal suppresses it: no code-spoken offer, no pending question.
    assert gate["handoff_available"] is False
    assert task._offer_pending is None


@pytest.mark.asyncio
async def test_pre_lookup_argument_errors_request_silent_retry(task):
    task._chat_ctx.add_message(role="user", content="Please find the earliest slot")
    earliest = await DynamicBookingTask.search_schedules._func(
        task,
        start_date="2026-09-12",
        end_date="2026-09-12",
        service_id=None,
        mode="earliest_for_booking",
    )
    invalid_time = await _search(task, time_from="25:00")

    assert earliest["code"] == "invalid_arguments"
    assert earliest["retry"] == "silent"
    assert invalid_time["code"] == "invalid_time"
    assert invalid_time["retry"] == "silent"
    task._fetch_day.assert_not_called()


@pytest.mark.asyncio
async def test_schedule_lookup_failure_is_not_marked_for_silent_retry(task):
    task._fetch_day.side_effect = BookingApiError("lookup failed")

    result = await _search(task)

    assert result["code"] == "schedule_lookup_failed"
    assert "retry" not in result
