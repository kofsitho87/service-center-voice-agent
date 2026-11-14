"""Async Redis client singleton for warm-transfer queue + other shared state.

Driven by environment variables:
- REDIS_ADDR: "<host>:<port>". When unset, an in-process fakeredis server is
  used, so the warm-transfer queue runs without Redis.
- REDIS_USE_TLS: "true" / "false" (default "false")
- REDIS_USERNAME / REDIS_PASSWORD: optional ACL auth (unset = legacy no-auth default user)
"""

from __future__ import annotations

import asyncio
import os

import redis.asyncio as aioredis

_client: aioredis.Redis | None = None
_lock = asyncio.Lock()


def _parse_env() -> tuple[str, int, bool, str | None, str | None]:
    addr = os.getenv("REDIS_ADDR", "localhost:6379").strip()
    if ":" in addr:
        host, port_str = addr.rsplit(":", 1)
        port = int(port_str)
    else:
        host, port = addr, 6379
    use_tls = os.getenv("REDIS_USE_TLS", "false").strip().lower() == "true"
    username = os.getenv("REDIS_USERNAME", "").strip() or None
    password = os.getenv("REDIS_PASSWORD", "").strip() or None
    return host, port, use_tls, username, password


def _build_client(
    host: str,
    port: int,
    use_tls: bool,
    username: str | None,
    password: str | None,
) -> aioredis.Redis:
    return aioredis.Redis(
        host=host,
        port=port,
        ssl=use_tls,
        username=username,
        password=password,
        decode_responses=True,
        socket_timeout=5.0,
        socket_connect_timeout=5.0,
    )


def _build_in_process_client() -> aioredis.Redis:
    from fakeredis import aioredis as fake_aioredis

    return fake_aioredis.FakeRedis(decode_responses=True)


async def get_client() -> aioredis.Redis:
    global _client
    if _client is not None:
        return _client
    async with _lock:
        if _client is None:
            if not os.getenv("REDIS_ADDR", "").strip():
                _client = _build_in_process_client()
            else:
                host, port, use_tls, username, password = _parse_env()
                _client = _build_client(host, port, use_tls, username, password)
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
