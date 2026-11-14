"""Agent LLM selection helpers for CLI and production sessions."""

from __future__ import annotations

import asyncio
import logging
import os

from google.genai.types import ThinkingConfig, ThinkingLevel
from livekit.agents import llm
from livekit.plugins import aws, google, openai
from openai.types import Reasoning

AGENT_LLM_MODEL_ENV = "AGENT_LLM_MODEL"
DEFAULT_AGENT_LLM_MODEL = "openai:gpt-5.6-luna"
SUPPORTED_AGENT_LLM_PROVIDERS = {
    "aws",
    "google",
    "openai",
}
# No private/self-hosted providers are configured. The "never fall back from a
# private LLM to an external one" guard still applies to any listed here.
PRIVATE_AGENT_LLM_PROVIDERS: frozenset[str] = frozenset()
PRODUCTION_FALLBACK_AGENT_LLM_MODELS = (
    "openai:gpt-5.6-luna",
    "google:gemini-2.5-flash-lite",
)

logger = logging.getLogger(__name__)


def resolve_agent_llm_model(
    explicit: str | None = None,
    *,
    default: str = DEFAULT_AGENT_LLM_MODEL,
) -> str:
    """Resolve the ``provider:model`` value from explicit value, env, then default."""
    if explicit and explicit.strip():
        return explicit.strip()
    if env_model := os.getenv(AGENT_LLM_MODEL_ENV, "").strip():
        return env_model
    return default


def parse_agent_llm_model(value: str) -> tuple[str, str]:
    """Split and validate an agent LLM selection in ``provider:model`` format."""
    provider, separator, model = value.partition(":")
    provider = provider.strip().lower()
    model = model.strip()

    if not separator or not provider or not model:
        raise ValueError(
            f"Invalid AGENT_LLM_MODEL={value!r}. "
            "Expected 'provider:model' (for example, 'openai:gpt-5.4-mini')."
        )
    if provider not in SUPPORTED_AGENT_LLM_PROVIDERS:
        supported = ", ".join(sorted(SUPPORTED_AGENT_LLM_PROVIDERS))
        raise ValueError(
            f"Unsupported LLM provider={provider!r}. Supported providers: {supported}"
        )

    return provider, model


def _build_openai_llm(model: str, *, parallel_tool_calls: bool):
    options = {
        "model": model,
        "temperature": 0.1,
        "max_completion_tokens": 500,
        "parallel_tool_calls": parallel_tool_calls,
        "service_tier": "priority",
        "reasoning_effort": "low",
    }

    if model.startswith("gpt-5.6") is False:
        return openai.LLM(**options)

    return openai.responses.LLM(
        model=model,
        temperature=0.1,
        reasoning=Reasoning(effort="low"),
        max_output_tokens=1000,
        parallel_tool_calls=parallel_tool_calls,
        service_tier="priority",
        use_websocket=True,
    )


def _build_google_llm(model: str):
    options = {
        "model": model,
        "temperature": 0.1,
        "vertexai": True,
        "max_output_tokens": 300 if "flash-lite" in model else 500,
    }

    if model.startswith("gemini-3"):
        options["location"] = "global"
        options["thinking_config"] = ThinkingConfig(
            include_thoughts=False,
            thinking_level=ThinkingLevel.LOW,
        )
    elif model.startswith("gemini-2.5"):
        if model == "gemini-2.5-flash":
            options["location"] = "asia-northeast3"
        options["thinking_config"] = ThinkingConfig(
            include_thoughts=False,
            thinking_budget=0,
        )

    return google.LLM(**options)


def _build_aws_llm(model: str):
    return aws.LLM(
        model=model,
        temperature=0.1,
        max_output_tokens=500,
        cache_system=True,
        # additional_request_fields={
        #     "type": "enabled",
        #     "maxReasoningEffort": "low",
        # },
        # Region/credentials come from the standard AWS chain.
    )


def build_agent_llm(
    explicit: str | None = None,
    *,
    default: str = DEFAULT_AGENT_LLM_MODEL,
    parallel_tool_calls: bool = True,
):
    """Build an LLM synchronously; use build_agent_llm_async on the agent loop."""
    selection = resolve_agent_llm_model(explicit, default=default)
    provider, model = parse_agent_llm_model(selection)

    if provider == "aws":
        return _build_aws_llm(model)
    if provider == "openai":
        return _build_openai_llm(
            model,
            parallel_tool_calls=parallel_tool_calls,
        )
    if provider == "google":
        return _build_google_llm(model)

    raise AssertionError(f"Unhandled LLM provider: {provider}")


async def _close_unused_google_llm(construction: asyncio.Task) -> None:
    if construction.cancelled():
        return
    try:
        unused_llm = construction.result()
        await unused_llm.aclose()
    except Exception:
        logger.warning(
            "Failed to clean up cancelled Google LLM initialization", exc_info=True
        )


async def build_agent_llm_async(
    explicit: str | None = None,
    *,
    default: str = DEFAULT_AGENT_LLM_MODEL,
    parallel_tool_calls: bool = True,
):
    """Build an LLM without loading Google's SSL certificates on the agent loop."""
    selection = resolve_agent_llm_model(explicit, default=default)
    provider, model = parse_agent_llm_model(selection)
    if provider != "google":
        # Other plugins can create loop-bound resources in their constructors.
        return build_agent_llm(selection, parallel_tool_calls=parallel_tool_calls)

    construction = asyncio.create_task(asyncio.to_thread(_build_google_llm, model))
    try:
        return await asyncio.shield(construction)
    except asyncio.CancelledError:
        # Cancelling to_thread cannot stop its worker. Close its eventual client
        # on this loop even if the call ends before construction finishes.
        construction.add_done_callback(
            lambda task: asyncio.create_task(_close_unused_google_llm(task))
        )
        raise


def build_production_agent_llm(config_model: str | None = None):
    """Keep the external fallback unless configuration selects a private LLM."""
    selection = (config_model or "").strip()
    if selection and parse_agent_llm_model(selection)[0] in PRIVATE_AGENT_LLM_PROVIDERS:
        # Never silently send private-LLM call data to an external provider.
        return build_agent_llm(selection, parallel_tool_calls=False)

    return llm.FallbackAdapter(
        [
            build_agent_llm(model, parallel_tool_calls=False)
            for model in PRODUCTION_FALLBACK_AGENT_LLM_MODELS
        ]
    )


async def build_production_agent_llm_async(config_model: str | None = None):
    """Build production fallback plugins without blocking the agent loop."""
    selection = (config_model or "").strip()
    if selection and parse_agent_llm_model(selection)[0] in PRIVATE_AGENT_LLM_PROVIDERS:
        # Never silently send private-LLM call data to an external provider.
        return await build_agent_llm_async(selection, parallel_tool_calls=False)

    return llm.FallbackAdapter(
        [
            await build_agent_llm_async(model, parallel_tool_calls=False)
            for model in PRODUCTION_FALLBACK_AGENT_LLM_MODELS
        ]
    )
