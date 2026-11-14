"""Characterization of DynamicBookingTask search/stage outputs and side effects.

Pins the full tool payload, the developer events and the task state after each
scenario, so the search refactor (fetch helpers, search strategy extraction,
confirmation gate split) can be checked as byte-for-byte behavior-preserving.
The golden snapshot was captured from the pre-refactor implementation; the API
is faked at ``get_site_available_schedules`` so the fetch path is covered too.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pytest
from livekit.agents import llm

from data import DynamicBookingResumeContext
from tasks.dynamic_booking_task import DynamicBookingTask

KST = ZoneInfo("Asia/Seoul")
FIXED_NOW = dt.datetime(2026, 8, 27, 9, 0, tzinfo=KST)
GOLDEN_PATH = Path(__file__).with_name("test_b_search_schedules_golden.py")

# date -> [(HH:MM, advisor ids)]
CATALOG: dict[str, list[tuple[str, list[int]]]] = {
    "2026-08-27": [("08:30", [805]), ("10:00", [805, 806]), ("14:00", [807])],
    "2026-08-28": [
        ("09:00", [805]),
        ("09:30", [806]),
        ("11:00", [807]),
        ("15:00", [805]),
    ],
    "2026-08-29": [("13:00", [806])],
    "2026-08-30": [("12:00", [807])],
    "2026-08-31": [("10:00", [805]), ("10:30", [805])],
    **{
        f"2026-09-0{day}": [("16:00", [805, 807]), ("17:30", [806])]
        for day in range(1, 9)
    },
    "2026-09-10": [("09:00", [806])],
}


def _response(rows: list[Any]) -> SimpleNamespace:
    return SimpleNamespace(
        success=True,
        data=SimpleNamespace(schedules=rows),
        message=None,
        elapsed_ms=None,
        request_params=None,
    )


async def _fake_schedules(site_id, start, end, **kwargs):
    rows = []
    for day in sorted(CATALOG):
        if not start <= day <= end:
            continue
        if kwargs["schedule_type"] == "DATE_TIME":
            rows.extend(
                SimpleNamespace(time=f"{day} {clock}:00", operator_ids=list(ids))
                for clock, ids in CATALOG[day]
            )
        else:
            ids = sorted({i for _, slot_ids in CATALOG[day] for i in slot_ids})
            rows.append(SimpleNamespace(time=day, operator_ids=ids))
    return _response(rows)


def _message(message_id: str, role: str, text: str) -> llm.ChatMessage:
    return llm.ChatMessage(id=message_id, role=role, content=[text])


def _task(*, offers: bool = False, select_operator: bool = False):
    task = object.__new__(DynamicBookingTask)
    task._chat_ctx = llm.ChatContext()
    task._chat_ctx.items.append(_message("u1", "user", "예약하려고요"))
    task._select_operator = select_operator
    task._services = [
        {"id": 3590, "name": "휴대폰 점검", "keywords": []},
        {"id": 4720, "name": "노트북 상담", "keywords": []},
    ]
    task._advisors = [
        {"id": 805, "name": "김테스트", "service_ids": [3590]},
        {"id": 806, "name": "이테스트", "service_ids": [4720]},
        {"id": 807, "name": "박테스트", "service_ids": [3590, 4720]},
    ]
    task._advisors_by_id = {advisor["id"]: advisor for advisor in task._advisors}
    task._schedule_lookup_days = 30
    task._staged = None
    task._staged_requires_revalidation = False
    task._pending_earliest_offer = None
    task._day_rows_cache = {}
    task._date_rows_cache = {}
    task._service_reservation_setting = "ALLOW_ONLY_NEW"
    task._mutation_lock = asyncio.Lock()
    task.complete = Mock()
    task._call_session_data = SimpleNamespace(
        room_name="synthetic-characterization",
        dynamic_booking_pending=None,
        reservation_info={},
        business_data=SimpleNamespace(site_id="site-1", client_id=1),
        call_config_data=SimpleNamespace(
            language="ko-KR", transfer_mode="leave_memo", transfer_to=None
        ),
        dynamic_booking_resume=DynamicBookingResumeContext(
            handoff_offer_delivered=not offers
        ),
    )
    return task


def _user(task, message_id: str, text: str) -> None:
    task._chat_ctx.items.append(_message(message_id, "user", text))


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {"__dataclass__": type(value).__name__, **_jsonable(vars(value))}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (frozenset, set)):
        return sorted(_jsonable(item) for item in value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


STATE_FIELDS = (
    "_last_search_conditions",
    "_empty_searched_ranges",
    "_pending_empty_search_user_message_id",
    "_last_presented",
    "_schedule_blocker",
    "_pending_earliest_offer",
    "_offer_pending",
    "_staged",
    "_request_revision",
    "_declined_turn_id",
    "_searched_in_task",
    "_rejected_candidates",
)


def _snapshot(task, session, outputs: list[Any]) -> Any:
    state = {name: getattr(task, name) for name in STATE_FIELDS}
    state["_pending_empty_search_set"] = task._pending_empty_search is not None
    state["resume"] = task._call_session_data.dynamic_booking_resume
    state["pending"] = task._call_session_data.dynamic_booking_pending
    events = [
        call.kwargs.get("extra") for call in session.history.add_message.call_args_list
    ]
    return json.loads(
        json.dumps(
            _jsonable({"outputs": outputs, "state": state, "events": events}),
            ensure_ascii=False,
            default=str,
        )
    )


async def _search(task, **kwargs):
    return await DynamicBookingTask.search_schedules._func(task, **kwargs)


async def _stage(task, **kwargs):
    ctx = SimpleNamespace(disallow_interruptions=Mock())
    return await DynamicBookingTask.stage_booking._func(task, ctx, **kwargs)


def _day(day: str, **kwargs) -> dict[str, Any]:
    return {"start_date": day, "end_date": day, **kwargs}


def _range(start: str, end: str, **kwargs) -> dict[str, Any]:
    return {"start_date": start, "end_date": end, **kwargs}


# name -> (task kwargs, steps); a step is ("search"|"stage"|"user", payload)
SCENARIOS: dict[str, tuple[dict[str, Any], list[tuple[str, Any]]]] = {
    "one_day_no_service": ({}, [("search", _day("2026-08-28"))]),
    "one_day_today_floor": ({}, [("search", _day("2026-08-27"))]),
    "one_day_service": ({}, [("search", _day("2026-08-28", service_id=3590))]),
    "one_day_target_miss_nearby": (
        {},
        [("search", _day("2026-08-28", service_id=3590, target_time="10:00"))],
    ),
    "one_day_target_hit": (
        {},
        [("search", _day("2026-08-28", service_id=3590, target_time="15:00"))],
    ),
    "one_day_operator_miss_other_advisors": (
        {},
        [("search", _day("2026-08-29", service_id=4720, operator_id=807))],
    ),
    "one_day_earliest": (
        {},
        [("search", _day("2026-08-28", service_id=3590, mode="earliest"))],
    ),
    "one_day_empty_whole_day": (
        {},
        [("search", _day("2026-09-09", service_id=3590))],
    ),
    "one_day_empty_time_filtered": (
        {},
        [("search", _day("2026-08-29", service_id=4720, time_from="14:00"))],
    ),
    "one_day_single_slot_autostage": (
        {},
        [("search", _day("2026-08-29", service_id=4720))],
    ),
    "one_day_select_operator": (
        {"select_operator": True},
        [("search", _day("2026-08-28", service_id=3590))],
    ),
    "multi_plain_dates": (
        {},
        [("search", _range("2026-08-28", "2026-09-10", service_id=3590))],
    ),
    "multi_filtered_limit_mid_chunk": (
        {},
        [
            (
                "search",
                _range(
                    "2026-09-01", "2026-09-08", service_id=3590, time_from="15:00"
                ),
            )
        ],
    ),
    "multi_filtered_target_nearby": (
        {},
        [
            (
                "search",
                _range(
                    "2026-09-01", "2026-09-05", service_id=3590, target_time="15:00"
                ),
            )
        ],
    ),
    "multi_filtered_empty": (
        {},
        [
            (
                "search",
                _range("2026-08-29", "2026-09-04", service_id=3590, time_to="08:00"),
            )
        ],
    ),
    "multi_earliest_for_booking": (
        {},
        [
            (
                "search",
                _range(
                    "2026-08-28",
                    "2026-09-26",
                    service_id=4720,
                    mode="earliest_for_booking",
                ),
            )
        ],
    ),
    "multi_earliest_horizon_clamp": (
        {},
        [
            (
                "search",
                _range("2026-08-28", "2026-10-30", service_id=3590, mode="earliest"),
            )
        ],
    ),
    "multi_earliest_target_nearby": (
        {},
        [
            (
                "search",
                _range(
                    "2026-09-01",
                    "2026-09-03",
                    service_id=3590,
                    target_time="15:30",
                    mode="earliest",
                ),
            )
        ],
    ),
    "multi_earliest_empty": (
        {},
        [
            (
                "search",
                _range(
                    "2026-09-09",
                    "2026-09-26",
                    service_id=3590,
                    mode="earliest_for_booking",
                ),
            )
        ],
    ),
    "multi_operator_other_advisor_dates": (
        {},
        [
            (
                "search",
                _range("2026-08-29", "2026-08-30", service_id=3590, operator_id=805),
            )
        ],
    ),
    "multi_operator_other_advisor_hint_time": (
        {},
        [
            (
                "search",
                _range(
                    "2026-08-29",
                    "2026-08-30",
                    service_id=3590,
                    operator_id=805,
                    time_from="09:00",
                ),
            )
        ],
    ),
    "multi_strictly_after": (
        {},
        [
            (
                "search",
                _range(
                    "2026-08-31",
                    "2026-09-02",
                    service_id=3590,
                    strictly_after="2026-08-31 10:00",
                ),
            )
        ],
    ),
    "multi_rejected_candidates": (
        {"rejected": True},
        [("search", _range("2026-09-01", "2026-09-05", service_id=3590))],
    ),
    "multi_empty_through_horizon_offer": (
        {"offers": True},
        [("search", _range("2026-09-09", "2026-09-26", service_id=3590))],
    ),
    "invalid_range": ({}, [("search", _day("2026-08-20"))]),
    "invalid_time": ({}, [("search", _day("2026-08-28", time_from="25:00"))]),
    "invalid_time_order": (
        {},
        [("search", _day("2026-08-28", time_from="15:00", time_to="10:00"))],
    ),
    "earliest_for_booking_requires_service": (
        {},
        [("search", _range("2026-08-28", "2026-09-01", mode="earliest_for_booking"))],
    ),
    "awaiting_after_empty_same_turn": (
        {},
        [
            ("search", _day("2026-09-09", service_id=3590)),
            ("search", _day("2026-09-10", service_id=3590)),
        ],
    ),
    "declined_alternatives_offer": (
        {"offers": True},
        [
            ("search", _day("2026-08-28", service_id=3590)),
            ("user", "다른 날은요"),
            ("search", _day("2026-08-31", service_id=3590)),
            ("user", "그 다음 주는요"),
            ("search", _day("2026-09-01", service_id=3590)),
            ("user", "더 뒤는요"),
            ("search", _day("2026-09-02", service_id=3590)),
        ],
    ),
    "caller_date_correction": (
        {},
        [
            ("user", "내일 예약할게요"),
            ("search", _day("2026-08-29", service_id=3590)),
        ],
    ),
    "stage_success_after_search": (
        {},
        [
            ("search", _day("2026-08-28", service_id=3590)),
            ("user", "9시로 할게요"),
            ("stage", {"service_id": 3590, "datetime": "2026-08-28 09:00"}),
        ],
    ),
    "stage_unavailable": (
        {},
        [("stage", {"service_id": 3590, "datetime": "2026-08-28 10:00"})],
    ),
    "stage_select_operator_choice": (
        {"select_operator": True},
        [("stage", {"service_id": 4720, "datetime": "2026-08-27 10:00"})],
    ),
    "earliest_offer_then_stage": (
        {},
        [
            (
                "search",
                _range(
                    "2026-08-28",
                    "2026-09-26",
                    service_id=4720,
                    mode="earliest_for_booking",
                ),
            ),
            ("stage", {"service_id": 4720, "datetime": "2026-08-28 09:30"}),
        ],
    ),
}


async def run_scenario(name: str) -> Any:
    options, steps = SCENARIOS[name]
    task = _task(
        offers=options.get("offers", False),
        select_operator=options.get("select_operator", False),
    )
    if options.get("rejected"):
        task._rejected_candidates = [
            {
                "datetime": "2026-09-01 16:00",
                "scope": "slot",
                "user_message_id": "u0",
                "reconsidered_datetimes": [],
            },
            {
                "datetime": "2026-09-02 16:00",
                "scope": "date",
                "user_message_id": "u0",
                "reconsidered_datetimes": [],
            },
        ]
    session = SimpleNamespace(history=SimpleNamespace(add_message=Mock()))
    outputs: list[Any] = []
    offer_ids = iter(f"offer{index:028d}" for index in range(100))
    with (
        patch.object(DynamicBookingTask, "session", session),
        patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW),
        patch(
            "tasks.dynamic_booking_task.get_site_available_schedules",
            side_effect=_fake_schedules,
        ),
        patch("uuid.uuid4", side_effect=lambda: SimpleNamespace(hex=next(offer_ids))),
    ):
        for index, (kind, payload) in enumerate(steps):
            if kind == "user":
                _user(task, f"u{index + 2}", payload)
            elif kind == "search":
                outputs.append(await _search(task, **payload))
            else:
                outputs.append(await _stage(task, **payload))
    return _snapshot(task, session, outputs)


def _golden() -> dict[str, Any]:
    namespace: dict[str, Any] = {}
    exec(GOLDEN_PATH.read_text(encoding="utf-8"), namespace)
    return json.loads(namespace["GOLDEN_JSON"])


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_search_and_stage_match_pre_refactor_snapshot(name):
    assert await run_scenario(name) == _golden()[name]
