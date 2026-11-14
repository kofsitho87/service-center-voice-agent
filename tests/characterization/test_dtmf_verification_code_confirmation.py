from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from tasks.dtmf_verification_code_task import GetDtmfResult, GetVerificationCodeDtmfTask
from tasks.verification_code_confirmation_task import (
    build_verification_code_confirmation_announcement,
)
from utils.i18n import t


@pytest.mark.asyncio
async def test_valid_dtmf_verification_code_uses_deterministic_confirmation_announcement():
    task = GetVerificationCodeDtmfTask(num_digits=6)
    task._lang = "ko-KR"

    session = SimpleNamespace(
        input=SimpleNamespace(set_audio_enabled=Mock()),
        history=SimpleNamespace(add_message=Mock()),
        say=Mock(),
        generate_reply=Mock(),
    )
    task._active_session = session
    update_instructions = AsyncMock()
    result = GetDtmfResult(
        user_input="0 2 0 2 0 2",
        year=2002,
        month=2,
        day=2,
        normalized_date="2002-02-02",
    )
    announcement = build_verification_code_confirmation_announcement(
        "020202",
        task._lang,
    )

    with (
        patch.object(GetVerificationCodeDtmfTask, "session", session),
        patch.object(
            GetVerificationCodeDtmfTask,
            "update_instructions",
            update_instructions,
        ),
    ):
        await task._start_confirmation(result)

    assert task._confirming is True
    assert task._collected_result is result
    assert not task._reply_tasks
    session.input.set_audio_enabled.assert_called_once_with(True)
    update_instructions.assert_awaited_once_with(
        t(
            "verification_code.confirm_instructions",
            task._lang,
            announcement=announcement,
        )
    )
    session.history.add_message.assert_called_once_with(
        role="developer",
        content="",
        extra={
            "agent_event": {
                "name": "VERIFICATION_CODE_DTMF_TASK",
                "details": {"date": "2002-02-02"},
                "status": "CONFIRM",
            }
        },
    )
    session.say.assert_called_once_with(
        announcement,
        allow_interruptions=False,
        add_to_chat_ctx=True,
    )
    session.generate_reply.assert_not_called()


@pytest.mark.parametrize(
    ("last_digit", "spoken_pair", "josa"),
    [
        ("0", "일공", "이"),
        ("1", "일일", "이"),
        ("2", "일이", "가"),
        ("3", "일삼", "이"),
        ("4", "일사", "가"),
        ("5", "일오", "가"),
        ("6", "일육", "이"),
        ("7", "일칠", "이"),
        ("8", "일팔", "이"),
        ("9", "일구", "가"),
    ],
)
def test_confirmation_reads_digit_pairs_with_matching_josa(
    last_digit, spoken_pair, josa
):
    assert build_verification_code_confirmation_announcement(f"91071{last_digit}", "ko-KR") == (
        f"입력하신 번호가 구일 공칠 {spoken_pair}{josa} 맞는지 네, "
        "아니오로 말씀해 주세요."
    )


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (
            "ko-KR",
            "입력하신 번호가 구일 공칠 공칠이 맞는지 네, 아니오로 말씀해 주세요.",
        ),
        (
            None,
            "입력하신 번호가 구일 공칠 공칠이 맞는지 네, 아니오로 말씀해 주세요.",
        ),
        (
            "en-US",
            "You entered 9 1, 0 7, 0 7. Please say yes if that's correct, or no if it's not.",
        ),
    ],
)
def test_confirmation_reads_entered_digits_in_pairs(language, expected):
    assert build_verification_code_confirmation_announcement("910707", language) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "purpose", "expected"),
    [
        ("ko-KR", None, "데모 확인번호 6자리를 입력해 주세요."),
        (
            "ko-KR",
            "예약 신청을 위해 본인 확인이 필요합니다.",
            "예약 신청을 위해 본인 확인이 필요합니다. 데모 확인번호 6자리를 입력해 주세요.",
        ),
        ("en-US", None, "Please enter your 6-digit demo verification code."),
        (
            "en-US",
            "To book an appointment, I need to verify your identity.",
            "To book an appointment, I need to verify your identity. "
            "Please enter your 6-digit demo verification code.",
        ),
    ],
)
async def test_first_announcement_appends_only_the_keypad_instruction(
    language, purpose, expected
):
    task = GetVerificationCodeDtmfTask(num_digits=6, purpose=purpose, dtmf_source="sip")
    session = SimpleNamespace(
        userdata=SimpleNamespace(call_config_data=SimpleNamespace(language=language)),
        input=SimpleNamespace(set_audio_enabled=Mock()),
        history=SimpleNamespace(add_message=Mock()),
        on=Mock(),
        say=Mock(),
    )
    room = SimpleNamespace(on=Mock())
    with (
        patch(
            "tasks.dtmf_verification_code_task.get_job_context",
            return_value=SimpleNamespace(room=room),
        ),
        patch.object(GetVerificationCodeDtmfTask, "session", session),
    ):
        await task._enter_collection()

    session.say.assert_called_once_with(expected, add_to_chat_ctx=True)
