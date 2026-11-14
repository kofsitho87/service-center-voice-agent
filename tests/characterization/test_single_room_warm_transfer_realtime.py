import asyncio
import contextlib
from unittest.mock import AsyncMock

import pytest

from data import TransferRecord
from tasks.single_room_warm_transfer_task import SingleRoomWarmTransferTask


@pytest.mark.asyncio
async def test_cached_briefing_is_forwarded_to_realtime_callback():
    callback = AsyncMock()
    task = object.__new__(SingleRoomWarmTransferTask)
    task._transfer_record = TransferRecord(briefing_text="예약 변경 문의")
    task._briefing_text = None
    task._realtime_update_callback = callback

    briefing_text = await task._prepare_briefing_text()

    assert briefing_text == "예약 변경 문의"
    callback.assert_awaited_once_with(
        "briefing_ready",
        {"briefing_text": "예약 변경 문의"},
    )


@pytest.mark.asyncio
async def test_realtime_callback_failure_does_not_fail_transfer_task():
    callback = AsyncMock(side_effect=RuntimeError("redis unavailable"))
    task = object.__new__(SingleRoomWarmTransferTask)
    task._realtime_update_callback = callback

    await task._notify_realtime_update("briefing_ready", {"briefing_text": "문의"})

    callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_queued_briefing_future_is_reused_without_regeneration():
    async def _queued_briefing() -> str:
        return "큐 대기 중 생성된 브리핑"

    task = object.__new__(SingleRoomWarmTransferTask)
    task._queued_briefing_future = asyncio.create_task(_queued_briefing())
    task._transfer_record = TransferRecord()
    task._briefing_text = None

    briefing_text = await task._prepare_briefing_text()

    assert briefing_text == "큐 대기 중 생성된 브리핑"
    assert task._briefing_text == briefing_text
    assert task._transfer_record.briefing_text == briefing_text


@pytest.mark.asyncio
async def test_cancelling_dial_wait_does_not_cancel_queued_briefing_future():
    generation_started = asyncio.Event()
    generation_release = asyncio.Event()

    async def _queued_briefing() -> str:
        generation_started.set()
        await generation_release.wait()
        return "공유 브리핑"

    queued_future = asyncio.create_task(_queued_briefing())
    task = object.__new__(SingleRoomWarmTransferTask)
    task._queued_briefing_future = queued_future
    task._transfer_record = TransferRecord()
    task._briefing_text = None

    dial_wait = asyncio.create_task(task._prepare_briefing_text())
    await generation_started.wait()
    dial_wait.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await dial_wait

    assert not queued_future.cancelled()
    assert not queued_future.done()

    generation_release.set()
    assert await queued_future == "공유 브리핑"
