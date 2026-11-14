"""Late completions after the framework already settled a task must be no-ops.

LiveKit cancels an AgentTask by completing its future first; a late
participant-disconnect callback or a tool call that finishes afterwards must
not raise ``RuntimeError: ... is already done``.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from tasks.leave_message_task import LeaveMessageTask
from tasks.transfer_reason_task import TransferReasonTask


def _tool(task, name):
    return next(tool for tool in task.tools if tool.info.name == name)._func


def _ctx():
    handle = SimpleNamespace(wait_for_playout=AsyncMock())
    return SimpleNamespace(
        disallow_interruptions=Mock(),
        session=SimpleNamespace(say=Mock(return_value=handle)),
    )


async def test_transfer_reason_disconnect_after_framework_cancel_is_ignored():
    task = TransferReasonTask(caller_identity="caller", language="ko-KR")
    task.cancel()
    assert task.done()

    task._on_participant_disconnected(SimpleNamespace(identity="caller"))

    assert task._finished is False


async def test_transfer_reason_first_finish_still_completes():
    task = TransferReasonTask(caller_identity="caller", language="ko-KR")

    task._on_participant_disconnected(SimpleNamespace(identity="caller"))

    assert task.done()
    assert task._AgentTask__fut.result().caller_disconnected is True


async def test_leave_message_tool_after_framework_cancel_does_not_raise():
    task = LeaveMessageTask(is_after_transfer_failure=False, language="ko-KR")
    ctx = _ctx()
    task.cancel()

    await _tool(task, "collect_leave_message")(ctx, message="예약 변경 요청입니다.")

    ctx.session.say.assert_called_once()


async def test_booking_contact_tool_after_framework_cancel_does_not_raise():
    task = LeaveMessageTask(
        is_after_transfer_failure=False,
        action_key="booking_create",
        collect_desired_datetime=True,
        language="ko-KR",
    )
    ctx = _ctx()
    await _tool(task, "set_desired_datetime")(ctx, desired_datetime="07-12 18:00")
    task.cancel()

    assert await _tool(task, "submit_booking_contact")(ctx) is None


async def test_leave_message_tool_completes_normally():
    task = LeaveMessageTask(is_after_transfer_failure=False, language="ko-KR")

    await _tool(task, "collect_leave_message")(_ctx(), message="전달 사항 없음")

    result = task._AgentTask__fut.result()
    assert result.scenario == "leave_message_collected"
    assert result.message == "전달 사항 없음"
