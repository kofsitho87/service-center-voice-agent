import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

from tasks.caller_wait_reconfirmation_task import (
    CallerWaitReconfirmationResult,
    CallerWaitReconfirmationTask,
    ReconfirmationAction,
)

CALLER = "sip-caller"


def _make_task(*, timeout: float = 10.0, max_attempts: int = 3):
    task = CallerWaitReconfirmationTask(
        caller_identity=CALLER,
        timeout=timeout,
        max_attempts=max_attempts,
    )
    task._session = SimpleNamespace(
        interrupt=Mock(),
        history=SimpleNamespace(add_message=Mock()),
    )
    task.complete = Mock()
    return task


def _dtmf(digit: str, identity: str = CALLER):
    return SimpleNamespace(digit=digit, participant=SimpleNamespace(identity=identity))


def _completed_result(task) -> CallerWaitReconfirmationResult:
    task.complete.assert_called_once()
    return task.complete.call_args.args[0]


async def test_digit_one_continues_waiting():
    task = _make_task()

    task._on_sip_dtmf_received(_dtmf("1"))

    assert _completed_result(task) == CallerWaitReconfirmationResult(
        action=ReconfirmationAction.CONTINUE_WAITING, attempt_count=1
    )


async def test_digit_two_exits_to_leave_message():
    task = _make_task()

    task._on_sip_dtmf_received(_dtmf("2"))

    assert _completed_result(task) == CallerWaitReconfirmationResult(
        action=ReconfirmationAction.EXIT_TO_LEAVE_MESSAGE, attempt_count=1
    )


async def test_non_caller_dtmf_is_ignored():
    task = _make_task()

    task._on_sip_dtmf_received(_dtmf("1", identity="human-agent"))

    task.complete.assert_not_called()
    assert task._attempt_count == 0


async def test_invalid_digits_terminate_after_max_attempts():
    task = _make_task(max_attempts=2)

    task._on_sip_dtmf_received(_dtmf("9"))
    task.complete.assert_not_called()
    assert task._needs_replay is True

    task._on_sip_dtmf_received(_dtmf("9"))
    assert _completed_result(task) == CallerWaitReconfirmationResult(
        action=ReconfirmationAction.TERMINATE_CALL, attempt_count=2
    )


async def test_timeouts_terminate_after_max_attempts():
    task = _make_task(timeout=0.01, max_attempts=2)

    await task._start_timeout()
    task.complete.assert_not_called()
    assert task._needs_replay is True

    await task._start_timeout()
    assert _completed_result(task) == CallerWaitReconfirmationResult(
        action=ReconfirmationAction.TERMINATE_CALL, attempt_count=2
    )


async def test_invalid_digit_cancels_the_running_timeout():
    """An invalid key must not leave the previous prompt's timer running.

    Otherwise the stale timer burns a second attempt for the same prompt.
    """
    task = _make_task(timeout=0.2, max_attempts=5)
    task._timeout_task = asyncio.create_task(task._start_timeout())
    await asyncio.sleep(0.1)

    task._on_sip_dtmf_received(_dtmf("9"))
    assert task._attempt_count == 1

    # on_enter re-arms a fresh timer for the replayed prompt.
    task._wakeup_event.clear()
    task._needs_replay = False
    task._timeout_task = asyncio.create_task(task._start_timeout())
    await asyncio.sleep(0.15)

    assert task._attempt_count == 1
    task._cancel_timeout()


async def test_caller_disconnect_completes_once():
    task = _make_task()

    task._on_participant_disconnected(SimpleNamespace(identity=CALLER))
    task._on_participant_disconnected(SimpleNamespace(identity=CALLER))
    task._on_sip_dtmf_received(_dtmf("1"))

    assert _completed_result(task).action is ReconfirmationAction.CALLER_DISCONNECTED
