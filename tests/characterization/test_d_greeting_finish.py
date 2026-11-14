"""Pin how GreetingDtmfTask closes itself on each completion path.

Ordering matters for the early-disconnect races: input must already be closed
when the session interruption fires, and the timer must not outlive the task.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

from tasks.greeting_dtmf_task import GreetingDtmfTask


def _make_task(**kwargs) -> tuple[GreetingDtmfTask, list[dict]]:
    task = GreetingDtmfTask(
        message="", dtmf_options={"1": "booking"}, dtmf_source="sip", **kwargs
    )
    interrupts: list[dict] = []

    def _interrupt(*_args, **_kwargs):
        interrupts.append(
            {
                "completed": task._completed,
                "wakeup": task._wakeup_event.is_set(),
                "reason": task._playout_interruption_reason,
            }
        )

    task._active_session = SimpleNamespace(
        interrupt=Mock(side_effect=_interrupt),
        history=SimpleNamespace(add_message=Mock()),
    )
    task.complete = Mock()
    return task, interrupts


async def _arm_timeout(task: GreetingDtmfTask) -> asyncio.Task:
    task._timeout_task = asyncio.create_task(asyncio.sleep(3600))
    await asyncio.sleep(0)
    return task._timeout_task


async def test_valid_key_closes_input_before_interrupting():
    task, interrupts = _make_task()
    timer = await _arm_timeout(task)

    task._handle_digit("1")
    await asyncio.sleep(0)

    assert interrupts == [
        {"completed": True, "wakeup": True, "reason": "DTMF_SELECTED"}
    ]
    assert timer.cancelled()
    assert task.complete.call_args.args[0].selected_key == "1"
    await task.on_exit()


async def test_max_invalid_keys_time_out_with_interrupt():
    task, interrupts = _make_task(max_invalid_attempts=0)
    timer = await _arm_timeout(task)

    task._handle_digit("9")
    await asyncio.sleep(0)

    assert interrupts == [
        {"completed": True, "wakeup": True, "reason": "MAX_INVALID_DTMF"}
    ]
    assert timer.cancelled()
    result = task.complete.call_args.args[0]
    assert result.timed_out and result.selected_key is None
    await task.on_exit()


async def test_room_disconnect_completes_without_interrupt():
    task, interrupts = _make_task()
    timer = await _arm_timeout(task)

    task._on_room_disconnected()
    await asyncio.sleep(0)

    assert interrupts == []
    assert timer.cancelled()
    assert task._completed and task._wakeup_event.is_set()
    assert task._playout_interruption_reason is None
    assert task.complete.call_args.args[0].interrupted
    await task.on_exit()


async def test_select_option_tool_sets_reason_only_for_a_key():
    for selected_key, reason in (("1", "DTMF_SELECTED"), (None, None)):
        task, interrupts = _make_task()
        timer = await _arm_timeout(task)
        tool = next(tool for tool in task.tools if tool.id == "select_option")

        await tool(selected_key=selected_key)
        await asyncio.sleep(0)

        assert interrupts == []
        assert timer.cancelled()
        assert task._completed and task._wakeup_event.is_set()
        assert task._playout_interruption_reason == reason
        assert task.complete.call_args.args[0].selected_key == selected_key
        await task.on_exit()


async def test_timeout_counting_respects_pause_and_pending_audio():
    task, _ = _make_task()
    task._timeout_duration = 0.25
    task._timeout_paused = True

    waiter = asyncio.create_task(task._start_timeout())
    await asyncio.sleep(0.4)
    assert not waiter.done()

    task._timeout_paused = False
    await asyncio.wait_for(waiter, timeout=2)
    assert task._needs_replay
    assert task._invalid_attempt_count == 1
