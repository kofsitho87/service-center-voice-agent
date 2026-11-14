import asyncio

import pytest

from utils.async_utils import await_through_cancellation


async def test_returns_task_result():
    async def _work() -> str:
        await asyncio.sleep(0)
        return "done"

    assert await await_through_cancellation(asyncio.create_task(_work())) == "done"


async def test_propagates_task_exception():
    async def _work() -> None:
        await asyncio.sleep(0)
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await await_through_cancellation(asyncio.create_task(_work()))


async def test_caller_cancellation_waits_for_task_then_reraises():
    release = asyncio.Event()
    finished = []

    async def _cleanup() -> None:
        await release.wait()
        finished.append(True)

    cleanup = asyncio.create_task(_cleanup())
    waiter = asyncio.create_task(await_through_cancellation(cleanup))
    await asyncio.sleep(0)

    waiter.cancel()
    await asyncio.sleep(0)
    assert not waiter.done()
    assert not cleanup.cancelled()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert finished == [True]


async def test_repeated_cancellation_is_absorbed_until_task_finishes():
    release = asyncio.Event()

    async def _cleanup() -> str:
        await release.wait()
        return "closed"

    cleanup = asyncio.create_task(_cleanup())
    waiter = asyncio.create_task(await_through_cancellation(cleanup))
    for _ in range(3):
        await asyncio.sleep(0)
        waiter.cancel()
    await asyncio.sleep(0)
    assert not waiter.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert cleanup.result() == "closed"


async def test_cancelled_task_raises_cancelled_error():
    async def _work() -> None:
        await asyncio.sleep(3600)

    task = asyncio.create_task(_work())
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await await_through_cancellation(task)
