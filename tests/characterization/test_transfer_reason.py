import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from livekit.agents.llm.utils import prepare_function_arguments

from agents.base_agent import BaseAgent
from tasks.transfer_reason_task import (
    TransferPurpose,
    TransferReasonResult,
    TransferReasonTask,
)
from utils.i18n import t


def _reason_task(monkeypatch):
    room = SimpleNamespace(
        on=Mock(), off=Mock(), remote_participants={"caller": object()}
    )
    session = SimpleNamespace(
        input=SimpleNamespace(set_audio_enabled=Mock()),
        say=Mock(),
        userdata=SimpleNamespace(caller_disconnected=False),
    )
    monkeypatch.setattr(TransferReasonTask, "session", property(lambda _: session))
    monkeypatch.setattr(
        "tasks.transfer_reason_task.get_job_context",
        lambda: SimpleNamespace(room=room),
    )
    task = TransferReasonTask(
        caller_identity="caller",
        language="ko-KR",
    )
    task.complete = Mock()
    return task, session, room


@pytest.mark.asyncio
async def test_reason_task_accepts_tool_argument(monkeypatch):
    task, session, room = _reason_task(monkeypatch)

    await task.on_enter()
    assert session.say.call_args.args[0] == t("transfer.reason.question", "ko-KR")
    assert session.input.set_audio_enabled.call_args.args == (True,)
    task.complete.assert_not_called()

    assert (
        await TransferReasonTask.submit_reason._func(task, reason="예약 변경이 안 돼요")
        is None
    )
    assert task.complete.call_args.args[0] == TransferReasonResult(
        purpose=TransferPurpose(code="caller_stated", detail="예약 변경이 안 돼요")
    )
    await task.on_exit()
    room.off.assert_called_once()


@pytest.mark.asyncio
async def test_reason_task_does_not_reprompt_on_silence(monkeypatch):
    task, session, _ = _reason_task(monkeypatch)
    await task.on_enter()
    await asyncio.sleep(0.03)

    session.say.assert_called_once()
    task.complete.assert_not_called()
    await task.on_exit()


@pytest.mark.asyncio
async def test_reason_task_disconnect_cancels_handoff(monkeypatch):
    task, _, _ = _reason_task(monkeypatch)
    await task.on_enter()
    task._on_participant_disconnected(SimpleNamespace(identity="caller"))

    assert task.complete.call_args.args[0].caller_disconnected is True
    await task.on_exit()


@pytest.mark.asyncio
async def test_reason_task_prompts_without_a_sip_participant(monkeypatch):
    task, session, room = _reason_task(monkeypatch)
    room.remote_participants.clear()
    task._caller_identity = None

    await task.on_enter()

    session.say.assert_called_once()
    task.complete.assert_not_called()
    await task.on_exit()


@pytest.mark.asyncio
async def test_reason_task_skips_prompt_after_recorded_disconnect(monkeypatch):
    task, session, _ = _reason_task(monkeypatch)
    session.userdata.caller_disconnected = True

    await task.on_enter()

    session.say.assert_not_called()
    assert task.complete.call_args.args[0].caller_disconnected is True
    await task.on_exit()


@pytest.mark.asyncio
async def test_reason_task_records_explicit_refusal_as_unprovided(monkeypatch):
    task, session, _ = _reason_task(monkeypatch)
    await task.on_enter()

    assert await TransferReasonTask.submit_reason._func(task, reason=None) is None
    session.say.assert_called_once()
    assert task.complete.call_args.args[0].purpose.code == "unprovided"
    await task.on_exit()


# Async so AgentTask construction always has a running loop; as a sync test it
# would depend on test order (fails after test_single_agent_greeting).
@pytest.mark.asyncio
async def test_reason_tool_accepts_null_argument(monkeypatch):
    task, _, _ = _reason_task(monkeypatch)
    tool = next(tool for tool in task.tools if tool.info.name == "submit_reason")

    args, kwargs = prepare_function_arguments(fnc=tool, json_arguments={"reason": None})

    assert args == (None,)
    assert kwargs == {}


# Async so AgentTask construction always has a running loop; as a sync test it
# would depend on test order (fails after test_single_agent_greeting).
@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["ko-KR", "en-US"])
async def test_follow_up_uses_the_original_localized_question(language):
    task = TransferReasonTask(caller_identity="caller", language=language)

    assert f'"{t("transfer.reason.question", language)}"' in task.instructions
    assert "상담원에게 전달할지" not in task.instructions


def _transfer_agent(monkeypatch):
    session = SimpleNamespace(input=SimpleNamespace(set_audio_enabled=Mock()))
    monkeypatch.setattr(BaseAgent, "session", property(lambda _: session))
    agent = object.__new__(BaseAgent)
    agent._handle_cold_transfer_call = AsyncMock()
    agent._handle_warm_transfer_call = AsyncMock()
    data = SimpleNamespace(
        recipient_data=SimpleNamespace(sip_identity="caller"),
        call_config_data=SimpleNamespace(language="ko-KR"),
    )
    return agent, session, data


@pytest.mark.asyncio
async def test_known_purpose_skips_question_before_warm_transfer(monkeypatch):
    agent, session, data = _transfer_agent(monkeypatch)
    task = Mock()
    monkeypatch.setattr("agents.base_agent.TransferReasonTask", task)
    purpose = TransferPurpose(code="no_slot_memo")

    await agent._dispatch_transfer(
        data, "1004", "warm_transfer", direct=False, transfer_purpose=purpose
    )

    task.assert_not_called()
    session.input.set_audio_enabled.assert_called_once_with(False)
    agent._handle_warm_transfer_call.assert_awaited_once_with(
        call_session_data=data,
        transfer_to="1004",
        transfer_type="WARM",
        action_key=None,
        transfer_purpose=purpose,
        briefing_enabled=True,
    )


@pytest.mark.asyncio
async def test_unknown_purpose_is_collected_before_warm_transfer(monkeypatch):
    agent, session, data = _transfer_agent(monkeypatch)
    purpose = TransferPurpose(code="caller_stated", detail="예약 변경")
    task = AsyncMock(return_value=TransferReasonResult(purpose=purpose))
    monkeypatch.setattr("agents.base_agent.TransferReasonTask", task)

    await agent._dispatch_transfer(data, "1004", "warm_transfer", direct=False)

    task.assert_awaited_once_with(caller_identity="caller", language="ko-KR")
    session.input.set_audio_enabled.assert_called_once_with(False)
    agent._handle_warm_transfer_call.assert_awaited_once_with(
        call_session_data=data,
        transfer_to="1004",
        transfer_type="WARM",
        action_key=None,
        transfer_purpose=purpose,
        briefing_enabled=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transfer_mode", "direct", "handler", "transfer_type"),
    [
        ("cold_transfer", False, "_handle_cold_transfer_call", "COLD"),
        ("cold_transfer", True, "_handle_cold_transfer_call", "DIRECT_COLD"),
        ("warm_transfer", True, "_handle_warm_transfer_call", "DIRECT_WARM"),
    ],
)
async def test_cold_and_direct_transfers_skip_purpose_question(
    monkeypatch, transfer_mode, direct, handler, transfer_type
):
    # The purpose only feeds the warm briefing; cold and keypad-direct
    # transfers have nowhere to deliver it, so the caller is not asked.
    agent, _, data = _transfer_agent(monkeypatch)
    task = AsyncMock()
    monkeypatch.setattr("agents.base_agent.TransferReasonTask", task)

    await agent._dispatch_transfer(data, "1004", transfer_mode, direct=direct)

    task.assert_not_called()
    getattr(agent, handler).assert_awaited_once()
    assert getattr(agent, handler).await_args.kwargs["transfer_type"] == transfer_type


@pytest.mark.asyncio
async def test_disconnect_during_question_never_dials(monkeypatch):
    agent, session, data = _transfer_agent(monkeypatch)
    monkeypatch.setattr(
        "agents.base_agent.TransferReasonTask",
        AsyncMock(
            return_value=TransferReasonResult(
                purpose=TransferPurpose(code="unprovided"), caller_disconnected=True
            )
        ),
    )

    await agent._dispatch_transfer(data, "1004", "warm_transfer", direct=False)

    session.input.set_audio_enabled.assert_not_called()
    agent._handle_cold_transfer_call.assert_not_awaited()
    agent._handle_warm_transfer_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_transfer_handler_keeps_audio_available_until_dispatch(monkeypatch):
    agent, session, data = _transfer_agent(monkeypatch)
    agent._dispatch_transfer = AsyncMock()
    data.recipient_data.phone = "01000000000"
    data.room_name = "test-room"
    data.call_config_data.transfer_to = "1004"
    data.call_config_data.transfer_mode = "warm_transfer"

    await agent._handle_transfer_call(data)

    session.input.set_audio_enabled.assert_not_called()
    agent._dispatch_transfer.assert_awaited_once_with(
        call_session_data=data,
        transfer_to="1004",
        transfer_mode="warm_transfer",
        direct=False,
        action_key=None,
        transfer_purpose=None,
    )


@pytest.mark.asyncio
async def test_memo_only_route_does_not_ask_transfer_reason(monkeypatch):
    agent, _, data = _transfer_agent(monkeypatch)
    agent._dispatch_transfer = AsyncMock()
    agent._handle_leave_message = AsyncMock()
    data.recipient_data.phone = "01000000000"
    data.room_name = "test-room"
    data.call_config_data.transfer_to = None
    data.call_config_data.transfer_mode = "leave_memo"

    await agent._handle_transfer_call(data)

    agent._dispatch_transfer.assert_not_awaited()
    agent._handle_leave_message.assert_awaited_once_with(
        data,
        is_after_transfer_failure=False,
        entry_reason="memo_only",
        action_key=None,
        transfer_purpose=None,
        booking_context=None,
    )
