"""Construct HTTPX clients without blocking the agent's event loop on TLS setup."""

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


async def _close_unused_client(construction: asyncio.Task[httpx.AsyncClient]) -> None:
    if construction.cancelled():
        return
    try:
        client = construction.result()
        await client.aclose()
    except Exception:
        logger.warning(
            "Failed to clean up cancelled HTTP client initialization", exc_info=True
        )


async def create_async_http_client(**options: Any) -> httpx.AsyncClient:
    """Build off-loop; use requests and close the client on the caller's loop.

    Forward options unchanged so HTTPX retains its TLS, environment and proxy
    handling. Each caller owns its client, including its context-manager lifetime.

    Demo boundary: every client gets the in-process fake backend transport.
    It serves the fake reservation/context/trunk/SIP-status hosts and blocks all
    other hosts, so no business request can leave the process.
    """
    from fake_backend.transport import FakeBackendTransport

    options["transport"] = FakeBackendTransport()
    construction = asyncio.create_task(asyncio.to_thread(httpx.AsyncClient, **options))
    try:
        return await asyncio.shield(construction)
    except asyncio.CancelledError:
        # Cancelling a thread cannot stop construction. Close its eventual client
        # on the original loop if the caller disconnects while TLS is being set up.
        construction.add_done_callback(
            lambda task: asyncio.create_task(_close_unused_client(task))
        )
        raise
