"""Configuration for the in-process fake backend (demo-only).

Every external business system (reservation/customer API, call-context API, SMS,
trunk allocation, SIP status webhook) is served by a fake that lives in this
process. The HTTP clients are real; ``utils.http_client`` routes their requests
to ``fake_backend.transport``.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = Path(os.getenv("DEMO_FIXTURES_DIR", str(PROJECT_ROOT / "fixtures")))
ARTIFACTS_DIR = Path(os.getenv("DEMO_ARTIFACTS_DIR", str(PROJECT_ROOT / "artifacts")))

# Fake service hosts. The `.invalid` TLD (RFC 2606) can never resolve, so even
# if the transport were bypassed these URLs could not reach a real server.
RESERVATION_HOST = "booking.fake.invalid"
CONTEXT_HOST = "context.fake.invalid"
TRUNK_HOST = "trunk.fake.invalid"
SIP_STATUS_HOST = "sip-status.fake.invalid"

FAKE_RESERVATION_BASEURL = f"http://{RESERVATION_HOST}"
FAKE_CONTEXT_BASEURL = f"http://{CONTEXT_HOST}"
FAKE_TRUNK_BASEURL = f"http://{TRUNK_HOST}"
FAKE_SIP_STATUS_BASEURL = f"http://{SIP_STATUS_HOST}"

FAKE_HOSTS = frozenset({RESERVATION_HOST, CONTEXT_HOST, TRUNK_HOST, SIP_STATUS_HOST})

# Shared secret for the fake trunk API's HMAC check. It protects nothing real;
# it exists so the client's HMAC signing code is still exercised.
FAKE_TRUNK_HMAC_SECRET = "fake-local-trunk-secret"

BUSINESS_TZ = ZoneInfo("Asia/Seoul")


def db_path() -> Path:
    return Path(os.getenv("DEMO_DB_PATH", str(ARTIFACTS_DIR / "demo.sqlite3")))


def outbox_dir() -> Path:
    return Path(os.getenv("DEMO_OUTBOX_DIR", str(ARTIFACTS_DIR / "outbox")))


def now() -> datetime:
    """Business-local "now"; ``DEMO_NOW`` (ISO 8601) pins it for tests/demos."""
    pinned = os.getenv("DEMO_NOW", "").strip()
    if pinned:
        value = datetime.fromisoformat(pinned)
        if value.tzinfo is None:
            value = value.replace(tzinfo=BUSINESS_TZ)
        return value.astimezone(BUSINESS_TZ)
    return datetime.now(BUSINESS_TZ)
