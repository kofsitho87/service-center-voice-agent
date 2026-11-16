from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from livekit.agents import AgentTask

from data import BookingMemoContext
from tasks.leave_message_task import LeaveMessageTask, _is_valid_mmdd_hhmm
from tasks.transfer_reason_task import TransferPurpose
from utils.i18n import t


def _task(
    monkeypatch,
    context=None,
    *,
    action_key="booking_create",
    collect=True,
    language="ko-KR",
    transfer_purpose=None,
    is_after_transfer_failure=False,
    entry_reason=None,
    additional_instructions=None,
):
    def fake_init(self, *, instructions, chat_ctx, tools):
        self._test_instructions = instructions
        self._test_tools = tools

    monkeypatch.setattr(AgentTask, "__init__", fake_init)
    task = LeaveMessageTask(
        is_after_transfer_failure=is_after_transfer_failure,
        action_key=action_key,
        collect_desired_datetime=collect,
        language=language,
        booking_context=context,
        transfer_purpose=transfer_purpose,
        entry_reason=entry_reason,
        additional_instructions=additional_instructions,
    )
    task.complete = Mock()
    task._play_closing_message = AsyncMock()
    return task


def _tool(task, name):
    return next(tool for tool in task._test_tools if tool.info.name == name)._func


def _ctx():
    return SimpleNamespace(session=SimpleNamespace(say=Mock()))


@pytest.mark.parametrize("language", ["ko-KR", "en-US"])
def test_booking_context_renders_and_opening_uses_condition(monkeypatch, language):
    context = BookingMemoContext(
        service_name="Inspection",
        operator_name="Advisor",
        time_condition="weekday evenings at 19:30",
        searched_ranges=["2026-09-14~2026-09-20"],
    )
    task = _task(monkeypatch, context, language=language)
    assert "BOOKING_CONTEXT:" in task._test_instructions
    assert "weekday evenings at 19:30" in task._test_instructions
    assert context.time_condition in task.opening_line


@pytest.mark.parametrize("language", ["ko-KR", "en-US"])
def test_empty_booking_context_keeps_generic_opening(monkeypatch, language):
    task = _task(monkeypatch, language=language)
    assert "BOOKING_CONTEXT:" in task._test_instructions
    assert task.opening_line == t(
        "leave_message.opening_datetime.booking_create", language
    )
    assert "weekday evenings" not in task.opening_line


_CALLER = TransferPurpose(code="caller_stated", detail="예약 변경이 안 돼요")
_SYSTEM = TransferPurpose(code="urgent_handoff")
_UNPROVIDED = TransferPurpose(code="unprovided")


def test_caller_stated_purpose_after_final_failure_skips_repeat_apology(
    monkeypatch,
):
    task = _task(
        monkeypatch,
        action_key=None,
        collect=False,
        transfer_purpose=_CALLER,
        is_after_transfer_failure=True,
        entry_reason="final_failure",
    )

    assert task.opening_line == t(
        "leave_message.progress_notice.caller_stated", "ko-KR"
    )
    assert "어려" not in task.opening_line
    assert "TRANSFER_PURPOSE=예약 변경이 안 돼요" in task._test_instructions
    assert "TRANSFER_PURPOSE_SOURCE=caller" in task._test_instructions
    assert "REQUEST_SOURCE=transfer_purpose" in task._test_instructions
    assert "MEMO_ENTRY_REASON=final_failure" in task._test_instructions


@pytest.mark.parametrize("language", ["ko-KR", "en-US"])
@pytest.mark.parametrize(
    ("purpose", "is_after_failure", "entry_reason", "expected_keys"),
    [
        # memo_only: never mention a failed connection; system purpose is not
        # something the caller said.
        (_SYSTEM, False, "memo_only", ["leave_message.progress_notice.default"]),
        (None, False, "memo_only", ["leave_message.progress_notice.default"]),
        (_UNPROVIDED, False, "memo_only", ["leave_message.progress_notice.default"]),
        # preflight: nothing announced yet, so the apology is spoken once.
        (
            _CALLER,
            True,
            "preflight_failure",
            [
                "leave_message.transfer_failure_prefix",
                "leave_message.progress_notice.caller_stated",
            ],
        ),
        (
            _SYSTEM,
            True,
            "preflight_failure",
            # default notice already states the connection is not possible,
            # so no apology prefix is added (avoids a duplicate).
            ["leave_message.progress_notice.default"],
        ),
        (
            _UNPROVIDED,
            True,
            "preflight_failure",
            ["leave_message.progress_notice.default"],
        ),
        # final failure: transfer.failed was already spoken.
        (
            _CALLER,
            True,
            "final_failure",
            ["leave_message.progress_notice.caller_stated"],
        ),
        (_SYSTEM, True, "final_failure", ["leave_message.progress_notice.default"]),
        (_UNPROVIDED, True, "final_failure", ["leave_message.progress_notice.default"]),
        # caller exit: acknowledge the caller's choice, no failure wording.
        (
            _CALLER,
            True,
            "caller_exit",
            ["leave_message.progress_notice.caller_exit.caller_stated"],
        ),
        (
            _SYSTEM,
            True,
            "caller_exit",
            ["leave_message.progress_notice.caller_exit.default"],
        ),
    ],
)
def test_opening_matrix_by_purpose_source_and_entry_reason(
    monkeypatch, language, purpose, is_after_failure, entry_reason, expected_keys
):
    task = _task(
        monkeypatch,
        action_key=None,
        collect=False,
        language=language,
        transfer_purpose=purpose,
        is_after_transfer_failure=is_after_failure,
        entry_reason=entry_reason,
    )

    assert task.opening_line == " ".join(t(key, language) for key in expected_keys)
    assert f"MEMO_ENTRY_REASON={entry_reason}" in task._test_instructions


@pytest.mark.parametrize("language", ["ko-KR", "en-US"])
def test_system_purpose_on_memo_only_route_is_background_only(monkeypatch, language):
    task = _task(
        monkeypatch,
        action_key=None,
        collect=False,
        language=language,
        transfer_purpose=_SYSTEM,
    )

    assert task.entry_reason == "memo_only"
    assert "말씀해 주신" not in task.opening_line
    assert "told me" not in task.opening_line
    assert "TRANSFER_PURPOSE_SOURCE=system" in task._test_instructions
    assert f"TRANSFER_PURPOSE={_SYSTEM.describe(language)}" in task._test_instructions
    # A routing label must not become the caller's request label.
    assert task.request_label is None
    assert "REQUEST_SOURCE=chat_ctx" in task._test_instructions


def test_unprovided_purpose_renders_as_none_source(monkeypatch):
    task = _task(
        monkeypatch,
        action_key=None,
        collect=False,
        transfer_purpose=_UNPROVIDED,
        is_after_transfer_failure=True,
        entry_reason="final_failure",
    )
    assert "TRANSFER_PURPOSE_SOURCE=none" in task._test_instructions
    assert "TRANSFER_PURPOSE=\n" in task._test_instructions


@pytest.mark.parametrize(
    ("entry_reason", "has_prefix"),
    [
        ("preflight_failure", True),
        ("final_failure", False),
        ("caller_exit", False),
    ],
)
def test_action_key_notice_adds_apology_only_when_not_announced(
    monkeypatch, entry_reason, has_prefix
):
    task = _task(
        monkeypatch,
        action_key="booking_modify",
        collect=False,
        transfer_purpose=_CALLER,
        is_after_transfer_failure=True,
        entry_reason=entry_reason,
    )
    notice = t("leave_message.progress_notice.booking_modify", "ko-KR")
    prefix = t("leave_message.transfer_failure_prefix", "ko-KR")
    assert task.opening_line == (f"{prefix} {notice}" if has_prefix else notice)
    # action_key label wins; the caller's words stay in TRANSFER_PURPOSE.
    assert task.request_label == t(
        "leave_message.request_label.booking_modify", "ko-KR"
    )
    assert "TRANSFER_PURPOSE_SOURCE=caller" in task._test_instructions


@pytest.mark.parametrize(
    ("entry_reason", "has_prefix"),
    [
        ("preflight_failure", True),
        ("final_failure", False),
        ("caller_exit", False),
        ("memo_only", False),
    ],
)
def test_datetime_opener_apology_follows_entry_reason(
    monkeypatch, entry_reason, has_prefix
):
    task = _task(
        monkeypatch,
        is_after_transfer_failure=entry_reason != "memo_only",
        entry_reason=entry_reason,
    )
    opening = t("leave_message.opening_datetime.booking_create", "ko-KR")
    prefix = t("leave_message.transfer_failure_prefix", "ko-KR")
    assert task.opening_line == (f"{prefix} {opening}" if has_prefix else opening)


def test_missing_entry_reason_falls_back_to_failure_flag(monkeypatch):
    assert _task(monkeypatch, collect=False).entry_reason == "memo_only"
    task = _task(monkeypatch, collect=False, is_after_transfer_failure=True)
    assert task.entry_reason == "preflight_failure"


@pytest.mark.parametrize("language", ["ko-KR", "en-US"])
def test_modify_context_without_condition_uses_modify_opener(monkeypatch, language):
    context = BookingMemoContext(
        service_name="Inspection",
        detail="Advisor information missing",
        exit_reason_code="reservation_operator_missing",
    )
    task = _task(monkeypatch, context, action_key="booking_modify", language=language)
    assert task.opening_line == t(
        "leave_message.opening_datetime.booking_modify", language
    )
    assert "BOOKING_CONTEXT:" in task._test_instructions
    assert "reservation_operator_missing" in task._test_instructions


@pytest.mark.parametrize(
    ("operator", "service", "expected"),
    [
        # The opening line uses 상담사 and 서비스.
        ("정진수", "휴대폰 점검", "정진수 상담사 휴대폰 점검 예약을"),
        (None, "휴대폰 점검", "휴대폰 점검 예약을"),
        (None, None, "요청하신 서비스 예약을"),
    ],
)
def test_context_opening_survives_missing_names(
    monkeypatch, operator, service, expected
):
    context = BookingMemoContext(
        service_name=service,
        operator_name=operator,
        time_condition="월요일이나 수요일 오전 11시 45분",
    )
    task = _task(monkeypatch, context)
    assert task.opening_line.startswith(expected)
    assert "  " not in task.opening_line
    assert "월요일이나 수요일 오전 11시 45분 조건으로" in task.opening_line


@pytest.mark.asyncio
async def test_confirmed_condition_submits_without_datetime(monkeypatch):
    context = BookingMemoContext(
        service_name="Inspection",
        operator_name="Advisor",
        time_condition="평일 저녁 7시 30분",
        searched_ranges=["2026-09-14~2026-09-20"],
    )
    task = _task(monkeypatch, context)
    ctx = _ctx()
    await _tool(task, "confirm_desired_condition")(ctx)
    await _tool(task, "submit_booking_contact")(ctx)
    result = task.complete.call_args.args[0]
    assert result.desired_datetime is None
    assert result.desired_condition == "평일 저녁 7시 30분"
    assert "조회한 기간에 가능 일정 없음" in result.message


@pytest.mark.asyncio
async def test_datetime_format_remains_strict(monkeypatch):
    task = _task(monkeypatch)
    assert not _is_valid_mmdd_hhmm("weekday evening")
    assert not _is_valid_mmdd_hhmm("09-14 7:30")
    assert _is_valid_mmdd_hhmm("09-14 19:30")
    ctx = _ctx()
    response = await _tool(task, "set_desired_datetime")(ctx, "09-14 7:30")
    assert "먼저" not in response
    assert task._pending_datetime is None
    ctx.session.say.assert_not_called()


@pytest.mark.asyncio
async def test_flexible_condition_can_be_confirmed_and_submitted(monkeypatch):
    task = _task(monkeypatch)
    ctx = _ctx()

    response = await _tool(task, "set_desired_condition")(
        ctx, "차민호 상담사님 되는  아무 평일 오후"
    )
    assert response is None
    ctx.session.say.assert_called_once_with(
        "차민호 상담사님 되는 아무 평일 오후으로 말씀하신게 맞으신가요?"
    )

    response = await _tool(task, "confirm_desired_condition")(ctx)
    assert response == t("leave_message.condition_confirm", "ko-KR")

    await _tool(task, "submit_booking_contact")(ctx, "차민호 상담사님 점검 요청")
    result = task.complete.call_args.args[0]
    assert result.desired_datetime is None
    assert result.desired_condition == "차민호 상담사님 되는 아무 평일 오후"
    assert result.message == "차민호 상담사님 점검 요청"


@pytest.mark.parametrize(
    ("language", "condition"),
    [("ko-KR", "내일 오전"), ("en-US", "tomorrow morning")],
)
@pytest.mark.parametrize("existing_condition", [None, "weekday afternoons"])
@pytest.mark.asyncio
async def test_confirm_uses_latest_condition_and_can_be_repeated(
    monkeypatch, language, condition, existing_condition
):
    context = (
        BookingMemoContext(time_condition=existing_condition)
        if existing_condition
        else None
    )
    task = _task(monkeypatch, context, language=language)
    ctx = _ctx()
    await _tool(task, "set_desired_condition")(ctx, condition)

    for _ in range(2):
        response = await _tool(task, "confirm_desired_condition")(ctx)
        assert response == t("leave_message.condition_confirm", language)
        assert task._pending_condition == condition
        assert task._pending_datetime is None
        task.complete.assert_not_called()

    await _tool(task, "submit_booking_contact")(ctx)
    result = task.complete.call_args.args[0]
    assert result.desired_condition == condition
    assert result.desired_datetime is None


@pytest.mark.parametrize(
    "context",
    [None, BookingMemoContext(), BookingMemoContext(time_condition="   ")],
)
@pytest.mark.asyncio
async def test_confirm_without_condition_preserves_empty_state(monkeypatch, context):
    task = _task(monkeypatch, context)
    response = await _tool(task, "confirm_desired_condition")(SimpleNamespace())

    assert response == t("leave_message.condition_not_set", "ko-KR")
    assert task._pending_condition is None
    assert task._pending_datetime is None
    task.complete.assert_not_called()


@pytest.mark.asyncio
async def test_condition_confirmation_does_not_replace_new_datetime(monkeypatch):
    context = BookingMemoContext(time_condition="weekday afternoons")
    task = _task(monkeypatch, context)
    ctx = _ctx()
    await _tool(task, "set_desired_condition")(ctx, "내일 오전")
    await _tool(task, "set_desired_datetime")(ctx, "10-03 10:00")

    response = await _tool(task, "confirm_desired_condition")(ctx)
    assert response == t("leave_message.condition_not_set", "ko-KR")
    assert task._pending_condition is None
    assert task._pending_datetime == "10-03 10:00"
    task.complete.assert_not_called()

    await _tool(task, "submit_booking_contact")(ctx)
    result = task.complete.call_args.args[0]
    assert result.desired_datetime == "10-03 10:00"
    assert result.desired_condition is None


@pytest.mark.asyncio
async def test_switching_to_datetime_clears_pending_condition(monkeypatch):
    task = _task(monkeypatch)
    ctx = _ctx()
    await _tool(task, "set_desired_condition")(ctx, "평일 오후")
    await _tool(task, "set_desired_datetime")(ctx, "09-24 14:00")
    assert task._pending_condition is None
    assert task._pending_datetime == "09-24 14:00"


@pytest.mark.parametrize("language", ["ko-KR", "en-US"])
@pytest.mark.parametrize("timing_kind", ["datetime", "condition"])
@pytest.mark.asyncio
async def test_confirmed_timing_submits_without_additional_message_turn(
    monkeypatch, language, timing_kind
):
    task = _task(monkeypatch, language=language)
    ctx = _ctx()
    if timing_kind == "datetime":
        response = await _tool(task, "set_desired_datetime")(ctx, "10-03 10:00")
    else:
        condition = "내일 오전" if language == "ko-KR" else "tomorrow morning"
        response = await _tool(task, "set_desired_condition")(ctx, condition)

    # The tool speaks the question itself and asks the LLM for no follow-up reply.
    assert response is None
    ctx.session.say.assert_called_once()
    assert ctx.session.say.call_args.args[0].endswith("?")
    task.complete.assert_not_called()
    task._play_closing_message.assert_not_awaited()

    # Handle the caller's positive confirmation without another message question.
    if timing_kind == "condition":
        instruction = await _tool(task, "confirm_desired_condition")(ctx)
        assert "?" not in instruction
        assert "Do not read this result aloud" in instruction
        assert "call submit_booking_contact now" in instruction
        task.complete.assert_not_called()
        task._play_closing_message.assert_not_awaited()

    response = await _tool(task, "submit_booking_contact")(ctx)
    assert response is None
    task._play_closing_message.assert_awaited_once_with(ctx)
    task.complete.assert_called_once()
    result = task.complete.call_args.args[0]
    assert result.message == t("leave_message.request_label.booking_create", language)
    if timing_kind == "datetime":
        assert result.desired_datetime == "10-03 10:00"
        assert result.desired_condition is None
    else:
        assert result.desired_datetime is None
        assert result.desired_condition == condition


@pytest.mark.parametrize("language", ["ko-KR", "en-US"])
def test_datetime_prompt_skips_optional_message_but_preserves_required_details(
    monkeypatch, language
):
    task = _task(
        monkeypatch,
        language=language,
        additional_instructions="Collect the caller's name and verification code.",
    )
    instructions = task._test_instructions
    assert "call `submit_booking_contact` in the SAME turn" in instructions
    assert "do NOT speak it aloud" in instructions
    assert "collect any missing required details before submitting" in instructions
    assert "Collect the caller's name and verification code." in instructions
    assert (
        "Store the corrected concrete date/time or flexible condition" in instructions
    )
    assert "ASKING is MANDATORY" not in instructions
    assert "추가로 상담원에게 전달하고 싶은 내용이 있을까요?" not in instructions


@pytest.mark.parametrize("language", ["ko-KR", "en-US"])
@pytest.mark.asyncio
async def test_message_only_still_collects_caller_message(monkeypatch, language):
    task = _task(monkeypatch, collect=False, language=language)
    ctx = SimpleNamespace(disallow_interruptions=Mock())
    assert [tool.info.name for tool in task._test_tools] == ["collect_leave_message"]
    assert task.opening_line == t(
        "leave_message.progress_notice.booking_create", language
    )

    await _tool(task, "collect_leave_message")(ctx, "Please call me about my booking.")
    result = task.complete.call_args.args[0]
    assert result.scenario == "leave_message_collected"
    assert result.message == "Please call me about my booking."
    task._play_closing_message.assert_awaited_once_with(ctx)


@pytest.mark.parametrize("language", ["ko-KR", "en-US"])
@pytest.mark.asyncio
async def test_datetime_confirmation_is_spoken_by_tool_not_llm(monkeypatch, language):
    """An empty post-tool LLM reply would leave the call silent.

    The confirmation must be spoken by the tool itself; returning None keeps
    LiveKit from requesting a follow-up LLM reply that could be empty.
    """
    task = _task(monkeypatch, language=language)
    ctx = _ctx()

    response = await _tool(task, "set_desired_datetime")(ctx, "05-30 07:00")

    assert response is None
    ctx.session.say.assert_called_once()
    spoken = ctx.session.say.call_args.args[0]
    assert spoken.endswith("?")
    assert ("오전 7시" if language == "ko-KR" else "7") in spoken
    assert task._pending_datetime == "05-30 07:00"
    task.complete.assert_not_called()


@pytest.mark.asyncio
async def test_empty_condition_still_returns_text_for_llm(monkeypatch):
    task = _task(monkeypatch)
    ctx = _ctx()

    response = await _tool(task, "set_desired_condition")(ctx, "   ")

    assert response == t("leave_message.condition_not_set", "ko-KR")
    ctx.session.say.assert_not_called()
