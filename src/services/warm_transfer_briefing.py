"""Warm-transfer briefing extraction, generation, and formatting."""

from __future__ import annotations

from typing import TYPE_CHECKING

from utils.generate_briefing import generate_briefing_text
from utils.i18n import t

if TYPE_CHECKING:
    from livekit.agents import llm


def extract_warm_transfer_conversation_history(
    chat_ctx: llm.ChatContext | None,
) -> str:
    """Return a stable, message-only snapshot for briefing generation."""
    if not chat_ctx:
        return ""

    context_copy = chat_ctx.copy(
        exclude_empty_message=True,
        exclude_instructions=True,
        exclude_function_call=True,
    )
    return "".join(
        f"{item.role}: {item.text_content}\n"
        for item in context_copy.items
        if item.type == "message"
    )


def build_warm_transfer_briefing_text(
    conversation_summary: str,
    *,
    customer_name: str | None,
    is_new_customer: bool,
    language_code: str | None,
) -> str:
    """Combine customer context, conversation summary, and DTMF guidance."""
    parts = [t("warm_transfer.briefing.intro", language_code)]

    if is_new_customer:
        parts.append(t("warm_transfer.briefing.new_customer", language_code))
    elif customer_name:
        parts.append(
            t(
                "warm_transfer.briefing.revisit_customer",
                language_code,
                name=customer_name,
            )
        )
    else:
        parts.append(t("warm_transfer.briefing.unknown_customer", language_code))

    parts.append(t("warm_transfer.briefing.lead_in", language_code))
    parts.append(conversation_summary)
    parts.append(t("warm_transfer.briefing.dtmf_guide", language_code))
    return " ".join(parts)


async def generate_warm_transfer_briefing_text(
    *,
    conversation_history: str,
    customer_name: str | None,
    is_new_customer: bool,
    language_code: str | None,
    extra_instructions: str = "",
) -> str:
    """Generate one complete human-agent briefing from a conversation snapshot."""
    conversation_summary = await generate_briefing_text(
        conversation_history=conversation_history,
        extra_instructions=extra_instructions,
        language=language_code,
    )
    return build_warm_transfer_briefing_text(
        conversation_summary,
        customer_name=customer_name,
        is_new_customer=is_new_customer,
        language_code=language_code,
    )
