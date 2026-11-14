import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import tasks.single_room_warm_transfer_task as warm_transfer_module
from tasks.single_room_warm_transfer_task import SingleRoomWarmTransferTask


class _FakeSpeechHandle:
    def __init__(self) -> None:
        self._callbacks = []

    def add_done_callback(self, callback) -> None:
        self._callbacks.append(callback)

    def remove_done_callback(self, callback) -> None:
        self._callbacks.remove(callback)

    def finish_playout(self) -> None:
        for callback in list(self._callbacks):
            callback(self)


def _make_task() -> SingleRoomWarmTransferTask:
    task = object.__new__(SingleRoomWarmTransferTask)
    task._briefing_enabled = True
    task._auto_connect_claimed = False
    task._agent_identity = "human-agent-test"
    task._caller_disconnected = False
    task._dtmf_completed = False
    task._briefing_complete = False
    task._briefing_speech_handle = None
    task._dtmf_timeout_task = None
    task._dtmf_timeout_duration = 3600
    return task


@pytest.mark.asyncio
async def test_on_enter_does_not_start_timeout_during_briefing_playout(
    monkeypatch,
):
    task = _make_task()
    speech_handle = _FakeSpeechHandle()
    room = SimpleNamespace(name="test-room", on=MagicMock())
    session = SimpleNamespace(
        input=SimpleNamespace(set_audio_enabled=MagicMock()),
        say=MagicMock(return_value=speech_handle),
    )

    task._target_phone = "01000000000"
    task._caller_identity = "caller-test"
    task._get_activity_or_raise = lambda: SimpleNamespace(session=session)
    task._prepare_briefing_text = AsyncMock(return_value="상담원 브리핑")
    task._isolate_caller = AsyncMock()
    task._mute_caller_audio = AsyncMock()
    task._start_hold_music = AsyncMock()
    task._dial_agent = AsyncMock()
    task._wait_for_agent_connection = AsyncMock(return_value=True)
    task._notify_realtime_update = AsyncMock()
    task._start_blocking_agent_tracks_from_caller = MagicMock()
    task._block_caller_from_agent_audio = AsyncMock()
    task._wait_for_agent_audio_ready = AsyncMock(return_value=True)
    task._setup_briefing_audio = AsyncMock()

    monkeypatch.setattr(
        warm_transfer_module,
        "get_job_context",
        lambda: SimpleNamespace(room=room),
    )

    await task.on_enter()

    session.say.assert_called_once_with(
        "상담원 브리핑",
        add_to_chat_ctx=False,
    )
    assert task._briefing_complete is False
    assert task._dtmf_timeout_task is None

    speech_handle.finish_playout()

    assert task._briefing_complete is True
    assert task._dtmf_timeout_task is not None

    await asyncio.sleep(0)
    timeout_task = task._dtmf_timeout_task
    task._cancel_dtmf_timeout()
    await timeout_task


@pytest.mark.asyncio
async def test_dtmf_timeout_starts_after_briefing_playout_finishes():
    task = _make_task()
    speech_handle = _FakeSpeechHandle()

    task._watch_briefing_playout(speech_handle)

    assert task._dtmf_timeout_task is None
    assert task._briefing_complete is False

    speech_handle.finish_playout()

    assert task._briefing_complete is True
    assert task._dtmf_timeout_task is not None

    await asyncio.sleep(0)
    timeout_task = task._dtmf_timeout_task
    task._cancel_dtmf_timeout()
    await timeout_task


@pytest.mark.asyncio
async def test_early_dtmf_prevents_timeout_after_briefing_playout():
    task = _make_task()
    speech_handle = _FakeSpeechHandle()
    task._watch_briefing_playout(speech_handle)

    task._dtmf_completed = True
    speech_handle.finish_playout()
    await asyncio.sleep(0)

    assert task._dtmf_timeout_task is None
    assert task._briefing_complete is False
