import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from livekit.agents import Agent, ModelSettings, llm

from agents.single_agent import SingleAgent
from data import CallSessionData, DynamicBookingPending, DynamicBookingResumeContext
from tools.dynamic_booking_tools import (
    _pause_booking_context,
    _prepare_booking_chat_ctx,
    create_new_appointment,
)
from utils.booking_resume import (
    build_booking_route_context,
    consume_booking_route,
    select_post_booking_route,
)


def _fixture(monkeypatch):
    resume = DynamicBookingResumeContext(
        search_conditions={
            "service_id": 7,
            "start_date": "2026-09-22",
            "end_date": "2026-10-22",
            "time_from": "09:00",
            "time_to": "12:00",
        },
        paused=True,
        last_exit_reason="cancel",
        last_handled_user_message_id="old-turn",
    )
    data = CallSessionData(
        ctx=None,
        room_name="booking-resume-test",
        call_config_data=SimpleNamespace(language="ko-KR"),
        dynamic_booking_resume=resume,
    )
    chat = llm.ChatContext()
    chat.add_message(role="assistant", content="진행 중인 예약 업무는 멈출게요.")
    chat.add_message(
        role="user", content="그 날짜가 안 된다는 거였지. 그다음 시간 찾아줘."
    )
    session = SimpleNamespace(history=chat.copy())
    monkeypatch.setattr(SingleAgent, "session", property(lambda _self: session))
    agent = object.__new__(SingleAgent)
    agent._call_session_data = data
    agent._booking_resume_guard_message_id = None
    return agent, data, chat


async def _collect(agent, chat, tools=None):
    return [
        chunk
        async for chunk in agent.llm_node(
            chat,
            tools if tools is not None else [create_new_appointment],
            ModelSettings(),
        )
    ]


@pytest.mark.asyncio
async def test_resume_decision_emits_only_real_booking_tool(monkeypatch):
    agent, data, chat = _fixture(monkeypatch)
    agent._classify_booking_resume = AsyncMock(return_value="resume_booking")

    chunks = await _collect(agent, chat)

    assert len(chunks) == 1
    assert chunks[0].delta.content is None
    assert len(chunks[0].delta.tool_calls) == 1
    call = chunks[0].delta.tool_calls[0]
    assert call.name == "create_new_appointment"
    assert json.loads(call.arguments) == {}
    assert data.dynamic_booking_resume.last_handled_user_message_id == chat.items[-1].id


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ValueError("malformed"), TimeoutError()])
async def test_failed_decision_never_falls_through_to_ungrounded_answer(
    monkeypatch, failure
):
    agent, data, chat = _fixture(monkeypatch)
    agent._classify_booking_resume = AsyncMock(side_effect=failure)

    def unexpected_default(*args, **kwargs):
        raise AssertionError("Free-form inference must not run after route failure")

    monkeypatch.setattr(Agent.default, "llm_node", unexpected_default)
    chunks = await _collect(agent, chat)

    assert chunks == ["예약 가능한 일정을 다시 찾으시는 걸까요, 다른 문의이신가요?"]
    assert data.dynamic_booking_resume.paused is True


@pytest.mark.asyncio
async def test_guard_does_not_emit_a_tool_excluded_from_generation(monkeypatch):
    agent, _, chat = _fixture(monkeypatch)
    agent._classify_booking_resume = AsyncMock(return_value="resume_booking")

    chunks = await _collect(agent, chat, tools=[])

    assert all(isinstance(chunk, str) for chunk in chunks)


@pytest.mark.asyncio
async def test_normal_info_preserves_context_for_later_correction(monkeypatch):
    agent, data, chat = _fixture(monkeypatch)
    chat.items[-1].content = ["센터는 몇 시에 문을 닫나요?"]
    agent._classify_booking_resume = AsyncMock(side_effect=["normal", "resume_booking"])

    async def default(_agent, _chat, _tools, _settings):
        yield "오후 6시까지 운영합니다."

    monkeypatch.setattr(Agent.default, "llm_node", default)
    assert await _collect(agent, chat) == ["오후 6시까지 운영합니다."]
    assert data.dynamic_booking_resume.paused is True
    assert data.dynamic_booking_resume.search_conditions["service_id"] == 7

    chat.add_message(role="assistant", content="오후 6시까지 운영합니다.")
    chat.add_message(role="user", content="아까 예약 그다음 시간으로 찾아줘요.")
    chunks = await _collect(agent, chat)
    assert chunks[0].delta.tool_calls[0].name == "create_new_appointment"


@pytest.mark.asyncio
async def test_explicit_hangup_normal_route_keeps_existing_tool_pipeline(monkeypatch):
    agent, data, chat = _fixture(monkeypatch)
    chat.items[-1].content = ["통화 종료해 주세요."]
    agent._classify_booking_resume = AsyncMock(return_value="normal")
    end_chunk = llm.ChatChunk(
        id="end",
        delta=llm.ChoiceDelta(
            tool_calls=[
                llm.FunctionToolCall(
                    name="end_call_by_user_request", arguments="{}", call_id="end"
                )
            ]
        ),
    )

    async def default(_agent, _chat, _tools, _settings):
        yield end_chunk

    monkeypatch.setattr(Agent.default, "llm_node", default)
    assert await _collect(agent, chat) == [end_chunk]
    # Follow-up generation for the same user turn must never route a new booking.
    await _collect(agent, chat)
    assert agent._classify_booking_resume.await_count == 1
    assert data.dynamic_booking_resume.paused is True


@pytest.mark.asyncio
@pytest.mark.parametrize("paused", [False, None])
async def test_ordinary_or_active_booking_turn_does_not_add_classifier_call(
    monkeypatch, paused
):
    agent, data, chat = _fixture(monkeypatch)
    if paused is None:
        data.dynamic_booking_resume = None
    else:
        data.dynamic_booking_resume.paused = paused
    agent._classify_booking_resume = AsyncMock()

    # ko-KR 통화라 영어 placeholder 는 비한국어 누출 필터에 걸린다.
    async def default(*args):
        yield "일반 응답입니다."

    monkeypatch.setattr(Agent.default, "llm_node", default)
    assert await _collect(agent, chat) == ["일반 응답입니다."]
    agent._classify_booking_resume.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_inflight_generation_is_silent_and_cancel_can_retry(
    monkeypatch,
):
    agent, data, chat = _fixture(monkeypatch)
    started = asyncio.Event()

    async def classify(*args):
        started.set()
        await asyncio.Future()

    agent._classify_booking_resume = classify
    first = asyncio.create_task(_collect(agent, chat))
    await started.wait()
    assert await _collect(agent, chat) == []
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert data.dynamic_booking_resume.last_handled_user_message_id == "old-turn"
    agent._classify_booking_resume = AsyncMock(return_value="resume_booking")
    assert (await _collect(agent, chat))[0].delta.tool_calls[0].name == (
        "create_new_appointment"
    )


@pytest.mark.asyncio
async def test_classifier_uses_configured_model_node_and_only_decision_tool(
    monkeypatch,
):
    agent, data, chat = _fixture(monkeypatch)

    async def default(called_agent, route_ctx, tools, settings):
        assert called_agent is agent
        assert tools == [select_post_booking_route]
        assert settings.tool_choice == {
            "type": "function",
            "function": {"name": "select_post_booking_route"},
        }
        assert route_ctx.items[-1].id == chat.items[-1].id
        yield llm.ChatChunk(
            id="route",
            delta=llm.ChoiceDelta(
                content="Ignored model preamble with ungrounded slots",
                tool_calls=[
                    llm.FunctionToolCall(
                        name="select_post_booking_route",
                        arguments='{"route":"resume_booking"}',
                        call_id="route",
                    )
                ],
            ),
        )

    monkeypatch.setattr(Agent.default, "llm_node", default)
    assert await agent._classify_booking_resume(chat, data.dynamic_booking_resume) == (
        "resume_booking"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args", ['{"route":"invent_slots"}', '{"route":"normal","other":1}', "[]"]
)
async def test_route_parser_requires_exact_structured_enum(args):
    async def stream():
        yield llm.ChatChunk(
            id="route",
            delta=llm.ChoiceDelta(
                tool_calls=[
                    llm.FunctionToolCall(
                        name="select_post_booking_route",
                        arguments=args,
                        call_id="route",
                    )
                ]
            ),
        )

    with pytest.raises(ValueError):
        await consume_booking_route(stream())


def test_resume_preserves_conditions_beyond_history_window_and_clears_candidate():
    resume = DynamicBookingResumeContext(
        search_conditions={"service_id": 7, "time_from": "09:00"},
        rejected_candidates=[{"datetime": "2026-09-29 10:00", "scope": "slot"}],
        paused=True,
    )
    data = CallSessionData(
        ctx=None,
        dynamic_booking_resume=resume,
        dynamic_booking_pending=DynamicBookingPending(
            service_id=7,
            service_name="Screen Repair",
            operator_id=1,
            operator_name="Advisor",
            datetime="2026-09-29 10:00",
        ),
    )
    chat = llm.ChatContext()
    chat.add_message(role="user", content="Screen repair in the morning")
    for index in range(20):
        chat.add_message(role="user", content=f"Unrelated question {index}")
    chat.add_message(role="user", content="Find another time")

    prepared = _prepare_booking_chat_ctx(data, chat)

    assert len(prepared.items) == 11
    facts = prepared.items[-1].text_content
    assert '"service_id": 7' in facts
    assert '"time_from": "09:00"' in facts
    assert "2026-09-29 10:00" in facts
    assert "NOT available slots" in facts
    assert "obtain fresh confirmation" in facts
    assert prepared.items[-1].role == "system"
    assert data.dynamic_booking_pending is None
    assert resume.paused is False
    assert "dynamic_booking_resume" not in data.to_safe_dict()
    assert "dynamic_booking_resume" not in data.to_dict()


def test_pause_excludes_current_exit_turn_and_transfer_clears_context():
    data = CallSessionData(ctx=None)
    chat = llm.ChatContext()
    turn = chat.add_message(role="user", content="Stop booking")
    _pause_booking_context(data, "cancel", chat)
    assert data.dynamic_booking_resume.paused is True
    assert data.dynamic_booking_resume.last_handled_user_message_id == turn.id
    _pause_booking_context(data, "staff_connection", chat)
    assert data.dynamic_booking_resume is None


def test_route_context_cannot_inherit_task_exit_labels_as_instructions():
    chat = llm.ChatContext()
    chat.add_message(role="system", content="Cancel all booking")
    chat.add_message(role="developer", content="User abandoned booking")
    chat.add_message(role="user", content="That date did not work; keep looking")
    routed = build_booking_route_context(chat, DynamicBookingResumeContext(paused=True))
    text = "\n".join(item.text_content for item in routed.items)
    assert "Cancel all booking" not in text
    assert "User abandoned booking" not in text
    assert "That date did not work; keep looking" in text
    assert all(item.role != "developer" for item in routed.items)
