"""Pins for the Area B dynamic-booking refactor (behavior that must not move)."""

from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace
from typing import get_args
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pytest
from livekit.agents import llm

import tasks.dynamic_booking_task as dbt
from data import DynamicBookingResumeContext
from tasks.booking import exit_reasons
from tasks.booking.handoff import NoMatchingSlotBlocker, ServiceScopeBlocker
from tasks.dynamic_booking_task import DynamicBookingTask

KST = ZoneInfo("Asia/Seoul")
FIXED_NOW = dt.datetime(2026, 8, 27, 9, 0, tzinfo=KST)

# Names other modules, tools and tests import from tasks.dynamic_booking_task.
PUBLIC_NAMES = (
    "BookingApiError",
    "BookingReference",
    "CANCEL_ANNOUNCEMENT",
    "DynamicBookingResult",
    "DynamicBookingTask",
    "EarliestOffer",
    "KST",
    "NO_PROGRESS_ANNOUNCEMENT",
    "NO_PROGRESS_HARD_TURNS",
    "NO_PROGRESS_SOFT_TURNS",
    "NullableBookingRouteTarget",
    "NullableToolBool",
    "REFERENCE_LOAD_FAILED_ANNOUNCEMENT",
    "SCHEDULE_FETCH_CONCURRENCY",
    "StagedBooking",
    "_has_condition_bearing_user_utterance",
    "_has_prior_dialogue",
    "_prompt_language",
    "_prompt_sections",
    "_spoken_condition_text",
    "build_calendar_block",
    "caller_date_note",
    "get_site_available_schedules",
    "load_booking_reference",
    "now_kst",
    "resolve_caller_dates",
    "spoken_date",
    "spoken_date_range",
    "spoken_datetime",
)


@pytest.mark.parametrize("name", PUBLIC_NAMES)
def test_public_names_stay_importable_from_task_module(name):
    assert hasattr(dbt, name)


def test_filter_time_constraints_stays_a_class_attribute():
    slots = {"2026-08-28 09:00": [1], "2026-08-28 15:00": [1]}
    assert DynamicBookingTask._filter_time_constraints(
        slots, time_from="12:00", time_to=None, target_time=None
    ) == {"2026-08-28 15:00": [1]}


def _task(*, offers: bool = True) -> DynamicBookingTask:
    task = object.__new__(DynamicBookingTask)
    task._chat_ctx = llm.ChatContext()
    task._services = [{"id": 10, "name": "Implant", "keywords": []}]
    task._advisors = [{"id": 100, "name": "Provider", "service_ids": [10]}]
    task._advisors_by_id = {100: task._advisors[0]}
    task._select_operator = False
    task._schedule_lookup_days = 30
    task._staged = None
    task._staged_requires_revalidation = False
    task._mutation_lock = asyncio.Lock()
    task.complete = Mock()
    task._call_session_data = SimpleNamespace(
        room_name="synthetic-pins",
        dynamic_booking_pending=None,
        booking_memo_context=None,
        reservation_info={},
        call_config_data=SimpleNamespace(
            language="ko-KR", transfer_mode="leave_memo", transfer_to=None
        ),
        dynamic_booking_resume=DynamicBookingResumeContext(
            handoff_offer_delivered=not offers
        ),
    )
    return task


def _user(task, message_id: str, text: str) -> None:
    task._chat_ctx.items.append(
        llm.ChatMessage(id=message_id, role="user", content=[text])
    )


def test_latest_user_message_id_counts_empty_text_user_items():
    # Text generate_reply can commit a user item without text; offer freshness,
    # declined-turn counting and the empty-search wait key on that item.
    task = _task()
    _user(task, "u1", "내일 예약할게요")
    task._chat_ctx.items.append(llm.ChatMessage(id="u2", role="user", content=[""]))
    assert task._latest_user_message_id() == "u2"


def test_exit_reason_sets_match_their_literal_types():
    assert (
        frozenset(get_args(exit_reasons.ExitReasonCode))
        == exit_reasons.VALID_EXIT_REASON_CODES
    )
    assert (
        frozenset(get_args(exit_reasons.BookingExitRouteTarget))
        == exit_reasons.VALID_BOOKING_EXIT_ROUTE_TARGETS
    )
    assert (
        frozenset(get_args(dbt.DynamicBookingExitReasonCode))
        == dbt.DYNAMIC_BOOKING_EXIT_REASON_CODES
    )
    assert set(get_args(dbt.DynamicBookingToolExitReasonCode)) == (
        dbt.DYNAMIC_BOOKING_EXIT_REASON_CODES - {"no_progress"}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("sentinel", ["null", "None", "  ", "NULL"])
async def test_exit_task_ignores_null_sentinel_time_condition_and_detail(sentinel):
    """BUGFIX: a "null" echo must not become the spoken memo condition."""
    task = _task()
    _user(task, "u1", "상담원 연결해 주세요")
    task._last_search_conditions = {
        "service_id": 10,
        "operator_id": None,
        "target_time": "11:45",
    }
    with patch.object(
        DynamicBookingTask,
        "session",
        SimpleNamespace(history=SimpleNamespace(add_message=Mock())),
    ):
        await DynamicBookingTask.exit_task._func(
            task,
            reason_code="staff_connection",
            detail=sentinel,
            time_condition=sentinel,
        )
    memo = task._call_session_data.booking_memo_context
    assert memo.time_condition == "오전 11시 45분"
    assert memo.detail == ""
    result = task.complete.call_args.args[0]
    assert result.exit_reason_code == "staff_connection"
    assert result.detail == ""


@pytest.mark.asyncio
async def test_exit_task_keeps_real_time_condition_and_detail():
    task = _task()
    _user(task, "u1", "상담원 연결해 주세요")
    with patch.object(
        DynamicBookingTask,
        "session",
        SimpleNamespace(history=SimpleNamespace(add_message=Mock())),
    ):
        await DynamicBookingTask.exit_task._func(
            task,
            reason_code="staff_connection",
            detail=" 직접 상담 원함 ",
            time_condition="2026-08-28 14:30 이후",
        )
    memo = task._call_session_data.booking_memo_context
    assert memo.time_condition == "8월 28일 금요일 오후 2시 30분 이후"
    assert memo.detail == "직접 상담 원함"


@pytest.mark.asyncio
@pytest.mark.parametrize("quote", ["null", "고객이 하지 않은 말"])
async def test_record_caller_evidence_rejects_missing_or_ungrounded_quote(quote):
    task = _task()
    _user(task, "u1", "화면이 너무 깜빡여요")
    result = await DynamicBookingTask.record_caller_evidence._func(
        task, kind="safety_issue", quote=quote
    )
    assert result == {
        "status": "error",
        "code": "ungrounded_caller_evidence",
        "message": "Copy evidence verbatim from a recent USER message, never from "
        "assistant/tool text or an invented summary. Omit unsupported quotes "
        "and retry the same search. This invalid evidence does not change "
        "handoff state.",
        "retry": "silent",
    }
    assert task._safety_issue_evidence is None


@pytest.mark.parametrize(
    ("blocker", "reason_code", "issue"),
    [
        (None, "staff_connection", {"quote": "화면이 깜빡여요", "user_message_id": "u1"}),
        (None, "staff_connection", None),
        (None, "no_slot_memo", None),
        (
            ServiceScopeBlocker(subject="service", requested_label="틀니"),
            "service_not_offered",
            None,
        ),
        (
            ServiceScopeBlocker(subject="advisor", requested_label=None),
            "service_not_offered",
            None,
        ),
        (
            NoMatchingSlotBlocker(
                searched_range=("2026-08-28", "2026-08-30"),
                filters={"service_id": 10},
                alternative_slot_count=0,
                range_complete=True,
                horizon_reached=False,
            ),
            "no_slot_memo",
            None,
        ),
        (
            NoMatchingSlotBlocker(
                searched_range=("2026-08-28", "2026-08-28"),
                filters={},
                alternative_slot_count=1,
                range_complete=True,
                horizon_reached=False,
            ),
            "no_slot_memo",
            None,
        ),
    ],
)
def test_handoff_offer_basis_texts(blocker, reason_code, issue):
    task = _task()
    _user(task, "u1", "화면이 깜빡여요")
    task._safety_issue_evidence = issue
    session = SimpleNamespace(history=SimpleNamespace(add_message=Mock()))
    with (
        patch.object(DynamicBookingTask, "session", session),
        patch("uuid.uuid4", return_value=SimpleNamespace(hex="abcdef0123456789")),
    ):
        offer = task._open_offer(blocker, reason_code)
    pending = task._offer_pending
    event = session.history.add_message.call_args.kwargs["extra"]
    details = event["agent_event"]["details"]
    expected_kind = (
        blocker.kind
        if blocker
        else "issue"
        if reason_code == "staff_connection"
        else "declined_alternatives"
    )
    expected_reason = (
        "caller_condition_not_bookable"
        if blocker is not None
        else "safety_issue"
        if reason_code == "staff_connection"
        else "alternatives_declined"
    )
    assert details["blocker_kind"] == expected_kind
    assert details["offer_id"] == "abcdef012345"
    assert offer["reason"] == expected_reason
    assert offer["channel"] == "memo"
    detail = task._handoff_detail(pending)
    if isinstance(blocker, ServiceScopeBlocker):
        # Staff-memo kind labels use 서비스/상담사.
        kind = "서비스" if blocker.subject == "service" else "상담사"
        assert (
            detail
            == f"전화 예약 목록 밖 {kind} 요청: {blocker.requested_label or '미상'}"
        )
    elif isinstance(blocker, NoMatchingSlotBlocker):
        start, end = blocker.searched_range
        span = start if start == end else f"{start}~{end}"
        assert detail == f"조회 범위 {span}에 요청 조건과 일치하는 예약 일정 없음"
    elif reason_code == "no_slot_memo":
        assert detail == "제안한 대안 일정을 여러 번 거절함"
    elif issue:
        # Evidence label: 기기 문제 호소.
        assert detail == "기기 문제 호소: 화면이 깜빡여요"
    else:
        assert detail == "상담원 도움 동의"


def test_horizon_tracks_now_kst_patch():
    task = _task()
    with patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW):
        assert task._validate_range("2026-09-26", "2026-09-26") is None
        error = task._validate_range("2026-09-27", "2026-09-27")
    assert error["allowed_range"] == "2026-08-27 ~ 2026-09-26"
    assert task._date_horizon(dt.date(2026, 8, 27)) == dt.date(2026, 9, 26)


def _full_page_task():
    task = _task(offers=False)
    task._day_rows_cache = {}
    task._date_rows_cache = {}
    task._service_reservation_setting = "ALLOW_ONLY_NEW"
    task._pending_earliest_offer = None
    task._call_session_data.business_data = SimpleNamespace(site_id="s", client_id=1)
    return task


async def _full_page(site_id, start, end, **kwargs):
    rows = [
        SimpleNamespace(time=f"{start} 09:{minute:02d}:00", operator_ids=[100])
        for minute in range(50)
    ] * 2
    return SimpleNamespace(
        success=True,
        data=SimpleNamespace(schedules=rows),
        message=None,
        elapsed_ms=None,
        request_params=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("end_date", "stage", "where", "message"),
    [
        (
            "2026-08-28",
            "slot_page_tripwire",
            {"date": "2026-08-28"},
            "slot page assumed complete but hit size cap",
        ),
        (
            "2026-08-30",
            "date_page_tripwire",
            {"start": "2026-08-28", "end": "2026-08-30"},
            "date page assumed complete but hit size cap",
        ),
    ],
)
async def test_full_schedule_page_trips_and_fails_the_search(
    end_date, stage, where, message
):
    from loguru import logger

    task = _full_page_task()
    records = []
    sink = logger.add(lambda m: records.append(m.record), level="DEBUG")
    try:
        with (
            patch.object(
                DynamicBookingTask,
                "session",
                SimpleNamespace(history=SimpleNamespace(add_message=Mock())),
            ),
            patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW),
            patch(
                "tasks.dynamic_booking_task.get_site_available_schedules",
                side_effect=_full_page,
            ),
        ):
            result = await DynamicBookingTask.search_schedules._func(
                task, start_date="2026-08-28", end_date=end_date, service_id=10
            )
    finally:
        logger.remove(sink)
    kind = stage.split("_")[0]
    assert result == {
        "status": "error",
        "code": "schedule_lookup_failed",
        "message": f"Incomplete schedule response: {kind} page size cap",
    }
    tripwire = [r for r in records if r["extra"].get("stage") == stage]
    assert len(tripwire) == 1
    assert tripwire[0]["message"] == message
    assert tripwire[0]["level"].name == "ERROR"
    assert tripwire[0]["extra"] == {
        "event": "DYNAMIC_BOOKING_TASK",
        "stage": stage,
        **where,
        "rows": 100,
        "room_name": "synthetic-pins",
    }
    assert list(tripwire[0]["extra"]) == ["event", "stage", *where, "rows", "room_name"]
