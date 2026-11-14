"""Code-owned date grounding for the booking task.

Gemma searched 10-20 or 10-26 for "다음주 월요일"(10-19) in 10/10 replays
(2026-10-16) while GPT-5.6-luna was right 10/10. The fix moves date resolution
out of the model: the caller's own phrase is paired with its date right after
the utterance, the turn's first one-day search is corrected to that date, and
every returned date carries the words to speak.
"""

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from livekit.agents import llm
from livekit.agents.voice import ModelSettings

from tasks.dynamic_booking_task import (
    KST,
    DynamicBookingTask,
    _spoken_condition_text,
    build_calendar_block,
    caller_date_note,
    resolve_caller_dates,
    spoken_date,
    spoken_date_range,
)

TODAY = dt.date(2026, 10, 16)  # Friday
HORIZON = TODAY + dt.timedelta(days=30)
FIXED_NOW = dt.datetime(2026, 10, 16, 16, 2, tzinfo=KST)


def _hints(text: str) -> list[tuple[str, str, str]]:
    return [
        (hint.phrase, hint.date, hint.status)
        for hint in resolve_caller_dates(text, TODAY, HORIZON)
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("그럼 다음주 월요일로 봐주세요", [("다음주 월요일", "2026-10-19", "ok")]),
        ("담주 월욜 오전", [("담주 월욜", "2026-10-19", "ok")]),
        ("다음주월요일오전", [("다음주월요일", "2026-10-19", "ok")]),
        ("다음주 월요일은 없어요?", [("다음주 월요일", "2026-10-19", "ok")]),
        ("다다음 주 월요일이요", [("다다음 주 월요일", "2026-10-26", "ok")]),
        # A bare weekday is the next occurrence, today included.
        ("토요일 되나요", [("토요일", "2026-10-17", "ok")]),
        ("금요일이요", [("금요일", "2026-10-16", "ok")]),
        ("내일 오전에 박진철 상담사님으로 점검좀", [("내일", "2026-10-17", "ok")]),
        ("내일모레요", [("내일모레", "2026-10-18", "ok")]),
        ("10월 20일 오후요", [("10월 20일", "2026-10-20", "ok")]),
        ("20일이요", [("20일", "2026-10-20", "ok")]),
        # Day numbers already passed this month roll to next month.
        ("3일이요", [("3일", "2026-11-03", "ok")]),
        # Past and out-of-window dates are reported, never silently dropped.
        ("이번주 월요일은요", [("이번주 월요일", "2026-10-12", "past")]),
        ("12월 1일요", [("12월 1일", "2026-12-01", "beyond_horizon")]),
        # Two different days stay two hints, in utterance order.
        (
            "월요일이나 화요일",
            [("월요일", "2026-10-19", "ok"), ("화요일", "2026-10-20", "ok")],
        ),
        (
            "다음주 월요일 아니면 화요일",
            [("다음주 월요일", "2026-10-19", "ok"), ("화요일", "2026-10-20", "ok")],
        ),
        # Not dates: durations, clock times, verification codes, last week.
        ("3일 뒤에요", []),
        ("11시 30분이요", []),
        ("87년 9월 4일이요", []),
        (
            "지난주 금요일 갔는데 다음주 월요일로",
            [("다음주 월요일", "2026-10-19", "ok")],
        ),
        ("네", []),
    ],
)
def test_resolve_caller_dates(text, expected):
    assert _hints(text) == expected


def test_caller_date_note_pairs_phrase_with_date():
    note = caller_date_note(
        resolve_caller_dates("그럼 다음주 월요일로 봐주세요", TODAY, HORIZON), HORIZON
    )
    assert note.startswith("[날짜 해석]")
    assert "'다음주 월요일' = 2026-10-19(월요일)" in note
    assert "직접 계산하지" in note


def test_caller_date_note_flags_past_and_out_of_window():
    past = caller_date_note(
        resolve_caller_dates("이번주 월요일", TODAY, HORIZON), HORIZON
    )
    assert "2026-10-12(월요일)은 이미 지난 날짜" in past
    beyond = caller_date_note(resolve_caller_dates("12월 1일", TODAY, HORIZON), HORIZON)
    assert "2026-11-15까지" in beyond
    assert caller_date_note([], HORIZON) is None


class TestCalendarBlock:
    def test_lists_phrase_equals_date_without_past_days(self):
        block = build_calendar_block(FIXED_NOW.replace(tzinfo=None), 14)
        assert "다음 주 월요일 = 2026-10-19" in block
        assert "다다음 주 월요일 = 2026-10-26" in block
        assert "이번 주 금요일 = 2026-10-16 (오늘)" in block
        assert (
            "오늘 = 2026-10-16(금) / 내일 = 2026-10-17(토) / 모레 = 2026-10-18(일)"
            in block
        )
        assert "2026-10-12" not in block  # past Monday is not offered
        assert "이번 주:" not in block  # the old week-row format is gone
        assert "직접 계산하지 마세요" in block

    def test_far_weeks_collapse_to_rows(self):
        block = build_calendar_block(FIXED_NOW.replace(tzinfo=None), 30)
        assert "3주 뒤: 11-02(월), 11-03(화)" in block
        assert "2026-11-15" in block and "2026-11-16" not in block

    def test_english_labels(self):
        block = build_calendar_block(FIXED_NOW.replace(tzinfo=None), 14, lang="en")
        assert "Next week Monday = 2026-10-19" in block
        assert "This week Friday = 2026-10-16 (today)" in block


def test_spoken_forms():
    assert spoken_date("2026-10-19") == "10월 19일 월요일"
    assert spoken_date("2026-10-19", "en") == "Monday, October 19"
    assert spoken_date_range("2026-10-19", "2026-10-19") == "10월 19일 월요일"
    assert (
        spoken_date_range("2026-10-19", "2026-10-23")
        == "10월 19일 월요일부터 10월 23일 금요일까지"
    )
    assert (
        spoken_date_range("2026-10-19", "2026-10-23", "en")
        == "Monday, October 19 through Friday, October 23"
    )


def _task() -> DynamicBookingTask:
    task = object.__new__(DynamicBookingTask)
    task._chat_ctx = llm.ChatContext()
    task._call_session_data = SimpleNamespace(room_name="synthetic-date-grounding")
    task._schedule_lookup_days = 30
    task._no_progress_turns = 0
    task.done = lambda: False
    return task


def _say(task: DynamicBookingTask, text: str) -> None:
    task._chat_ctx.add_message(role="user", content=text)


async def _search(task: DynamicBookingTask, start: str, end: str) -> tuple[dict, dict]:
    impl = AsyncMock(return_value={"status": "ok"})
    with (
        patch.object(task, "_search_schedules_impl", impl),
        patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW),
    ):
        result = await task.search_schedules(start_date=start, end_date=end)
    return result, impl.call_args.kwargs


async def _inference_context(task: DynamicBookingTask) -> llm.ChatContext:
    """The chat context llm_node hands to the model for the next generation."""
    seen: list[llm.ChatContext] = []

    async def fake_default(agent, chat_ctx, tools, model_settings):
        seen.append(chat_ctx)
        yield "ok"

    with (
        patch("tasks.dynamic_booking_task.Agent.default.llm_node", fake_default),
        patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW),
    ):
        chunks = [
            chunk async for chunk in task.llm_node(task._chat_ctx, [], ModelSettings())
        ]
    assert chunks == ["ok"]
    [ctx] = seen
    return ctx


class TestInferenceNote:
    @pytest.mark.asyncio
    async def test_dated_utterance_gets_one_system_note_right_after_it(self):
        task = _task()
        _say(task, "예약하려고요")
        _say(task, "그럼 다음주 월요일로 봐주세요")
        ctx = await _inference_context(task)
        roles = [(item.role, item.text_content) for item in ctx.items]
        assert roles[-2][0] == "user"
        assert roles[-1][0] == "system"
        assert "'다음주 월요일' = 2026-10-19(월요일)" in roles[-1][1]
        # The durable history is untouched.
        assert [item.role for item in task._chat_ctx.items] == ["user", "user"]

    @pytest.mark.asyncio
    async def test_plain_reply_gets_no_note(self):
        task = _task()
        _say(task, "다음주 월요일이요")
        _say(task, "네")
        ctx = await _inference_context(task)
        assert [item.role for item in ctx.items] == ["user", "user"]

    @pytest.mark.asyncio
    async def test_note_is_rebuilt_for_every_inference_of_the_same_turn(self):
        task = _task()
        _say(task, "내일 오전이요")
        first = await _inference_context(task)
        second = await _inference_context(task)
        for ctx in (first, second):
            assert "'내일' = 2026-10-17(토요일)" in ctx.items[-1].text_content

    def test_date_facts_work_without_init(self):
        task = _task()
        assert task._date_facts(["2026-10-19"]) == [
            {"date": "2026-10-19", "weekday": "월", "spoken": "10월 19일 월요일"}
        ]


class TestSearchCorrection:
    @pytest.mark.asyncio
    async def test_wrong_one_day_search_is_moved_to_the_named_date(self):
        task = _task()
        _say(task, "그럼 다음주 월요일로 봐주세요")
        result, kwargs = await _search(task, "2026-10-20", "2026-10-20")
        assert (kwargs["start_date"], kwargs["end_date"]) == (
            "2026-10-19",
            "2026-10-19",
        )
        corrected = result["date_corrected"]
        assert (corrected["from"], corrected["to"]) == ("2026-10-20", "2026-10-19")
        assert corrected["phrase"] == "다음주 월요일"
        assert "2026-10-19(월요일)" in corrected["instruction"]

    @pytest.mark.asyncio
    async def test_matching_search_passes_untouched(self):
        task = _task()
        _say(task, "다음주 월요일이요")
        result, kwargs = await _search(task, "2026-10-19", "2026-10-19")
        assert kwargs["start_date"] == "2026-10-19"
        assert "date_corrected" not in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("start", "end"),
        [("2026-10-19", "2026-10-25"), ("2026-10-20", "2026-10-25")],
    )
    async def test_range_searches_are_never_rewritten(self, start, end):
        task = _task()
        _say(task, "다음주 월요일이요")
        result, kwargs = await _search(task, start, end)
        assert (kwargs["start_date"], kwargs["end_date"]) == (start, end)
        assert "date_corrected" not in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "text",
        [
            "다음주 월요일 말고요",  # negation
            "월요일이나 화요일이요",  # two expressions
            "이번주 월요일은요",  # past date
            "네",  # no expression
        ],
    )
    async def test_ambiguous_turns_are_left_alone(self, text):
        task = _task()
        _say(task, text)
        result, kwargs = await _search(task, "2026-10-20", "2026-10-20")
        assert kwargs["start_date"] == "2026-10-20"
        assert "date_corrected" not in result

    @pytest.mark.asyncio
    async def test_only_the_first_search_after_a_caller_message_is_checked(self):
        task = _task()
        _say(task, "다음주 월요일이요")
        first, _ = await _search(task, "2026-10-20", "2026-10-20")
        assert "date_corrected" in first
        # After an empty 10-19 the model may legitimately look at another day.
        second, kwargs = await _search(task, "2026-10-20", "2026-10-20")
        assert kwargs["start_date"] == "2026-10-20"
        assert "date_corrected" not in second
        # A new caller message arms the check again.
        _say(task, "다음주 월요일이요")
        third, _ = await _search(task, "2026-10-20", "2026-10-20")
        assert "date_corrected" in third


class TestPartialSearchReminder:
    @pytest.mark.asyncio
    async def test_note_says_the_named_day_was_only_searched_for_a_window(self):
        # Gemma replays (2026-10-16): after an empty "내일 오전" search, "내일은
        # 가능한 시간이 언제에요?" was answered from the morning result in 10/10
        # runs instead of searching the whole day.
        task = _task()
        task._last_search_conditions = {
            "start_date": "2026-10-17",
            "end_date": "2026-10-17",
            "time_from": "00:00",
            "time_to": "11:59",
            "target_time": None,
        }
        _say(task, "내일은 가능한 시간이 언제에요?")
        ctx = await _inference_context(task)
        note = ctx.items[-1].text_content
        assert "'내일' = 2026-10-17(토요일)" in note
        assert "00:00~11:59만 조회했습니다" in note
        assert "시간 조건 없이 다시 조회" in note

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "conditions",
        [
            None,
            {"start_date": "2026-10-17", "end_date": "2026-10-17"},  # whole day
            {  # a different day
                "start_date": "2026-10-19",
                "end_date": "2026-10-19",
                "time_from": "00:00",
                "time_to": "11:59",
            },
        ],
    )
    async def test_no_reminder_without_a_partial_search_of_that_day(self, conditions):
        task = _task()
        task._last_search_conditions = conditions
        _say(task, "내일은 가능한 시간이 언제에요?")
        ctx = await _inference_context(task)
        assert "만 조회했습니다" not in ctx.items[-1].text_content


class TestSpokenConditionText:
    # Gemma replays (2026-10-16): exit_task(time_condition="2026-10-19") was read
    # aloud as "2026-10-19 조건으로 요청", tripping the suite's ISO-date ban.
    @pytest.mark.parametrize(
        ("text", "language", "expected"),
        [
            ("2026-10-19", "ko-KR", "10월 19일 월요일"),
            ("2026-10-19 오전", "ko-KR", "10월 19일 월요일 오전"),
            ("2026-10-19 14:30", "ko-KR", "10월 19일 월요일 오후 2시 30분"),
            ("2026-10-19", "en-US", "Monday, October 19"),
            ("다음주 월요일", "ko-KR", "다음주 월요일"),
            ("  ", "ko-KR", None),
            (None, "ko-KR", None),
        ],
    )
    def test_tool_notation_becomes_speech(self, text, language, expected):
        assert _spoken_condition_text(text, language) == expected

    def test_whole_day_window_is_not_a_time_condition(self):
        task = _task()
        task._services = []
        task._advisors_by_id = {}
        task._last_search_conditions = {
            "start_date": "2026-10-17",
            "end_date": "2026-10-17",
            "time_from": "00:00",
            "time_to": "23:59",
            "target_time": None,
        }
        task._empty_searched_ranges = ["2026-10-17~2026-10-17"]
        task._call_session_data = SimpleNamespace(
            room_name="synthetic-date-grounding",
            call_config_data=SimpleNamespace(language="ko-KR"),
        )
        context = task._build_booking_memo_context(
            detail="", exit_reason_code="other", time_condition=None
        )
        assert context.time_condition is None
