"""TLS initialization must not interrupt the agent's event loop."""

import asyncio
import ssl
import threading
from unittest.mock import Mock

import httpx
import pytest

from agents import base_agent
from external_apis import context_api
from fake_backend import transport as fake_transport_module
from fake_backend.transport import FakeBackendTransport
from services import trunk_allocation_client
from utils import http_client


def _use_source_transport(monkeypatch, transport):
    """Bypass the factory's forced FakeBackendTransport.

    ``create_async_http_client`` always installs the in-process fake backend
    transport. To characterize the factory mechanics (off-loop
    construction, HTTPX TLS/env semantics, request/close loop), make the factory
    install ``transport`` instead (``None`` = HTTPX's default transport).
    """
    monkeypatch.setattr(fake_transport_module, "FakeBackendTransport", lambda: transport)


@pytest.mark.asyncio
async def test_constructor_runs_off_loop_and_preserves_options(monkeypatch):
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    started = asyncio.Event()
    release = threading.Event()
    client = object()
    transport = object()
    timeout = httpx.Timeout(3.0)
    options = {
        "base_url": "https://api.test",
        "timeout": timeout,
        "transport": transport,
        "trust_env": False,
        "proxy": "http://proxy.test:8080",
    }
    seen = []

    def construct(**kwargs):
        seen.append((threading.get_ident(), kwargs))
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=5), "Test did not release construction"
        return client

    monkeypatch.setattr(http_client.httpx, "AsyncClient", construct)
    construction = asyncio.create_task(http_client.create_async_http_client(**options))
    try:
        await asyncio.wait_for(started.wait(), timeout=3)
        await asyncio.wait_for(asyncio.sleep(0), timeout=3)
        assert not construction.done()
        assert seen[0][0] != loop_thread
        # Every option is forwarded unchanged except ``transport``,
        # which the factory always replaces with the fake backend transport.
        forwarded = dict(seen[0][1])
        assert isinstance(forwarded.pop("transport"), FakeBackendTransport)
        assert forwarded == {k: v for k, v in options.items() if k != "transport"}
    finally:
        release.set()
        assert await asyncio.wait_for(construction, timeout=3) is client


@pytest.mark.asyncio
async def test_constructor_error_propagates(monkeypatch):
    error = ValueError("Invalid HTTP client configuration")

    def fail(**options):
        raise error

    monkeypatch.setattr(http_client.httpx, "AsyncClient", fail)
    with pytest.raises(ValueError) as caught:
        await http_client.create_async_http_client()
    assert caught.value is error


@pytest.mark.asyncio
async def test_cancelled_construction_closes_late_client_on_original_loop(monkeypatch):
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    closed = asyncio.Event()
    release = threading.Event()
    close_loops = []

    class Client:
        async def aclose(self):
            close_loops.append(asyncio.get_running_loop())
            closed.set()

    def construct(**options):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=5), "Test did not release construction"
        return Client()

    monkeypatch.setattr(http_client.httpx, "AsyncClient", construct)
    construction = asyncio.create_task(http_client.create_async_http_client())
    try:
        await asyncio.wait_for(started.wait(), timeout=3)
        construction.cancel()
        with pytest.raises(asyncio.CancelledError):
            await construction
        assert not closed.is_set()
    finally:
        release.set()
    await asyncio.wait_for(closed.wait(), timeout=3)
    assert close_loops == [loop]


@pytest.mark.asyncio
@pytest.mark.parametrize("trust_env", [True, False])
async def test_real_httpx_preserves_tls_environment_semantics(monkeypatch, trust_env):
    loop_thread = threading.get_ident()
    calls = []
    monkeypatch.setenv("SSL_CERT_FILE", "test-certificates.pem")
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)

    def make_context(*args, **kwargs):
        calls.append((threading.get_ident(), kwargs))
        return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    monkeypatch.setattr(ssl, "create_default_context", make_context)
    _use_source_transport(monkeypatch, None)
    async with await http_client.create_async_http_client(trust_env=trust_env):
        assert calls
        assert all(thread != loop_thread for thread, _ in calls)
        for _, options in calls:
            assert (options.get("cafile") == "test-certificates.pem") is trust_env


@pytest.mark.asyncio
async def test_real_httpx_requests_and_closes_on_original_loop(monkeypatch):
    loop = asyncio.get_running_loop()
    request_loops = []

    async def handle(request):
        request_loops.append(asyncio.get_running_loop())
        return httpx.Response(200, json={"ok": True})

    mock_transport = httpx.MockTransport(handle)
    _use_source_transport(monkeypatch, mock_transport)
    client = await http_client.create_async_http_client(
        base_url="https://api.test", transport=mock_transport, timeout=3.0
    )
    async with client:
        response = await client.get("/check")
        assert response.json() == {"ok": True}
        assert response.request.extensions["timeout"]["connect"] == 3.0
    assert client.is_closed
    assert request_loops == [loop]


@pytest.mark.asyncio
async def test_call_context_uses_off_loop_factory_and_preserves_api_behavior(
    monkeypatch,
):
    requests = []
    timestamps = "2026-10-02T00:00:00Z"

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "id": "test-context",
                    "client_id": 42,
                    "site_id": "test-site",
                    "center_name": "Test Center",
                    "context": "Test context",
                    "content_updated_at": timestamps,
                    "created_at": timestamps,
                    "updated_at": timestamps,
                }
            },
        )

    mock_transport = httpx.MockTransport(handle)
    _use_source_transport(monkeypatch, mock_transport)

    async def factory(**options):
        assert options == {"timeout": 3.0}
        return await http_client.create_async_http_client(
            **options, transport=mock_transport
        )

    monkeypatch.setattr(context_api, "FAQ_API_BASEURL", "https://api.test")
    monkeypatch.setattr(context_api, "create_async_http_client", factory)
    monkeypatch.setattr(context_api, "log_api_response", Mock())
    response = await context_api.get_call_context(42)
    assert response.data.id == "test-context"
    assert response.data.client_id == 42
    assert len(requests) == 1
    assert requests[0].url == "https://api.test/call-context?client_id=42"
    assert requests[0].extensions["timeout"]["connect"] == 3.0


@pytest.mark.asyncio
async def test_trunk_async_factory_preserves_settings(monkeypatch):
    client = object()
    calls = []

    async def factory(**options):
        calls.append(options)
        return client

    monkeypatch.setenv("WARM_TRANSFER_API_BASE_URL", "https://api.test/")
    monkeypatch.setenv("WARM_TRANSFER_HMAC_SECRET", "test-secret")
    monkeypatch.setattr(trunk_allocation_client, "create_async_http_client", factory)
    result = await trunk_allocation_client.TrunkAllocationClient.from_env_async()
    assert result._http is client
    assert result._secret == b"test-secret"
    assert calls == [{"base_url": "https://api.test", "timeout": 5.0}]


@pytest.mark.asyncio
async def test_concurrent_trunk_initialization_creates_only_one_client(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    client = object()
    calls = []

    async def from_env_async():
        calls.append(True)
        started.set()
        await release.wait()
        return client

    monkeypatch.setattr(base_agent, "_trunk_client_singleton", None)
    monkeypatch.setattr(base_agent, "_trunk_client_lock", asyncio.Lock())
    monkeypatch.setattr(
        base_agent.TrunkAllocationClient, "from_env_async", from_env_async
    )
    tasks = [
        asyncio.create_task(base_agent.get_trunk_allocation_client()) for _ in range(2)
    ]
    try:
        await asyncio.wait_for(started.wait(), timeout=3)
        await asyncio.sleep(0)
        assert calls == [True]
    finally:
        release.set()
    results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=3)
    assert results == [client, client]
    assert calls == [True]


@pytest.mark.asyncio
async def test_factory_always_installs_fake_backend_transport():
    """A caller-supplied transport cannot bypass the fake boundary."""
    caller_transport = httpx.MockTransport(lambda request: httpx.Response(200))
    client = await http_client.create_async_http_client(transport=caller_transport)
    async with client:
        assert isinstance(client._transport, FakeBackendTransport)

