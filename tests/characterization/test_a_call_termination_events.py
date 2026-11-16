"""Pin the CALL_TERMINATION developer events emitted by SingleAgent/BaseAgent.

These events preserve actor/reason, including the double emission on tool-driven
hang-ups (the tool's own event followed by hangup()'s SYSTEM/HANGUP).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

import pytest

from agents.base_agent import BaseAgent
from agents.single_agent import SingleAgent
from data import FlowConfig, FlowNode
from tasks.greeting_dtmf_task import GreetingDtmfResult
from utils.i18n import t


def _termination(actor, reason):
    return {
        "agent_event": {
            "name": "CALL_TERMINATION",
            "details": {"actor": actor, "reason": reason},
        }
    }


def _handle(*, interrupted=False, wait_side_effect=None):
    return SimpleNamespace(
        wait_for_playout=AsyncMock(side_effect=wait_side_effect),
        interrupted=interrupted,
        interrupt=Mock(),
    )


def _session(**overrides):
    session = SimpleNamespace(
        say=Mock(return_value=_handle()),
        history=SimpleNamespace(add_message=Mock()),
        input=SimpleNamespace(set_audio_enabled=Mock()),
        shutdown=Mock(),
        current_speech=None,
    )
    for key, value in overrides.items():
        setattr(session, key, value)
    return session


def _extras(session):
    return [c.kwargs["extra"] for c in session.history.add_message.call_args_list]


def _terminations(session):
    return [
        extra
        for extra in _extras(session)
        if extra["agent_event"]["name"] == "CALL_TERMINATION"
    ]


def _single_agent(**data_overrides):
    agent = object.__new__(SingleAgent)
    data = SimpleNamespace(
        caller_disconnected=False,
        room_name="room-1",
        call_config_data=SimpleNamespace(language="ko-KR"),
        recipient_data=SimpleNamespace(phone="01000000000"),
        pending_api_results=[],
        flow_config=None,
        agents_data=None,
    )
    for key, value in data_overrides.items():
        setattr(data, key, value)
    agent._call_session_data = data
    agent._center_context_chunks = []
    return agent, data


# --- SingleAgent.on_enter ---------------------------------------------------


@pytest.mark.asyncio
async def test_on_enter_early_disconnect():
    agent, _ = _single_agent(caller_disconnected=True)
    session = _session()

    with patch.object(SingleAgent, "session", session):
        await agent.on_enter()

    assert _terminations(session) == [_termination("USER", "CALLER_DISCONNECTED")]
    session.shutdown.assert_called_once_with(drain=False)
    session.say.assert_not_called()


@pytest.mark.asyncio
async def test_on_enter_without_flow_config():
    agent, _ = _single_agent()
    session = _session()

    with patch.object(SingleAgent, "session", session):
        await agent.on_enter()

    session.say.assert_called_once_with(
        t("system.config_error", "ko-KR"), allow_interruptions=False
    )
    assert _terminations(session) == [_termination("SYSTEM", "NO_FLOW_CONFIG")]
    session.shutdown.assert_called_once_with()


# --- SingleAgent DTMF greeting ----------------------------------------------


def _dtmf_node():
    return FlowNode(
        id="greeting",
        type="greeting",
        name="인사말",
        input_method="dtmf",
        message="안녕하세요.",
        dtmf_options={"1": "info_agent"},
        next="fallback",
    )


async def _run_dtmf(agent, session, result):
    async def fake_task(**kwargs):
        return result

    with (
        patch.dict("os.environ", {"ENV": "local"}),
        patch.object(SingleAgent, "session", session),
        patch("agents.single_agent.GreetingDtmfTask", side_effect=fake_task),
    ):
        return await agent._handle_greeting_node(_dtmf_node())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("disconnected", "actor", "reason", "drain"),
    [
        (False, "SYSTEM", "GREETING_PLAYOUT_INTERRUPTED", True),
        (True, "USER", "CALLER_DISCONNECTED", False),
    ],
)
async def test_dtmf_greeting_interrupted(disconnected, actor, reason, drain):
    agent, _ = _single_agent(caller_disconnected=disconnected)
    session = _session()

    next_node = await _run_dtmf(
        agent, session, GreetingDtmfResult(selected_key=None, interrupted=True)
    )

    assert next_node is None
    assert _extras(session)[0] == {
        "agent_event": {
            "name": "GREETING_DTMF_TASK",
            "details": {"selected_key": None},
            "status": "INTERRUPTED",
        }
    }
    assert _terminations(session) == [_termination(actor, reason)]
    session.shutdown.assert_called_once_with(drain=drain)


@pytest.mark.asyncio
async def test_dtmf_timeout_after_caller_disconnect():
    agent, _ = _single_agent(caller_disconnected=True)
    session = _session()

    await _run_dtmf(agent, session, GreetingDtmfResult.timeout())

    session.say.assert_not_called()
    assert _terminations(session) == [_termination("USER", "CALLER_DISCONNECTED")]
    session.shutdown.assert_called_once_with(drain=False)


@pytest.mark.asyncio
async def test_dtmf_timeout_ends_call():
    agent, _ = _single_agent()
    session = _session()

    await _run_dtmf(agent, session, GreetingDtmfResult.timeout())

    session.say.assert_called_once_with(
        t("dtmf.timeout_exit", "ko-KR"), allow_interruptions=False
    )
    assert _terminations(session) == [_termination("SYSTEM", "MAX_INVALID_DTMF")]
    session.shutdown.assert_called_once_with()


@pytest.mark.asyncio
async def test_dtmf_timeout_playout_error_still_terminates():
    agent, _ = _single_agent()
    session = _session(say=Mock(return_value=_handle(wait_side_effect=RuntimeError)))

    await _run_dtmf(agent, session, GreetingDtmfResult.timeout())

    assert _terminations(session) == [_termination("SYSTEM", "MAX_INVALID_DTMF")]


# --- SingleAgent voice greeting ---------------------------------------------


def _voice_node():
    return FlowNode(
        id="greeting",
        type="greeting",
        name="인사말",
        input_method="voice",
        message="안녕하세요.",
    )


@pytest.mark.asyncio
async def test_voice_greeting_caller_disconnected_during_greeting():
    agent, data = _single_agent()

    def say(*args, **kwargs):
        data.caller_disconnected = True
        return _handle()

    session = _session(say=Mock(side_effect=say))

    with (
        patch.dict("os.environ", {"ENV": "dev"}),
        patch.object(SingleAgent, "session", session),
    ):
        assert await agent._handle_greeting_node(_voice_node()) is None

    assert _terminations(session) == [_termination("USER", "CALLER_DISCONNECTED")]
    session.shutdown.assert_called_once_with(drain=False)
    assert session.say.call_count == 1


@pytest.mark.asyncio
async def test_voice_greeting_caller_disconnected_before_recording_notice():
    agent, data = _single_agent()
    notice_handle = _handle()
    handles = iter([_handle(), notice_handle])

    def say(*args, **kwargs):
        handle = next(handles)
        if handle is notice_handle:
            data.caller_disconnected = True
        return handle

    session = _session(say=Mock(side_effect=say))

    with (
        patch.dict("os.environ", {"ENV": "dev"}),
        patch.object(SingleAgent, "session", session),
    ):
        assert await agent._handle_greeting_node(_voice_node()) is None

    notice_handle.interrupt.assert_called_once_with(force=True)
    assert _terminations(session) == [_termination("USER", "CALLER_DISCONNECTED")]
    session.shutdown.assert_called_once_with(drain=False)


# --- SingleAgent exit / missing nodes ---------------------------------------


@pytest.mark.asyncio
async def test_exit_node_termination_and_events():
    agent, _ = _single_agent()
    session = _session()
    node = FlowNode(
        id="bye",
        type="exit",
        name="종료",
        message="안녕히 계세요.",
        termination={"actor": "SYSTEM", "reason": "OFF_HOURS"},
        events=[{"type": "OFF_HOURS_EXIT", "Status": "DONE", "Hint": "x"}],
    )

    with patch.object(SingleAgent, "session", session):
        await agent._handle_exit_node(node)

    assert _extras(session) == [
        _termination("SYSTEM", "OFF_HOURS"),
        {
            "agent_event": {
                "name": "OFF_HOURS_EXIT",
                "details": {"hint": "x"},
                "status": "DONE",
            }
        },
    ]
    session.shutdown.assert_called_once_with()


@pytest.mark.asyncio
async def test_exit_node_defaults_to_user_explicit_request():
    agent, _ = _single_agent()
    session = _session()

    with patch.object(SingleAgent, "session", session):
        await agent._handle_exit_node(FlowNode(id="bye", type="exit", name="종료"))

    assert _extras(session) == [_termination("USER", "EXPLICIT_REQUEST")]
    session.say.assert_not_called()


@pytest.mark.asyncio
async def test_missing_node_terminates_call():
    agent, _ = _single_agent(flow_config=FlowConfig(entry_node_id="nowhere"))
    session = _session()

    with patch.object(SingleAgent, "session", session):
        await agent._process_node("nowhere")

    session.input.set_audio_enabled.assert_called_once_with(False)
    session.say.assert_called_once_with(
        t("system.error", "ko-KR"), allow_interruptions=False
    )
    assert _terminations(session) == [_termination("SYSTEM", "MISSING_FLOW_NODE")]
    session.shutdown.assert_called_once_with()


# --- BaseAgent hang-up tools ------------------------------------------------


def _base_agent(monkeypatch):
    session = _session()
    monkeypatch.setattr(BaseAgent, "session", property(lambda _: session))
    agent = object.__new__(BaseAgent)
    data = SimpleNamespace(
        room_name="room-1",
        recipient_data=SimpleNamespace(phone="01000000000"),
        call_config_data=SimpleNamespace(language="ko-KR"),
    )
    session.userdata = data
    ctx = SimpleNamespace(session=session, userdata=data, wait_for_playout=AsyncMock())
    return agent, session, ctx


@pytest.mark.asyncio
async def test_hangup_records_system_hangup(monkeypatch):
    agent, session, _ = _base_agent(monkeypatch)

    await agent.hangup()

    assert _extras(session) == [_termination("SYSTEM", "HANGUP")]
    session.shutdown.assert_called_once_with(drain=True)


@pytest.mark.asyncio
async def test_end_call_by_user_request_emits_both_terminations(monkeypatch):
    agent, session, ctx = _base_agent(monkeypatch)

    await BaseAgent.end_call_by_user_request._func(agent, ctx)

    session.input.set_audio_enabled.assert_called_once_with(False)
    session.say.assert_called_once_with(
        t("end_call.goodbye", "ko-KR"), allow_interruptions=False
    )
    assert _extras(session) == [
        _termination("USER", "EXPLICIT_REQUEST"),
        _termination("SYSTEM", "HANGUP"),
    ]
    session.shutdown.assert_called_once_with(drain=True)


@pytest.mark.asyncio
async def test_end_call_by_system_emits_both_terminations(monkeypatch):
    agent, session, ctx = _base_agent(monkeypatch)
    session.current_speech = object()

    await BaseAgent.end_call_by_system._func(agent, ctx, reason="task_completed")

    ctx.wait_for_playout.assert_awaited_once()
    assert _extras(session) == [
        _termination("SYSTEM", "task_completed"),
        _termination("SYSTEM", "HANGUP"),
    ]
    assert session.shutdown.call_args_list == [call(drain=True)]
