from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from utils.session_event_handlers import register_session_event_handlers


class _EventEmitter:
    def __init__(self, *, name: str = "room-1") -> None:
        self.name = name
        self.handlers = {}

    def on(self, event_name):
        def register(handler):
            self.handlers[event_name] = handler
            return handler

        return register


class SessionEventHandlersTest(TestCase):
    def test_caller_disconnect_interrupts_active_and_queued_speech(self):
        room = _EventEmitter()
        session = _EventEmitter()
        session.history = SimpleNamespace(add_message=Mock())
        session.interrupt = Mock()
        call_session_data = SimpleNamespace(
            caller_disconnected=False,
            recipient_data=SimpleNamespace(sip_identity="sip-caller"),
        )

        with patch(
            "utils.session_event_handlers.get_job_context",
            return_value=SimpleNamespace(room=room),
        ):
            register_session_event_handlers(
                session=session,
                call_session_data=call_session_data,
            )

        room.handlers["participant_disconnected"](
            SimpleNamespace(identity="sip-caller")
        )

        self.assertTrue(call_session_data.caller_disconnected)
        session.interrupt.assert_called_once_with(force=True)
        session.history.add_message.assert_called_once()

    def test_non_caller_disconnect_does_not_interrupt_agent_speech(self):
        room = _EventEmitter()
        session = _EventEmitter()
        session.history = SimpleNamespace(add_message=Mock())
        session.interrupt = Mock()
        call_session_data = SimpleNamespace(
            caller_disconnected=False,
            recipient_data=SimpleNamespace(sip_identity="sip-caller"),
        )

        with patch(
            "utils.session_event_handlers.get_job_context",
            return_value=SimpleNamespace(room=room),
        ):
            register_session_event_handlers(
                session=session,
                call_session_data=call_session_data,
            )

        room.handlers["participant_disconnected"](
            SimpleNamespace(identity="transfer-agent")
        )

        self.assertFalse(call_session_data.caller_disconnected)
        session.interrupt.assert_not_called()
        session.history.add_message.assert_not_called()
