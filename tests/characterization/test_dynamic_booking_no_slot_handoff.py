"""Booking handoff controller, with synthetic local schedules and no live services.

Code decides when a help offer is available and returns it as ``handoff_offer``; the
model speaks it. Code keeps consent, decline and session-level offer history.
"""

import asyncio
import datetime as dt
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from livekit.agents import StopResponse, function_tool, llm
from livekit.agents.llm.utils import prepare_function_arguments

import tasks.dynamic_booking_task as dbt
from agents.base_agent import BaseAgent
from data import BookingMemoContext, DynamicBookingResumeContext
from tasks.booking.handoff import NoMatchingSlotBlocker
from tasks.dynamic_booking_task import DynamicBookingTask, StagedBooking
from tasks.transfer_reason_task import TransferPurpose

FIXED_NOW = dt.datetime(2026, 9, 11, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))
MEMO = SimpleNamespace(transfer_mode="leave_memo", transfer_to="1004", language="ko-KR")
TRANSFER = SimpleNamespace(
    transfer_mode="warm_transfer", transfer_to="1004", language="ko-KR"
)


class _Handle:
    def __await__(self):
        if False:  # pragma: no cover - makes this a generator
            yield
        return self


def _build_task(call_session_data, monkeypatch, speech):
    task = object.__new__(DynamicBookingTask)
    task._select_operator = False
    task._services = [{"id": 10, "name": "Inspection", "keywords": []}]
    task._advisors = [{"id": 100, "name": "Advisor", "service_ids": [10]}]
    task._advisors_by_id = {100: task._advisors[0]}
    task._schedule_lookup_days = 30
    task._staged = None
    task._staged_requires_revalidation = False
    task._mutation_lock = asyncio.Lock()
    task._fetch_day = AsyncMock(return_value={})
    task._fetch_available_dates = AsyncMock(return_value=[])
    task._call_session_data = call_session_data
    task._chat_ctx = llm.ChatContext()
    task.complete = Mock()
    task.done = lambda: False

    def say(text, allow_interruptions=True):
        task._chat_ctx.add_message(role="assistant", content=text)
        speech["spoken"].append(text)
        return _Handle()

    speech["say"] = say
    return task


@pytest.fixture
def speech():
    return {"spoken": []}


@pytest.fixture
def history():
    return Mock()


@pytest.fixture
def task(monkeypatch, speech, history):
    call_session_data = SimpleNamespace(
        room_name="synthetic-no-slot-handoff",
        dynamic_booking_pending=None,
        reservation_info={},
        call_config_data=MEMO,
    )
    task = _build_task(call_session_data, monkeypatch, speech)
    monkeypatch.setattr(
        DynamicBookingTask,
        "session",
        SimpleNamespace(
            history=SimpleNamespace(add_message=history),
            say=lambda text, allow_interruptions=True: speech["say"](
                text, allow_interruptions
            ),
        ),
    )
    monkeypatch.setattr("tasks.dynamic_booking_task.now_kst", lambda: FIXED_NOW)
    return task


def _user(task, text="Please check this date"):
    return task._chat_ctx.add_message(role="user", content=text)


async def _search(task, *, day="2026-09-12", end=None, new_turn=True, **kwargs):
    if new_turn:
        _user(task)
    return await DynamicBookingTask.search_schedules._func(
        task, start_date=day, end_date=end or day, service_id=10, **kwargs
    )


async def _stage(task, when, operator_id=None):
    return await DynamicBookingTask.stage_booking._func(
        task,
        SimpleNamespace(disallow_interruptions=Mock()),
        service_id=10,
        datetime=when,
        operator_id=operator_id,
    )


def _events(history, status):
    return [
        call.kwargs["extra"]
        for call in history.call_args_list
        if status in repr(call.kwargs.get("extra"))
    ]


async def _report(task, subject="service", requested_label="드론 수리", **kwargs):
    return await DynamicBookingTask.report_booking_obstacle._func(
        task, subject=subject, requested_label=requested_label, **kwargs
    )


async def _answer(task, reply):
    return await DynamicBookingTask.answer_handoff_offer._func(task, reply=reply)


async def _record(task, kind, quote):
    return await DynamicBookingTask.record_caller_evidence._func(
        task, kind=kind, quote=quote
    )


def _declined(task):
    return task._call_session_data.dynamic_booking_resume.declined_alternatives


async def _miss_with_nearby(task, day, text="오후 2시요"):
    """A no-match at 14:00 that presents 15:00 on the same day."""
    task._fetch_day.return_value = {f"{day} 15:00": [100]}
    _user(task, text)
    return await _search(task, day=day, new_turn=False, target_time="14:00")


# ---------- no matching slot: alternatives first, help after three ----------


@pytest.mark.asyncio
async def test_first_no_match_proposes_alternatives_without_an_offer(task, speech):
    result = await _miss_with_nearby(task, "2026-09-12")
    assert [s["datetime"] for s in result["slots"]] == ["2026-09-12 15:00"]
    assert "handoff_offer" not in result
    assert result["required_next_action"]["handoff_available"] is True
    assert task._offer_pending is None
    assert speech["spoken"] == []
    blocker = task._schedule_blocker
    assert isinstance(blocker, NoMatchingSlotBlocker)
    assert blocker.filters["target_time"] == "14:00"


@pytest.mark.asyncio
async def test_single_empty_day_does_not_offer_at_once(task):
    result = await _search(task)
    assert result["availability"] == "none_in_searched_range"
    assert "handoff_offer" not in result
    assert task._offer_pending is None


@pytest.mark.asyncio
async def test_help_is_offered_after_three_declined_alternatives(task, history):
    await _miss_with_nearby(task, "2026-09-12")
    for count, day in enumerate(("2026-09-13", "2026-09-14"), start=1):
        result = await _miss_with_nearby(task, day, "그 시간 말고 다른 날이요")
        assert _declined(task) == count
        assert "handoff_offer" not in result
    result = await _miss_with_nearby(task, "2026-09-15", "그것도 안 돼요")
    assert _declined(task) == 3
    offer = result["handoff_offer"]
    assert offer["channel"] == "memo"
    assert offer["reason"] == "caller_condition_not_bookable"
    # The alternative question is replaced by the help question.
    assert "handoff_offer" in result["required_next_action"]["instruction"]
    assert task._offer_pending.reason_code == "no_slot_memo"
    assert _events(history, "HANDOFF_OFFERED")
    task.complete.assert_not_called()


@pytest.mark.asyncio
async def test_rejected_candidates_count_as_declined_alternatives(task):
    task._fetch_day.return_value = {
        "2026-09-12 10:00": [100],
        "2026-09-12 11:00": [100],
        "2026-09-12 12:00": [100],
    }
    for count, when in enumerate(
        ("2026-09-12 10:00", "2026-09-12 11:00", "2026-09-12 12:00"), start=1
    ):
        _user(task, "그 시간으로요")
        assert (await _stage(task, when))["status"] == "staged"
        _user(task, "아니요 그 시간 안 돼요")
        result = await DynamicBookingTask.reject_candidate._func(task)
        assert _declined(task) == count
    assert result["handoff_offer"]["reason"] == "alternatives_declined"
    assert "required_next_tool" not in result
    assert task._offer_pending.reason_code == "no_slot_memo"


@pytest.mark.asyncio
async def test_choosing_a_presented_alternative_is_not_a_decline(task):
    await _miss_with_nearby(task, "2026-09-12")
    task._fetch_day.return_value = {"2026-09-12 15:00": [100]}
    _user(task, "3시로 할게요")
    await _search(task, new_turn=False, target_time="15:00")
    assert _declined(task) == 0


@pytest.mark.asyncio
async def test_moving_past_presented_matching_times_counts_as_a_decline(task):
    task._fetch_day.return_value = {"2026-09-12 10:00": [100]}
    await _search(task)
    task._fetch_day.return_value = {"2026-09-13 10:00": [100]}
    _user(task, "그 시간 말고 13일은요?")
    await _search(task, day="2026-09-13", new_turn=False)
    assert _declined(task) == 1


@pytest.mark.asyncio
async def test_chained_searches_in_one_caller_turn_count_once(task):
    await _miss_with_nearby(task, "2026-09-12")
    _user(task, "다른 날로 알아봐 주세요")
    await _search(task, day="2026-09-13", end="2026-09-19", new_turn=False)
    task._pending_empty_search = None  # a chained search the guard let through
    await _search(task, day="2026-09-20", end="2026-09-26", new_turn=False)
    assert _declined(task) == 1


@pytest.mark.asyncio
async def test_narrowing_within_presented_dates_is_not_a_decline(task):
    task._fetch_available_dates.return_value = ["2026-09-14", "2026-09-15"]
    _user(task, "다음 주 언제 돼요?")
    await _search(task, day="2026-09-14", end="2026-09-18", new_turn=False)
    task._fetch_day.return_value = {"2026-09-15 10:00": [100]}
    _user(task, "15일은 몇 시요?")
    await _search(task, day="2026-09-15", new_turn=False)
    assert _declined(task) == 0


@pytest.mark.asyncio
async def test_fixed_condition_is_offered_at_once(task):
    await _miss_with_nearby(task, "2026-09-12", "Advisor 상담사님으로 오후 2시요")
    _user(task, "그날 그 시간밖에 안 돼요")
    result = await _report(task, subject="schedule", requested_label=None)
    assert result["status"] == "handoff_offer"
    assert result["handoff_offer"]["channel"] == "memo"
    assert result["blocker"]["filters"]["target_time"] == "14:00"
    assert task._offer_pending.reason_code == "no_slot_memo"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "channel"), [(MEMO, "memo"), (TRANSFER, "transfer")]
)
async def test_nothing_left_in_the_lookup_window_is_offered_at_once(
    task, config, channel
):
    task._call_session_data.call_config_data = config
    result = await _search(task, day="2026-09-12", end="2026-10-11")
    assert result["search_horizon_reached"] is True
    assert result["handoff_offer"]["channel"] == channel
    assert task._offer_pending.channel == channel


@pytest.mark.asyncio
async def test_availability_only_question_never_becomes_a_handoff(task):
    _user(task, "가장 빠른 시간이 언제예요?")
    result = await DynamicBookingTask.search_schedules._func(
        task,
        start_date="2026-09-12",
        end_date="2026-10-11",
        service_id=10,
        mode="earliest",
    )
    assert result["slots"] == []
    assert "handoff_offer" not in result
    assert task._schedule_blocker is None


@pytest.mark.asyncio
async def test_schedule_report_requires_a_verified_no_match(task):
    result = await _report(task, subject="schedule", requested_label=None)
    assert result["code"] == "no_schedule_blocker"
    task._fetch_day.return_value = {"2026-09-12 14:00": [100]}
    await _search(task, target_time="14:00")
    result = await _report(task, subject="schedule", requested_label=None)
    assert result["code"] == "no_schedule_blocker"


@pytest.mark.asyncio
async def test_failed_new_search_cannot_reuse_the_old_blocker(task):
    await _miss_with_nearby(task, "2026-09-12")
    task._fetch_day.side_effect = dbt.BookingApiError("down")
    _user(task, "13일로 찾아주세요")
    failed = await _search(task, day="2026-09-13", new_turn=False)
    assert failed["code"] == "schedule_lookup_failed"
    result = await _report(task, subject="schedule", requested_label=None)
    assert result["code"] == "no_schedule_blocker"


@pytest.mark.asyncio
async def test_same_turn_research_stays_blocked(task):
    await _miss_with_nearby(task, "2026-09-12")
    result = await _search(task, new_turn=False, target_time="15:00")
    assert result["code"] == "awaiting_customer_after_no_availability"


@pytest.mark.asyncio
async def test_time_filtered_advisor_hint_is_not_a_verified_alternative(task):
    async def dates(start, end, *, service_id=None, operator_id=None):
        return [] if operator_id == 100 else ["2026-09-14"]

    task._fetch_available_dates = dates
    _user(task, "다음 주 오후 2시에 Advisor 상담사님으로요")
    result = await _search(
        task, end="2026-09-18", new_turn=False, operator_id=100, target_time="14:00"
    )
    assert task._schedule_blocker.alternative_slot_count == 0
    assert "handoff_offer" not in result


@pytest.mark.asyncio
async def test_other_advisors_on_the_day_count_as_an_alternative(task):
    task._advisors.append({"id": 200, "name": "Second", "service_ids": [10]})
    task._advisors_by_id[200] = task._advisors[-1]

    async def fetch_day(day, *, service_id=None, operator_id=None):
        rows = {"2026-09-12 14:00": [200]}
        if operator_id is None:
            return rows
        return {when: ids for when, ids in rows.items() if operator_id in ids}

    task._fetch_day = fetch_day
    _user(task, "Advisor 상담사님으로 오후 2시요")
    result = await _search(task, new_turn=False, target_time="14:00", operator_id=100)
    assert result["other_advisors_available"] is True
    assert task._schedule_blocker.alternative_slot_count == 1


# ---------- service / advisor outside the phone-booking catalog ----------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "channel"), [(MEMO, "memo"), (TRANSFER, "transfer")]
)
async def test_off_list_service_is_offered_immediately(task, config, channel):
    task._call_session_data.call_config_data = config
    _user(task, "드론 수리로 해야 돼")
    result = await _report(task)
    assert result["status"] == "handoff_offer"
    assert result["blocker"]["requested_label"] == "드론 수리"
    assert result["handoff_offer"]["channel"] == channel
    assert task._offer_pending.reason_code == "service_not_offered"
    task.complete.assert_not_called()


@pytest.mark.asyncio
async def test_listed_service_cannot_be_reported_as_off_list(task):
    result = await _report(task, requested_label="inspection")
    assert result["code"] == "listed_in_catalog"
    assert result["id"] == 10


@pytest.mark.asyncio
async def test_second_report_after_an_offer_is_suppressed(task):
    _user(task, "드론 수리로 해야 돼")
    await _report(task)
    _user(task, "드론 수리요")
    await _answer(task, "other")
    result = await _report(task)
    assert result["status"] == "handoff_suppressed"
    assert result["reason"] == "already_offered"


# ---------- off-list advisor: verified alternative first ----------


async def _report_advisor(task, **kwargs):
    return await _report(
        task, subject="advisor", requested_label="송형석 상담사님", **kwargs
    )


def _dates_for(available: dict[int, list[str]]):
    async def dates(start, end, *, service_id=None, operator_id=None):
        return available.get(operator_id, [])

    return dates


@pytest.mark.asyncio
@pytest.mark.parametrize("select_operator", [True, False])
async def test_verified_other_advisor_is_offered_once_before_handoff(
    task, history, select_operator
):
    task._select_operator = select_operator
    task._fetch_available_dates = _dates_for({100: ["2026-09-14"]})
    _user(task, "송형석 상담사님으로 Inspection 예약하고 싶어요")
    result = await _report_advisor(task, service_id=10)
    assert result["status"] == "alternative_available"
    assert task._offer_pending is None
    if select_operator:
        assert result["alternative_advisors"] == [
            {"id": 100, "name": "Advisor", "earliest_date": "2026-09-14"}
        ]
    else:
        assert result["alternative"] == {
            "kind": "auto_assign",
            "earliest_date": "2026-09-14",
        }
    assert _events(history, "ALTERNATIVE_OFFERED")
    _user(task, "아니요 송형석 상담사님이어야 해요")
    second = await _report_advisor(task, service_id=10)
    assert second["status"] == "handoff_offer"
    assert task._offer_pending.reason_code == "service_not_offered"


@pytest.mark.asyncio
async def test_advisor_fixed_from_the_start_is_offered_handoff_at_once(task):
    task._fetch_available_dates = _dates_for({100: ["2026-09-14"]})
    _user(task, "송형석 상담사님 아니면 안 돼요")
    result = await _report_advisor(task, service_id=10, caller_fixed_advisor=True)
    assert result["status"] == "handoff_offer"
    assert task._offer_pending.blocker.subject == "advisor"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dates", [_dates_for({}), AsyncMock(side_effect=dbt.BookingApiError("down"))]
)
async def test_no_verified_alternative_goes_straight_to_handoff(task, dates):
    task._fetch_available_dates = dates
    _user(task, "송형석 상담사님으로 예약할게요")
    result = await _report_advisor(task, service_id=10)
    assert result["status"] == "handoff_offer"
    resume = task._call_session_data.dynamic_booking_resume
    assert resume.advisor_alternative_presented is False


@pytest.mark.asyncio
async def test_unknown_appointment_asks_first_when_several_are_listed(task):
    task._services.append({"id": 20, "name": "Repair", "keywords": []})
    task._fetch_available_dates = _dates_for({100: ["2026-09-14"]})
    _user(task, "송형석 상담사님으로 예약할게요")
    result = await _report_advisor(task)
    assert result["status"] == "service_required"
    assert task._offer_pending is None


@pytest.mark.asyncio
async def test_alternative_is_presented_once_per_attempt_even_after_reentry(
    task, monkeypatch, speech
):
    task._fetch_available_dates = _dates_for({100: ["2026-09-14"]})
    _user(task, "송형석 상담사님으로 예약할게요")
    await _report_advisor(task, service_id=10)
    reentered = _build_task(task._call_session_data, monkeypatch, speech)
    reentered._fetch_available_dates = _dates_for({100: ["2026-09-14"]})
    _user(reentered, "송형석 상담사님이요")
    result = await _report_advisor(reentered, service_id=10)
    assert result["status"] == "handoff_offer"


@pytest.mark.asyncio
async def test_refused_handoff_also_skips_the_alternative_step(task):
    task._fetch_available_dates = _dates_for({100: ["2026-09-14"]})
    refusal = "상담원 연결은 원하지 않아요"
    _user(task, refusal)
    await _record(task, "handoff_refusal", refusal)
    result = await _report_advisor(task, service_id=10)
    assert result["status"] == "handoff_suppressed"


# ---------- explicit advisor IDs and off-list name matching ----------


def _two_advisor_task(task):
    task._select_operator = True
    task._advisors = [
        {"id": 100, "name": "정진수", "service_ids": [10]},
        {"id": 200, "name": "김철수", "service_ids": [10]},
    ]
    task._advisors_by_id = {d["id"]: d for d in task._advisors}
    return task


# handoff._ADVISOR_TITLES strips advisor honorifics
# (상담사님/매니저님/...) before matching names.
@pytest.mark.parametrize(
    ("reference", "name", "expected"),
    [
        ("정진수 상담사님", "정진수", "exact"),
        ("정 매니저님", "정진수", "partial"),
        ("정매니저님", "정진수", "partial"),
        ("진수 상담사님", "정진수", "partial"),
        ("정지수 상담사님", "정진수", "similar"),
        ("김진수 상담사님", "김철수", "similar"),
        ("송형석 상담사님", "정진수", None),
        ("정매니저님", "김정민", None),
        ("정민 매니저님", "김정민", "partial"),
    ],
)
def test_advisor_name_match_levels(reference, name, expected):
    from tasks.booking.handoff import advisor_name_match

    assert advisor_name_match(reference, name) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["search", "stage"])
@pytest.mark.parametrize(
    ("utterance", "question", "reply"),
    [
        ("휴대폰 점검 예약하려고요", None, None),
        ("정 엔지니어님 예약하려구요", None, None),
        ("정지수 상담사님 예약하려구요", None, None),
        (
            "정 엔지니어님 예약하려구요",
            "정진수 상담사님을 말씀하시나요? 어떤 서비스로 예약할까요?",
            "노트북 상담이요",
        ),
    ],
)
async def test_explicit_advisor_id_does_not_require_dialogue_match(
    task, tool, utterance, question, reply
):
    _two_advisor_task(task)
    _user(task, utterance)
    if question is not None:
        task._chat_ctx.add_message(role="assistant", content=question)
        _user(task, reply)
    task._fetch_day.return_value = {"2026-09-12 10:00": [100]}

    # Stage directly so prior search results cannot establish advisor grounding.
    if tool == "search":
        result = await _search(task, new_turn=False, operator_id=100)
        assert result["status"] == "ok"
    else:
        result = await _stage(task, "2026-09-12 10:00", operator_id=100)
        assert result["status"] == "staged"
        assert result["staged"]["operator_id"] == 100
        assert result["required_next_action"] == "ask_booking_confirmation"
    task._fetch_day.assert_awaited_once_with(
        "2026-09-12", service_id=10, operator_id=100
    )
    task.complete.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["search", "stage"])
async def test_unknown_advisor_id_is_rejected_before_lookup(task, tool):
    _two_advisor_task(task)
    _user(task, "휴대폰 점검 예약하려고요")
    if tool == "search":
        result = await _search(task, new_turn=False, operator_id=999)
    else:
        result = await _stage(task, "2026-09-12 10:00", operator_id=999)
    assert result["status"] == "error"
    assert result["code"] == "unknown_advisor"
    task._fetch_day.assert_not_awaited()
    task._fetch_available_dates.assert_not_awaited()
    assert task._staged is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "slots",
    [{}, {"2026-09-12 10:00": [200]}, {"2026-09-12 11:00": [100]}],
    ids=["no_slots", "different_advisor", "different_time"],
)
async def test_explicit_advisor_still_requires_available_slot(task, slots):
    _two_advisor_task(task)
    _user(task, "휴대폰 점검 예약하려고요")
    task._fetch_day.return_value = slots
    result = await _stage(task, "2026-09-12 10:00", operator_id=100)
    assert result["status"] == "unavailable"
    assert task._staged is None
    task.complete.assert_not_called()


@pytest.mark.asyncio
async def test_exact_name_and_offered_advisors_need_no_confirmation(task):
    _two_advisor_task(task)
    _user(task, "정진수 상담사님으로 예약할게요")
    task._fetch_day.return_value = {"2026-09-12 10:00": [100, 200]}
    assert (await _search(task, new_turn=False, operator_id=100))["status"] == "ok"
    _user(task, "그럼 다른 상담사님으로요")
    result = await _search(task, new_turn=False)
    assert 200 in {d["id"] for slot in result["slots"] for d in slot.get("advisors", [])}
    staged = await _stage(task, "2026-09-12 10:00", operator_id=200)
    assert staged["status"] == "staged"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "candidates"),
    [
        # Advisor honorifics (매니저님/상담사님).
        ("정 매니저님", [{"id": 100, "name": "정진수"}]),
        (
            "김진수 상담사님",
            [{"id": 100, "name": "정진수"}, {"id": 200, "name": "김철수"}],
        ),
    ],
)
async def test_close_name_report_asks_once_before_off_list_handling(
    task, label, candidates
):
    _two_advisor_task(task)
    task._fetch_available_dates = _dates_for({100: ["2026-09-14"]})
    _user(task, f"{label} 예약하려구요")
    first = await _report(
        task, subject="advisor", requested_label=label, service_id=10
    )
    assert first["status"] == "advisor_confirmation_required"
    assert first["candidates"] == candidates
    _user(task, "아니요 다른 분이에요")
    second = await _report(
        task, subject="advisor", requested_label=label, service_id=10
    )
    assert second["status"] == "alternative_available"


@pytest.mark.asyncio
async def test_exact_listed_advisor_with_title_is_not_off_list(task):
    _two_advisor_task(task)
    result = await _report(task, subject="advisor", requested_label="정진수 상담사님")
    assert result["code"] == "listed_in_catalog"
    assert result["id"] == 100


# ---------- consent and decline ----------


@pytest.mark.asyncio
async def test_fresh_accept_completes_once_with_legacy_reason(task, history):
    _user(task, "드론 수리로 해야 돼")
    await _report(task)
    _user(task, "네 남겨주세요")
    assert await _answer(task, "accept") is None
    result = task.complete.call_args.args[0]
    assert result.exit_reason_code == "service_not_offered"
    assert result.handoff_accepted is True
    assert result.handoff_channel == "memo"
    assert task.complete.call_count == 1


@pytest.mark.asyncio
async def test_accept_in_the_same_turn_as_the_offer_is_rejected(task):
    _user(task, "드론 수리로 해야 돼")
    await _report(task)
    result = await _answer(task, "accept")
    assert result["code"] == "consent_not_fresh"
    task.complete.assert_not_called()


@pytest.mark.asyncio
async def test_accept_without_any_offer_is_rejected(task):
    _user(task, "네")
    result = await _answer(task, "accept")
    assert result["code"] == "consent_not_fresh"
    task.complete.assert_not_called()


@pytest.mark.asyncio
async def test_a_new_search_replaces_the_unanswered_offer(task):
    result = await _search(task, day="2026-09-12", end="2026-10-11")
    assert "handoff_offer" in result
    task._fetch_day.return_value = {"2026-09-13 10:00": [100]}
    _user(task, "13일은요?")
    await _search(task, day="2026-09-13", new_turn=False)
    assert task._offer_pending is None
    _user(task, "네")
    assert (await _answer(task, "accept"))["code"] == "consent_not_fresh"
    task.complete.assert_not_called()


@pytest.mark.asyncio
async def test_commit_waits_for_the_handoff_answer(task):
    task._fetch_day.return_value = {"2026-09-12 10:00": [100]}
    _user(task, "10시요")
    await _stage(task, "2026-09-12 10:00")
    task._offer_pending = dbt.HandoffOffer(
        id="o1",
        blocker=None,
        reason_code="no_slot_memo",
        channel="memo",
        revision=task._request_revision,
        user_message_id=None,
    )
    _user(task, "네")
    result = await DynamicBookingTask.commit_booking._func(
        task, confirmed_datetime="2026-09-12 10:00"
    )
    assert result["code"] == "awaiting_handoff_decision"


@pytest.mark.asyncio
async def test_decline_keeps_booking_and_suppresses_further_offers(task, history):
    _user(task, "드론 수리로 해야 돼")
    await _report(task)
    _user(task, "연결은 싫고 예약만 할게요")
    result = await _answer(task, "decline")
    assert result == {
        "status": "booking_continues",
        "handoff": "declined",
        # Nothing was chosen yet: one question, and "no" ends without a cancel
        # confirmation.
        "next_step": "ask_if_another_booking",
        "if_caller_wants_nothing_else": "exit_task:cancel",
    }
    task.complete.assert_not_called()
    assert _events(history, "HANDOFF_DECLINED")
    assert (await _report(task))["status"] == "handoff_suppressed"
    result = await _search(task, day="2026-09-12", end="2026-10-11")
    assert "handoff_offer" not in result


@pytest.mark.asyncio
async def test_refusal_evidence_in_reply_to_the_offer_declines_it(task, history):
    _user(task, "드론 수리로 해야 돼")
    await _report(task)
    refusal = "아니요 괜찮아요"
    _user(task, refusal)
    result = await _record(task, "handoff_refusal", refusal)
    assert task._offer_pending is None
    assert result["handoff"] == "declined"
    assert result["if_caller_wants_nothing_else"] == "exit_task:cancel"
    assert _events(history, "HANDOFF_DECLINED")


@pytest.mark.asyncio
async def test_decline_with_a_candidate_in_progress_has_no_stop_hint(task):
    await _miss_with_nearby(task, "2026-09-12")
    _user(task, "그 시간밖에 안 돼요")
    await _report(task, subject="schedule", requested_label=None)
    _user(task, "메모는 됐고 다른 날 찾아주세요")
    result = await _answer(task, "decline")
    assert result == {"status": "booking_continues", "handoff": "declined"}


@pytest.mark.asyncio
async def test_refusal_survives_task_reentry(task, monkeypatch, speech):
    _user(task, "드론 수리로 해야 돼")
    await _report(task)
    _user(task, "싫어요")
    await _answer(task, "decline")
    reentered = _build_task(task._call_session_data, monkeypatch, speech)
    _user(reentered, "드론 수리요")
    assert (await _report(reentered))["status"] == "handoff_suppressed"


@pytest.mark.asyncio
async def test_other_reply_withdraws_the_offer_without_a_refusal(task):
    _user(task, "드론 수리로 해야 돼")
    await _report(task)
    _user(task, "그럼 다음 주에 Checkup으로 찾아주세요")
    assert await _answer(task, "other") == {"status": "booking_continues"}
    assert task._offer_pending is None
    assert task._handoff_refusal() is None


@pytest.mark.asyncio
async def test_accept_after_schedule_offer_builds_grounded_memo(task):
    await _miss_with_nearby(task, "2026-09-12", "Advisor 상담사님으로 오후 2시요")
    _user(task, "그 시간밖에 안 돼요")
    await _report(task, subject="schedule", requested_label=None)
    _user(task, "네")
    await _answer(task, "accept")
    result = task.complete.call_args.args[0]
    assert result.exit_reason_code == "no_slot_memo"
    context = task._call_session_data.booking_memo_context
    assert context.service_name == "Inspection"
    assert context.time_condition == "오후 2시"
    assert context.searched_ranges == ["2026-09-12~2026-09-12"]


@pytest.mark.asyncio
async def test_memo_names_a_morning_window_plainly(task):
    _user(task, "내일 오전으로요")
    await _search(task, new_turn=False, time_from="00:00", time_to="11:59")
    _user(task, "그날 오전밖에 안 돼요")
    await _report(task, subject="schedule", requested_label=None)
    _user(task, "네")
    await _answer(task, "accept")
    assert task._call_session_data.booking_memo_context.time_condition == "오전"


@pytest.mark.asyncio
async def test_staff_request_in_reply_to_offer_keeps_the_grounded_reason(task):
    _user(task, "드론 수리로 해야 돼")
    await _report(task)
    _user(task, "네 연결해 주세요")
    await DynamicBookingTask.exit_task._func(task, reason_code="staff_connection")
    result = task.complete.call_args.args[0]
    assert result.exit_reason_code == "service_not_offered"
    assert result.handoff_accepted is True


@pytest.mark.asyncio
async def test_explicit_staff_request_after_refusal_is_honored(task):
    refusal = "메모는 필요 없어요"
    _user(task, refusal)
    await _record(task, "handoff_refusal", refusal)
    _user(task, "그냥 사람이랑 통화할게요")
    await DynamicBookingTask.exit_task._func(
        task, reason_code="staff_connection", detail="사람 연결 요청"
    )
    assert task.complete.call_args.args[0].exit_reason_code == "staff_connection"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["no_slot_memo", "service_not_offered"])
async def test_exit_task_cannot_start_or_accept_a_handoff(task, reason):
    _user(task, "네")
    result = await DynamicBookingTask.exit_task._func(task, reason_code=reason)
    assert result["code"] == "use_handoff_tools"
    task.complete.assert_not_called()


def test_confirmed_cancel_resets_handoff_history():
    from tools.dynamic_booking_tools import _pause_booking_context

    resume = DynamicBookingResumeContext(
        handoff_declined_evidence={"quote": "싫어요", "user_message_id": "u1"},
        handoff_offer_delivered=True,
        declined_alternatives=3,
    )
    userdata = SimpleNamespace(dynamic_booking_resume=resume)
    _pause_booking_context(userdata, "cancel", llm.ChatContext())
    assert resume.handoff_declined_evidence is None
    assert resume.handoff_offer_delivered is False
    assert resume.declined_alternatives == 0
    resume.handoff_offer_delivered = True
    _pause_booking_context(userdata, "unrelated", llm.ChatContext())
    assert resume.handoff_offer_delivered is True


def test_session_state_defaults_allow_one_offer():
    resume = DynamicBookingResumeContext()
    assert resume.handoff_declined_evidence is None
    assert resume.handoff_offer_delivered is False
    assert resume.declined_alternatives == 0


def test_forced_turn_layer_is_gone():
    assert not hasattr(DynamicBookingTask, "respond_without_booking_change")
    # llm_node exists only to annotate caller dates in the inference context
    # (test_dynamic_booking_date_grounding). It must pass tools and
    # model_settings through unchanged and never force a tool choice.
    source = inspect.getsource(DynamicBookingTask.llm_node)
    assert "tool_choice" not in source
    assert "Agent.default.llm_node(" in source


# ---------- stop after declining help ----------


@pytest.mark.asyncio
async def test_stop_with_nothing_chosen_skips_the_cancel_question(task, speech):
    _user(task, "드론 수리 예약하려고요")
    await _report(task, requested_label="드론 수리")
    _user(task, "아니요 괜찮아요")
    await _answer(task, "decline")
    _user(task, "다른 예약은 없어요")
    assert await DynamicBookingTask.exit_task._func(task, reason_code="cancel") is None
    result = task.complete.call_args.args[0]
    assert result.exit_reason_code == "cancel"
    assert result.stopped_before_selection is True
    assert speech["spoken"] == []


@pytest.mark.asyncio
async def test_stop_after_a_search_still_asks_to_confirm(task, speech):
    task._fetch_day.return_value = {"2026-09-12 15:00": [100]}
    _user(task, "오후 2시요")
    await _search(task, new_turn=False, target_time="14:00")
    _user(task, "예약 그만할게요")
    with pytest.raises(StopResponse):
        await DynamicBookingTask.exit_task._func(task, reason_code="cancel")
    assert speech["spoken"][-1] == "신규 예약 신청 자체를 그만 진행할까요?"


# ---------- issue-grounded staff help ----------


@pytest.mark.asyncio
async def test_issue_evidence_offers_staff_help_on_a_no_match(task):
    task._call_session_data.call_config_data = TRANSFER
    quote = "배터리가 부풀어서 점검을 받고 싶어요"
    _user(task, quote)
    await _record(task, "safety_issue", quote)
    result = await _search(task)
    assert result["handoff_offer"]["reason"] == "caller_condition_not_bookable"
    assert task._offer_pending.reason_code == "staff_connection"
    _user(task, "네 연결해 주세요")
    await _answer(task, "accept")
    result = task.complete.call_args.args[0]
    assert result.exit_reason_code == "staff_connection"
    assert result.handoff_channel == "transfer"


@pytest.mark.asyncio
async def test_reject_candidate_with_issue_offers_staff_help(task):
    task._set_staged(
        StagedBooking(
            service_id=10,
            service_name="Inspection",
            operator_id=100,
            operator_name="Advisor",
            datetime="2026-09-12 10:00",
        )
    )
    quote = "배터리가 부풀고 뜨거워요"
    _user(task, quote)
    await _record(task, "safety_issue", quote)
    result = await DynamicBookingTask.reject_candidate._func(task)
    assert result["handoff_offer"]["reason"] == "safety_issue"
    assert task._rejected_candidates[0]["datetime"] == "2026-09-12 10:00"


@pytest.mark.asyncio
async def test_reject_candidate_without_issue_has_no_offer(task):
    task._set_staged(
        StagedBooking(
            service_id=10,
            service_name="Inspection",
            operator_id=100,
            operator_name="Advisor",
            datetime="2026-09-12 10:00",
        )
    )
    result = await DynamicBookingTask.reject_candidate._func(task)
    assert result["status"] == "candidate_rejected"
    assert "handoff_offer" not in result
    assert result["required_next_tool"] == "search_schedules"


@pytest.mark.asyncio
async def test_issue_withdrawn_suppresses_the_issue_offer(task):
    quote = "화면이 깜빡여요"
    _user(task, quote)
    await _record(task, "safety_issue", quote)
    retraction = "아픈 건 아니고 시간이 불편해요"
    _user(task, retraction)
    await _record(task, "safety_issue_withdrawn", retraction)
    result = await _search(task)
    assert "handoff_offer" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["assistant", "developer"])
async def test_issue_evidence_must_come_from_user_not_other_roles(task, role):
    quote = "화면이 심하게 깜빡여요"
    task._chat_ctx.add_message(role=role, content=quote)
    result = await _record(task, "safety_issue", quote)
    assert result["code"] == "ungrounded_caller_evidence"
    assert task._safety_issue_evidence is None


@pytest.mark.asyncio
@pytest.mark.parametrize("sentinel", [None, "None", "null"])
async def test_evidence_null_sentinels_are_rejected_without_state_change(
    task, sentinel
):
    result = await _record(task, "handoff_refusal", sentinel)
    assert result["code"] == "ungrounded_caller_evidence"
    assert task._proactive_offer_available() is True


@pytest.mark.asyncio
async def test_explicit_refusal_before_any_offer_suppresses_offers(task):
    refusal = "상담원 연결이나 메모는 원하지 않아요"
    _user(task, refusal)
    await _record(task, "handoff_refusal", refusal)
    result = await _search(task, day="2026-09-12", end="2026-10-11")
    assert result["required_next_action"]["handoff_available"] is False
    assert "handoff_offer" not in result


# ---------- exit_task routing contracts ----------


@pytest.mark.asyncio
async def test_dynamic_task_discards_privileged_target_from_forged_reason(task):
    await DynamicBookingTask.exit_task._func(
        task,
        reason_code="forged",
        route_target="end_call",
    )

    result = task.complete.call_args.args[0]
    assert result.exit_reason_code == "other"
    assert result.route_target == "other"


def test_dynamic_exit_schema_accepts_direct_reason_echo(task):
    bound_exit = DynamicBookingTask.exit_task._func.__get__(task, DynamicBookingTask)
    bound_tool = function_tool(
        bound_exit,
        name="exit_task",
        description="test schema",
    )

    args, kwargs = prepare_function_arguments(
        fnc=bound_tool,
        json_arguments={
            "reason_code": "staff_connection",
            "route_target": "staff_connection",
        },
    )

    assert kwargs == {}
    assert (
        inspect.signature(bound_exit).bind(*args, **kwargs).arguments["route_target"]
        == "staff_connection"
    )


def test_dynamic_exit_schema_accepts_transfer_alias(task):
    bound_exit = DynamicBookingTask.exit_task._func.__get__(task, DynamicBookingTask)
    bound_tool = function_tool(
        bound_exit,
        name="exit_task",
        description="test schema",
    )

    args, kwargs = prepare_function_arguments(
        fnc=bound_tool,
        json_arguments={
            "reason_code": "staff_connection",
            "route_target": "transfer",
        },
    )

    assert kwargs == {}
    assert (
        inspect.signature(bound_exit).bind(*args, **kwargs).arguments["route_target"]
        == "transfer"
    )


@pytest.mark.asyncio
async def test_dynamic_direct_reason_echo_is_not_stored_as_route_target(task):
    await DynamicBookingTask.exit_task._func(
        task,
        reason_code="staff_connection",
        route_target="staff_connection",
    )

    result = task.complete.call_args.args[0]
    assert result.exit_reason_code == "staff_connection"
    assert result.route_target is None


@pytest.mark.asyncio
async def test_dynamic_transfer_alias_is_not_stored_as_route_target(task):
    await DynamicBookingTask.exit_task._func(
        task,
        reason_code="staff_connection",
        route_target="transfer",
    )

    result = task.complete.call_args.args[0]
    assert result.exit_reason_code == "staff_connection"
    assert result.route_target is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", ["no_slot_memo", "staff_connection", "service_not_offered"]
)
async def test_transfer_passes_booking_action_for_booking_handoff(reason):
    agent = object.__new__(BaseAgent)
    agent._handle_transfer_call = AsyncMock()
    context = BookingMemoContext(exit_reason_code=reason)
    userdata = SimpleNamespace(
        booking_memo_context=context,
        call_config_data=SimpleNamespace(transfer_to="1004"),
        recipient_data=SimpleNamespace(phone="test"),
        room_name="synthetic",
    )
    ctx = SimpleNamespace(userdata=userdata, disallow_interruptions=Mock())
    await BaseAgent.transfer_call._func(agent, ctx)
    agent._handle_transfer_call.assert_awaited_once_with(
        call_session_data=userdata,
        action_key="booking_create",
        transfer_purpose=None,
    )


@pytest.mark.asyncio
async def test_urgent_transfer_skips_reason_question():
    agent = object.__new__(BaseAgent)
    agent._handle_transfer_call = AsyncMock()
    userdata = SimpleNamespace(
        booking_memo_context=None,
        call_config_data=SimpleNamespace(transfer_to="1004"),
        recipient_data=SimpleNamespace(phone="test"),
        room_name="synthetic",
    )

    await BaseAgent.transfer_call._func(
        agent,
        SimpleNamespace(userdata=userdata, disallow_interruptions=Mock()),
        urgent=True,
    )

    agent._handle_transfer_call.assert_awaited_once_with(
        call_session_data=userdata,
        action_key=None,
        transfer_purpose=TransferPurpose(code="urgent_handoff"),
    )


@pytest.mark.asyncio
async def test_warm_transfer_infrastructure_fallback_preserves_booking_context(
    monkeypatch,
):
    session = SimpleNamespace(
        input=SimpleNamespace(set_audio_enabled=Mock()),
        history=SimpleNamespace(add_message=Mock()),
    )
    monkeypatch.setattr(BaseAgent, "session", property(lambda _self: session))
    agent = object.__new__(BaseAgent)
    agent._handle_leave_message = AsyncMock()
    context = BookingMemoContext(exit_reason_code="no_slot_memo")
    call_session_data = SimpleNamespace(
        business_data=SimpleNamespace(client_id=1),
        recipient_data=SimpleNamespace(phone="test", sip_identity="caller"),
        room_name="synthetic",
        call_config_data=SimpleNamespace(transfer_message=None),
        booking_memo_context=context,
    )
    monkeypatch.setattr(
        "agents.base_agent.get_job_context",
        lambda: SimpleNamespace(
            room=SimpleNamespace(remote_participants={"caller": object()})
        ),
    )
    monkeypatch.setattr(
        "agents.base_agent.get_warm_transfer_queue",
        AsyncMock(side_effect=RuntimeError("unavailable")),
    )

    await agent._handle_warm_transfer_call(
        call_session_data,
        transfer_to="1004",
        action_key="booking_create",
        transfer_purpose=TransferPurpose(code="no_slot_memo"),
    )

    agent._handle_leave_message.assert_awaited_once_with(
        call_session_data,
        is_after_transfer_failure=True,
        entry_reason="preflight_failure",
        action_key="booking_create",
        transfer_purpose=TransferPurpose(code="no_slot_memo"),
        booking_context=context,
    )
