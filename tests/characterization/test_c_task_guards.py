"""Guards in the legacy booking selection and DTMF verification_code tasks."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tasks.booking.select_reservation_task import SelectReservationTask
from tasks.dtmf_verification_code_task import _is_valid_verification_code

_RESERVATIONS = [
    {"reservation_id": "R1", "appointment_date": "2030-01-07 09:00:00"},
    {"reservation_id": "R2", "appointment_date": "2030-01-08 10:00:00"},
]


def _reservation_task(monkeypatch, reservations):
    session = SimpleNamespace(
        history=SimpleNamespace(add_message=Mock()),
        userdata=SimpleNamespace(recipient_data=SimpleNamespace(customer_no=None)),
    )
    monkeypatch.setattr(
        SelectReservationTask, "session", property(lambda self: session)
    )
    task = SelectReservationTask(reservations=reservations, action="취소")
    task.complete = Mock()
    return task


@pytest.mark.asyncio
async def test_exit_after_single_reservation_select_does_not_complete_twice(
    monkeypatch,
):
    task = _reservation_task(monkeypatch, _RESERVATIONS[:1])

    await SelectReservationTask.select._func(task, index=1)
    await SelectReservationTask.exit_task._func(task, reason_code="cancel")

    task.complete.assert_called_once()
    assert task.complete.call_args.args[0].selected_reservation == _RESERVATIONS[0]


@pytest.mark.asyncio
async def test_select_and_confirm_after_exit_are_ignored(monkeypatch):
    task = _reservation_task(monkeypatch, _RESERVATIONS)

    await SelectReservationTask.select._func(task, index=2)
    await SelectReservationTask.exit_task._func(task, reason_code="cancel")
    assert await SelectReservationTask.confirm._func(task) is None
    assert await SelectReservationTask.select._func(task, index=1) is None

    task.complete.assert_called_once()
    result = task.complete.call_args.args[0]
    assert result.exited is True
    assert result.exit_reason_code == "cancel"


@pytest.mark.parametrize("digits", ["000101", "000202", "123456", "999999"])
def test_any_six_digit_verification_code_is_accepted(digits):
    assert _is_valid_verification_code(digits) is True


@pytest.mark.parametrize("digits", ["", "12345", "1234567", "12a456", "12 456"])
def test_invalid_verification_code_digits_are_rejected(digits):
    assert _is_valid_verification_code(digits) is False
