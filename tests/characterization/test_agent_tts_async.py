"""Google credential loading must leave audio processing's loop available."""

import asyncio
import threading
from unittest.mock import Mock

import pytest
from google.auth.credentials import AnonymousCredentials

from utils import agent_tts


@pytest.mark.asyncio
async def test_google_adc_load_does_not_block_agent_loop(monkeypatch):
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    started = asyncio.Event()
    release = threading.Event()
    credentials = AnonymousCredentials()
    client = object()
    auth_threads = []
    plugin_options = []

    def load_adc(*, scopes):
        assert scopes == ["https://www.googleapis.com/auth/cloud-platform"]
        auth_threads.append(threading.get_ident())
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=5), "Test did not release credential loading"
        return credentials, "test-project"

    def build_google(**options):
        assert asyncio.get_running_loop() is loop
        assert threading.get_ident() == loop_thread
        plugin_options.append(options)
        return client

    monkeypatch.setattr(agent_tts.google_auth, "default", load_adc)
    monkeypatch.setattr(agent_tts.google, "TTS", build_google)
    monkeypatch.setenv(agent_tts.AGENT_TTS_MODEL_ENV, "google:chirp_3")
    construction = asyncio.create_task(
        agent_tts.build_agent_tts_async(
            language="ko-KR", voice="Korean-voice", speaking_rate=1.1
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=3)
        await asyncio.wait_for(asyncio.sleep(0), timeout=3)
        assert not construction.done()
        assert not plugin_options
        assert len(auth_threads) == 1
        assert auth_threads[0] != loop_thread
    finally:
        release.set()
        assert await asyncio.wait_for(construction, timeout=3) is client
    assert plugin_options == [
        {
            "voice_name": "Korean-voice",
            "model_name": "chirp_3",
            "language": "ko-KR",
            "use_streaming": True,
            "speaking_rate": 1.1,
            "credentials": credentials,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "expected_voice"),
    [("ko-KR", "configured-voice"), ("en-US", "Aoede")],
)
async def test_explicit_google_credentials_preserve_voice_and_rate(
    monkeypatch, language, expected_voice
):
    def forbidden(*args, **kwargs):
        pytest.fail("Explicit credentials must skip ADC loading")

    credentials = AnonymousCredentials()
    constructor = Mock(return_value=object())
    monkeypatch.setattr(agent_tts.google_auth, "default", forbidden)
    monkeypatch.setattr(agent_tts.google, "TTS", constructor)
    result = await agent_tts.build_agent_tts_async(
        "google:custom-model",
        language=language,
        voice="configured-voice",
        speed=0.9,
        speaking_rate=1.2,
        non_ko_google_voice="Aoede",
        google_credentials=credentials,
    )
    assert result is constructor.return_value
    constructor.assert_called_once_with(
        voice_name=expected_voice,
        model_name="custom-model",
        language=language,
        use_streaming=True,
        speaking_rate=1.2,
        credentials=credentials,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["aws", "cartesia", "elevenlabs", "fishaudio"])
async def test_other_providers_stay_on_agent_loop_without_google_auth(
    monkeypatch, provider
):
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    calls = []
    client = object()

    def forbidden(*args, **kwargs):
        pytest.fail("Other TTS providers must not load Google credentials")

    def build_sync(selection, **options):
        assert asyncio.get_running_loop() is loop
        assert threading.get_ident() == loop_thread
        calls.append((selection, options))
        return client

    monkeypatch.setattr(agent_tts.google_auth, "default", forbidden)
    monkeypatch.setattr(agent_tts, "build_agent_tts", build_sync)
    assert (
        await agent_tts.build_agent_tts_async(
            f"{provider}:model", language="ko-KR", voice="configured", speed=1.1
        )
        is client
    )
    assert calls == [
        (
            f"{provider}:model",
            {
                "language": "ko-KR",
                "voice": "configured",
                "speed": 1.1,
                "speaking_rate": None,
                "non_ko_google_voice": None,
                "google_credentials": None,
            },
        )
    ]


@pytest.mark.asyncio
async def test_adc_failure_propagates_before_plugin_creation(monkeypatch):
    error = ValueError("Credential configuration failed")

    def fail(*args, **kwargs):
        raise error

    constructor = Mock()
    monkeypatch.setattr(agent_tts.google_auth, "default", fail)
    monkeypatch.setattr(agent_tts.google, "TTS", constructor)
    with pytest.raises(ValueError) as caught:
        await agent_tts.build_agent_tts_async(
            "google:chirp_3", language="ko-KR", voice="configured"
        )
    assert caught.value is error
    constructor.assert_not_called()


@pytest.mark.asyncio
async def test_google_lazy_grpc_client_uses_supplied_credentials_on_agent_loop(
    monkeypatch,
):
    loop = asyncio.get_running_loop()
    credentials = AnonymousCredentials()
    auth_calls = []

    def load_adc(*, scopes):
        assert threading.get_ident() != loop_thread
        auth_calls.append(scopes)
        return credentials, "test-project"

    loop_thread = threading.get_ident()
    monkeypatch.setattr(agent_tts.google_auth, "default", load_adc)
    plugin = await agent_tts.build_agent_tts_async(
        "google:chirp_3", language="ko-KR", voice="configured"
    )
    assert plugin._client is None

    def forbidden(*args, **kwargs):
        pytest.fail("Lazy gRPC construction must not reload ADC on the agent loop")

    monkeypatch.setattr(agent_tts.google_auth, "default", forbidden)
    client = plugin._ensure_client()
    try:
        assert client.transport._credentials is credentials
        assert client.transport.grpc_channel._loop is loop
        assert auth_calls == [["https://www.googleapis.com/auth/cloud-platform"]]
    finally:
        await client.transport.close()
        await plugin.aclose()


def test_sync_google_builder_preserves_legacy_plugin_defaults(monkeypatch):
    constructor = Mock(return_value=object())
    monkeypatch.setattr(agent_tts.google, "TTS", constructor)
    agent_tts.build_agent_tts("google:chirp_3", language="ko-KR", voice="configured")
    assert "credentials" not in constructor.call_args.kwargs
    assert constructor.call_args.kwargs["speaking_rate"] == 1.0
