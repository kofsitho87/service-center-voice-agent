"""Booking change/cancellation completion call-lifecycle checks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tools.booking_tools import _announce_and_shutdown_call
from utils.i18n import t


@pytest.mark.asyncio
@pytest.mark.parametrize("log_event", ["MODIFY_APPOINTMENT", "CANCEL_APPOINTMENT"])
async def test_completion_announcement_ends_call(log_event):
    session = SimpleNamespace(say=Mock(), shutdown=Mock())
    ctx = SimpleNamespace(
        session=session,
        userdata=SimpleNamespace(room_name="test-room"),
        wait_for_playout=AsyncMock(),
    )

    await _announce_and_shutdown_call(
        ctx,
        instructions="처리 완료 안내",
        log_event=log_event,
        phone="01000000000",
    )

    session.say.assert_called_once_with(
        "처리 완료 안내",
        allow_interruptions=False,
    )
    ctx.wait_for_playout.assert_awaited_once_with()
    session.shutdown.assert_called_once_with(drain=True)


@pytest.mark.parametrize(
    "message_key", ["booking.modify.success", "booking.cancel.success"]
)
@pytest.mark.parametrize("language", ["ko-KR", "en-US"])
def test_completion_message_announces_call_termination(message_key, language):
    message = t(message_key, language)

    if language == "ko-KR":
        assert "통화를 종료" in message
    else:
        assert "end the call" in message
