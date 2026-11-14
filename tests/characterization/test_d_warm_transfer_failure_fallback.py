"""SingleRoomWarmTransferTask must always settle, even when a completion path fails.

base_agent awaits the task without a timeout, so an exception inside a
background completion path would otherwise leave the caller on hold forever.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import tasks.single_room_warm_transfer_task as warm_transfer_module
from tasks.single_room_warm_transfer_task import (
    SingleRoomWarmTransferTask,
    WarmTransferStatus,
)

CALLER = "caller-test"
TARGET_PHONE = "01000000000"
AGENT = f"human-agent-{TARGET_PHONE}"
TEST_TIMEOUT_SECONDS = 5.0


class _Room:
    def __init__(self) -> None:
        self.name = "test-room"
        self.remote_participants: dict[str, SimpleNamespace] = {}
        self.local_participant = SimpleNamespace(
            set_track_subscription_permissions=MagicMock(),
            track_publications={},
        )
        self.on = MagicMock()
        self.off = MagicMock()


def _make_task(*, briefing_enabled: bool = True):
    room = _Room()
    session = SimpleNamespace(
        input=SimpleNamespace(set_audio_enabled=MagicMock()),
        say=MagicMock(),
        interrupt=MagicMock(),
        history=SimpleNamespace(add_message=MagicMock()),
    )
    job_ctx = SimpleNamespace(
        room=room,
        api=SimpleNamespace(
            room=SimpleNamespace(
                mute_published_track=AsyncMock(),
                update_subscriptions=AsyncMock(),
                remove_participant=AsyncMock(),
            ),
            sip=SimpleNamespace(create_sip_participant=AsyncMock()),
        ),
    )
    task = SingleRoomWarmTransferTask(
        target_phone_number=TARGET_PHONE,
        sip_trunk_id="trunk",
        caller_identity=CALLER,
        external_hold_music_player=SimpleNamespace(publication=None),
        restore_caller_ai_audio_on_failure=False,
        briefing_enabled=briefing_enabled,
    )
    task._get_activity_or_raise = lambda: SimpleNamespace(session=session)
    return task, room, job_ctx


def _result(task):
    return task._AgentTask__fut.result()


async def _wait_until_done(task) -> None:
    async def _poll() -> None:
        while not task.done():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout=TEST_TIMEOUT_SECONDS)


@pytest.fixture
def no_connect_delay(monkeypatch):
    """Skip the 0.8 s in-band DTMF settle delay in _execute_connect."""
    real_sleep = asyncio.sleep

    async def _fast_sleep(delay, *args, **kwargs):
        return await real_sleep(0, *args, **kwargs)

    monkeypatch.setattr(warm_transfer_module.asyncio, "sleep", _fast_sleep)


async def test_failed_connect_after_agent_accepts_settles_with_error(
    no_connect_delay,
):
    task, room, job_ctx = _make_task()
    task._room = room
    task._job_ctx = job_ctx
    task._agent_identity = AGENT
    task._release_ai_from_bridged_audio = MagicMock(
        side_effect=RuntimeError("room is closing")
    )

    task._on_agent_dtmf_received(
        SimpleNamespace(digit="1", participant=SimpleNamespace(identity=AGENT))
    )
    await _wait_until_done(task)

    result = _result(task)
    assert result.status is WarmTransferStatus.ERROR
    assert result.agent_identity == AGENT
    assert result.error_message == "room is closing"
    # The agent leg already left the fake room, so re-dialing stays allowed.
    assert result.terminate_call is False


async def test_failure_with_agent_still_in_room_forbids_redial(no_connect_delay):
    task, room, job_ctx = _make_task()
    task._room = room
    task._job_ctx = job_ctx
    task._agent_identity = AGENT
    room.remote_participants[AGENT] = SimpleNamespace(
        identity=AGENT, track_publications={}
    )
    task._release_ai_from_bridged_audio = MagicMock(
        side_effect=RuntimeError("room is closing")
    )

    task._on_agent_dtmf_received(
        SimpleNamespace(digit="1", participant=SimpleNamespace(identity=AGENT))
    )
    await _wait_until_done(task)

    result = _result(task)
    assert result.status is WarmTransferStatus.ERROR
    assert result.terminate_call is True


async def test_failed_dtmf_timeout_path_settles_with_error():
    task, room, job_ctx = _make_task()
    task._room = room
    task._job_ctx = job_ctx
    task._agent_identity = AGENT
    task._dtmf_timeout_duration = 0
    task._cleanup = AsyncMock(side_effect=RuntimeError("cleanup failed"))

    task._start_dtmf_timeout()
    await _wait_until_done(task)

    result = _result(task)
    assert result.status is WarmTransferStatus.ERROR
    assert result.error_message == "cleanup failed"


async def test_setup_failure_cancels_pending_briefing_generation(monkeypatch):
    task, _room, job_ctx = _make_task()
    monkeypatch.setattr(warm_transfer_module, "get_job_context", lambda: job_ctx)
    briefing_cancelled = asyncio.Event()

    async def _slow_briefing() -> str:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            briefing_cancelled.set()
            raise
        return "unreachable"

    async def _failing_isolation() -> None:
        # Yield once so briefing generation is already in flight.
        await asyncio.sleep(0)
        raise RuntimeError("isolation failed")

    task._prepare_briefing_text = _slow_briefing
    task._isolate_caller = _failing_isolation

    await asyncio.wait_for(task.on_enter(), timeout=TEST_TIMEOUT_SECONDS)

    assert _result(task).status is WarmTransferStatus.ERROR
    await asyncio.wait_for(briefing_cancelled.wait(), timeout=TEST_TIMEOUT_SECONDS)


async def test_successful_connect_is_unchanged(no_connect_delay):
    task, room, job_ctx = _make_task()
    task._room = room
    task._job_ctx = job_ctx
    task._agent_identity = AGENT

    task._on_agent_dtmf_received(
        SimpleNamespace(digit="1", participant=SimpleNamespace(identity=AGENT))
    )
    await _wait_until_done(task)

    result = _result(task)
    assert result.status is WarmTransferStatus.SUCCESS
    assert result.agent_identity == AGENT
