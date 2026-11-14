import asyncio
from types import SimpleNamespace

import pytest
from livekit.agents import Agent, ModelSettings, llm
from livekit.agents.types import FlushSentinel
from loguru import logger

from agents.base_agent import BaseAgent
from agents.single_agent import SingleAgent
from tasks.booking.select_reservation_task import SelectReservationTask
from tasks.caller_wait_reconfirmation_task import CallerWaitReconfirmationTask
from tasks.dtmf_verification_code_task import GetVerificationCodeDtmfTask
from tasks.dynamic_booking_task import DynamicBookingTask
from tasks.greeting_dtmf_task import GreetingDtmfTask
from tasks.leave_message_task import LeaveMessageTask
from tasks.schedule_selection_base import BaseScheduleSelectionTask
from tasks.single_room_warm_transfer_task import SingleRoomWarmTransferTask
from tasks.transfer_reason_task import TransferReasonTask
from utils.korean_speech_guard import (
    KoreanSpeechGuardMixin,
    call_language,
    guard_korean_speech,
)
from utils.non_korean_leak_filter import FALLBACK_UTTERANCE

KOREAN = "안내해 드리겠습니다."
LEAK = "Internal reasoning in English."


class GuardedAgent(KoreanSpeechGuardMixin, Agent):
    pass


def _agent(monkeypatch, cls=GuardedAgent, language="ko-KR"):
    data = SimpleNamespace(
        call_config_data=SimpleNamespace(language=language),
        room_name="synthetic-korean-speech-guard",
        dynamic_booking_resume=None,
    )
    session = SimpleNamespace(userdata=data, history=llm.ChatContext())
    monkeypatch.setattr(cls, "session", property(lambda _self: session))
    agent = object.__new__(cls)
    agent._call_session_data = data
    return agent, session


def _chunk(text, **kwargs):
    return llm.ChatChunk(
        id="speech", delta=llm.ChoiceDelta(role="assistant", content=text, **kwargs)
    )


async def _stream(items):
    for item in items:
        yield item


def _spoken(items):
    return "".join(
        item if isinstance(item, str) else item.delta.content or ""
        for item in items
        if isinstance(item, str)
        or (isinstance(item, llm.ChatChunk) and item.delta is not None)
    )


def _event(session):
    [message] = session.history.items
    assert message.role == "developer"
    assert message.text_content == ""
    event = message.extra["agent_event"]
    assert event["name"] == "LLM_OUTPUT_SANITIZED"
    return event


@pytest.mark.parametrize("plain_text", [False, True])
async def test_korean_prefix_and_english_tail_are_sanitized(monkeypatch, plain_text):
    agent, session = _agent(monkeypatch)
    pieces = ["안내해 ", "드리겠습니다. ", "Internal ", "reasoning in ", "English."]
    items = pieces if plain_text else [_chunk(piece) for piece in pieces]

    output = [item async for item in guard_korean_speech(agent, _stream(items))]

    assert _spoken(output) == KOREAN
    if not plain_text:
        assert all(any(item is chunk for item in output) for chunk in items)
    event = _event(session)
    assert event["status"] == "DROPPED"
    assert event["details"] == {
        "reason": "latin_sentence",
        "script": None,
        "dropped_chars": len(LEAK),
        "fallback": False,
        "tool_call": False,
        "agent": "GuardedAgent",
        "dropped_text": LEAK,
    }


@pytest.mark.parametrize("tool_call", [False, True])
async def test_leak_only_fallback_depends_on_tool_calls(monkeypatch, tool_call):
    agent, session = _agent(monkeypatch)
    items = [_chunk(LEAK)]
    call = llm.FunctionToolCall(name="lookup", arguments="{}", call_id="lookup-1")
    tool_chunk = _chunk(None, tool_calls=[call], extra={"provider": "preserved"})
    if tool_call:
        items.append(tool_chunk)

    output = [item async for item in guard_korean_speech(agent, _stream(items))]

    assert _spoken(output) == ("" if tool_call else FALLBACK_UTTERANCE)
    if tool_call:
        assert any(item is tool_chunk for item in output)
        assert tool_chunk.delta.tool_calls == [call]
        assert tool_chunk.delta.extra == {"provider": "preserved"}
    event = _event(session)
    assert event["status"] == ("DROPPED" if tool_call else "FALLBACK")
    assert event["details"]["fallback"] is not tool_call
    assert event["details"]["tool_call"] is tool_call


async def test_content_chunk_keeps_tool_calls_usage_and_extra(monkeypatch):
    agent, session = _agent(monkeypatch)
    usage = llm.CompletionUsage(completion_tokens=4, prompt_tokens=6, total_tokens=10)
    chunk = _chunk(
        LEAK,
        tool_calls=[llm.FunctionToolCall(name="lookup", arguments="{}", call_id="1")],
        extra={"provider": "preserved"},
    )
    chunk.usage = usage

    output = [item async for item in guard_korean_speech(agent, _stream([chunk]))]

    assert output == [chunk]
    assert chunk.delta.content == ""
    assert chunk.delta.tool_calls[0].name == "lookup"
    assert chunk.delta.extra == {"provider": "preserved"}
    assert chunk.usage is usage
    assert _event(session)["details"]["tool_call"] is True


async def test_usage_only_and_flush_sentinel_pass_through_unchanged(monkeypatch):
    agent, session = _agent(monkeypatch)
    usage = llm.ChatChunk(
        id="usage",
        usage=llm.CompletionUsage(completion_tokens=1, prompt_tokens=2, total_tokens=3),
    )
    sentinel = FlushSentinel()
    output = [
        item async for item in guard_korean_speech(agent, _stream([usage, sentinel]))
    ]
    assert len(output) == 2
    assert output[0] is usage
    assert output[1] is sentinel
    assert session.history.items == []


@pytest.mark.parametrize("language", ["en-US", None])
async def test_inactive_filter_preserves_input_without_events(monkeypatch, language):
    agent, session = _agent(monkeypatch, language=language)
    if language is None:
        del session.userdata
        del agent._call_session_data
    items = ["English reply. ", _chunk("ગુજરાતી English"), FlushSentinel()]

    output = [item async for item in guard_korean_speech(agent, _stream(items))]

    assert output == items
    assert output[1] is items[1]
    assert items[1].delta.content == "ગુજરાતી English"
    assert session.history.items == []


def test_call_language_prefers_userdata_and_uses_legacy_fallback():
    fallback = SimpleNamespace(call_config_data=SimpleNamespace(language="ko-KR"))
    agent = SimpleNamespace(
        session=SimpleNamespace(
            userdata=SimpleNamespace(call_config_data=SimpleNamespace(language="en-US"))
        ),
        _call_session_data=fallback,
    )
    assert call_language(agent) == "en-US"
    del agent.session.userdata
    assert call_language(agent) == "ko-KR"
    agent.session.userdata = SimpleNamespace()
    assert call_language(agent) == "ko-KR"
    del agent._call_session_data
    assert call_language(agent) is None
    assert call_language(SimpleNamespace()) is None


def test_call_language_handles_raising_properties():
    class UnsetSession:
        @property
        def userdata(self):
            raise RuntimeError("userdata is not set")

    class UnboundAgent:
        @property
        def session(self):
            raise RuntimeError("agent has no activity")

        @property
        def _call_session_data(self):
            raise RuntimeError("call data is not set")

    agent = SimpleNamespace(
        session=UnsetSession(),
        _call_session_data=SimpleNamespace(
            call_config_data=SimpleNamespace(language="ko-KR")
        ),
    )
    assert call_language(agent) == "ko-KR"
    assert call_language(UnboundAgent()) is None


@pytest.mark.parametrize(
    "cls", [GuardedAgent, SingleAgent, DynamicBookingTask, BaseScheduleSelectionTask]
)
async def test_real_llm_node_filters_model_speech(monkeypatch, cls):
    agent, session = _agent(monkeypatch, cls=cls)
    chat = llm.ChatContext()
    chat.add_message(role="user", content="안내해 주세요.")
    if cls in (DynamicBookingTask, BaseScheduleSelectionTask):
        monkeypatch.setattr(agent, "_with_caller_date_note", lambda ctx: ctx)

    async def default(called_agent, chat_ctx, tools, settings):
        assert called_agent is agent
        assert chat_ctx is chat
        yield _chunk(KOREAN + " ")
        yield _chunk(LEAK)

    monkeypatch.setattr(Agent.default, "llm_node", default)
    output = [item async for item in agent.llm_node(chat, [], ModelSettings())]

    assert _spoken(output) == KOREAN
    assert _event(session)["details"]["agent"] == cls.__name__


@pytest.mark.parametrize(
    "cls",
    [
        BaseAgent,
        TransferReasonTask,
        GreetingDtmfTask,
        SingleRoomWarmTransferTask,
        GetVerificationCodeDtmfTask,
        LeaveMessageTask,
        CallerWaitReconfirmationTask,
        SelectReservationTask,
    ],
)
def test_all_other_agents_and_tasks_use_guard_mixin(cls):
    assert issubclass(cls, KoreanSpeechGuardMixin)
    assert cls.llm_node is KoreanSpeechGuardMixin.llm_node


async def test_cancelled_generation_emits_no_tail_fallback_or_event(monkeypatch):
    agent, session = _agent(monkeypatch)
    started = asyncio.Event()
    output = []

    async def interrupted():
        yield "Internal reasoning"
        started.set()
        await asyncio.Future()

    async def collect():
        async for item in guard_korean_speech(agent, interrupted()):
            output.append(item)

    task = asyncio.create_task(collect())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert output == []
    assert session.history.items == []


async def test_event_recording_failure_does_not_interrupt_speech(monkeypatch):
    agent, session = _agent(monkeypatch)

    def fail_recording(**kwargs):
        raise RuntimeError("synthetic history failure")

    session.history = SimpleNamespace(add_message=fail_recording)
    output = [item async for item in guard_korean_speech(agent, _stream([LEAK]))]
    assert output == [FALLBACK_UTTERANCE]


async def test_sanitization_warning_contains_metadata_without_dropped_text(monkeypatch):
    agent, _ = _agent(monkeypatch)
    records = []
    sink = logger.add(lambda message: records.append(message.record), level="WARNING")
    try:
        _ = [item async for item in guard_korean_speech(agent, _stream([LEAK]))]
    finally:
        logger.remove(sink)

    warnings = [record for record in records if record["level"].name == "WARNING"]
    assert len(warnings) == 1
    record = warnings[0]
    assert record["extra"]["event"] == "LLM_OUTPUT_SANITIZED"
    assert record["extra"]["reason"] == "latin_sentence"
    assert record["extra"]["script"] is None
    assert record["extra"]["dropped_chars"] == len(LEAK)
    assert record["extra"]["agent"] == "GuardedAgent"
    assert record["extra"]["room_name"] == "synthetic-korean-speech-guard"
    assert "dropped_text" not in record["extra"]
    assert LEAK not in str(record)


async def test_event_dropped_text_is_bounded(monkeypatch):
    agent, session = _agent(monkeypatch)
    leak = "Internal reasoning. " * 40
    _ = [item async for item in guard_korean_speech(agent, _stream([leak]))]
    details = _event(session)["details"]
    assert details["dropped_chars"] == len(leak)
    assert len(details["dropped_text"]) == 300
    assert details["dropped_text"] in leak
