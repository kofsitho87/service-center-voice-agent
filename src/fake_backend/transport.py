"""HTTPX transport that serves fake hosts in-process and blocks everything else.

``utils.http_client.create_async_http_client`` installs this transport on every
client it builds, which covers every business HTTP call in the project. Model
providers (LLM/STT/TTS) use their own SDK clients and are unaffected.
"""

from __future__ import annotations

import asyncio

import httpx
from loguru import logger

from . import app, config


class OutboundRequestBlocked(httpx.ConnectError):
    """Raised for any request to a host that is not a fake demo service."""


class FakeBackendTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host not in config.FAKE_HOSTS:
            logger.bind(
                event="OUTBOUND_HTTP_BLOCKED",
                host=request.url.host,
                method=request.method,
            ).error("Blocked outbound HTTP request to a non-fake host")
            raise OutboundRequestBlocked(
                f"Outbound HTTP to {request.url.host!r} is blocked in this demo",
                request=request,
            )
        await request.aread()
        # Handlers touch SQLite/JSONL; keep that blocking I/O off the agent loop.
        response = await asyncio.to_thread(app.dispatch, request)
        response.request = request
        return response
