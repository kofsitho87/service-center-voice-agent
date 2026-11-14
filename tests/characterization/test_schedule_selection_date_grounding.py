"""Caller dates stay grounded across schedule-selection inference and tools."""

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from livekit.agents import Agent, ModelSettings, llm

from tasks.booking.dynamic_dates import caller_date_note, resolve_caller_dates
from tasks.operator_schedule_selection_task import OperatorScheduleSelectionTask

TODAY = dt.date(2026, 10, 29)
HORIZON = dt.date(2026, 11, 27)


class FixedDatetime(dt.datetime):
    instant = dt.datetime(2026, 10, 29, 7, tzinfo=dt.timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.instant.astimezone(tz)


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr("tasks.schedule_selection_base.datetime", FixedDatetime)


def _task(monkeypatch, available_times=(), *, language_code="ko-KR"):
    api = AsyncMock(
        return_value=SimpleNamespace(
            success=True,
            data=SimpleNamespace(availableTimes=list(available_times)),
            message="ok",
        )
    )
    monkeypatch.setattr(
        "tasks.operator_schedule_selection_task.api_get_operator_available_times", api
    )
    session = SimpleNamespace(
        history=SimpleNamespace(add_message=Mock()),
        say=Mock(),
        generate_reply=Mock(),
        userdata=SimpleNamespace(business_data=SimpleNamespace(client_id=9001)),
    )
    monkeypatch.setattr(
        OperatorScheduleSelectionTask, "session", property(lambda self: session)
    )
    task = OperatorScheduleSelectionTask(
        operator_id=955,
        reservation_data={},
        language_code=language_code,
        search_start_date=TODAY.isoformat(),
        search_end_date=HORIZON.isoformat(),
    )
    completed = []
    monkeypatch.setattr(task, "complete", completed.append)
    return task, api, completed


def _say(task, text):
    task._chat_ctx.add_message(role="user", content=text)


async def _search(task, start, end=None, **kwargs):
    return await task.search_preferred_schedules._func(
        task,
        SimpleNamespace(),
        start_date=start,
        end_date=start if end is None else end,
        **kwargs,
    )


def _load_snapshot(task, slots):
    task._availability_loaded = True
    task._schedules = [{"time": slot, "advisor_id": 955} for slot in slots]


@pytest.mark.asyncio
async def test_llm_node_inserts_note_after_latest_caller_without_mutating_context(
    monkeypatch,
):
    task, _, _ = _task(monkeypatch)
    _say(task, "다음 주 화요일로요")
    chat = llm.ChatContext()
    chat.add_message(role="user", content="예약 변경이요", created_at=1)
    latest = chat.add_message(
        role="user", content="이번 주 금요일 오전 10시 반으로요", created_at=2
    )
    chat.add_message(role="assistant", content="확인하겠습니다", created_at=3)
    original = list(chat.items)
    seen = []
    bound_logger = Mock()
    monkeypatch.setattr("tasks.schedule_selection_base.logger.bind", bound_logger)

    async def default(agent, chat_ctx, tools, model_settings):
        seen.append(chat_ctx)
        yield "확인하겠습니다."

    monkeypatch.setattr(Agent.default, "llm_node", default)
    for _ in range(2):
        assert [chunk async for chunk in task.llm_node(chat, [], ModelSettings())] == [
            "확인하겠습니다."
        ]
    assert chat.items == original
    for noted in seen:
        assert noted is not chat
        assert [item.role for item in noted.items] == [
            "user",
            "user",
            "system",
            "assistant",
        ]
        note = noted.items[2]
        assert note.created_at == latest.created_at + 1e-3
        assert note.text_content == (
            "[날짜 해석] 고객 발화의 '이번 주 금요일' = 2026-10-30(금요일). "
            "날짜 인자(start_date/end_date/date)에는 이 날짜를 그대로 쓰고 "
            "직접 계산하지 마세요."
        )
    bound_logger.assert_called_once_with(
        task=task._TASK_TAG,
        stage="caller_date_note",
        hints=[("이번 주 금요일", "2026-10-30", "ok")],
    )


def test_note_uses_latest_nonblank_user_and_skips_plain_reply(monkeypatch):
    task, _, _ = _task(monkeypatch)
    task._chat_ctx.add_message(role="user", content="이번 주 금요일로요", created_at=1)
    task._chat_ctx.add_message(role="user", content=" ", created_at=2)
    assert (
        "2026-10-30"
        in task._with_caller_date_note(task.chat_ctx).items[-2].text_content
    )
    _say(task, "네")
    chat = task.chat_ctx
    assert task._with_caller_date_note(chat) is chat


@pytest.mark.asyncio
async def test_search_corrects_friday_once_per_caller_message(monkeypatch):
    task, api, _ = _task(monkeypatch)
    _say(task, "이번 주 금요일 오전 10시 반으로요")
    first = await _search(task, "2026-10-31")
    assert api.call_args.kwargs["start_date"] == "2026-10-30"
    assert api.call_args.kwargs["end_date"] == "2026-10-30"
    assert first["status"] == "unavailable"
    assert first["date_corrected"]["from"] == "2026-10-31"
    assert first["date_corrected"]["to"] == "2026-10-30"
    assert first["date_corrected"]["phrase"] == "이번 주 금요일"
    assert (
        "이 결과의 user_message로 안내하세요" in first["date_corrected"]["instruction"]
    )

    second = await _search(task, "2026-10-31")
    assert api.call_args.kwargs["start_date"] == "2026-10-31"
    assert "date_corrected" not in second
    _say(task, "이번 주 금요일이요")
    third = await _search(task, "2026-10-31")
    assert third["date_corrected"]["to"] == "2026-10-30"


@pytest.mark.asyncio
async def test_corrected_search_stages_exact_time_on_friday(monkeypatch):
    task, _, _ = _task(monkeypatch, ["2026-10-30T10:30:00.000Z"])
    _say(task, "이번 주 금요일 오전 10시 반으로요")
    result = await _search(
        task, "2026-10-31", after_time="10:30", before_time="10:30", weekdays=["fri"]
    )
    assert result["response_type"] == "pending_confirmation"
    assert result["appointment_datetime"] == "2026-10-30 10:30:00"
    assert result["date_corrected"]["to"] == "2026-10-30"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "utterance",
    [
        "11월 20일 예약 말고 다음 주 화요일로요",
        "다음 주 화요일 말고요",
        "이번 주 월요일로요",
        "12월 1일로요",
    ],
)
async def test_ambiguous_negated_past_or_distant_dates_are_not_corrected(
    monkeypatch, utterance
):
    task, api, _ = _task(monkeypatch)
    _say(task, utterance)
    result = await _search(task, "2026-11-10")
    assert "date_corrected" not in result
    assert api.call_args.kwargs["start_date"] == "2026-11-10"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start", "end"),
    [("2026-11-02", "2026-11-06"), ("2026-11-09", "2026-11-13")],
)
async def test_range_search_is_untouched_and_consumes_first_call(
    monkeypatch, start, end
):
    task, api, _ = _task(monkeypatch)
    _say(task, "다음 주 화요일 오후 3시요")
    result = await _search(task, start, end)
    assert "date_corrected" not in result
    assert api.call_args.kwargs["start_date"] == start
    assert api.call_args.kwargs["end_date"] == end
    assert "date_corrected" not in await _search(task, "2026-11-10")


@pytest.mark.asyncio
async def test_pick_stages_code_resolved_tuesday(monkeypatch):
    task, _, _ = _task(monkeypatch)
    _load_snapshot(task, ["2026-11-03 15:00:00"])
    _say(task, "다음 주 화요일 오후 3시요")
    result = await task.pick._func(
        task, SimpleNamespace(), date="2026-11-10", time="오후 3시"
    )
    assert result["appointment_datetime"] == "2026-11-03 15:00:00"
    assert result["date_corrected"]["to"] == "2026-11-03"
    assert task._pending_pick["appointment_datetime"] == "2026-11-03 15:00:00"


@pytest.mark.asyncio
async def test_availability_correction_and_pick_share_first_call(monkeypatch):
    task, _, _ = _task(monkeypatch)
    _load_snapshot(task, ["2026-11-03 15:00:00", "2026-11-10 15:00:00"])
    _say(task, "다음 주 화요일 오후 3시요")
    checked = await task.check_time_availability._func(
        task, SimpleNamespace(), date="2026-11-10", time="오후 3시"
    )
    assert checked["date"] == "2026-11-03"
    assert checked["available"] is True
    assert checked["date_corrected"]["to"] == "2026-11-03"
    picked = await task.pick._func(
        task, SimpleNamespace(), date="2026-11-10", time="오후 3시"
    )
    assert picked["appointment_datetime"] == "2026-11-10 15:00:00"
    assert "date_corrected" not in picked


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["pick", "check_time_availability"])
async def test_omitted_date_does_not_rewrite_stored_date(monkeypatch, tool_name):
    task, _, _ = _task(monkeypatch)
    _load_snapshot(task, ["2026-11-10 15:00:00"])
    task._reservation_data["reservation_date"] = "2026-11-10"
    task._stage = "select_time"
    _say(task, "다음 주 화요일 오후 3시요")
    result = await getattr(task, tool_name)._func(
        task, SimpleNamespace(), time="오후 3시"
    )
    assert "date_corrected" not in result
    assert task._date_hint_checked_message_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["pick", "check_time_availability"])
async def test_invalid_time_response_retains_correction(monkeypatch, tool_name):
    task, _, _ = _task(monkeypatch)
    _load_snapshot(task, [])
    _say(task, "다음 주 화요일 오후 3시요")
    result = await getattr(task, tool_name)._func(
        task, SimpleNamespace(), date="2026-11-10", time="잘못된 시간"
    )
    assert result["response_type"] == "invalid_time"
    assert result["date"] == "2026-11-03"
    assert result["date_corrected"]["to"] == "2026-11-03"


@pytest.mark.asyncio
async def test_lookup_failure_still_returns_none_after_correction(monkeypatch):
    task, api, completed = _task(monkeypatch)
    api.return_value = SimpleNamespace(
        success=False, data=None, message="lookup failed"
    )
    _say(task, "이번 주 금요일 오전 10시 반으로요")
    assert await _search(task, "2026-10-31") is None
    assert api.call_args.kwargs["start_date"] == "2026-10-30"
    assert completed[0].lookup_failed is True


@pytest.mark.parametrize("end", [None, "", "invalid"])
def test_missing_horizon_falls_back_to_thirty_days(monkeypatch, end):
    task, _, _ = _task(monkeypatch)
    task._search_end_date = end
    assert task._date_horizon(TODAY) == TODAY + dt.timedelta(days=30)


# The business timezone is Asia/Seoul for every call language, so en-US gets
# the same "today" as ko-KR.
@pytest.mark.parametrize(
    ("language", "expected"), [("ko-KR", "2026-10-30"), ("en-US", "2026-10-30")]
)
def test_grounding_uses_task_timezone_at_day_boundary(monkeypatch, language, expected):
    monkeypatch.setattr(
        FixedDatetime, "instant", dt.datetime(2026, 10, 29, 1, tzinfo=dt.timezone.utc)
    )
    task, _, _ = _task(monkeypatch, language_code=language)
    _say(task, "내일이요")
    _, hints = task._caller_date_hints()
    assert hints[0].date == expected


def test_dynamic_booking_note_default_output_is_unchanged():
    hints = resolve_caller_dates("이번 주 금요일", TODAY, HORIZON)
    assert caller_date_note(hints, HORIZON) == (
        "[날짜 해석] 고객 발화의 '이번 주 금요일' = 2026-10-30(금요일). "
        "search_schedules의 start_date/end_date에는 이 날짜를 그대로 쓰고 "
        "직접 계산하지 마세요."
    )
