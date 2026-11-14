"""상담원 브리핑용 대화 요약 생성 유틸리티

LangChain의 structured output으로 대화 내역을 한 문장으로 요약한다.
고객 정보·DTMF 안내를 붙인 최종 브리핑 조합은
``services.warm_transfer_briefing``이 담당한다.
"""

from __future__ import annotations

from langchain.chat_models import init_chat_model
from loguru import logger
from pydantic import BaseModel, Field

from utils.i18n import language_display_name

# 요약 생성 실패 시 폴백 텍스트 (언어별). 미등록 언어는 영어로 폴백.
BRIEFING_FALLBACK_TEXT = {
    "ko-KR": "고객이 상담원 연결을 요청하셨습니다.",
    "en-US": "The customer has requested to be connected to an agent.",
}
DEFAULT_BRIEFING_FALLBACK = BRIEFING_FALLBACK_TEXT["en-US"]


def _fallback_text(language: str | None) -> str:
    """언어 설정에 맞는 폴백 텍스트 반환 (미등록 언어는 영어)."""
    return BRIEFING_FALLBACK_TEXT.get(language or "en-US", DEFAULT_BRIEFING_FALLBACK)


BRIEFING_PROMPT = """You are an AI counseling assistant. Analyze the conversation history below and summarize, in a single intuitive and concise sentence, what was discussed between the customer and the AI.

# Rules
- Use ONLY information explicitly stated in the conversation history.
- NEVER add information not present in the conversation history.
- Do not speculate or make assumptions.
- Write naturally, in a conversational/spoken style.
- Write the summary in {language}. Regardless of which language the conversation itself used, the summary MUST be written in {language}.

# Conversation history
{conversation_history}

{extra_instructions}"""


class BriefingSummary(BaseModel):
    """상담원 브리핑을 위한 대화 요약"""

    conversation_summary: str = Field(
        description="A single intuitive, concise sentence summarizing what the customer asked about and what was discussed. Write it in a conversational/spoken style, in the language requested by the prompt. e.g.: 'The customer asked about changing an appointment and is currently waiting to be connected to an agent.'"
    )


def _get_briefing_llm():
    """Briefing LLM (OpenAI GPT-5.6 Luna)."""
    return init_chat_model(
        "openai:gpt-5.6-luna",
        temperature=0,
    )


async def generate_briefing_text(
    conversation_history: str,
    extra_instructions: str = "",
    language: str | None = None,
) -> str:
    """대화 내역을 기반으로 상담원 브리핑용 요약 문장을 생성합니다.

    LangChain structured output으로 대화를 한 문장으로 요약합니다. 요약 문장은
    ``language`` 설정값에 맞는 언어로 작성된다(미지정 시 영어).

    Args:
        conversation_history: "role: text" 형식의 대화 내역 문자열
        extra_instructions: 추가 지시사항
        language: 요약을 작성할 언어 (BCP 47, 예: "ko-KR"/"en-US"). 미지정 시 영어.

    Returns:
        TTS용 브리핑 텍스트 문자열. LLM 실패 시 fallback 텍스트 반환.
    """
    if not conversation_history.strip():
        logger.warning("Empty conversation history, returning fallback text")
        return _fallback_text(language)

    try:
        llm = _get_briefing_llm().with_structured_output(BriefingSummary)

        extra_section = ""
        if extra_instructions:
            extra_section = f"# Additional info\n{extra_instructions}"

        prompt = BRIEFING_PROMPT.format(
            language=language_display_name(language),
            conversation_history=conversation_history,
            extra_instructions=extra_section,
        )

        summary: BriefingSummary = await llm.ainvoke(prompt)

        briefing_text = summary.conversation_summary

        logger.bind(
            event="GENERATE_BRIEFING_TEXT",
            language=language,
            conversation_summary=summary.conversation_summary,
            briefing_text=briefing_text,
        ).debug("Briefing text generated successfully")

        return briefing_text

    except Exception as e:
        logger.opt(exception=e).error(
            "Failed to generate briefing text, using fallback"
        )
        return _fallback_text(language)
