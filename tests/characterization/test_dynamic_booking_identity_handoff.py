from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tasks.transfer_reason_task import TransferPurpose
from tasks.verify_customer_identity_task import VerifyCustomerResult
from tools import booking_common, dynamic_booking_tools
from utils.i18n import t


def _identity_failure_fixture(monkeypatch, *, caller_disconnected=False):
    order: list[str] = []
    events: list[dict] = []

    async def wait_for_playout():
        order.append("playout")

    async def handle_transfer(call_session_data, *, action_key, transfer_purpose):
        order.append("transfer")
        assert call_session_data is userdata
        assert action_key == "booking_create"
        assert transfer_purpose == TransferPurpose(code="identity_verification_failed")

    speech_handle = SimpleNamespace(wait_for_playout=wait_for_playout)
    current_agent = SimpleNamespace(
        _handle_transfer_call=AsyncMock(side_effect=handle_transfer)
    )
    session = SimpleNamespace(
        current_agent=current_agent,
        generate_reply=Mock(),
        history=SimpleNamespace(
            add_message=lambda role, content, extra: events.append(extra)
        ),
        say=Mock(
            side_effect=lambda *args, **kwargs: order.append("say") or speech_handle
        ),
    )
    userdata = SimpleNamespace(
        booking_memo_context=object(),
        business_data=SimpleNamespace(site_id="site-test"),
        call_config_data=SimpleNamespace(language="ko-KR"),
        caller_disconnected=caller_disconnected,
        recipient_data=SimpleNamespace(
            phone="01000000000",
            verification_code=None,
            user_name=None,
            customer_no=None,
        ),
        room_name="dynamic-identity-failure",
        workflow_config=SimpleNamespace(
            booking=SimpleNamespace(
                booking_create=SimpleNamespace(verify_single_customer_identity=True)
            )
        ),
    )
    ctx = SimpleNamespace(
        disallow_interruptions=Mock(),
        session=session,
        userdata=userdata,
    )
    verify_task = SimpleNamespace(
        run=AsyncMock(
            return_value=VerifyCustomerResult(
                status="escalated",
                escalation_reason="verify_failed",
            )
        )
    )
    monkeypatch.setattr(
        booking_common,
        "VerifyCustomerIdentityTask",
        Mock(return_value=verify_task),
    )
    return ctx, current_agent, session, order, events


async def _run_dynamic_create_body(ctx):
    # Skip FunctionTool, action-mode, and Langfuse wrappers to exercise the
    # dynamic implementation directly with a focused synthetic context.
    tool_body = dynamic_booking_tools.create_new_appointment._func
    while hasattr(tool_body, "__wrapped__"):
        tool_body = tool_body.__wrapped__
    return await tool_body(ctx)


@pytest.mark.asyncio
async def test_identity_failure_announces_then_transfers_without_llm(monkeypatch):
    ctx, current_agent, session, order, events = _identity_failure_fixture(monkeypatch)

    result = await _run_dynamic_create_body(ctx)

    assert result is None
    assert order == ["say", "playout", "transfer"]
    session.say.assert_called_once_with(
        t("verify_customer.escalation.verify_failed", "ko-KR"),
        allow_interruptions=False,
    )
    session.generate_reply.assert_not_called()
    current_agent._handle_transfer_call.assert_awaited_once_with(
        ctx.userdata,
        action_key="booking_create",
        transfer_purpose=TransferPurpose(code="identity_verification_failed"),
    )
    assert [event["agent_event"] for event in events] == [
        {"name": "BOOKING_CREATE", "status": "STARTED", "details": {}},
        {
            "name": "IDENTITY_VERIFY",
            "status": "FAILED",
            "details": {"action": "create", "reason": "verify_failed"},
        },
        {
            "name": "BOOKING_CREATE",
            "status": "ESCALATED",
            "details": {"reason": "verify_failed"},
        },
    ]


@pytest.mark.asyncio
async def test_identity_failure_skips_transfer_after_caller_disconnect(monkeypatch):
    ctx, current_agent, session, order, _ = _identity_failure_fixture(
        monkeypatch,
        caller_disconnected=True,
    )

    result = await _run_dynamic_create_body(ctx)

    assert result is None
    # The caller is gone, so the failure line is not spoken into an empty room.
    assert order == []
    session.generate_reply.assert_not_called()
    current_agent._handle_transfer_call.assert_not_awaited()
