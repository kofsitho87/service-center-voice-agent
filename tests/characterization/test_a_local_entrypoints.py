"""Local console entrypoints: default fixture and AgentsData wiring."""

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock, Mock

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"


def _load_entrypoint(file_name: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, SRC / file_name)
    module = importlib.util.module_from_spec(spec)
    original_environment = os.environ.copy()
    try:
        spec.loader.exec_module(module)
    finally:
        os.environ.clear()
        os.environ.update(original_environment)
    return module


agent_cli_text = _load_entrypoint("agent-cli-text.py", "local_agent_cli_text")


class _FakeSession:
    instances: ClassVar[list["_FakeSession"]] = []

    def __class_getitem__(cls, item):
        return cls

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.history = SimpleNamespace(items=[])
        self._recorded_events = []
        self.start = AsyncMock(side_effect=self._capture_agents_data)
        self.agents_data_at_start = None
        _FakeSession.instances.append(self)

    async def _capture_agents_data(self, **kwargs):
        self.agents_data_at_start = self.kwargs["userdata"].agents_data


@pytest.mark.asyncio
async def test_text_console_wires_agents_data_like_the_server(monkeypatch):
    single_agent = object()
    create = AsyncMock(return_value=single_agent)
    monkeypatch.setattr(agent_cli_text, "SingleAgent", SimpleNamespace(create=create))
    monkeypatch.setattr(agent_cli_text, "AgentSession", _FakeSession)
    monkeypatch.setattr(agent_cli_text, "build_agent_llm", Mock())
    monkeypatch.setattr(agent_cli_text, "register_session_event_handlers", Mock())
    monkeypatch.setattr(agent_cli_text, "load_supplement_prompt", AsyncMock())
    ctx = SimpleNamespace(
        room=SimpleNamespace(
            name="room-1",
            local_participant=SimpleNamespace(set_attributes=AsyncMock()),
        ),
        add_shutdown_callback=Mock(),
        proc=SimpleNamespace(userdata={"vad": None}),
    )

    await agent_cli_text.agent_session_start(ctx, {"recipient_data": {}})

    session = _FakeSession.instances[-1]
    call_session_data = session.kwargs["userdata"]
    create.assert_awaited_once_with(call_session_data=call_session_data)
    assert call_session_data.agents_data.agents == {"single_agent": single_agent}
    assert session.agents_data_at_start is call_session_data.agents_data
    assert session.start.await_args.kwargs["agent"] is single_agent
