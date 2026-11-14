import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
import pytest
from test_greeting_dtmf_audio import Room, Stream, settle, task_session, track

from tasks.greeting_dtmf_task import GreetingDtmfTask
from utils.dtmf_input_arbiter import DtmfInputArbiter


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["sip", "inband"])
async def test_corresponding_sources_accept_one_sip_input(first):
    digits = []
    clock = [0.0]
    arbiter = DtmfInputArbiter(digits.append, lambda: True, clock=lambda: clock[0])
    arbiter.observe("1", first)
    if first == "inband":
        assert digits == []
        assert arbiter.has_pending
    clock[0] = 0.1
    arbiter.observe("1", "inband" if first == "sip" else "sip")
    clock[0] = 0.3
    arbiter._deliver_due()
    assert digits == ["1"]
    assert not arbiter.has_pending
    await arbiter.aclose()


@pytest.mark.asyncio
async def test_audio_fallback_late_sip_dedup_and_per_input_recovery():
    digits = []
    clock = [0.0]
    arbiter = DtmfInputArbiter(digits.append, lambda: True, clock=lambda: clock[0])
    arbiter.observe("*", "inband")
    clock[0] = 0.199
    arbiter._deliver_due()
    assert not digits
    clock[0] = 0.2
    arbiter._deliver_due()
    assert digits == ["*"]
    clock[0] = 0.45
    arbiter.observe("*", "sip")
    assert digits == ["*"]
    arbiter.observe("9", "sip")
    assert digits == ["*", "9"]
    arbiter.observe("9", "inband")
    assert digits == ["*", "9"]
    clock[0] = 1.0
    arbiter.observe("9", "inband")
    clock[0] = 1.3
    arbiter._deliver_due()
    assert digits == ["*", "9", "9"]
    await arbiter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sources",
    [
        ["sip", "sip", "inband", "inband"],
        ["inband", "inband", "sip", "sip"],
        ["sip", "inband", "sip", "inband"],
        ["inband", "sip", "inband", "sip"],
    ],
)
async def test_two_dense_same_key_presses_pair_one_to_one(sources):
    digits = []
    arbiter = DtmfInputArbiter(digits.append, lambda: True, clock=lambda: 0)
    for source in sources:
        arbiter.observe("*", source)
    assert digits == ["*", "*"]
    assert not arbiter.has_pending
    await arbiter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["sip", "inband"])
async def test_repeated_same_source_inputs_are_never_debounced(source):
    digits = []
    clock = [0.0]
    arbiter = DtmfInputArbiter(digits.append, lambda: True, clock=lambda: clock[0])
    arbiter.observe("*", source)
    arbiter.observe("*", source)
    clock[0] = 0.3
    arbiter._deliver_due()
    assert digits == ["*", "*"]
    await arbiter.aclose()


@pytest.mark.asyncio
async def test_bounded_pending_single_timer_sip_priority_and_close_fence():
    digits = []
    arbiter = DtmfInputArbiter(digits.append, lambda: True, clock=lambda: 0)
    arbiter.observe("*", "inband")
    timer = arbiter._timer
    for _ in range(99):
        arbiter.observe("*", "inband")
    assert len(arbiter._records) == arbiter.MAX_RECORDS
    assert arbiter._timer is timer
    arbiter.observe("1", "sip")
    assert digits == ["1"]
    assert len(arbiter._records) == arbiter.MAX_RECORDS
    await arbiter.aclose()
    arbiter.observe("9", "sip")
    arbiter._deliver_due()
    assert digits == ["1"]
    assert not arbiter.has_pending
    assert timer.done()


@pytest.mark.asyncio
async def test_done_fence_and_pairing_window_expires():
    digits = []
    clock = [0.0]
    active = [True]
    arbiter = DtmfInputArbiter(digits.append, lambda: active[0], clock=lambda: clock[0])
    arbiter.observe("*", "sip")
    clock[0] = 0.6
    arbiter.observe("*", "inband")
    clock[0] = 0.9
    arbiter._deliver_due()
    assert digits == ["*", "*"]
    arbiter.observe("1", "inband")
    active[0] = False
    clock[0] = 1.3
    arbiter._deliver_due()
    arbiter.observe("9", "sip")
    assert digits == ["*", "*"]
    await arbiter.aclose()


@pytest.mark.asyncio
async def test_pending_audio_prevents_last_timeout_tick_from_losing_input():
    task = GreetingDtmfTask(
        message="",
        dtmf_options={"1": "booking"},
        dtmf_source="auto",
        timeout=0.1,
        max_invalid_attempts=0,
    )
    task._active_session = task_session()
    task.complete = Mock()
    clock = [0.0]
    task._arbiter._clock = lambda: clock[0]
    tick = 0

    async def timer_tick(_seconds):
        nonlocal tick
        tick += 1
        if tick == 1:
            task._on_audio_digit("1")
        else:
            clock[0] = 0.3
            task._arbiter._deliver_due()

    with patch("tasks.greeting_dtmf_task.asyncio.sleep", timer_tick):
        await task._start_timeout()
    assert tick == 2
    assert task._invalid_attempt_count == 0
    assert task.complete.call_args.args[0].selected_key == "1"
    await task.on_exit()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["fallback", "sip", "cancel", "disconnect"])
async def test_auto_registers_both_paths_caller_filter_and_pending_cleanup(ending):
    Stream.instances = []
    room = Room([track("caller-audio")])
    session = task_session()
    task = GreetingDtmfTask(
        message="",
        dtmf_options={"1": "booking"},
        dtmf_source="auto",
        dtmf_fallback_delay_ms=0 if ending == "fallback" else 200,
    )
    task.complete = Mock()
    with (
        patch.object(GreetingDtmfTask, "session", session),
        patch.object(task, "update_instructions", AsyncMock()),
        patch(
            "tasks.greeting_dtmf_task.get_job_context",
            return_value=SimpleNamespace(room=room),
        ),
        patch("utils.greeting_dtmf_audio.rtc.AudioStream", Stream),
    ):
        running = asyncio.create_task(task.on_enter())
        await settle()
        assert room.listeners["sip_dtmf_received"]
        assert Stream.instances
        session.input.set_audio_enabled.assert_called_once_with(False)
        room.emit("sip_dtmf_received", SimpleNamespace(digit="1"))
        room.emit(
            "sip_dtmf_received",
            SimpleNamespace(digit="1", participant=SimpleNamespace(identity="other")),
        )
        task.complete.assert_not_called()
        samples = np.arange(1600) / 8000
        pcm = np.asarray(
            8000
            * (np.sin(2 * np.pi * 697 * samples) + np.sin(2 * np.pi * 1209 * samples)),
            dtype=np.int16,
        )
        Stream.instances[0].frames.put_nowait(
            SimpleNamespace(frame=SimpleNamespace(data=memoryview(pcm)))
        )
        await settle()
        if ending == "sip":
            assert task._arbiter.has_pending
            room.emit(
                "sip_dtmf_received",
                SimpleNamespace(
                    digit="1", participant=SimpleNamespace(identity="caller")
                ),
            )
        elif ending == "cancel":
            assert task._arbiter.has_pending
            running.cancel()
        elif ending == "disconnect":
            assert task._arbiter.has_pending
            room.emit("participant_disconnected", SimpleNamespace(identity="caller"))
        if ending == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await running
            task.complete.assert_not_called()
        else:
            await running
            result = task.complete.call_args.args[0]
            assert (
                result.interrupted
                if ending == "disconnect"
                else result.selected_key == "1"
            )
        assert not task._arbiter.has_pending
        assert task._arbiter._timer.done()
        assert Stream.instances[0].closed
        assert not any(room.listeners.values())
        task._on_audio_digit("9")
        task._on_sip_dtmf_received(
            SimpleNamespace(digit="9", participant=SimpleNamespace(identity="caller"))
        )
        assert task._invalid_attempt_count == 0


@pytest.mark.parametrize(
    "delay,window",
    [(float("nan"), 500), (200, float("inf")), (-1, 500), (500, 500), (200, 5001)],
)
def test_invalid_auto_timing_fails_fast(delay, window):
    with pytest.raises(ValueError, match="DTMF delays"):
        GreetingDtmfTask(
            message="",
            dtmf_options={},
            dtmf_source="auto",
            dtmf_fallback_delay_ms=delay,
            dtmf_dedup_window_ms=window,
        )


@pytest.mark.asyncio
async def test_scheduler_delivers_positive_delay_and_late_sip_is_counterpart():
    accepted = asyncio.Event()
    digits = []

    def on_digit(digit):
        digits.append(digit)
        accepted.set()

    arbiter = DtmfInputArbiter(
        on_digit, lambda: True, fallback_delay=0.01, dedup_window=0.05
    )
    arbiter.observe("*", "inband")
    assert not digits
    await asyncio.wait_for(accepted.wait(), timeout=1)
    assert digits == ["*"]
    arbiter.observe("*", "sip")
    assert digits == ["*"]
    # Another press uses SIP independently after audio fallback on the prior one.
    arbiter.observe("*", "sip")
    arbiter.observe("*", "inband")
    assert digits == ["*", "*"]
    assert not arbiter.has_pending
    await arbiter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["sip", "inband"])
async def test_auto_repeated_replay_inputs_remain_active_and_pair_one_to_one(first):
    task = GreetingDtmfTask(
        message="", dtmf_options={"1": "booking"}, replay_keys={"*"}, dtmf_source="auto"
    )
    task._active_session = task_session()
    task.complete = Mock()
    event = SimpleNamespace(digit="*", participant=SimpleNamespace(identity="caller"))

    def observe(source):
        if source == "sip":
            task._on_sip_dtmf_received(event)
        else:
            task._on_audio_digit("*")

    observe(first)
    observe(first)
    observe("inband" if first == "sip" else "sip")
    observe("inband" if first == "sip" else "sip")
    assert task._needs_replay
    assert task._active_session.interrupt.call_count == 2
    assert task._invalid_attempt_count == 0
    assert task._accepts_input()
    assert not task._arbiter.has_pending
    task.complete.assert_not_called()
    await task.on_exit()
