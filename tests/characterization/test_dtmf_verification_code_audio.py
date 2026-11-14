import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
import pytest
from test_greeting_dtmf_audio import Room, Stream, settle, task_session, track

from tasks.dtmf_verification_code_task import GetVerificationCodeDtmfTask
from utils.i18n import t


@asynccontextmanager
async def verification_code_task(source="sip", **kwargs):
    Stream.instances = []
    room = Room([track("caller-audio")])
    session = task_session()
    session.say = Mock()
    task = GetVerificationCodeDtmfTask(num_digits=6, dtmf_source=source, **kwargs)
    task.complete = Mock()
    task.cancel = Mock()
    with (
        patch.object(GetVerificationCodeDtmfTask, "session", session),
        patch.object(task, "update_instructions", AsyncMock()),
        patch(
            "tasks.dtmf_verification_code_task.get_job_context",
            return_value=SimpleNamespace(room=room),
        ),
        patch("utils.greeting_dtmf_audio.rtc.AudioStream", Stream),
    ):
        await task.on_enter()
        await settle()
        try:
            yield task, room, session
        finally:
            await task.on_exit()


def sip(room, digit, identity="caller"):
    room.emit(
        "sip_dtmf_received",
        SimpleNamespace(digit=digit, participant=SimpleNamespace(identity=identity)),
    )


def pcm_digits(digits):
    freqs = {"0": (941, 1336), "1": (697, 1209), "2": (697, 1336)}
    samples = np.arange(1280) / 8000
    frames = []
    for digit in digits:
        low, high = freqs[digit]
        frames.append(
            np.asarray(
                8000
                * (
                    np.sin(2 * np.pi * low * samples)
                    + np.sin(2 * np.pi * high * samples)
                ),
                dtype=np.int16,
            )
        )
        frames.append(np.zeros(800, dtype=np.int16))
    return np.concatenate(frames)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["sip", "inband", "auto"])
async def test_six_repeated_digits_confirm_once_and_stop_audio_before_voice(source):
    async with verification_code_task(source, dtmf_fallback_delay_ms=0) as (
        task,
        room,
        session,
    ):
        assert bool(room.listeners["sip_dtmf_received"]) == (source != "inband")
        assert bool(Stream.instances) == (source != "sip")
        assert not task._exited  # successful on_enter returns without cleaning up
        session.input.set_audio_enabled.assert_called_once_with(False)
        if source == "sip":
            # Burst extra digits must not replace the scheduled valid-six-digit job.
            for digit in "00112212":
                sip(room, digit)
        else:
            Stream.instances[0].frames.put_nowait(
                SimpleNamespace(
                    frame=SimpleNamespace(data=memoryview(pcm_digits("001122")))
                )
            )
        await settle()
        assert task._confirming
        assert task._collected_result.normalized_date == "2000-11-22"
        task.update_instructions.assert_awaited_once()
        assert session.say.call_count == 2
        assert session.say.call_args.kwargs["allow_interruptions"] is False
        assert session.input.set_audio_enabled.call_args.args == (True,)
        assert not room.listeners["sip_dtmf_received"]
        assert all(stream.closed for stream in Stream.instances)
        if task._arbiter is not None:
            assert not task._arbiter.has_pending
        before = session.say.call_count
        task._on_audio_digit("1")
        task._on_sip_dtmf_received(
            SimpleNamespace(digit="1", participant=SimpleNamespace(identity="caller"))
        )
        await settle()
        assert session.say.call_count == before
        context = SimpleNamespace(disallow_interruptions=Mock())
        await task.confirm_information(context)
        await task.reject_information(context)
        assert task.complete.call_count == 1
        assert task.complete.call_args.args[0].confirmed


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["sip", "inband"])
async def test_auto_both_sources_repeated_digits_are_one_to_one(first):
    async with verification_code_task("auto") as (task, room, session):
        for digit in "001122":
            if first == "sip":
                sip(room, digit)
                task._on_audio_digit(digit)
            else:
                task._on_audio_digit(digit)
                sip(room, digit)
        await settle()
        assert task._collected_result.normalized_date == "2000-11-22"
        assert session.say.call_count == 2
        assert not task._arbiter.has_pending


@pytest.mark.asyncio
async def test_auto_mixed_sources_keep_digit_order_and_ignore_other_participants():
    async with verification_code_task("auto") as (task, room, session):
        clock = [0.0]
        task._arbiter._clock = lambda: clock[0]
        sip(room, "9", "other")
        assert not task._curr_dtmf_inputs
        # Audio first, then a different key from SIP must remain chronological.
        task._on_audio_digit("0")
        sip(room, "1")
        assert not task._curr_dtmf_inputs
        clock[0] = 0.3
        task._arbiter._deliver_due()
        assert list(task._curr_dtmf_inputs) == ["0", "1"]
        # After the correlation horizon, subsequent SIP digits are fresh inputs.
        clock[0] = 0.6
        for digit in "1122":
            sip(room, digit)
        await settle()
        assert task._collected_result.normalized_date == "2001-11-22"
        assert session.say.call_count == 2


@pytest.mark.asyncio
async def test_invalid_and_incomplete_clear_inputs_before_retry():
    async with verification_code_task(dtmf_input_timeout=0.01) as (task, room, session):
        for digit in "001332":
            sip(room, digit)
        await settle()
        assert not task._confirming
        assert not task._curr_dtmf_inputs
        assert session.say.call_args.args[0] == t(
            "dtmf_verification_code.reprompt.invalid", task._lang
        )
        sip(room, "0")
        await asyncio.sleep(0.02)
        assert not task._curr_dtmf_inputs
        assert session.say.call_args.args[0] == t(
            "dtmf_verification_code.reprompt.incomplete", task._lang
        )
        for digit in "001122":
            sip(room, digit)
        await settle()
        assert task._confirming
        assert task._collected_result.normalized_date == "2000-11-22"


@pytest.mark.asyncio
async def test_pending_last_audio_digit_survives_per_digit_timeout():
    async with verification_code_task("auto", dtmf_input_timeout=0.01) as (task, room, session):
        clock = [0.0]
        task._arbiter._clock = lambda: clock[0]
        for digit in "00112":
            sip(room, digit)
        # The final press shares a key with the fifth SIP press. Advance beyond
        # the matching horizon so it is unambiguously a fresh audio-only press.
        clock[0] = 0.6
        task._on_audio_digit("2")
        await asyncio.sleep(0.025)
        assert len(task._curr_dtmf_inputs) == 5
        assert session.say.call_count == 1
        clock[0] = 0.9
        task._arbiter._deliver_due()
        await settle()
        assert task._collected_result.normalized_date == "2000-11-22"
        assert session.say.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["exit", "caller", "room", "done"])
async def test_pending_cleanup_and_late_digit_fences(ending):
    async with verification_code_task("auto") as (task, room, session):
        sip(room, "0")
        task._on_audio_digit("1")
        assert task._arbiter.has_pending
        if ending == "caller":
            room.emit("participant_disconnected", SimpleNamespace(identity="other"))
            task.cancel.assert_not_called()
            room.emit("participant_disconnected", SimpleNamespace(identity="caller"))
            assert session.userdata.caller_disconnected
            task.cancel.assert_called_once()
        elif ending == "room":
            room.emit("disconnected")
            task.cancel.assert_called_once()
        elif ending == "done":
            with patch.object(task, "done", return_value=True):
                task._handle_digit("9")
                assert len(task._curr_dtmf_inputs) == 1
        await task.on_exit()
        assert not task._reply_tasks
        assert not task._arbiter.has_pending
        assert task._arbiter._timer.done()
        assert all(stream.closed for stream in Stream.instances)
        assert not any(room.listeners.values())
        task._on_audio_digit("9")
        task._handle_digit("9")
        assert not task._curr_dtmf_inputs
        assert session.say.call_count == 1


@pytest.mark.asyncio
async def test_cancelled_entry_cleans_resources_without_job_context_on_exit():
    Stream.instances = []
    room = Room([track("caller-audio")])
    session = task_session()
    session.say = Mock(side_effect=asyncio.CancelledError)
    task = GetVerificationCodeDtmfTask(num_digits=6, dtmf_source="auto")
    with (
        patch.object(GetVerificationCodeDtmfTask, "session", session),
        patch(
            "tasks.dtmf_verification_code_task.get_job_context",
            return_value=SimpleNamespace(room=room),
        ),
        patch("utils.greeting_dtmf_audio.rtc.AudioStream", Stream),
    ):
        with pytest.raises(asyncio.CancelledError):
            await task.on_enter()
        assert task._exited
        assert not any(room.listeners.values())
        assert all(stream.closed for stream in Stream.instances)
    # Cached references make exit independent of torn-down activity/job context.
    await task.on_exit()


@pytest.mark.asyncio
async def test_repeated_exit_cancellation_waits_for_audio_close():
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowStream(Stream):
        async def aclose(self):
            started.set()
            await release.wait()
            self.closed = True

    Stream.instances = []
    room = Room([track("caller-audio")])
    session = task_session()
    session.say = Mock()
    task = GetVerificationCodeDtmfTask(num_digits=6, dtmf_source="auto")
    with (
        patch.object(GetVerificationCodeDtmfTask, "session", session),
        patch(
            "tasks.dtmf_verification_code_task.get_job_context",
            return_value=SimpleNamespace(room=room),
        ),
        patch("utils.greeting_dtmf_audio.rtc.AudioStream", SlowStream),
    ):
        await task.on_enter()
        await settle()
        task._on_audio_digit("0")
        exiting = asyncio.create_task(task.on_exit())
        await started.wait()
        exiting.cancel()
        await settle()
        exiting.cancel()
        await settle()
        assert not exiting.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await exiting
        assert Stream.instances[0].closed
        assert not task._arbiter.has_pending
        assert not task._reply_tasks
        assert not any(room.listeners.values())
        await task.on_exit()


@pytest.mark.asyncio
async def test_exit_during_confirmation_instructions_prevents_stale_announcement():
    async with verification_code_task() as (task, room, session):
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked_instructions(_instructions):
            started.set()
            await release.wait()

        task.update_instructions.side_effect = blocked_instructions
        for digit in "001122":
            sip(room, digit)
        await started.wait()
        await task.on_exit()
        assert session.say.call_count == 1
        statuses = [
            call.kwargs["extra"]["agent_event"]["status"]
            for call in session.history.add_message.call_args_list
        ]
        assert "CONFIRM" not in statuses
        assert not task._reply_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["sip", "inband"])
async def test_default_auto_logs_accepted_key_but_not_dedup_key(source):
    logs = Mock()
    async with verification_code_task(None) as (task, _room, _session):
        assert task._dtmf_source == "auto"
        clock = [0.0]
        task._arbiter._clock = lambda: clock[0]
        with patch("utils.dtmf_input_arbiter.logger", logs):
            if source == "sip":
                task._on_sip_dtmf_received(
                    SimpleNamespace(
                        digit="5", participant=SimpleNamespace(identity="caller")
                    )
                )
                task._on_audio_digit("5")
            else:
                task._on_audio_digit("5")
                clock[0] = 0.2
                task._arbiter._deliver_due()
                clock[0] = 0.3
                task._on_sip_dtmf_received(
                    SimpleNamespace(
                        digit="5", participant=SimpleNamespace(identity="caller")
                    )
                )
        extras = [call.kwargs for call in logs.bind.call_args_list]
        accepted = [
            entry for entry in extras if entry["event"] == "VERIFICATION_CODE_DTMF_ACCEPTED"
        ]
        assert accepted == [
            {
                "event": "VERIFICATION_CODE_DTMF_ACCEPTED",
                "source": source,
                "digit": "5",
                "fallback": source == "inband",
            }
        ]
        dedup = [
            entry for entry in extras if entry["event"] == "VERIFICATION_CODE_DTMF_DEDUPLICATED"
        ]
        assert len(dedup) == 1
        assert "digit" not in dedup[0]
        assert len(task._curr_dtmf_inputs) == 1
