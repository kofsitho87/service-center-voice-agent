"""LLM initialization must leave the call's event loop available."""

import asyncio
import threading

import pytest

from utils import agent_llm


@pytest.mark.asyncio
async def test_google_constructor_allows_loop_progress(monkeypatch):
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    started = asyncio.Event()
    release = threading.Event()
    client = object()
    constructor_threads = []

    def build_google(model):
        constructor_threads.append(threading.get_ident())
        assert model == "gemini-2.5-flash-lite"
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=5), "Test did not release the constructor"
        return client

    monkeypatch.setattr(agent_llm, "_build_google_llm", build_google)
    construction = asyncio.create_task(
        agent_llm.build_agent_llm_async("google:gemini-2.5-flash-lite")
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=3)
        heartbeat = asyncio.Event()

        async def advance_loop():
            await asyncio.sleep(0)
            heartbeat.set()

        await asyncio.wait_for(advance_loop(), timeout=3)
        assert heartbeat.is_set()
        assert not construction.done()
        assert len(constructor_threads) == 1
        assert constructor_threads[0] != loop_thread
    finally:
        release.set()
        assert await asyncio.wait_for(construction, timeout=3) is client


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["aws", "openai"])
async def test_other_providers_construct_on_agent_loop(monkeypatch, provider):
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    calls = []
    client = object()

    def build_other(model, **options):
        assert asyncio.get_running_loop() is loop
        assert threading.get_ident() == loop_thread
        calls.append((model, options))
        return client

    monkeypatch.setattr(agent_llm, f"_build_{provider}_llm", build_other)
    monkeypatch.setenv(agent_llm.AGENT_LLM_MODEL_ENV, f"{provider}:test-model")
    assert await agent_llm.build_agent_llm_async(parallel_tool_calls=False) is client
    expected_options = {"parallel_tool_calls": False} if provider == "openai" else {}
    assert calls == [("test-model", expected_options)]


@pytest.mark.asyncio
async def test_google_constructor_error_propagates(monkeypatch):
    error = ValueError("Google configuration failed")

    def fail(model):
        raise error

    monkeypatch.setattr(agent_llm, "_build_google_llm", fail)
    with pytest.raises(ValueError) as caught:
        await agent_llm.build_agent_llm_async("google:gemini-2.5-flash-lite")
    assert caught.value is error


@pytest.mark.asyncio
async def test_cancelled_google_constructor_closes_client_on_agent_loop(monkeypatch):
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    closed = asyncio.Event()
    release = threading.Event()
    close_calls = []

    class Client:
        async def aclose(self):
            close_calls.append(asyncio.get_running_loop())
            closed.set()

    def build_google(model):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=5), "Test did not release the constructor"
        return Client()

    monkeypatch.setattr(agent_llm, "_build_google_llm", build_google)
    construction = asyncio.create_task(
        agent_llm.build_agent_llm_async("google:gemini-2.5-flash-lite")
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=3)
        construction.cancel()
        with pytest.raises(asyncio.CancelledError):
            await construction
        assert not closed.is_set()
    finally:
        release.set()
    await asyncio.wait_for(closed.wait(), timeout=3)
    assert close_calls == [loop]


@pytest.mark.asyncio
@pytest.mark.parametrize("config_model", [None, "", "openai:other", "google:other"])
async def test_production_fallback_preserves_models_and_loop(monkeypatch, config_model):
    loop = asyncio.get_running_loop()
    builds = []
    adapters = []
    monkeypatch.setenv(agent_llm.AGENT_LLM_MODEL_ENV, "solar:llm")

    def build_sync(selection, *, parallel_tool_calls):
        assert asyncio.get_running_loop() is loop
        builds.append((selection, parallel_tool_calls))
        return selection

    def build_google(model):
        builds.append((f"google:{model}", False))
        return f"google:{model}"

    def fallback(models):
        assert asyncio.get_running_loop() is loop
        adapters.append(models)
        return models

    monkeypatch.setattr(agent_llm, "build_agent_llm", build_sync)
    monkeypatch.setattr(agent_llm, "_build_google_llm", build_google)
    monkeypatch.setattr(agent_llm.llm, "FallbackAdapter", fallback)

    result = await agent_llm.build_production_agent_llm_async(config_model)
    expected = ["openai:gpt-5.6-luna", "google:gemini-2.5-flash-lite"]
    assert result == expected
    assert builds == [(model, False) for model in expected]
    assert adapters == [expected]
    builds.clear()
    assert agent_llm.build_production_agent_llm(config_model) == result
    assert builds == [(model, False) for model in expected]


@pytest.mark.asyncio
async def test_production_private_model_has_no_external_fallback(monkeypatch):
    # PRIVATE_AGENT_LLM_PROVIDERS is empty, so mark a public provider as
    # private to exercise the no-external-fallback guard.
    provider = "aws"
    monkeypatch.setattr(
        agent_llm, "PRIVATE_AGENT_LLM_PROVIDERS", frozenset({provider})
    )
    calls = []
    client = object()

    def build_sync(selection, *, parallel_tool_calls):
        calls.append((selection, parallel_tool_calls))
        return client

    def forbidden(*args, **kwargs):
        pytest.fail("Private model must not build external clients or fallback")

    monkeypatch.setattr(agent_llm, "build_agent_llm", build_sync)
    monkeypatch.setattr(agent_llm, "_build_google_llm", forbidden)
    monkeypatch.setattr(agent_llm.llm, "FallbackAdapter", forbidden)
    selection = f"{provider}:llm"
    assert await agent_llm.build_production_agent_llm_async(f" {selection} ") is client
    assert calls == [(selection, False)]


@pytest.mark.asyncio
async def test_invalid_production_selection_fails_before_construction(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid selection must be rejected before construction")

    monkeypatch.setattr(agent_llm, "build_agent_llm", forbidden)
    with pytest.raises(ValueError, match="Unsupported LLM provider"):
        await agent_llm.build_production_agent_llm_async("invalid:model")
