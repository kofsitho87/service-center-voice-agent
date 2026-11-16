"""An empty appointment change or cancel lookup hands off without ending the call."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from livekit.agents import AgentTask

from agents.base_agent import BaseAgent
from data import BookingMemoContext
from tasks.booking.get_my_reservations_task import GetMyReservationsResult
from tasks.leave_message_task import LeaveMessageTask
from tasks.transfer_reason_task import TransferPurpose
from tools import booking_tools
from utils.i18n import t


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["modify", "cancel"])
@pytest.mark.parametrize("language", ["ko-KR", "en-US"])
@pytest.mark.parametrize(
    ("transfer_mode", "transfer_to", "channel"),
    [
        ("warm_transfer", "1004", "transfer"),
        ("warm_transfer_no_briefing", "1004", "transfer"),
        ("cold_transfer", "1004", "transfer"),
        ("leave_memo", "1004", "memo"),
        ("warm_transfer", None, "memo"),
    ],
)
async def test_empty_lookup_uses_existing_configured_staff_route(
    monkeypatch, action, language, transfer_mode, transfer_to, channel
):
    speech = SimpleNamespace(wait_for_playout=AsyncMock())
    session = SimpleNamespace(
        history=SimpleNamespace(add_message=Mock()),
        input=SimpleNamespace(set_audio_enabled=Mock()),
        say=Mock(return_value=speech),
        shutdown=Mock(),
    )
    monkeypatch.setattr(BaseAgent, "session", property(lambda _: session))
    agent = object.__new__(BaseAgent)
    agent._handle_leave_message = AsyncMock()
    agent._dispatch_transfer = AsyncMock()
    session.current_agent = agent
    userdata = SimpleNamespace(
        recipient_data=SimpleNamespace(phone="01000000000", verification_code="123456"),
        call_config_data=SimpleNamespace(
            language=language, transfer_mode=transfer_mode, transfer_to=transfer_to
        ),
        room_name="synthetic-room",
        booking_memo_context=None,
        caller_disconnected=False,
    )
    ctx = SimpleNamespace(
        userdata=userdata, session=session, disallow_interruptions=Mock()
    )
    lookup = Mock(
        return_value=SimpleNamespace(
            run=AsyncMock(return_value=GetMyReservationsResult(status="completed"))
        )
    )
    monkeypatch.setattr(booking_tools, "GetMyReservationsTask", lookup)
    selection = AsyncMock()
    monkeypatch.setattr(booking_tools, "SelectReservationTask", selection)

    tool = getattr(booking_tools, f"{action}_appointment")
    await tool._func.__wrapped__.__wrapped__(ctx)
    assert lookup.call_args.kwargs["action"] == action

    purpose = TransferPurpose(
        code="no_reservations", detail=userdata.booking_memo_context.detail
    )
    assert userdata.booking_memo_context.exit_reason_code == "no_reservations"
    assert userdata.booking_memo_context.time_condition is None
    assert purpose.detail == (
        f"예약 {'변경' if action == 'modify' else '취소'} 요청, 등록된 예약 내역 조회 결과 없음"
        if language == "ko-KR"
        else f"Request to {'change' if action == 'modify' else 'cancel'} appointment; no registered appointments found"
    )
    events = [
        call.kwargs["extra"].get("agent_event", {})
        for call in session.history.add_message.call_args_list
    ]
    assert {
        "name": f"BOOKING_{action.upper()}",
        "status": "FAILED",
        "details": {"reason": "no_reservations"},
    } in events
    if channel == "transfer":
        session.say.assert_called_once_with(
            t(f"booking.{action}.no_reservations_handoff.transfer", language),
            allow_interruptions=False,
        )
        speech.wait_for_playout.assert_awaited_once()
        agent._dispatch_transfer.assert_awaited_once_with(
            call_session_data=userdata,
            transfer_to=transfer_to,
            transfer_mode=transfer_mode,
            direct=False,
            action_key=f"booking_{action}",
            transfer_purpose=purpose,
        )
        agent._handle_leave_message.assert_not_awaited()
    else:
        session.say.assert_not_called()
        agent._handle_leave_message.assert_awaited_once_with(
            userdata,
            is_after_transfer_failure=False,
            entry_reason="memo_only",
            action_key=f"booking_{action}",
            transfer_purpose=purpose,
            booking_context=userdata.booking_memo_context,
        )
        agent._dispatch_transfer.assert_not_awaited()
    session.shutdown.assert_not_called()
    session.input.set_audio_enabled.assert_not_called()
    selection.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["modify", "cancel"])
@pytest.mark.parametrize("already_disconnected", [False, True])
async def test_disconnect_before_or_during_empty_lookup_notice_stops_handoff(
    action, already_disconnected
):
    userdata = SimpleNamespace(
        call_config_data=SimpleNamespace(
            language="ko-KR", transfer_mode="warm_transfer", transfer_to="1004"
        ),
        caller_disconnected=already_disconnected,
    )

    async def disconnect():
        userdata.caller_disconnected = True

    speech = SimpleNamespace(wait_for_playout=AsyncMock(side_effect=disconnect))
    agent = SimpleNamespace(_handle_transfer_call=AsyncMock())
    session = SimpleNamespace(
        history=SimpleNamespace(add_message=Mock()),
        say=Mock(return_value=speech),
        current_agent=agent,
    )
    await booking_tools._handoff_no_reservations(
        SimpleNamespace(session=session, userdata=userdata), action=action
    )
    if already_disconnected:
        session.say.assert_not_called()
        speech.wait_for_playout.assert_not_awaited()
    else:
        session.say.assert_called_once()
        speech.wait_for_playout.assert_awaited_once()
    agent._handle_transfer_call.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action_key", "reason", "expected_collect"),
    [
        ("booking_modify", "no_reservations", False),
        ("booking_modify", "reservation_operator_missing", True),
        ("booking_create", "no_reservations", True),
        ("booking_cancel", "no_reservations", False),
        ("booking_cancel", "reservation_operator_missing", True),
    ],
)
async def test_message_collection_override_only_applies_to_empty_change_or_cancel_lookup(
    monkeypatch, action_key, reason, expected_collect
):
    task_factory = AsyncMock(
        return_value=SimpleNamespace(
            scenario="message_only",
            desired_datetime=None,
            desired_condition=None,
            message="Please check my appointment change.",
        )
    )
    monkeypatch.setattr("agents.base_agent.LeaveMessageTask", task_factory)
    session = SimpleNamespace(
        input=SimpleNamespace(set_audio_enabled=Mock()),
        history=SimpleNamespace(add_message=Mock()),
        current_speech=None,
    )
    monkeypatch.setattr(BaseAgent, "session", property(lambda _: session))
    chat_ctx = SimpleNamespace(copy=Mock())
    chat_ctx.copy.return_value.truncate.return_value = None
    monkeypatch.setattr(BaseAgent, "chat_ctx", property(lambda _: chat_ctx))
    agent = object.__new__(BaseAgent)
    agent.hangup = AsyncMock()
    userdata = SimpleNamespace(
        room_name="synthetic-room",
        recipient_data=SimpleNamespace(phone="01000000000"),
        business_data=SimpleNamespace(is_work_time=True),
        call_config_data=SimpleNamespace(language="ko-KR"),
        leave_message_flow_config=SimpleNamespace(
            should_collect_desired_datetime=Mock(return_value=True),
            additional_instructions="Ask for the caller's name.",
        ),
    )
    context = BookingMemoContext(exit_reason_code=reason)

    await agent._handle_leave_message(
        userdata,
        is_after_transfer_failure=False,
        action_key=action_key,
        booking_context=context,
        entry_reason="memo_only",
    )

    assert (
        task_factory.await_args.kwargs["collect_desired_datetime"] is expected_collect
    )
    assert task_factory.await_args.kwargs["booking_context"] is context
    agent.hangup.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["modify", "cancel"])
@pytest.mark.parametrize("language", ["ko-KR", "en-US"])
async def test_empty_memo_says_exact_opener_and_collects_message(
    monkeypatch, action, language
):
    def fake_init(self, *, instructions, chat_ctx, tools):
        self._test_instructions = instructions
        self._test_tools = tools

    monkeypatch.setattr(AgentTask, "__init__", fake_init)
    session = SimpleNamespace(
        input=SimpleNamespace(set_audio_enabled=Mock()),
        say=Mock(),
        generate_reply=Mock(),
    )
    monkeypatch.setattr(LeaveMessageTask, "session", property(lambda _: session))
    context = BookingMemoContext(
        detail="Change lookup found no appointments", exit_reason_code="no_reservations"
    )
    task = LeaveMessageTask(
        is_after_transfer_failure=False,
        action_key=f"booking_{action}",
        collect_desired_datetime=False,
        language=language,
        booking_context=context,
        entry_reason="memo_only",
        additional_instructions="Ask for the caller's name.",
    )
    task.complete = Mock()
    task._play_closing_message = AsyncMock()

    await task.on_enter()

    session.say.assert_called_once_with(
        t(f"booking.{action}.no_reservations_handoff.memo", language),
        allow_interruptions=False,
    )
    session.generate_reply.assert_not_called()
    assert "Ask for the caller's name." in task._test_instructions
    assert [tool.info.name for tool in task._test_tools] == ["collect_leave_message"]
    collect = task._test_tools[0]._func
    ctx = SimpleNamespace(disallow_interruptions=Mock())
    await collect(ctx, message="Please check why my appointment is missing.")
    task._play_closing_message.assert_awaited_once_with(ctx)
    result = task.complete.call_args.args[0]
    assert result.message == "Please check why my appointment is missing."
    assert result.desired_datetime is None


@pytest.mark.parametrize("action", ["modify", "cancel"])
def test_requested_korean_copy_is_exact(action):
    assert t(f"booking.{action}.no_reservations_handoff.transfer", "ko-KR") == (
        "확인해 보니 등록된 예약 내역이 없네요. "
        "정확한 확인을 위해 상담원에게 연결해 드릴게요."
    )
    assert t(f"booking.{action}.no_reservations_handoff.memo", "ko-KR") == (
        "확인해 보니 등록된 예약 내역이 없네요. "
        "상담원에게 전달해 드릴 테니, 어떤 내용인지 말씀해 주시겠어요? "
        "상담원이 확인한 후 다시 연락드릴게요."
    )


@pytest.mark.parametrize("action", ["modify", "cancel"])
@pytest.mark.parametrize("language", ["ko-KR", "en-US"])
@pytest.mark.parametrize(
    "entry_reason", ["preflight_failure", "final_failure", "caller_exit"]
)
def test_empty_lookup_after_transfer_keeps_existing_failure_opening(
    monkeypatch, action, language, entry_reason
):
    monkeypatch.setattr(AgentTask, "__init__", lambda *args, **kwargs: None)
    task = LeaveMessageTask(
        is_after_transfer_failure=True,
        action_key=f"booking_{action}",
        collect_desired_datetime=False,
        language=language,
        booking_context=BookingMemoContext(exit_reason_code="no_reservations"),
        entry_reason=entry_reason,
    )
    expected = t(f"leave_message.progress_notice.booking_{action}", language)
    if entry_reason == "preflight_failure":
        expected = f"{t('leave_message.transfer_failure_prefix', language)} {expected}"
    assert task.opening_line == expected
    assert task.opening_line != t(
        f"booking.{action}.no_reservations_handoff.memo", language
    )
