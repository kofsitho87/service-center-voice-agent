"""Warm-Transfer Trunk Allocation API client with HMAC signing."""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

import httpx

from fake_backend.config import FAKE_TRUNK_BASEURL, FAKE_TRUNK_HMAC_SECRET
from utils.http_client import create_async_http_client


class TrunkStatus(Enum):
    AVAILABLE = "available"  # 200
    BUSY = "busy"  # 503
    FATAL = "fatal"  # 401 / 404
    NETWORK_ERROR = "network"  # transport error


@dataclass
class TrunkCheckResult:
    status: TrunkStatus
    status_code: int | None = None
    elapsed_ms: int | None = None
    error: str | None = None


class TrunkAllocationClient:
    """Client for GET /api/v1/warm-transfer/companies/{id}/available-trunk."""

    def __init__(self, http: httpx.AsyncClient, *, hmac_secret: str) -> None:
        self._http = http
        self._secret = hmac_secret.encode("utf-8")

    @classmethod
    async def from_env_async(cls) -> TrunkAllocationClient:
        """Load settings from the environment, constructing HTTPX off the agent loop."""
        # Defaults target the in-process fake trunk API, which
        # verifies this same HMAC contract.
        base = os.getenv("WARM_TRANSFER_API_BASE_URL", FAKE_TRUNK_BASEURL).rstrip("/")
        secret = os.getenv("WARM_TRANSFER_HMAC_SECRET", FAKE_TRUNK_HMAC_SECRET)
        http = await create_async_http_client(base_url=base, timeout=5.0)
        return cls(http, hmac_secret=secret)

    def _sign(self, method: str, path_with_query: str) -> tuple[str, str]:
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = f"{ts}\n{method}\n{path_with_query}".encode()
        sig = hmac.new(self._secret, payload, hashlib.sha256).hexdigest()
        return ts, sig

    async def check(self, company_id: int) -> TrunkCheckResult:
        path_with_query = (
            f"/api/v1/warm-transfer/companies/{company_id}/available-trunk"
        )
        ts, sig = self._sign("GET", path_with_query)
        started = time.monotonic()
        try:
            resp = await self._http.get(
                path_with_query,
                headers={"X-VB-Timestamp": ts, "X-VB-Signature": sig},
            )
        except httpx.HTTPError as exc:
            return TrunkCheckResult(
                status=TrunkStatus.NETWORK_ERROR,
                error=str(exc),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

        elapsed = int((time.monotonic() - started) * 1000)
        code = resp.status_code
        if code == 200:
            status = TrunkStatus.AVAILABLE
        elif code == 503:
            status = TrunkStatus.BUSY
        else:
            # 401 / 404 and any other unexpected status are not retriable.
            status = TrunkStatus.FATAL
        return TrunkCheckResult(status=status, status_code=code, elapsed_ms=elapsed)

    async def aclose(self) -> None:
        await self._http.aclose()
