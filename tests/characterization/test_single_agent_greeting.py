from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, call, patch

from agents.single_agent import SingleAgent
from data import FlowNode
from tasks.greeting_dtmf_task import GreetingDtmfResult


class SingleAgentGreetingTest(IsolatedAsyncioTestCase):
    async def test_audio_input_is_disabled_during_greeting_and_restored(self):
        greeting_handle = SimpleNamespace(
            wait_for_playout=AsyncMock(),
            interrupted=False,
        )
        notice_handle = SimpleNamespace()
        session = SimpleNamespace(
            say=Mock(side_effect=[greeting_handle, notice_handle]),
            history=SimpleNamespace(add_message=Mock()),
            input=SimpleNamespace(set_audio_enabled=Mock()),
            shutdown=Mock(),
        )
        agent = object.__new__(SingleAgent)
        agent._call_session_data = SimpleNamespace(
            caller_disconnected=False,
            room_name="room-1",
            call_config_data=SimpleNamespace(language="ko-KR"),
        )
        node = FlowNode(
            id="greeting",
            type="greeting",
            name="인사말",
            input_method="voice",
            message="안녕하세요.",
        )

        with (
            patch.dict("os.environ", {"ENV": "dev"}),
            patch.object(SingleAgent, "session", session),
        ):
            next_node_id = await agent._handle_greeting_node(node)

        self.assertIsNone(next_node_id)
        self.assertEqual(
            session.input.set_audio_enabled.call_args_list,
            [call(False), call(True), call(True)],
        )
        self.assertEqual(
            session.say.call_args_list,
            [
                call("안녕하세요.", allow_interruptions=False),
                call(
                    "본 통화는 AI 응대로 진행되며, 통화 내용은 녹음됩니다.",
                    allow_interruptions=True,
                ),
            ],
        )

    async def test_local_voice_greeting_skips_recording_notice(self):
        greeting_handle = SimpleNamespace(
            wait_for_playout=AsyncMock(),
            interrupted=False,
        )
        session = SimpleNamespace(
            say=Mock(return_value=greeting_handle),
            history=SimpleNamespace(add_message=Mock()),
            input=SimpleNamespace(set_audio_enabled=Mock()),
            shutdown=Mock(),
        )
        agent = object.__new__(SingleAgent)
        agent._call_session_data = SimpleNamespace(
            caller_disconnected=False,
            room_name="room-1",
            call_config_data=SimpleNamespace(language="ko-KR"),
        )
        node = FlowNode(
            id="greeting",
            type="greeting",
            name="인사말",
            input_method="voice",
            message="안녕하세요.",
            next="info_agent",
        )

        with (
            patch.dict("os.environ", {"ENV": "local"}),
            patch.object(SingleAgent, "session", session),
        ):
            next_node_id = await agent._handle_greeting_node(node)

        self.assertEqual(next_node_id, "info_agent")
        session.say.assert_called_once_with("안녕하세요.", allow_interruptions=False)
        self.assertEqual(
            session.input.set_audio_enabled.call_args_list,
            [call(False), call(True)],
        )
        session.shutdown.assert_not_called()

    async def test_dtmf_greeting_notice_depends_on_environment(self):
        agent = object.__new__(SingleAgent)
        agent._call_session_data = SimpleNamespace(
            caller_disconnected=False,
            room_name="room-1",
            call_config_data=SimpleNamespace(language="ko-KR"),
        )
        node = FlowNode(
            id="greeting",
            type="greeting",
            name="인사말",
            input_method="dtmf",
            message="안녕하세요.",
            dtmf_options={"1": "info_agent"},
            next="fallback",
        )
        session = SimpleNamespace(history=SimpleNamespace(add_message=Mock()))

        async def no_selection(**kwargs):
            return GreetingDtmfResult(selected_key=None)

        for environment, expected_message in (
            ("local", "안녕하세요."),
            ("dev", "안녕하세요.본 통화는 AI 응대로 진행되며, 통화 내용은 녹음됩니다."),
        ):
            with (
                self.subTest(environment=environment),
                patch.dict("os.environ", {"ENV": environment}),
                patch.object(SingleAgent, "session", session),
                patch(
                    "agents.single_agent.GreetingDtmfTask", side_effect=no_selection
                ) as greeting_task,
            ):
                self.assertEqual(await agent._handle_greeting_node(node), "fallback")
                self.assertEqual(
                    greeting_task.call_args.kwargs["message"], expected_message
                )

    async def test_audio_input_is_restored_when_greeting_playout_fails(self):
        speech_handle = SimpleNamespace(
            wait_for_playout=AsyncMock(side_effect=RuntimeError("playout failed")),
            interrupted=False,
        )
        session = SimpleNamespace(
            say=Mock(return_value=speech_handle),
            history=SimpleNamespace(add_message=Mock()),
            input=SimpleNamespace(set_audio_enabled=Mock()),
            shutdown=Mock(),
        )
        agent = object.__new__(SingleAgent)
        agent._call_session_data = SimpleNamespace(
            caller_disconnected=False,
            room_name="room-1",
            call_config_data=SimpleNamespace(language="ko-KR"),
        )
        node = FlowNode(
            id="greeting",
            type="greeting",
            name="인사말",
            message="안녕하세요.",
        )

        with (
            patch.object(SingleAgent, "session", session),
            self.assertRaisesRegex(RuntimeError, "playout failed"),
        ):
            await agent._handle_greeting_node(node)

        self.assertEqual(
            session.input.set_audio_enabled.call_args_list,
            [call(False), call(True)],
        )

    async def test_audio_input_is_restored_when_caller_disconnected(self):
        greeting_handle = SimpleNamespace(
            wait_for_playout=AsyncMock(),
            interrupted=True,
        )
        session = SimpleNamespace(
            say=Mock(return_value=greeting_handle),
            history=SimpleNamespace(add_message=Mock()),
            input=SimpleNamespace(set_audio_enabled=Mock()),
            shutdown=Mock(),
        )
        agent = object.__new__(SingleAgent)
        agent._call_session_data = SimpleNamespace(
            caller_disconnected=True,
            room_name="room-1",
            call_config_data=SimpleNamespace(language="ko-KR"),
        )
        node = FlowNode(
            id="greeting",
            type="greeting",
            name="인사말",
            message="안녕하세요.",
        )

        with patch.object(SingleAgent, "session", session):
            next_node_id = await agent._handle_greeting_node(node)

        self.assertIsNone(next_node_id)
        self.assertEqual(
            session.input.set_audio_enabled.call_args_list,
            [call(False), call(True)],
        )
        session.say.assert_called_once_with(
            "안녕하세요.",
            allow_interruptions=False,
        )
        session.shutdown.assert_called_once_with(drain=False)

    async def test_recording_notice_is_interrupted_when_caller_disconnects_during_say(
        self,
    ):
        greeting_handle = SimpleNamespace(
            wait_for_playout=AsyncMock(),
            interrupted=False,
        )
        recording_notice_handle = SimpleNamespace(interrupt=Mock())
        session = SimpleNamespace(
            history=SimpleNamespace(add_message=Mock()),
            input=SimpleNamespace(set_audio_enabled=Mock()),
            shutdown=Mock(),
        )
        agent = object.__new__(SingleAgent)
        agent._call_session_data = SimpleNamespace(
            caller_disconnected=False,
            room_name="room-1",
            call_config_data=SimpleNamespace(language="ko-KR"),
        )

        def say(message, **kwargs):
            if message == "안녕하세요.":
                return greeting_handle
            agent._call_session_data.caller_disconnected = True
            return recording_notice_handle

        session.say = Mock(side_effect=say)
        node = FlowNode(
            id="greeting",
            type="greeting",
            name="인사말",
            input_method="voice",
            message="안녕하세요.",
        )

        with patch.object(SingleAgent, "session", session):
            next_node_id = await agent._handle_greeting_node(node)

        self.assertIsNone(next_node_id)
        recording_notice_handle.interrupt.assert_called_once_with(force=True)
        session.shutdown.assert_called_once_with(drain=False)

    async def test_direct_input_ignores_misconfigured_booking_action_in_history(self):
        session = SimpleNamespace(
            history=SimpleNamespace(add_message=Mock()),
            input=SimpleNamespace(set_audio_enabled=Mock()),
        )
        agents_data = SimpleNamespace(user_request="stale", dtmf_tool="booking_create")
        agent = object.__new__(SingleAgent)
        agent._call_session_data = SimpleNamespace(
            caller_disconnected=False,
            room_name="room-1",
            call_config_data=SimpleNamespace(language="ko-KR"),
            agents_data=agents_data,
        )
        agent._play_on_select_message = AsyncMock()
        node = FlowNode(
            id="greeting",
            type="greeting",
            name="Greeting",
            input_method="dtmf",
            dtmf_options={
                "9": {
                    "next": "direct_input",
                    "tool": "booking_cancel",
                    "user_request": "Cancel",
                }
            },
        )

        async def select_direct_input(**kwargs):
            return GreetingDtmfResult.from_key("9")

        with (
            patch.object(SingleAgent, "session", session),
            patch(
                "agents.single_agent.GreetingDtmfTask", side_effect=select_direct_input
            ),
        ):
            next_node = await agent._handle_greeting_node(node)

        self.assertIsNone(next_node)
        event = session.history.add_message.call_args.kwargs["extra"]["agent_event"]
        self.assertEqual(event["status"], "SELECTED")
        self.assertIsNone(event["details"]["action_key"])
        self.assertIsNone(agents_data.user_request)
        self.assertIsNone(agents_data.dtmf_tool)
