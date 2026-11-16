"""Shared entrypoint bootstrap helpers (src/utils/entrypoint_bootstrap.py)."""

import warnings
from datetime import datetime, timedelta
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

import utils.entrypoint_bootstrap as bootstrap
from data import AgentsData


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


def test_build_agents_data_registers_single_agent():
    single_agent = object()

    agents_data = bootstrap.build_agents_data(single_agent=single_agent)

    assert isinstance(agents_data, AgentsData)
    assert agents_data.agents == {"single_agent": single_agent}
    assert agents_data.user_request is None
    assert agents_data.dtmf_tool is None
