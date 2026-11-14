"""Agent TTS selection helpers for local and production sessions."""

from __future__ import annotations

import asyncio
import os

from google import auth as google_auth
from google.auth.credentials import Credentials
from livekit.plugins import aws, cartesia, elevenlabs, fishaudio, google

AGENT_TTS_MODEL_ENV = "AGENT_TTS_MODEL"
DEFAULT_AGENT_TTS_MODEL = "google:chirp_3"
AWS_POLLY_DEFAULT_ENGINE = "neural"
AWS_POLLY_ENGINES = {"generative", "long-form", "neural", "standard"}
SUPPORTED_AGENT_TTS_PROVIDERS = {
    "aws",
    "cartesia",
    "elevenlabs",
    "fishaudio",
    "google",
}


def resolve_agent_tts_model(
    explicit: str | None = None,
    *,
    default: str = DEFAULT_AGENT_TTS_MODEL,
) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    if env_model := os.getenv(AGENT_TTS_MODEL_ENV, "").strip():
        return env_model
    return default


def parse_agent_tts_model(value: str) -> tuple[str, str]:
    provider, separator, model = value.partition(":")
    provider = provider.strip().lower()
    model = model.strip()
    if not separator or not provider or not model:
        raise ValueError(f"Invalid TTS model selection: {value!r}")
    if provider not in SUPPORTED_AGENT_TTS_PROVIDERS:
        raise ValueError(f"Unsupported TTS provider: {provider}")
    return provider, model


def build_agent_tts(
    selection: str | None = None,
    *,
    language: str,
    voice: str,
    speed: float | None = None,
    speaking_rate: float | None = None,
    non_ko_google_voice: str | None = None,
    google_credentials: Credentials | None = None,
):
    """Build TTS synchronously; use build_agent_tts_async on the agent loop."""
    provider, model = parse_agent_tts_model(resolve_agent_tts_model(selection))
    rate = speaking_rate if speaking_rate is not None else speed
    if rate is None:
        rate = 1.0
    if provider == "aws":
        requested_engine = model.lower()
        if requested_engine == "polly":
            speech_engine = (
                os.getenv("AWS_POLLY_ENGINE", "").strip().lower()
                or AWS_POLLY_DEFAULT_ENGINE
            )
        else:
            speech_engine = requested_engine
        if speech_engine not in AWS_POLLY_ENGINES:
            supported = ", ".join(sorted(AWS_POLLY_ENGINES))
            raise ValueError(
                f"Unsupported AWS Polly speech engine={speech_engine!r}. "
                f"Supported engines: {supported}"
            )

        region = (
            os.getenv("AWS_POLLY_REGION", "").strip()
            or os.getenv("AWS_REGION", "").strip()
            or os.getenv("AWS_DEFAULT_REGION", "").strip()
        )
        access_key_id = os.getenv("AWS_SPEECH_ACCESS_KEY_ID", "").strip()
        secret_access_key = os.getenv("AWS_SPEECH_SECRET_ACCESS_KEY", "").strip()
        if not access_key_id or not secret_access_key:
            raise ValueError(
                "AWS_SPEECH_ACCESS_KEY_ID and AWS_SPEECH_SECRET_ACCESS_KEY "
                "are required for AWS Polly TTS"
            )
        options = {
            "voice": voice,
            "language": language,
            "speech_engine": speech_engine,
            "api_key": access_key_id,
            "api_secret": secret_access_key,
        }
        if region:
            options["region"] = region
        return aws.TTS(**options)
    if provider == "fishaudio":
        return fishaudio.TTS(
            model=model,
            voice_id=voice,
            speed=float(rate),
            latency_mode="low",
            output_format="pcm",
        )

    google_voice = (
        non_ko_google_voice if non_ko_google_voice and language != "ko-KR" else voice
    )
    if provider == "google":
        credential_options = (
            {"credentials": google_credentials}
            if google_credentials is not None
            else {}
        )
        return google.TTS(
            voice_name=google_voice,
            model_name=model,
            language=language,
            use_streaming=True,
            speaking_rate=rate,
            **credential_options,
        )

    if provider == "elevenlabs":
        return elevenlabs.TTS(
            voice_id=voice,
            model=model,
            encoding="pcm_24000",
            language=language.split("-", maxsplit=1)[0],
            voice_settings=elevenlabs.VoiceSettings(
                stability=0.5,
                similarity_boost=0.75,
                speed=rate,
            ),
        )

    if provider == "cartesia":
        return cartesia.TTS(
            model=model,
            voice=voice,
            language=language.split("-", maxsplit=1)[0],
            speed=float(rate),
        )

    raise AssertionError(f"Unhandled TTS provider: {provider}")


async def build_agent_tts_async(
    selection: str | None = None,
    *,
    language: str,
    voice: str,
    speed: float | None = None,
    speaking_rate: float | None = None,
    non_ko_google_voice: str | None = None,
    google_credentials: Credentials | None = None,
):
    """Load Google credentials off-loop while keeping gRPC on the agent loop."""
    selection = resolve_agent_tts_model(selection)
    provider, _ = parse_agent_tts_model(selection)
    if provider == "google" and google_credentials is None:
        # ADC can read a service-account file and parse its RSA key. Supplying
        # credentials prevents the lazy gRPC client from doing this on first audio.
        google_credentials, _ = await asyncio.to_thread(
            google_auth.default,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )

    # Google TTS's lazy gRPC channel, and other plugins, need this running loop.
    return build_agent_tts(
        selection,
        language=language,
        voice=voice,
        speed=speed,
        speaking_rate=speaking_rate,
        non_ko_google_voice=non_ko_google_voice,
        google_credentials=google_credentials,
    )
