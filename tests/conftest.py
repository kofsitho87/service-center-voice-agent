"""Shared test setup: isolated demo data, a pinned clock, and no network."""

from __future__ import annotations

import socket

import pytest

# Monday 2026-11-16 09:30 KST: a business day with a full week ahead.
FIXED_NOW = "2026-11-16T09:30:00+09:00"

_real_connect = socket.socket.connect


def _guarded_connect(self, address):  # pragma: no cover - only hit on violation
    if self.family == socket.AF_UNIX:
        return _real_connect(self, address)
    host = address[0] if isinstance(address, tuple) else address
    if host in {"127.0.0.1", "::1", "localhost"}:
        return _real_connect(self, address)
    raise RuntimeError(f"Network access attempted during offline tests: {address!r}")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)


@pytest.fixture(autouse=True)
def demo_env(tmp_path, monkeypatch):
    from fake_backend import faults

    monkeypatch.setenv("DEMO_NOW", FIXED_NOW)
    monkeypatch.setenv("DEMO_DB_PATH", str(tmp_path / "demo.sqlite3"))
    monkeypatch.setenv("DEMO_OUTBOX_DIR", str(tmp_path / "outbox"))
    monkeypatch.setenv("DEMO_EVENTS_DIR", str(tmp_path / "events"))
    monkeypatch.delenv("DEMO_FAULTS", raising=False)
    faults.reset()
    yield tmp_path
    faults.reset()
