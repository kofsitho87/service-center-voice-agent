"""Shared entrypoint bootstrap helpers (src/utils/entrypoint_bootstrap.py)."""

import warnings
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from loguru import logger

import utils.entrypoint_bootstrap as bootstrap
from data import AgentsData, LeaveMessageFlowConfig


@pytest.fixture
def log_records():
    records: list[dict] = []
    sink_id = logger.add(lambda message: records.append(message.record), level="DEBUG")
    yield records
    logger.remove(sink_id)


@pytest.mark.parametrize("override", [True, False])
def test_configure_process_environment(monkeypatch, override):
    load_dotenv = Mock()
    monkeypatch.setattr(bootstrap, "load_dotenv", load_dotenv)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("TRANSFORMERS_NO_ADVISORY_WARNINGS", raising=False)

    with warnings.catch_warnings():
        bootstrap.configure_process_environment(dotenv_override=override)
        messages = [f[1].pattern for f in warnings.filters if f[0] == "ignore" and f[1]]
        modules = [f[3].pattern for f in warnings.filters if f[0] == "ignore" and f[3]]

    load_dotenv.assert_called_once_with(override=override)
    assert bootstrap.os.environ["TRANSFORMERS_NO_ADVISORY_WARNINGS"] == "1"
    # No service-account default; the variable stays unset.
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in bootstrap.os.environ
    assert "pydantic._migration" in modules
    assert "Api key is used with an insecure connection" in messages
    assert ".*Qdrant client version.*is incompatible with server version.*" in (
        messages
    )
    assert ".*PyTorch.*TensorFlow.*Flax.*have been found.*" in messages


def test_configure_process_environment_keeps_explicit_credentials(monkeypatch):
    monkeypatch.setattr(bootstrap, "load_dotenv", Mock())
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/explicit.json")

    with warnings.catch_warnings():
        bootstrap.configure_process_environment(dotenv_override=False)

    assert bootstrap.os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == "/explicit.json"


def test_credentials_default_applies_after_dotenv(monkeypatch):
    def load_dotenv(override):
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/from-dotenv.json")

    monkeypatch.setattr(bootstrap, "load_dotenv", load_dotenv)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    with warnings.catch_warnings():
        bootstrap.configure_process_environment(dotenv_override=True)

    assert bootstrap.os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == (
        "/from-dotenv.json"
    )


def test_kst_now_naive_iso_is_naive_seoul_wall_clock():
    value = datetime.fromisoformat(bootstrap.kst_now_naive_iso())
    now = datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)

    assert value.tzinfo is None
    assert abs(now - value) < timedelta(seconds=5)


def _call_session_data(client_id):
    return SimpleNamespace(
        business_data=SimpleNamespace(client_id=client_id) if client_id else None,
        room_name="room-1",
        leave_message_flow_config=LeaveMessageFlowConfig(
            additional_instructions="configured"
        ),
    )


@pytest.mark.asyncio
async def test_load_supplement_prompt_skips_without_client_id(monkeypatch, log_records):
    fetch = AsyncMock()
    monkeypatch.setattr("external_apis.fastapi_app_api.get_supplement_prompt", fetch)
    data = _call_session_data(None)

    await bootstrap.load_supplement_prompt(data)

    fetch.assert_not_awaited()
    assert data.leave_message_flow_config.additional_instructions == "configured"
    assert [r["extra"] for r in log_records] == [
        {"event": "SUPPLEMENT_PROMPT_FETCH_SKIPPED", "room_name": "room-1"}
    ]


@pytest.mark.asyncio
async def test_load_supplement_prompt_keeps_prompt_on_failure(monkeypatch, log_records):
    fetch = AsyncMock(side_effect=RuntimeError("down"))
    monkeypatch.setattr("external_apis.fastapi_app_api.get_supplement_prompt", fetch)
    data = _call_session_data(7)

    await bootstrap.load_supplement_prompt(data)

    fetch.assert_awaited_once_with(7)
    assert data.leave_message_flow_config.additional_instructions == "configured"
    assert [r["extra"] for r in log_records] == [
        {
            "event": "SUPPLEMENT_PROMPT_FETCH_FAILED",
            "client_id": 7,
            "room_name": "room-1",
        }
    ]


@pytest.mark.asyncio
async def test_load_supplement_prompt_injects_prompt(monkeypatch, log_records):
    fetch = AsyncMock(
        return_value=SimpleNamespace(prompt_text="이름도 받으세요", site_id="s-1")
    )
    monkeypatch.setattr("external_apis.fastapi_app_api.get_supplement_prompt", fetch)
    data = _call_session_data(7)

    await bootstrap.load_supplement_prompt(data)

    assert data.leave_message_flow_config.additional_instructions == "이름도 받으세요"
    assert [r["extra"] for r in log_records] == [
        {
            "event": "SUPPLEMENT_PROMPT_FETCHED",
            "client_id": 7,
            "site_id": "s-1",
            "room_name": "room-1",
        }
    ]


def test_build_agents_data_registers_single_agent():
    single_agent = object()

    agents_data = bootstrap.build_agents_data(single_agent=single_agent)

    assert isinstance(agents_data, AgentsData)
    assert agents_data.agents == {"single_agent": single_agent}
    assert agents_data.user_request is None
    assert agents_data.dtmf_tool is None
