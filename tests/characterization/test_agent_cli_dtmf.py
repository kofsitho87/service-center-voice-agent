"""Local CLI configuration must be safe before any Room connection is made."""

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

spec = importlib.util.spec_from_file_location(
    "local_agent_cli", Path(__file__).parents[2] / "src/agent-cli.py"
)
agent_cli = importlib.util.module_from_spec(spec)
_original_environment = os.environ.copy()
try:
    spec.loader.exec_module(agent_cli)
finally:
    os.environ.clear()
    os.environ.update(_original_environment)


def test_default_fixture_exists_and_yaml_override_is_used(monkeypatch, tmp_path):
    monkeypatch.delenv("DIAL_INFO_FILE", raising=False)
    monkeypatch.delenv("DIAL_INFO_JSON", raising=False)
    assert agent_cli.load_dev_dial_info()["flow_config"]
    fixture = tmp_path / "fixture.yaml"
    fixture.write_text("recipient_data: {}\nflow_config: {}\n")
    monkeypatch.setenv("DIAL_INFO_FILE", str(fixture))
    assert agent_cli.load_dev_dial_info() == {
        "recipient_data": {},
        "flow_config": {},
    }


@pytest.mark.asyncio
async def test_default_missing_identity_fails_before_connect(monkeypatch):
    monkeypatch.delenv("TEST_CALLER_IDENTITY", raising=False)
    ctx = SimpleNamespace(room=SimpleNamespace(name="test-room"), connect=AsyncMock())
    with pytest.raises(ValueError, match="TEST_CALLER_IDENTITY"):
        await agent_cli.entrypoint(ctx)
    ctx.connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_caller_identity_replaces_fixture_identity(monkeypatch):
    monkeypatch.setenv("TEST_CALLER_IDENTITY", "test-caller")
    ctx = SimpleNamespace(room=SimpleNamespace(name="test-room"), connect=AsyncMock())
    start = AsyncMock()
    monkeypatch.setattr(agent_cli, "agent_session_start", start)
    await agent_cli.entrypoint(ctx)
    ctx.connect.assert_awaited_once()
    assert start.await_args.args[1]["recipient_data"]["sip_identity"] == "test-caller"


@pytest.mark.asyncio
async def test_stale_transport_environment_cannot_disable_caller_requirement(
    monkeypatch,
):
    for name in ("DTMF_SOURCE", "GREETING_DTMF_SOURCE", "VERIFICATION_CODE_DTMF_SOURCE"):
        monkeypatch.setenv(name, "sip")
    monkeypatch.delenv("TEST_CALLER_IDENTITY", raising=False)
    ctx = SimpleNamespace(room=SimpleNamespace(name="test-room"), connect=AsyncMock())
    with pytest.raises(ValueError, match="TEST_CALLER_IDENTITY"):
        await agent_cli.entrypoint(ctx)
    ctx.connect.assert_not_awaited()
