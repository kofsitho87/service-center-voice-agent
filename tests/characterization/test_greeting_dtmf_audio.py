import asyncio
from collections import defaultdict
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
import pytest
from livekit import rtc

from tasks.greeting_dtmf_task import GreetingDtmfTask
from utils.greeting_dtmf_audio import CallerDtmfAudio


class Room:
    def __init__(self, tracks=()):
        self.listeners = defaultdict(list)
        self.remote_participants = {
            "caller": SimpleNamespace(
                track_publications={
                    str(i): SimpleNamespace(track=track)
                    for i, track in enumerate(tracks)
                }
            )
        }

    def on(self, event, callback):
        self.listeners[event].append(callback)

    def off(self, event, callback):
        if callback in self.listeners[event]:
            self.listeners[event].remove(callback)

    def emit(self, event, *args):
        for callback in list(self.listeners[event]):
            callback(*args)


class Stream:
    instances: ClassVar[list] = []

    def __init__(self, track, **kwargs):
        self.track = track
        self.kwargs = kwargs
        self.frames = asyncio.Queue()
        self.closed = False
        self.instances.append(self)

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.frames.get()

    async def aclose(self):
        await asyncio.sleep(0)
        self.closed = True


def track(sid):
    return SimpleNamespace(sid=sid, kind=rtc.TrackKind.KIND_AUDIO)


async def settle():
    for _ in range(15):
        await asyncio.sleep(0)


def task_session():
    return SimpleNamespace(
        userdata=SimpleNamespace(
            recipient_data=SimpleNamespace(sip_identity="caller"),
            caller_disconnected=False,
            call_config_data=SimpleNamespace(language="en-US"),
        ),
        input=SimpleNamespace(set_audio_enabled=Mock()),
        history=SimpleNamespace(add_message=Mock()),
        interrupt=Mock(),
        on=Mock(),
        off=Mock(),
    )


@pytest.mark.asyncio
async def test_caller_track_replacement_late_subscription_and_cleanup():
    Stream.instances = []
    old, replacement, late = track("old"), track("new"), track("late")
    room = Room([old])
    digits = []
    active = True
    with patch("utils.greeting_dtmf_audio.rtc.AudioStream", Stream):
        reader = CallerDtmfAudio(room, "caller", digits.append, lambda: active)
        reader.start()
        await settle()
        assert len(Stream.instances) == 1
        assert Stream.instances[0].kwargs == {
            "sample_rate": 8000,
            "num_channels": 1,
            "frame_size_ms": 20,
            "capacity": 5,
        }
        # Unrelated audio never creates a stream or stops the caller's stream.
        other = SimpleNamespace(identity="other")
        caller = SimpleNamespace(identity="caller")
        room.emit("track_subscribed", track("other"), None, other)
        room.emit("track_unsubscribed", old, None, other)
        await settle()
        assert len(Stream.instances) == 1
        room.emit("track_subscribed", replacement, None, caller)
        await settle()
        assert Stream.instances[0].closed
        assert Stream.instances[-1].track is replacement
        room.emit("track_unsubscribed", old, None, caller)
        await settle()
        assert not Stream.instances[-1].closed
        room.emit("track_unpublished", SimpleNamespace(sid="new"), caller)
        await settle()
        assert Stream.instances[-1].closed
        room.emit("track_subscribed", late, None, caller)
        await settle()
        # Actual detector fed PCM through the independent RTC path.
        samples = np.arange(1600) / 8000
        pcm = np.asarray(
            8000
            * (np.sin(2 * np.pi * 697 * samples) + np.sin(2 * np.pi * 1209 * samples)),
            dtype=np.int16,
        )
        Stream.instances[-1].frames.put_nowait(
            SimpleNamespace(frame=SimpleNamespace(data=memoryview(pcm)))
        )
        await settle()
        assert digits == ["1"]
        active = False
        Stream.instances[-1].frames.put_nowait(
            SimpleNamespace(frame=SimpleNamespace(data=memoryview(pcm)))
        )
        await reader.aclose()
        await reader.aclose()
        assert digits == ["1"]
        assert all(stream.closed for stream in Stream.instances)
        assert not any(room.listeners.values())


@pytest.mark.asyncio
async def test_rapid_replacements_coalesce_and_no_caller_fallback():
    Stream.instances = []
    room = Room()
    room.remote_participants = {
        "other": Room([track("other")]).remote_participants["caller"]
    }
    with patch("utils.greeting_dtmf_audio.rtc.AudioStream", Stream):
        reader = CallerDtmfAudio(room, "caller", Mock(), lambda: True)
        reader.start()
        await settle()
        assert not Stream.instances
        caller = SimpleNamespace(identity="caller")
        for i in range(100):
            room.emit("track_subscribed", track(str(i)), None, caller)
        await settle()
        assert len(Stream.instances) == 1
        assert Stream.instances[0].track.sid == "99"
        await reader.aclose()
        assert Stream.instances[0].closed


@pytest.mark.asyncio
async def test_common_digit_handler_replay_invalid_selection_and_fence():
    task = GreetingDtmfTask(
        message="", dtmf_options={"1": "booking"}, replay_keys={"*"}
    )
    task._active_session = task_session()
    task.complete = Mock()
    task._handle_digit("*")
    assert task._needs_replay
    assert task._invalid_attempt_count == 0
    task._handle_digit("9")
    task._handle_digit("9")
    assert task._invalid_attempt_count == 2
    task.complete.assert_not_called()
    task._handle_digit("9")
    assert task.complete.call_args.args[0].timed_out
    task._handle_digit("1")
    assert task.complete.call_count == 1
    await task.on_exit()
    assert not task._history_tasks
    task._active_session.history.add_message.assert_not_called()

    selected = GreetingDtmfTask(message="", dtmf_options={"1": "booking"})
    selected._active_session = task_session()
    selected.complete = Mock()
    selected._on_sip_dtmf_received(
        SimpleNamespace(digit="1", participant=SimpleNamespace(identity="caller"))
    )
    assert selected.complete.call_args.args[0].selected_key == "1"
    selected._handle_digit("9")
    assert selected._invalid_attempt_count == 0
    await selected.on_exit()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["sip", "inband"])
async def test_source_registration_audio_gate_and_cancel_cleanup(source):
    Stream.instances = []
    room = Room([track("caller-audio")])
    session = task_session()
    task = GreetingDtmfTask(
        message="", dtmf_options={"1": "booking"}, dtmf_source=source
    )
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
        session.input.set_audio_enabled.assert_called_once_with(False)
        assert bool(room.listeners["sip_dtmf_received"]) == (source == "sip")
        assert bool(Stream.instances) == (source == "inband")
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert all(stream.closed for stream in Stream.instances)
        assert not any(room.listeners.values())
        task._handle_digit("1")
        assert not task._completed
        assert task._exited
        assert task._timeout_task.done()


@pytest.mark.asyncio
async def test_room_disconnect_timeout_and_framework_done_fences():
    task = GreetingDtmfTask(message="", dtmf_options={"1": "booking"}, timeout=0)
    task._active_session = task_session()
    task.complete = Mock()
    for _ in range(2):
        await task._start_timeout()
    task.complete.assert_not_called()
    await task._start_timeout()
    assert task.complete.call_args.args[0].timed_out
    await task.on_exit()

    disconnected = GreetingDtmfTask(message="", dtmf_options={"1": "booking"})
    disconnected.complete = Mock()
    disconnected._on_room_disconnected()
    assert disconnected.complete.call_args.args[0].interrupted
    disconnected._handle_digit("1")
    assert disconnected.complete.call_count == 1

    cancelled = GreetingDtmfTask(message="", dtmf_options={"1": "booking"})
    cancelled.complete = Mock()
    with patch.object(cancelled, "done", return_value=True):
        cancelled._handle_digit("9")
        cancelled._on_room_disconnected()
    cancelled.complete.assert_not_called()
    assert cancelled._invalid_attempt_count == 0


def test_invalid_source_fails_fast():
    with pytest.raises(ValueError, match="sip or inband"):
        GreetingDtmfTask(message="", dtmf_options={}, dtmf_source="both")


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["digit", "caller_disconnect", "room_disconnect"])
async def test_task_completion_removes_audio_and_all_listeners(ending):
    Stream.instances = []
    room = Room([track("caller-audio")])
    session = task_session()
    task = GreetingDtmfTask(
        message="", dtmf_options={"1": "booking"}, dtmf_source="inband"
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
        if ending == "digit":
            samples = np.arange(1600) / 8000
            pcm = np.asarray(
                8000
                * (
                    np.sin(2 * np.pi * 697 * samples)
                    + np.sin(2 * np.pi * 1209 * samples)
                ),
                dtype=np.int16,
            )
            Stream.instances[0].frames.put_nowait(
                SimpleNamespace(frame=SimpleNamespace(data=memoryview(pcm)))
            )
            await settle()
            session.input.set_audio_enabled.assert_called_once_with(False)
            assert task.complete.call_args.args[0].selected_key == "1"
        elif ending == "caller_disconnect":
            room.emit("participant_disconnected", SimpleNamespace(identity="other"))
            task.complete.assert_not_called()
            room.emit("participant_disconnected", SimpleNamespace(identity="caller"))
            assert session.userdata.caller_disconnected
            assert task.complete.call_args.args[0].interrupted
        else:
            room.emit("disconnected")
            assert task.complete.call_args.args[0].interrupted
        await running
        assert Stream.instances[0].closed
        assert not any(room.listeners.values())
        task._handle_digit("9")
        assert task._invalid_attempt_count == 0


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_stream_close():
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    class SlowStream(Stream):
        async def aclose(self):
            close_started.set()
            await release_close.wait()
            self.closed = True

    Stream.instances = []
    room = Room([track("caller-audio")])
    task = GreetingDtmfTask(
        message="", dtmf_options={"1": "booking"}, dtmf_source="inband"
    )
    with (
        patch.object(GreetingDtmfTask, "session", task_session()),
        patch.object(task, "update_instructions", AsyncMock()),
        patch(
            "tasks.greeting_dtmf_task.get_job_context",
            return_value=SimpleNamespace(room=room),
        ),
        patch("utils.greeting_dtmf_audio.rtc.AudioStream", SlowStream),
    ):
        running = asyncio.create_task(task.on_enter())
        await settle()
        running.cancel()
        await close_started.wait()
        running.cancel()
        await settle()
        assert not running.done()
        assert not Stream.instances[0].closed
        release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert Stream.instances[0].closed
        assert task._cleanup_task.done()
        assert not any(room.listeners.values())


class GreetingSpeech:
    def __init__(self):
        self.callbacks = []
        self.interrupted = False
        self.error = None

    def add_done_callback(self, callback):
        self.callbacks.append(callback)

    def remove_done_callback(self, callback):
        if callback in self.callbacks:
            self.callbacks.remove(callback)

    def exception(self):
        return self.error

    def finish(self, *, error=None, interrupted=False):
        self.error = error
        self.interrupted = interrupted
        for callback in list(self.callbacks):
            callback(self)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["sip", "inband", "select_option"])
@pytest.mark.parametrize("greeting_state", ["playing", "completed", "empty"])
async def test_selection_records_skip_before_real_task_completion(
    source, greeting_state
):
    room = Room()
    session = task_session()
    speech = GreetingSpeech()
    session.say = Mock(return_value=speech)
    task = GreetingDtmfTask(
        message="Hello" if greeting_state != "empty" else "",
        dtmf_options={"4": "booking inquiry"},
        dtmf_source="inband" if source == "inband" else "sip",
    )
    events = []

    def record_event(**kwargs):
        event = kwargs["extra"]["agent_event"]
        events.append(event)
        if event.get("status") == "SKIPPED":
            # Use the real AgentTask future: a mocked complete() hides this race.
            assert task.done() is False

    session.history.add_message.side_effect = record_event
    with (
        patch.object(GreetingDtmfTask, "session", session),
        patch.object(task, "update_instructions", AsyncMock()),
        patch(
            "tasks.greeting_dtmf_task.get_job_context",
            return_value=SimpleNamespace(room=room),
        ),
        patch("tasks.greeting_dtmf_task.CallerDtmfAudio"),
    ):
        running = asyncio.create_task(task.on_enter())
        await settle()
        if greeting_state == "completed":
            speech.finish()
            await settle()
            assert any(event.get("status") == "COMPLETED" for event in events)

        async def select():
            if source == "sip":
                room.emit("sip_dtmf_received", SimpleNamespace(digit="4"))
            elif source == "inband":
                task._on_audio_digit("4")
            else:
                tool = next(tool for tool in task.tools if tool.id == "select_option")
                await tool("4")

        await select()
        assert task.done() is True
        await asyncio.wait_for(running, timeout=1)
        await select()  # Late duplicate input must not append a second event.

    skipped = [event for event in events if event.get("status") == "SKIPPED"]
    if greeting_state == "playing":
        assert len(skipped) == 1
        assert skipped[0]["details"]["reason"] == "DTMF_SELECTED"
        assert not any(event.get("status") == "COMPLETED" for event in events)
    else:
        assert skipped == []
    assert not any(room.listeners.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["disconnect", "playout_error", "interrupted"])
async def test_playout_end_preserves_skip_with_real_task_completion(ending):
    room = Room()
    session = task_session()
    speech = GreetingSpeech()
    session.say = Mock(return_value=speech)
    task = GreetingDtmfTask(
        message="Hello", dtmf_options={"4": "inquiry"}, dtmf_source="sip"
    )
    events = []
    session.history.add_message.side_effect = lambda **kwargs: events.append(
        kwargs["extra"]["agent_event"]
    )
    with (
        patch.object(GreetingDtmfTask, "session", session),
        patch.object(task, "update_instructions", AsyncMock()),
        patch(
            "tasks.greeting_dtmf_task.get_job_context",
            return_value=SimpleNamespace(room=room),
        ),
    ):
        running = asyncio.create_task(task.on_enter())
        await settle()
        if ending == "disconnect":
            room.emit("participant_disconnected", SimpleNamespace(identity="caller"))
        elif ending == "playout_error":
            speech.finish(error=RuntimeError("Playout failed"))
        else:
            speech.finish(interrupted=True)
        await asyncio.wait_for(running, timeout=1)
        assert task.done() is True
    skipped = [event for event in events if event.get("status") == "SKIPPED"]
    assert len(skipped) == 1
    assert skipped[0]["details"]["reason"] == "PLAYOUT_INTERRUPTED"
    assert not any(event.get("status") == "COMPLETED" for event in events)
