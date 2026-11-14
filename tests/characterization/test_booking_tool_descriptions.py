import re

import pytest

from agents.single_agent import SingleAgent
from data import BusinessData, CallConfigData, CallSessionData
from tools.booking_tools import (
    cancel_appointment,
    get_my_appointment_list,
    modify_appointment,
)
from tools.dynamic_booking_tools import (
    create_new_appointment as dynamic_create_new_appointment,
)


@pytest.mark.parametrize("language", ["ko-KR", "en-US"])
@pytest.mark.parametrize(
    "center_context",
    ["", '검증용 센터 자료: 주차장 B2. 원문 표기: {client_name}, {"floor": 2}.'],
    ids=["without-knowledge", "with-knowledge"],
)
def test_single_agent_prompt_renders_runtime_context(language, center_context):
    business_data = BusinessData(
        client_name="프롬프트검증센터",
        client_phone="02-2345-6789",
        client_address="서울특별시 검증로 123",
    )
    call_config = CallConfigData(language=language)
    call_session_data = CallSessionData(
        ctx=None,
        business_data=business_data,
        call_config_data=call_config,
    )

    prompt = SingleAgent._build_instructions(call_session_data, center_context)

    assert business_data.client_name in prompt
    assert business_data.client_phone in prompt
    assert business_data.client_address in prompt
    assert call_config.get_language_prompt_string() in prompt

    if center_context:
        assert prompt.count(center_context) == 1
        assert center_context not in SingleAgent._build_instructions(
            call_session_data
        )

    # Knowledge is data: literal braces must survive without a second format pass.
    rendered_instructions = (
        prompt.replace(center_context, "") if center_context else prompt
    )
    assert not re.search(r"\{[a-z_][a-z_0-9]*\}", rendered_instructions)


@pytest.mark.parametrize("center_context", ["", "검증용 센터 안내 자료"])
def test_single_agent_prompt_uses_phone_counselor_response_rhythm(
    center_context,
):
    call_session_data = CallSessionData(
        ctx=None,
        business_data=BusinessData(client_name="검증센터"),
        call_config_data=CallConfigData(language="ko-KR"),
    )

    prompt = SingleAgent._build_instructions(call_session_data, center_context)

    assert "State the confirmed answer or outcome first" in prompt
    assert "end with a single question only when a next decision is needed" in prompt
    assert "restate a newly changed condition once" in prompt
    assert "Offer one chance for further questions only when" in prompt
    assert "do not present a submitted request as a confirmed booking" in prompt
    assert "if the call continues and the caller speaks again" in prompt
    assert (
        "Do not automatically ask whether they need anything else after each answer"
        in prompt
    )
    assert "If the caller explicitly declines the staff connection offer" in prompt
    assert "Related information does not substitute for the answer" in prompt
    assert "no word is duplicated back-to-back" in prompt
    assert "Automatically adding a closing question is prohibited" in prompt
    assert 'answer only "검증센터입니다."' in prompt
    assert "Waiting quietly for the next utterance after an answer is normal" in prompt
    assert "stretch general availability to cover the condition" in prompt
    assert "Do not talk as if reporting internal data or system status" in prompt
    assert '"정보가 확인되지 않습니다"' in prompt
    assert (
        "Do not automatically offer a staff connection or further help just "
        "because you don't know" in prompt
    )
    assert "If the caller already declined a staff connection" in prompt
    assert "do not repeat the connection offer or a closing question" in prompt
    assert "방문하실 때 기기와 충전기를 함께 가져오시면 돼요" in prompt
    assert "그 밖에 더 준비하실 것은 제가 지금 바로 확인해 드리기 어려워요" in prompt
    assert "Do not read a script; respond to what you just heard" in prompt
    assert "answer in verb-centered spoken style" in prompt
    assert "If only part is unclear, keep what you understood" in prompt
    assert "simpler, shorter, or with numbers broken into chunks" in prompt
    assert "Acknowledge discomfort or worry in a few words only when" in prompt
    # Speech register: Korean polite honorifics must still be required.
    assert "honorifics (존댓말)" in prompt


@pytest.mark.parametrize(
    "tool",
    [
        dynamic_create_new_appointment,
        get_my_appointment_list,
        modify_appointment,
        cancel_appointment,
    ],
)
def test_booking_tool_descriptions_hide_internal_identity_steps(tool):
    description = tool.info.description

    assert "Call this tool immediately" in description
    assert "Do not ask for or confirm any" in description
    assert "verification_code" not in description.lower()
    assert "identity verification" not in description.lower()
    assert "DTMF" not in description
