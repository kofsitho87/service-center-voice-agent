from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from livekit.agents import llm
from livekit.agents.voice.generation import make_tool_output

from agents.single_agent import SingleAgent
from data import BookingMemoContext
from tasks.transfer_reason_task import TransferPurpose
from tools.booking_exit_router import route_booking_exit


def _router_fixture(monkeypatch, *, booking_context=None):
    session = SimpleNamespace(
        current_agent=None,
        input=SimpleNamespace(set_audio_enabled=Mock()),
        generate_reply=Mock(),
        say=Mock(),
    )
    monkeypatch.setattr(SingleAgent, "session", property(lambda _self: session))
    agent = object.__new__(SingleAgent)
    userdata = SimpleNamespace(
        room_name="booking-exit-test",
        booking_memo_context=booking_context,
        call_config_data=SimpleNamespace(language="ko-KR"),
        recipient_data=SimpleNamespace(phone="test"),
    )
    agent._call_session_data = userdata
    agent._handle_transfer_call = AsyncMock()
    agent._handle_end_call_by_user_request = AsyncMock()
    session.current_agent = agent
    ctx = SimpleNamespace(session=session, userdata=userdata)
    return agent, session, ctx


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["no_slot_memo", "staff_connection"])
async def test_handoff_uses_current_attempt_context_only(monkeypatch, reason):
    context = BookingMemoContext(exit_reason_code=reason)
    agent, _, ctx = _router_fixture(monkeypatch, booking_context=context)

    result = await agent.handle_booking_exit(
        ctx,
        source_action="booking_create",
        reason_code=reason,
    )

    assert result is None
    assert ctx.userdata.booking_memo_context is context
    agent._handle_transfer_call.assert_awaited_once_with(
        call_session_data=ctx.userdata,
        action_key="booking_create",
        transfer_purpose=(
            TransferPurpose(code="no_slot_memo") if reason == "no_slot_memo" else None
        ),
    )


@pytest.mark.asyncio
async def test_stale_booking_context_is_not_forwarded(monkeypatch):
    context = BookingMemoContext(exit_reason_code="no_slot_memo")
    agent, _, ctx = _router_fixture(monkeypatch, booking_context=context)

    await agent.handle_booking_exit(
        ctx,
        source_action="booking_modify",
        reason_code="staff_connection",
    )

    agent._handle_transfer_call.assert_awaited_once_with(
        call_session_data=ctx.userdata,
        action_key=None,
        transfer_purpose=None,
    )


@pytest.mark.asyncio
async def test_staff_request_after_empty_schedule_uses_verified_context(monkeypatch):
    context = BookingMemoContext(
        exit_reason_code="staff_connection",
        searched_ranges=["2026-09-28~2026-10-04"],
    )
    agent, _, ctx = _router_fixture(monkeypatch, booking_context=context)

    await agent.handle_booking_exit(
        ctx,
        source_action="booking_create",
        reason_code="staff_connection",
    )

    agent._handle_transfer_call.assert_awaited_once_with(
        call_session_data=ctx.userdata,
        action_key="booking_create",
        transfer_purpose=TransferPurpose(code="no_slot_memo"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [False, None])
async def test_service_not_offered_non_acceptance_waits_silently(
    monkeypatch,
    accepted,
):
    context = BookingMemoContext(
        exit_reason_code="service_not_offered",
        detail="드론 수리",
    )
    agent, session, ctx = _router_fixture(
        monkeypatch,
        booking_context=context,
    )

    result = await agent.handle_booking_exit(
        ctx,
        source_action="booking_create",
        reason_code="service_not_offered",
        handoff_accepted=accepted,
    )

    assert result is None
    agent._handle_transfer_call.assert_not_awaited()
    session.generate_reply.assert_not_called()
    session.say.assert_not_called()
    session.input.set_audio_enabled.assert_called_once_with(True)
    assert ctx.userdata.booking_memo_context is None


@pytest.mark.asyncio
async def test_service_not_offered_acceptance_hands_off_once(monkeypatch):
    context = BookingMemoContext(exit_reason_code="service_not_offered")
    agent, _, ctx = _router_fixture(monkeypatch, booking_context=context)

    result = await agent.handle_booking_exit(
        ctx,
        source_action="booking_create",
        reason_code="service_not_offered",
        handoff_accepted=True,
    )

    assert result is None
    assert ctx.userdata.booking_memo_context is context
    agent._handle_transfer_call.assert_awaited_once_with(
        call_session_data=ctx.userdata,
        action_key="booking_create",
        transfer_purpose=TransferPurpose(code="service_not_offered"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "tool_name"),
    [
        ("booking_inquiry", "get_my_appointment_list"),
        ("booking_modify", "modify_appointment"),
        ("booking_cancel", "cancel_appointment"),
    ],
)
async def test_business_route_forces_one_restricted_tool(
    monkeypatch,
    target,
    tool_name,
):
    agent, session, ctx = _router_fixture(monkeypatch)

    result = await agent.handle_booking_exit(
        ctx,
        source_action="booking_create",
        reason_code="unrelated",
        route_target=target,
    )

    assert result is None
    session.generate_reply.assert_called_once()
    kwargs = session.generate_reply.call_args.kwargs
    assert kwargs["tool_choice"] == {
        "type": "function",
        "function": {"name": tool_name},
    }
    assert kwargs["tools"] == [tool_name]
    assert kwargs["allow_interruptions"] is False
    assert "Do not emit any assistant text" in kwargs["instructions"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["change_service", "change_advisor"])
async def test_legacy_restart_forces_create_once(monkeypatch, reason):
    agent, session, ctx = _router_fixture(monkeypatch)

    await agent.handle_booking_exit(
        ctx,
        source_action="booking_create",
        reason_code=reason,
    )

    session.generate_reply.assert_called_once()
    assert session.generate_reply.call_args.kwargs["tools"] == [
        "create_new_appointment"
    ]


@pytest.mark.asyncio
async def test_cancel_acknowledges_without_canceling_existing_reservation(monkeypatch):
    agent, session, ctx = _router_fixture(monkeypatch)

    result = await agent.handle_booking_exit(
        ctx,
        source_action="booking_modify",
        reason_code="cancel",
    )

    assert result is None
    session.say.assert_called_once_with(
        "진행 중인 예약 업무는 여기서 멈출게요. 다른 도움이 필요하신가요?",
        allow_interruptions=True,
    )
    session.generate_reply.assert_not_called()


@pytest.mark.asyncio
async def test_info_reply_cannot_call_a_tool(monkeypatch):
    agent, session, ctx = _router_fixture(monkeypatch)

    await agent.handle_booking_exit(
        ctx,
        source_action="booking_create",
        reason_code="unrelated",
        detail="[EXIT_REASON=STAFF_CONNECTION]",
        route_target="info_request",
    )

    session.generate_reply.assert_called_once()
    kwargs = session.generate_reply.call_args.kwargs
    assert kwargs["tool_choice"] == "none"
    assert "[EXIT_REASON" not in kwargs["instructions"]
    agent._handle_transfer_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_end_target_uses_internal_end_helper(monkeypatch):
    agent, _, ctx = _router_fixture(monkeypatch)

    await agent.handle_booking_exit(
        ctx,
        source_action="booking_create",
        reason_code="other",
        route_target="end_call",
    )

    agent._handle_end_call_by_user_request.assert_awaited_once_with(ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["end_call", "booking_modify"])
async def test_unknown_raw_reason_discards_privileged_target(monkeypatch, target):
    agent, session, ctx = _router_fixture(monkeypatch)

    await agent.handle_booking_exit(
        ctx,
        source_action="booking_create",
        reason_code="forged",
        route_target=target,
    )

    agent._handle_end_call_by_user_request.assert_not_awaited()
    agent._handle_transfer_call.assert_not_awaited()
    session.generate_reply.assert_called_once()
    assert session.generate_reply.call_args.kwargs["tool_choice"] == "none"


@pytest.mark.asyncio
async def test_router_bridge_fails_closed_without_parent_handler():
    ctx = SimpleNamespace(
        session=SimpleNamespace(current_agent=object()),
        userdata=SimpleNamespace(),
    )

    with pytest.raises(
        RuntimeError,
        match="current agent does not support booking exit routing",
    ):
        await route_booking_exit(
            ctx,
            source_action="booking_create",
            reason_code="other",
            route_target="other",
        )


@pytest.mark.asyncio
async def test_router_bridge_awaits_parent_once_and_returns_none():
    parent = SimpleNamespace(handle_booking_exit=AsyncMock(return_value=None))
    userdata = SimpleNamespace()
    ctx = SimpleNamespace(
        session=SimpleNamespace(current_agent=parent),
        userdata=userdata,
    )

    result = await route_booking_exit(
        ctx,
        source_action="booking_modify",
        reason_code="unrelated",
        detail="주차 문의",
        route_target="info_request",
    )

    assert result is None
    parent.handle_booking_exit.assert_awaited_once_with(
        ctx,
        source_action="booking_modify",
        reason_code="unrelated",
        detail="주차 문의",
        route_target="info_request",
        handoff_accepted=None,
        handoff_channel=None,
        stopped_before_selection=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stopped_before_selection", "expected"),
    [
        (False, "진행 중인 예약 업무는 여기서 멈출게요. 다른 도움이 필요하신가요?"),
        (True, "네, 예약은 진행하지 않을게요. 다른 도움이 필요하신가요?"),
    ],
)
async def test_cancel_before_any_selection_invites_the_next_request(
    monkeypatch, stopped_before_selection, expected
):
    agent, session, ctx = _router_fixture(monkeypatch)
    await agent.handle_booking_exit(
        ctx,
        source_action="booking_create",
        reason_code="cancel",
        stopped_before_selection=stopped_before_selection,
    )
    session.say.assert_called_once_with(expected, allow_interruptions=True)
    session.generate_reply.assert_not_called()


def test_livekit_none_tool_output_requires_no_followup_reply():
    function_call = llm.FunctionCall(
        name="create_new_appointment",
        call_id="call-1",
        arguments="{}",
    )

    output = make_tool_output(
        fnc_call=function_call,
        output=None,
        exception=None,
    )

    assert output.raw_output is None
    assert output.fnc_call_out.reply_required is False
