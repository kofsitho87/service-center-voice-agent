"""Keypad option side actions: SMS sending and end_call."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from agents.single_agent import SingleAgent
from data import DtmfSmsConfig, FlowNode, parse_dtmf_side_actions
from external_apis.sms_api import SmsSendResult
from tasks.greeting_dtmf_task import GreetingDtmfResult

SMS = {
    "subject": "[{client_name}] 주차 안내",
    "content": "{client_name} 주차 안내 {unknown}",
    "on_success_message": "문자로 보내드렸어요.",
}


@pytest.mark.parametrize(
    ("option", "expected_end_call"),
    [
        ({"next": "info_agent", "end_call": True}, True),
        ({"next": "info_agent", "end_call": "on"}, True),
        ({"next": "info_agent", "end_call": False}, False),
        ({"next": "info_agent", "end_call": "false"}, False),
        ({"next": "info_agent"}, False),
        ("info_agent", False),
    ],
)
def test_parse_end_call_flag(option, expected_end_call):
    assert parse_dtmf_side_actions(option)[1] is expected_end_call


def test_parse_sms_requires_content():
    assert parse_dtmf_side_actions({"next": "x", "sms": {"subject": "s"}})[0] is None
    sms, _ = parse_dtmf_side_actions({"next": "x", "sms": SMS})
    assert sms == DtmfSmsConfig(
        content=SMS["content"],
        subject=SMS["subject"],
        on_success_message=SMS["on_success_message"],
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            ["https://cdn.example.com/a.jpg", " https://cdn.example.com/b.png "],
            ["https://cdn.example.com/a.jpg", "https://cdn.example.com/b.png"],
        ),
        ("https://cdn.example.com/a.jpg", ["https://cdn.example.com/a.jpg"]),
        (["", "  ", None, 3], None),
        ([], None),
        (None, None),
        ({"url": "x"}, None),
    ],
)
def test_parse_sms_image_urls_is_optional(raw, expected):
    sms_value = {"content": "본문"}
    if raw is not None:
        sms_value["image_urls"] = raw
    sms, _ = parse_dtmf_side_actions({"next": "x", "sms": sms_value})
    assert sms.image_urls == expected


async def test_send_dtmf_sms_passes_image_urls():
    agent = _agent()
    config = DtmfSmsConfig(
        content="본문", image_urls=["https://cdn.example.com/hours.jpg"]
    )
    send = AsyncMock(return_value=SmsSendResult(success=True, message_type="MMS"))

    with patch("agents.single_agent.send_sms", send):
        await agent._send_dtmf_sms("6", config)

    assert send.await_args.kwargs["image_urls"] == ["https://cdn.example.com/hours.jpg"]


def _agent(caller_phone="010-0000-0101"):
    agent = object.__new__(SingleAgent)
    agent._call_session_data = SimpleNamespace(
        caller_disconnected=False,
        room_name="room-1",
        call_config_data=SimpleNamespace(language="ko-KR"),
        business_data=SimpleNamespace(
            site_id="demo-device-care", client_name="Example Device Care", client_address="가상 주소"
        ),
        recipient_data=SimpleNamespace(caller_phone=caller_phone, phone=None),
        agents_data=SimpleNamespace(user_request=None, dtmf_tool=None),
    )
    return agent


def _session():
    handle = SimpleNamespace(wait_for_playout=AsyncMock())
    return SimpleNamespace(
        say=Mock(return_value=handle),
        history=SimpleNamespace(add_message=Mock()),
        input=SimpleNamespace(set_audio_enabled=Mock()),
        shutdown=Mock(),
        generate_reply=Mock(),
    )


def _node(option):
    return FlowNode(
        id="greeting",
        type="greeting",
        name="인사말",
        input_method="dtmf",
        message="안녕하세요.",
        dtmf_options={"6": option},
    )


def _events(session, event_type):
    return [
        c.kwargs["extra"]
        for c in session.history.add_message.call_args_list
        if event_type in str(c.kwargs.get("extra"))
    ]


async def _select_six(agent, session, option, send):
    async def pick(**kwargs):
        return GreetingDtmfResult.from_key("6")

    with (
        patch.dict("os.environ", {"ENV": "local"}),
        patch.object(SingleAgent, "session", session),
        patch("agents.single_agent.GreetingDtmfTask", side_effect=pick),
        patch("agents.single_agent.send_sms", send),
        patch.object(SingleAgent, "_handle_agent_node", AsyncMock()) as agent_node,
    ):
        next_node = await agent._handle_greeting_node(_node(option))
        # Let background SMS / announcement tasks run inside the patches.
        for _ in range(5):
            await asyncio.sleep(0)
    return next_node, agent_node


async def test_end_call_on_sends_sms_then_hangs_up():
    agent, session = _agent(), _session()
    send = AsyncMock(return_value=SmsSendResult(success=True, message_type="LMS"))
    option = {
        "next": "info_agent",
        "on_select_message": "주차 정보를 안내해 드리겠습니다.",
        "sms": SMS,
        "end_call": True,
    }

    next_node, agent_node = await _select_six(agent, session, option, send)

    assert next_node is None
    agent_node.assert_not_called()
    send.assert_awaited_once_with(
        site_id="demo-device-care",
        receiver_phone_number="01000000101",
        subject="[Example Device Care] 주차 안내",
        content="Example Device Care 주차 안내 {unknown}",
        image_urls=None,
    )
    spoken = [c.args[0] for c in session.say.call_args_list]
    assert spoken == [
        "주차 정보를 안내해 드리겠습니다.",
        "문자로 보내드렸어요.",
        "통화를 종료하겠습니다.",
    ]
    session.shutdown.assert_called_once_with()
    assert _events(session, "SMS_SEND")[0]["agent_event"]["status"] == "SUCCESS"
    termination = _events(session, "CALL_TERMINATION")[0]["agent_event"]
    assert termination["details"] == {"actor": "SYSTEM", "reason": "DTMF_END_CALL"}


async def test_end_call_off_sends_sms_and_continues_to_next():
    agent, session = _agent(), _session()
    send = AsyncMock(return_value=SmsSendResult(success=True))
    option = {"next": "info_agent", "user_request": "주차", "sms": SMS}

    _, agent_node = await _select_six(agent, session, option, send)

    agent_node.assert_awaited_once()
    send.assert_awaited_once()
    session.shutdown.assert_not_called()
    session.say.assert_called_once_with(
        "문자로 보내드렸어요.", allow_interruptions=True
    )


async def test_failed_sms_still_ends_call_without_success_message():
    agent, session = _agent(), _session()
    send = AsyncMock(return_value=SmsSendResult(success=False, message="HTTP 500"))
    option = {"next": "info_agent", "sms": SMS, "end_call": True}

    await _select_six(agent, session, option, send)

    session.say.assert_called_once_with(
        "통화를 종료하겠습니다.", allow_interruptions=False
    )
    session.shutdown.assert_called_once_with()
    assert _events(session, "SMS_SEND")[0]["agent_event"]["status"] == "FAILED"


async def test_landline_caller_skips_sms():
    agent, session = _agent(caller_phone="0212345678"), _session()
    send = AsyncMock()
    option = {"next": "info_agent", "sms": SMS, "end_call": True}

    await _select_six(agent, session, option, send)

    send.assert_not_called()
    assert _events(session, "SMS_SEND")[0]["agent_event"]["status"] == "SKIPPED"
    session.shutdown.assert_called_once_with()


async def test_end_call_without_sms_hangs_up_after_message():
    agent, session = _agent(), _session()
    send = AsyncMock()
    option = {"next": "info_agent", "on_select_message": "안내", "end_call": "on"}

    await _select_six(agent, session, option, send)

    send.assert_not_called()
    assert [c.args[0] for c in session.say.call_args_list] == [
        "안내",
        "통화를 종료하겠습니다.",
    ]
    session.shutdown.assert_called_once_with()
