import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from livekit.agents import (
    Agent,
    AgentSession,
    AgentTask,
    ModelSettings,
    RunContext,
    function_tool,
    llm,
)

from agents.single_agent import SingleAgent
from tools.booking_tools import (
    cancel_appointment,
    get_my_appointment_list,
    modify_appointment,
)
from tools.dynamic_booking_tools import create_new_appointment

BOOKING_TOOLS = [
    create_new_appointment,
    get_my_appointment_list,
    modify_appointment,
    cancel_appointment,
]


def _choice(name):
    return {"type": "function", "function": {"name": name}}


def _agent():
    agent = object.__new__(SingleAgent)
    agent._call_session_data = SimpleNamespace(
        dynamic_booking_resume=SimpleNamespace(
            paused=True, last_handled_user_message_id="previous"
        )
    )
    agent._classify_booking_resume = AsyncMock(
        side_effect=AssertionError("Selected tool must bypass resume classification")
    )
    return agent


async def _collect(agent, chat, tools, choice):
    return [
        chunk
        async for chunk in agent.llm_node(
            chat, tools, ModelSettings(tool_choice=choice)
        )
    ]


def _forbid_provider(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Selected booking tool must not call the provider")

    monkeypatch.setattr(Agent.default, "llm_node", unexpected)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", BOOKING_TOOLS, ids=lambda tool: tool.id)
@pytest.mark.parametrize("has_user_turn", [False, True])
async def test_named_booking_uses_registered_tool_without_model_or_resume(
    monkeypatch, tool, has_user_turn
):
    _forbid_provider(monkeypatch)
    agent = _agent()
    chat = llm.ChatContext()
    if has_user_turn:
        chat.add_message(role="user", content="Please find another time.")

    chunks = await _collect(agent, chat, [tool], _choice(tool.id))

    assert len(chunks) == 1
    assert chunks[0].delta.content is None
    assert len(chunks[0].delta.tool_calls) == 1
    call = chunks[0].delta.tool_calls[0]
    assert call.name == tool.id
    assert json.loads(call.arguments) == {}
    agent._classify_booking_resume.assert_not_awaited()
    # The direct path's empty arguments must remain valid for the real tools.
    schema = llm.ToolContext([tool]).parse_function_tools("openai", strict=True)[0]
    assert schema["function"]["parameters"].get("required", []) == []


@pytest.mark.asyncio
async def test_each_generation_has_a_distinct_tool_call_id(monkeypatch):
    _forbid_provider(monkeypatch)
    agent = _agent()
    results = await asyncio.gather(
        *(
            _collect(
                agent,
                llm.ChatContext(),
                [modify_appointment],
                _choice("modify_appointment"),
            )
            for _ in range(3)
        )
    )
    assert len({chunks[0].delta.tool_calls[0].call_id for chunks in results}) == 3


@pytest.mark.asyncio
async def test_unavailable_named_booking_fails_without_provider(monkeypatch):
    _forbid_provider(monkeypatch)
    agent = _agent()
    with pytest.raises(ValueError, match="Selected booking tool unavailable"):
        await _collect(
            agent,
            llm.ChatContext(),
            [cancel_appointment],
            _choice("modify_appointment"),
        )
    agent._classify_booking_resume.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "choice", ["auto", "none", "required", None, _choice("transfer_call")]
)
async def test_other_choices_keep_default_inference(monkeypatch, choice):
    agent = _agent()
    agent._call_session_data.dynamic_booking_resume = None
    observed = []

    async def default(called_agent, chat, tools, settings):
        observed.append((called_agent, settings.tool_choice))
        yield "normal response"

    monkeypatch.setattr(Agent.default, "llm_node", default)
    assert await _collect(agent, llm.ChatContext(), [], choice) == ["normal response"]
    assert observed == [(agent, choice)]


class _ForbiddenLLM(llm.LLM):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def chat(self, **kwargs):
        self.calls += 1
        raise AssertionError("Direct tool execution must not invoke an LLM")


class _CompletingTask(AgentTask[str]):
    def __init__(self):
        super().__init__(instructions="Complete the synthetic test task.")

    async def on_enter(self):
        self.complete("completed")


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["modify_appointment", "cancel_appointment"])
async def test_scheduled_booking_executes_once_with_real_context_and_agent_task(
    name, monkeypatch
):
    observed = []

    @function_tool(name=name)
    async def booking(ctx: RunContext) -> None:
        """Run the synthetic booking workflow."""
        observed.append((ctx, await _CompletingTask()))

    class DirectAgent(SingleAgent):
        def __init__(self):
            Agent.__init__(
                self, instructions="Synthetic booking test.", tools=[booking]
            )
            self._call_session_data = SimpleNamespace(dynamic_booking_resume=None)

        async def on_enter(self):
            pass

        async def on_exit(self):
            pass

    model = _ForbiddenLLM()
    userdata = object()
    async with AgentSession(llm=model, userdata=userdata) as session:
        agent = DirectAgent()
        await session.start(agent)
        speeches = []
        generate_reply = session.generate_reply

        def capture_reply(**kwargs):
            handle = generate_reply(**kwargs)
            speeches.append(handle)
            return handle

        monkeypatch.setattr(session, "generate_reply", capture_reply)
        agent._request_exact_tool_call(name)
        async with asyncio.timeout(5):
            await speeches[0]
        assert len(observed) == 1
        ctx, result = observed[0]
        assert isinstance(ctx, RunContext)
        assert ctx.session is session
        assert ctx.userdata is userdata
        assert result == "completed"
        assert model.calls == 0
        calls = [item for item in session.history.items if item.type == "function_call"]
        assert [call.name for call in calls] == [name]


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", [_choice("create_new_appointment"), "auto"])
async def test_disconnected_caller_generates_nothing(monkeypatch, choice):
    _forbid_provider(monkeypatch)
    agent = _agent()
    agent._call_session_data.caller_disconnected = True

    chunks = await _collect(agent, llm.ChatContext(), [create_new_appointment], choice)

    assert chunks == []
    agent._classify_booking_resume.assert_not_awaited()


def test_disconnected_caller_does_not_schedule_tool_turn(monkeypatch):
    session = SimpleNamespace(generate_reply=Mock())
    monkeypatch.setattr(SingleAgent, "session", property(lambda self: session))
    agent = _agent()
    agent._call_session_data.caller_disconnected = True

    agent._request_exact_tool_call("create_new_appointment")

    session.generate_reply.assert_not_called()
