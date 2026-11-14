"""Agent STT selection helpers."""

from __future__ import annotations

import os

from livekit.agents.vad import VAD
from livekit.plugins import aws, elevenlabs, google
from livekit.plugins.google.beta import GeminiSTT

from plugins.custom_stt import DEFAULT_BACKCHANNEL_WORDS, FilteredAWSSTT

DEFAULT_AGENT_STT_MODEL = "aws:transcribe"
SUPPORTED_AGENT_STT_PROVIDERS = {
    "aws",
    "elevenlabs",
    "google",
}
GOOGLE_VERTEX_GEMINI_LIVE_PREVIEW_MODEL = "gemini-3.5-transcribe-live-preview"


def resolve_agent_stt_model(
    explicit: str | None = None,
    *,
    default: str = DEFAULT_AGENT_STT_MODEL,
) -> str:
    """Resolve the ``provider:model`` value from explicit call config, then default."""
    if explicit and explicit.strip():
        return explicit.strip()
    return default


def parse_agent_stt_model(value: str) -> tuple[str, str]:
    """Split and validate an agent STT selection in ``provider:model`` format."""
    provider, separator, model = value.partition(":")
    provider = provider.strip().lower()
    model = model.strip()

    if not separator or not provider or not model:
        raise ValueError(
            f"Invalid stt_model={value!r}. "
            "Expected 'provider:model' (for example, 'google:latest_long')."
        )
    if provider not in SUPPORTED_AGENT_STT_PROVIDERS:
        supported = ", ".join(sorted(SUPPORTED_AGENT_STT_PROVIDERS))
        raise ValueError(
            f"Unsupported STT provider={provider!r}. Supported providers: {supported}"
        )

    return provider, model


def build_agent_stt(
    selection: str | None = None,
    *,
    language: str,
    custom_vocabulary: list[str] | None = None,
    vad: VAD | None = None,
):
    """Build a LiveKit STT plugin from a ``provider:model`` selection."""
    provider, model = parse_agent_stt_model(resolve_agent_stt_model(selection))

    if provider == "aws":
        if model != "transcribe":
            raise ValueError(
                f"Unsupported AWS STT model={model!r}. Supported model: transcribe"
            )
        access_key_id = os.getenv("AWS_SPEECH_ACCESS_KEY_ID", "").strip()
        secret_access_key = os.getenv("AWS_SPEECH_SECRET_ACCESS_KEY", "").strip()
        if not access_key_id or not secret_access_key:
            raise ValueError(
                "AWS_SPEECH_ACCESS_KEY_ID and AWS_SPEECH_SECRET_ACCESS_KEY "
                "are required for aws:transcribe"
            )
        options = {
            "region": "ap-northeast-2",
            "language": language,
            "vocabulary_name": None,  # no custom vocabulary
            "identify_language": False,
            "identify_multiple_languages": False,
            "backchannel_words": DEFAULT_BACKCHANNEL_WORDS,
            "confidence_threshold": 0.2,
            "credentials": aws.stt.Credentials(
                access_key_id=access_key_id,
                secret_access_key=secret_access_key,
            ),
        }
        return FilteredAWSSTT(**options)

    if provider == "elevenlabs":
        options = {
            "model": model,
            "language_code": language.split("-", maxsplit=1)[0],
        }
        if model == "scribe_v2_realtime":
            # Enable automatic commits on silence; the LiveKit stream stays open.
            options["server_vad"] = {}
        if custom_vocabulary is not None:
            options["keyterms"] = custom_vocabulary
        return elevenlabs.STT(**options)

    if provider == "google":
        if model == GOOGLE_VERTEX_GEMINI_LIVE_PREVIEW_MODEL:
            options = {
                "model": model,
                "language": language,
                "vertexai": True,
                "credentials_path": os.getenv("GOOGLE_APPLICATION_CREDENTIALS"),
                "location": os.getenv("GOOGLE_CLOUD_LOCATION", "global"),
            }
            if custom_vocabulary is not None:
                options["custom_vocabulary"] = custom_vocabulary
            return GeminiSTT(**options)

        return google.STT(
            languages=language,
            detect_language=False,
            model=model,
            use_streaming=True,
        )

    raise AssertionError(f"Unhandled STT provider: {provider}")


def bind_agent_stt_session(agent_stt: object, session: object) -> None:
    """Bind session-aware filters when the selected STT implementation supports it."""
    bind_session = getattr(agent_stt, "bind_session", None)
    if callable(bind_session):
        bind_session(session)
