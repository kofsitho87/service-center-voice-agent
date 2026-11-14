"""Code-only defaults, sequence ordering, and shared verification_code log privacy."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from utils.dtmf_config import resolve_dtmf_config
from utils.dtmf_input_arbiter import DtmfInputArbiter
from utils.greeting_dtmf_audio import CallerDtmfAudio


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_source", ["sip", "inband", "invalid"])
async def test_both_tasks_ignore_all_transport_environment(monkeypatch, stale_source):
    from tasks.dtmf_verification_code_task import GetVerificationCodeDtmfTask
    from tasks.greeting_dtmf_task import GreetingDtmfTask

    for prefix in ("DTMF", "GREETING_DTMF", "VERIFICATION_CODE_DTMF"):
        monkeypatch.setenv(f"{prefix}_SOURCE", stale_source)
        monkeypatch.setenv(f"{prefix}_FALLBACK_DELAY_MS", "not-a-number")
        monkeypatch.setenv(f"{prefix}_DEDUP_WINDOW_MS", "NaN")
    config = resolve_dtmf_config()
    assert (config.source, config.fallback_delay, config.dedup_window) == (
        "auto",
        0.2,
        0.5,
    )
    greeting = GreetingDtmfTask(message="Hello", dtmf_options={"1": "menu"})
    verification_code = GetVerificationCodeDtmfTask(num_digits=6)
    for task in (greeting, verification_code):
        assert task._dtmf_source == "auto"
        assert task._arbiter._fallback_delay == 0.2
        assert task._arbiter._dedup_window == 0.5


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["sip", "inband", "auto"])
async def test_explicit_task_source_overrides_remain_available(source):
    from tasks.dtmf_verification_code_task import GetVerificationCodeDtmfTask
    from tasks.greeting_dtmf_task import GreetingDtmfTask

    greeting = GreetingDtmfTask(
        message="Hello", dtmf_options={"1": "menu"}, dtmf_source=source
    )
    verification_code = GetVerificationCodeDtmfTask(num_digits=6, dtmf_source=source)
    assert greeting._dtmf_source == verification_code._dtmf_source == source


def test_explicit_timing_overrides_use_seconds():
    config = resolve_dtmf_config(fallback_delay_ms=0, dedup_window_ms=5000)
    assert (config.fallback_delay, config.dedup_window) == (0, 5)


@pytest.mark.parametrize(
    "fallback,window",
    [(200, 200), (-1, 500), (200, 5001), (float("nan"), 500), (0, float("inf"))],
)
def test_auto_rejects_invalid_and_nonfinite_timing(fallback, window):
    with pytest.raises(ValueError, match="DTMF delays"):
        resolve_dtmf_config("auto", fallback, window)


def test_nonauto_does_not_parse_irrelevant_timing(monkeypatch):
    monkeypatch.setenv("DTMF_FALLBACK_DELAY_MS", "not-a-number")
    assert resolve_dtmf_config("sip").source == "sip"
    assert resolve_dtmf_config("inband").source == "inband"


@pytest.mark.asyncio
async def test_ordered_delivery_holds_later_sip_behind_pending_audio():
    digits = []
    now = [0.0]
    arbiter = DtmfInputArbiter(
        digits.append, lambda: True, clock=lambda: now[0], preserve_order=True
    )
    arbiter.observe("1", "inband")
    now[0] = 0.1
    arbiter.observe("2", "sip")
    assert digits == []
    assert arbiter.has_pending
    now[0] = 0.2
    arbiter._deliver_due()
    assert digits == ["1", "2"]
    assert not arbiter.has_pending
    await arbiter.aclose()


@pytest.mark.asyncio
async def test_verification_code_arbiter_logs_metadata_without_digits():
    logs = Mock()
    digits = []
    with patch("utils.dtmf_input_arbiter.logger", logs):
        arbiter = DtmfInputArbiter(
            digits.append, lambda: True, event_prefix="VERIFICATION_CODE_DTMF", log_digits=False
        )
        arbiter.observe("9", "sip")
        arbiter.observe("9", "inband")
        await arbiter.aclose()
    assert digits == ["9"]
    extras = [call.kwargs for call in logs.bind.call_args_list]
    assert {entry["event"] for entry in extras} == {
        "VERIFICATION_CODE_DTMF_ACCEPTED",
        "VERIFICATION_CODE_DTMF_DEDUPLICATED",
    }
    assert all("digit" not in entry for entry in extras)


@pytest.mark.asyncio
async def test_verification_code_audio_logs_metadata_without_digits():
    class OneFrame:
        def __aiter__(self):
            async def frames():
                yield SimpleNamespace(frame=SimpleNamespace(data=b"\x00\x00"))

            return frames()

        aclose = AsyncMock()

    logs = Mock()
    stream = OneFrame()
    digits = []
    reader = CallerDtmfAudio(
        Mock(),
        "caller",
        digits.append,
        lambda: True,
        event_prefix="VERIFICATION_CODE_DTMF",
        log_digits=False,
    )
    with (
        patch("utils.greeting_dtmf_audio.rtc.AudioStream", return_value=stream),
        patch("utils.greeting_dtmf_audio.InbandDtmfDetector") as detector,
        patch("utils.greeting_dtmf_audio.logger", logs),
    ):
        detector.return_value.feed.return_value = ["9"]
        await reader._read(Mock(), 0)
    assert digits == ["9"]
    logs.bind.assert_called_once_with(event="VERIFICATION_CODE_DTMF_INBAND_DETECTED")
    stream.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_audio_concurrent_close_shares_reader_snapshot():
    import asyncio

    reader = CallerDtmfAudio(Mock(), "caller", Mock(), lambda: True)
    release = asyncio.Event()
    reader._reader = asyncio.create_task(release.wait())
    closing = [asyncio.create_task(reader.aclose()) for _ in range(2)]
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(*closing)
    assert reader._reader is None
    assert reader._closed
