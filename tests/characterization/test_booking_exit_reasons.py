from unittest.mock import Mock

import pytest

from tasks.booking.exit_reasons import (
    exit_event_details,
    exit_reason_text,
    normalize_booking_exit_route_target,
    normalize_exit_reason_code,
    route_target_for_exit,
)
from tasks.booking.select_reservation_task import SelectReservationTask
from tasks.schedule_selection_base import BaseScheduleSelectionTask


def test_service_not_offered_is_a_supported_exit_reason():
    assert normalize_exit_reason_code("service_not_offered") == "service_not_offered"
    assert exit_reason_text("service_not_offered", language="ko-KR") == (
        "AI 예약 범위에 없는 서비스 요청"
    )


def test_exit_event_details_preserves_text_and_normalizes_reason_code():
    assert exit_event_details("staff_connection", "상담원 연결 요청 (직원 연결)") == {
        "reason": "상담원 연결 요청 (직원 연결)",
        "reason_code": "staff_connection",
    }
    assert exit_event_details("unknown", "Unknown reason") == {
        "reason": "Unknown reason",
        "reason_code": "other",
    }


def test_no_slot_memo_is_a_distinct_supported_exit_reason():
    assert normalize_exit_reason_code("no_slot_memo") == "no_slot_memo"
    assert normalize_exit_reason_code("leave_memo") == "other"
    assert exit_reason_text("no_slot_memo", language="ko-KR") == (
        "예약 가능한 일정이 없어 상담원 인계(연결 또는 메모)에 동의"
    )
    assert exit_reason_text("no_slot_memo", language="en-US") == (
        "agreed to a staff handoff (transfer or memo) because no booking slot matched"
    )


def test_route_target_is_separate_from_analytics_reason_code():
    assert route_target_for_exit("unrelated", "booking_modify") == "booking_modify"
    assert route_target_for_exit("other", "end_call") == "end_call"
    assert route_target_for_exit("cancel", "booking_cancel") is None
    assert route_target_for_exit("staff_connection", "transfer") is None
    assert exit_event_details(
        "unrelated",
        "기존 예약 변경 요청",
        "booking_modify",
    ) == {
        "reason": "기존 예약 변경 요청",
        "reason_code": "unrelated",
        "route_target": "booking_modify",
    }


def test_unknown_reason_discards_privileged_route_target():
    assert normalize_exit_reason_code("forged") == "other"
    assert route_target_for_exit("forged", "end_call") == "other"
    assert route_target_for_exit("forged", "booking_modify") == "other"


def test_unknown_route_target_uses_safe_other_fallback():
    assert normalize_booking_exit_route_target("forged") == "other"
    assert normalize_booking_exit_route_target(None) == "other"


@pytest.mark.asyncio
async def test_select_task_discards_privileged_target_from_forged_reason():
    task = object.__new__(SelectReservationTask)
    task._language_code = "ko-KR"
    task.complete = Mock()

    await SelectReservationTask.exit_task._func(
        task,
        reason_code="forged",
        route_target="booking_modify",
    )

    result = task.complete.call_args.args[0]
    assert result.exit_reason_code == "other"
    assert result.route_target == "other"


@pytest.mark.asyncio
async def test_schedule_task_discards_privileged_target_from_forged_reason():
    task = object.__new__(BaseScheduleSelectionTask)
    task._language_code = "ko-KR"
    task.complete = Mock()

    await BaseScheduleSelectionTask.exit_task._func(
        task,
        None,
        reason_code="forged",
        route_target="end_call",
    )

    result = task.complete.call_args.args[0]
    assert result.exit_reason_code == "other"
    assert result.route_target == "other"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route_target", "expected"),
    [
        ("staff_connection", "other"),
        ("booking_modify", "booking_modify"),
    ],
)
async def test_select_task_normalizes_route_target_input(route_target, expected):
    task = object.__new__(SelectReservationTask)
    task._language_code = "ko-KR"
    task.complete = Mock()

    await SelectReservationTask.exit_task._func(
        task,
        reason_code="unrelated",
        route_target=route_target,
    )

    result = task.complete.call_args.args[0]
    assert result.exit_reason_code == "unrelated"
    assert result.route_target == expected
