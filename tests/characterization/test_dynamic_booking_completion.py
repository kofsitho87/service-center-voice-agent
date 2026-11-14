"""Dynamic booking completion announcement and call-lifecycle checks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tools.booking_tools import _announce_and_shutdown_call


@pytest.mark.asyncio
async def test_booking_success_announcement_ends_call():
    session = SimpleNamespace(say=Mock(), shutdown=Mock())
    ctx = SimpleNamespace(
        session=session,
        userdata=SimpleNamespace(room_name="test-room"),
        wait_for_playout=AsyncMock(),
    )

    await _announce_and_shutdown_call(
        ctx,
        instructions="예약 접수 안내",
        log_event="CREATE_NEW_APPOINTMENT",
        phone="01000000000",
    )

    session.say.assert_called_once_with(
        "예약 접수 안내",
        allow_interruptions=False,
    )
    ctx.wait_for_playout.assert_awaited_once_with()
    session.shutdown.assert_called_once_with(drain=True)
