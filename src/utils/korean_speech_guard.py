"""LLM의 한국어 발화 누출을 음성과 대화 이력에 반영되기 전에 제거한다.

``tts_text_transforms`` 대신 ``llm_node``에서 처리해 이력도 함께 정제하고,
스크립트로 지정한 ``session.say()`` 발화는 그대로 둔다.
"""

from collections.abc import AsyncIterable

from livekit.agents import Agent, FlushSentinel, ModelSettings, llm
from loguru import logger

from utils.agent_event import agent_event_extra
from utils.non_korean_leak_filter import NonKoreanLeakFilter


def call_language(agent) -> str | None:
    """세션 설정을 우선하고 세션 연결 전에는 에이전트의 통화 설정을 쓴다."""
    try:
        return agent.session.userdata.call_config_data.language
    except Exception:
        try:
            return agent._call_session_data.call_config_data.language
        except Exception:
            return None


async def guard_korean_speech(
    agent,
    stream: AsyncIterable[llm.ChatChunk | str | FlushSentinel],
) -> AsyncIterable[llm.ChatChunk | str | FlushSentinel]:
    """발화 텍스트만 정제하고 도구 호출·사용량·flush 신호는 보존한다."""
    speech_filter = NonKoreanLeakFilter(language=call_language(agent))
    raw_parts: list[str] = []
    tool_call = False
    async for chunk in stream:
        if isinstance(chunk, str):
            raw_parts.append(chunk)
            text = speech_filter.push(chunk)
            if text:
                yield text
        elif isinstance(chunk, llm.ChatChunk):
            if chunk.delta is not None:
                tool_call |= bool(chunk.delta.tool_calls)
                if chunk.delta.content:
                    raw_parts.append(chunk.delta.content)
                    chunk.delta.content = speech_filter.push(chunk.delta.content)
            yield chunk
        else:
            yield chunk

    # 취소된 생성에서는 이 지점에 도달하지 않아 보류 텍스트를 발화하지 않는다.
    tail = speech_filter.finish()
    result = speech_filter.result
    # 도구가 다음 발화를 이어갈 때는 불필요한 재질문을 끼워 넣지 않는다.
    fallback = result.fallback and not tool_call
    if tail and (not result.fallback or fallback):
        yield tail

    if result.reason is None:
        return

    agent_name = type(agent).__name__
    try:
        room_name = agent.session.userdata.room_name
    except Exception:
        try:
            room_name = agent._call_session_data.room_name
        except Exception:
            room_name = None
    log = logger.bind(
        event="LLM_OUTPUT_SANITIZED",
        reason=result.reason,
        script=result.script,
        dropped_chars=result.dropped_chars,
        agent=agent_name,
        room_name=room_name,
    )
    # 제거 내용에는 고객 발화 인용이 섞일 수 있어 이력에만 남기고 로그하지 않는다.
    raw_text = "".join(raw_parts)
    dropped_text = (
        raw_text[-result.dropped_chars :][:300] if result.dropped_chars else ""
    )
    try:
        agent.session.history.add_message(
            role="developer",
            content="",
            extra=agent_event_extra(
                "LLM_OUTPUT_SANITIZED",
                status="FALLBACK" if fallback else "DROPPED",
                details={
                    "reason": result.reason,
                    "script": result.script,
                    "dropped_chars": result.dropped_chars,
                    "fallback": fallback,
                    "tool_call": tool_call,
                    "agent": agent_name,
                    "dropped_text": dropped_text,
                },
            ),
        )
    except Exception as error:
        log.bind(error_type=type(error).__name__).error(
            "Failed to record LLM output sanitization in conversation history"
        )
    log.warning("Sanitized non-Korean text in LLM speech output")


class KoreanSpeechGuardMixin:
    """Agent/AgentTask보다 앞에 상속해 기본 LLM 발화 스트림을 보호한다."""

    async def llm_node(
        self,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        model_settings: ModelSettings,
    ) -> AsyncIterable[llm.ChatChunk | str | FlushSentinel]:
        async for chunk in guard_korean_speech(
            self, Agent.default.llm_node(self, chat_ctx, tools, model_settings)
        ):
            yield chunk
