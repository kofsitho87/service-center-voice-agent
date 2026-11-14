"""Small asyncio helpers shared by tasks and media readers."""

from __future__ import annotations

import asyncio
from typing import TypeVar

T = TypeVar("T")


async def await_through_cancellation(task: asyncio.Future[T]) -> T:
    """Wait until ``task`` finishes, even if the caller is cancelled meanwhile.

    Used for cleanup that must not be abandoned halfway (listener removal, RTC
    stream close). The task's own result or exception is propagated; a
    cancellation received while waiting is re-raised only after the task is done.
    """
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result
