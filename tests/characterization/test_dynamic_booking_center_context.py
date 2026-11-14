"""Center context answers made inside booking retain INFO_LOOKUP evidence."""

from types import SimpleNamespace
from unittest.mock import Mock

from agents.single_agent import SingleAgent
from tasks.dynamic_booking_task import DynamicBookingTask


def test_booking_context_answer_records_only_matching_task_speech(monkeypatch):
    handlers = {}

    def on(event_name):
        def register(handler):
            handlers[event_name] = handler

        return register

    session = SimpleNamespace(
        current_agent=None,
        on=on,
        history=SimpleNamespace(add_message=Mock()),
    )
    monkeypatch.setattr(SingleAgent, "session", property(lambda _self: session))
    agent = object.__new__(SingleAgent)
    agent._center_context = "주차장은 지하 2층에 있습니다."
    agent._center_context_chunks = [agent._center_context]
    agent._call_session_data = SimpleNamespace(room_name="test-room")
    task = object.__new__(DynamicBookingTask)
    task._center_context = agent._center_context

    agent._register_context_match_handler()
    added = handlers["conversation_item_added"]

    session.current_agent = task
    added(
        SimpleNamespace(
            item=SimpleNamespace(
                type="message",
                role="assistant",
                text_content="주차장은 지하 2층에 있습니다.",
            )
        )
    )
    assert session.history.add_message.call_count == 1
    assert (
        session.history.add_message.call_args.kwargs["extra"]["agent_event"]["name"]
        == "INFO_LOOKUP"
    )

    added(
        SimpleNamespace(
            item=SimpleNamespace(
                type="message",
                role="assistant",
                text_content="예약 일정을 다시 확인할게요.",
            )
        )
    )
    assert session.history.add_message.call_count == 1

    session.current_agent = object()
    added(
        SimpleNamespace(
            item=SimpleNamespace(
                type="message",
                role="assistant",
                text_content="주차장은 지하 2층에 있습니다.",
            )
        )
    )
    assert session.history.add_message.call_count == 1


def test_mixed_center_answer_and_booking_continuation_records_once(monkeypatch):
    session = SimpleNamespace(history=SimpleNamespace(add_message=Mock()))
    monkeypatch.setattr(SingleAgent, "session", property(lambda _self: session))
    agent = object.__new__(SingleAgent)
    agent._center_context_chunks = [
        "주차는 최대 2시간까지 무료이고, 2시간을 초과하면 주차요금이 발생할 수 있습니다."
    ]
    agent._call_session_data = SimpleNamespace(room_name="test-room")

    agent._record_context_answer_if_matched(
        SimpleNamespace(
            item=SimpleNamespace(
                type="message",
                role="assistant",
                text_content=(
                    "주차는 최대 2시간까지 무료이고, 2시간을 초과하면 "
                    "주차요금이 발생할 수 있습니다. "
                    "예약을 계속하시려면 해당 일정은 다시 확인해드릴게요."
                ),
            )
        )
    )
    assert session.history.add_message.call_count == 1

    session.history.add_message.reset_mock()
    agent._record_context_answer_if_matched(
        SimpleNamespace(
            item=SimpleNamespace(
                type="message",
                role="assistant",
                text_content="예약을 계속하시려면 해당 일정을 다시 확인해드릴게요.",
            )
        )
    )
    assert session.history.add_message.call_count == 0
