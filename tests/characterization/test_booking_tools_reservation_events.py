from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tasks.transfer_reason_task import TransferPurpose
from tools.booking_tools import (
    _escalate_to_human,
    _handoff_operator_missing,
    _handoff_operator_schedule_lookup_failed,
    _record_reservation_failure,
)
from tools.dynamic_booking_tools import _handoff_identity_failure


def test_record_reservation_failure_adds_failed_developer_event():
    calls = []
    ctx = SimpleNamespace(
        session=SimpleNamespace(
            history=SimpleNamespace(
                add_message=lambda role, content, extra: calls.append(
                    (role, content, extra)
                )
            )
        )
    )

    _record_reservation_failure(ctx, "BOOKING_CANCEL", "no_reservations")

    assert calls == [
        (
            "developer",
            "",
            {
                "agent_event": {
                    "name": "BOOKING_CANCEL",
                    "status": "FAILED",
                    "details": {"reason": "no_reservations"},
                }
            },
        )
    ]


@pytest.mark.asyncio
async def test_identity_failure_handoff_keeps_known_purpose():
    speech = SimpleNamespace(wait_for_playout=AsyncMock())
    agent = SimpleNamespace(_handle_transfer_call=AsyncMock())
    session = SimpleNamespace(current_agent=agent, say=Mock(return_value=speech))
    userdata = SimpleNamespace(
        call_config_data=SimpleNamespace(language="ko-KR"),
        caller_disconnected=False,
    )

    await _escalate_to_human(
        SimpleNamespace(session=session, userdata=userdata), "verify_failed"
    )

    speech.wait_for_playout.assert_awaited_once()
    agent._handle_transfer_call.assert_awaited_once_with(
        userdata,
        transfer_purpose=TransferPurpose(code="identity_verification_failed"),
    )


@pytest.mark.asyncio
async def test_schedule_lookup_failure_skips_repeat_question():
    speech = SimpleNamespace(wait_for_playout=AsyncMock())
    agent = SimpleNamespace(_handle_transfer_call=AsyncMock())
    session = SimpleNamespace(
        current_agent=agent,
        say=Mock(return_value=speech),
        history=SimpleNamespace(add_message=Mock()),
    )
    userdata = SimpleNamespace(
        call_config_data=SimpleNamespace(
            language="ko-KR", transfer_mode="warm_transfer", transfer_to="1004"
        ),
        room_name="test-room",
    )

    await _handoff_operator_schedule_lookup_failed(
        SimpleNamespace(session=session, userdata=userdata),
        selected_reservation={
            "service_name": "내과",
            "operator_name": "홍길동",
            "appointment_date": "2026-09-30 09:00:00",
        },
    )

    purpose = agent._handle_transfer_call.await_args.kwargs["transfer_purpose"]
    assert purpose.code == "operator_schedule_lookup_failed"
    assert "상담사 일정 조회 실패" in purpose.detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action_key", "event_name", "transfer_mode", "channel"),
    [
        ("booking_modify", "BOOKING_MODIFY", "leave_memo", "memo"),
        ("booking_cancel", "BOOKING_CANCEL", "cold", "transfer"),
    ],
)
async def test_operator_missing_handoff_preserves_event_and_routes_to_staff(
    action_key, event_name, transfer_mode, channel
):
    events = []
    speech_handle = SimpleNamespace(wait_for_playout=AsyncMock())
    current_agent = SimpleNamespace(_handle_transfer_call=AsyncMock())
    session = SimpleNamespace(
        history=SimpleNamespace(
            add_message=lambda role, content, extra: events.append(extra)
        ),
        say=Mock(return_value=speech_handle),
        current_agent=current_agent,
        shutdown=Mock(),
    )
    userdata = SimpleNamespace(
        call_config_data=SimpleNamespace(
            language="ko-KR", transfer_mode=transfer_mode, transfer_to="1004"
        )
    )
    ctx = SimpleNamespace(session=session, userdata=userdata)

    await _handoff_operator_missing(
        ctx,
        action_key=action_key,
        event_name=event_name,
        selected_reservation={
            "service_name": "내과",
            "appointment_date": "2026-09-14 09:20:00",
        },
    )

    assert events[0]["agent_event"] == {
        "name": event_name,
        "status": "FAILED",
        "details": {"reason": "reservation_operator_missing"},
    }
    assert (
        userdata.booking_memo_context.exit_reason_code == "reservation_operator_missing"
    )
    assert userdata.booking_memo_context.time_condition is None
    assert "9월 14일 오전 9시 20분" in userdata.booking_memo_context.detail
    assert (
        "메모를 남겨드리겠습니다" in session.say.call_args.args[0]
        if channel == "memo"
        else "상담원에게 연결해 드리겠습니다" in session.say.call_args.args[0]
    )
    assert speech_handle.wait_for_playout.await_count == 1
    current_agent._handle_transfer_call.assert_awaited_once_with(
        userdata,
        action_key=action_key,
        transfer_purpose=TransferPurpose(
            code="reservation_operator_missing",
            detail=userdata.booking_memo_context.detail,
        ),
    )
    session.shutdown.assert_not_called()


def _identity_failure_ctx(*, disconnected_before, disconnect_on_say=False):
    userdata = SimpleNamespace(
        call_config_data=SimpleNamespace(language="ko-KR"),
        caller_disconnected=disconnected_before,
        room_name="test-room",
    )

    def say(*args, **kwargs):
        if disconnect_on_say:
            userdata.caller_disconnected = True
        return SimpleNamespace(wait_for_playout=AsyncMock())

    agent = SimpleNamespace(_handle_transfer_call=AsyncMock())
    session = SimpleNamespace(current_agent=agent, say=Mock(side_effect=say))
    return SimpleNamespace(session=session, userdata=userdata), session, agent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handoff",
    [_escalate_to_human, _handoff_identity_failure],
    ids=["legacy", "dynamic"],
)
async def test_identity_failure_after_hangup_stays_silent(handoff):
    ctx, session, agent = _identity_failure_ctx(disconnected_before=True)

    await handoff(ctx, "verification_code_not_provided")

    session.say.assert_not_called()
    agent._handle_transfer_call.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handoff",
    [_escalate_to_human, _handoff_identity_failure],
    ids=["legacy", "dynamic"],
)
async def test_identity_failure_hangup_during_line_skips_transfer(handoff):
    ctx, session, agent = _identity_failure_ctx(
        disconnected_before=False, disconnect_on_say=True
    )

    await handoff(ctx, "verify_failed")

    session.say.assert_called_once()
    agent._handle_transfer_call.assert_not_awaited()
