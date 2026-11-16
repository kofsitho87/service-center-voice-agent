"""Dynamic booking slot facts: normalization, filters and tool-result shaping.

Stateless helpers over slot dicts ("YYYY-MM-DD HH:MM" -> eligible advisor ids)
and tool payloads. DynamicBookingTask exposes the shaping helpers under their
old private method names.
"""

from __future__ import annotations

from typing import Any

from tasks.booking.dynamic_dates import _is_whole_day
from tasks.booking.dynamic_reference import advisor_is_eligible

MAX_SCHEDULE_OPTIONS_TO_PRESENT = 3


def normalize_slot_datetime(raw: str) -> str | None:
    value = str(raw).strip().replace("T", " ")
    if len(value) >= 16 and value[4] == "-" and value[7] == "-":
        return value[:16]
    return None


def normalize_day_rows(
    rows: list[Any],
    advisors_by_id: dict[int, dict[str, Any]],
    *,
    service_id: int | None = None,
    operator_id: int | None = None,
) -> dict[str, list[int]]:
    """일시 → eligible 상담사 id(주입 순서)."""
    order = {advisor_id: i for i, advisor_id in enumerate(advisors_by_id)}
    slots: dict[str, list[int]] = {}
    for item in rows:
        when = normalize_slot_datetime(item.time)
        if when is None:
            continue
        eligible: list[int] = []
        for raw_id in item.operator_ids:
            advisor_id = int(raw_id)
            advisor = advisors_by_id.get(advisor_id)
            if advisor is not None and advisor_is_eligible(
                advisor_id,
                advisor,
                service_id=service_id,
                operator_id=operator_id,
            ):
                eligible.append(advisor_id)
        if eligible:
            slots[when] = sorted(set(eligible), key=order.__getitem__)
    return slots


def apply_strictly_after(
    slots: dict[str, list[int]], strictly_after: str | None
) -> dict[str, list[int]]:
    """`strictly_after` 초과(그 일시 제외)만 남긴다.

    이름 유래: 과거 `not_before`였으나 "not before 18:00"이 18:00 포함으로
    읽혀 LLM이 특정 시각 질의에 오용했다. exclusive
    의미가 이름에 드러나도록 개명.
    """
    if not strictly_after:
        return slots
    return {when: ids for when, ids in slots.items() if when > strictly_after}


def limit_schedule_options(
    result: dict[str, Any], *, option_key: str
) -> dict[str, Any]:
    """Expose at most three customer-facing date/time choices to the LLM.

    The booking API result remains complete inside ``search_schedules`` for
    filtering and analytics. Only the final tool payload is capped, so prompt
    compliance is not the sole guard against reading a long schedule list.
    """
    options = result.get(option_key)
    if not isinstance(options, list) or not options:
        return result

    total_available = len(options)
    result[option_key] = options[:MAX_SCHEDULE_OPTIONS_TO_PRESENT]
    remaining_count = max(0, total_available - MAX_SCHEDULE_OPTIONS_TO_PRESENT)
    result["presentation"] = {
        "option_kind": ("date" if option_key == "available_dates" else "datetime"),
        "max_options": MAX_SCHEDULE_OPTIONS_TO_PRESENT,
        "returned": len(result[option_key]),
        "total_available": total_available,
        "has_more": remaining_count > 0,
        "remaining_count": remaining_count,
        "instruction": (
            f"{option_key}에 담긴 선택지만 안내하세요. 날짜와 시각 선택지는 "
            f"합계 최대 {MAX_SCHEDULE_OPTIONS_TO_PRESENT}개입니다. has_more가 "
            "true이면 다른 구체적인 일정은 말하지 말고 추가 일정이 있다고만 "
            "요약한 뒤 선호 요일, 주 또는 시간대를 물으세요."
        ),
    }
    return result


def error_result(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"status": "error", "code": code, "message": message, **extra}


def add_earliest_only_truncation(
    result: dict[str, Any], *, total_slot_count: int
) -> dict[str, Any]:
    """earliest_only 로 잘라낸 사실을 응답에 남긴다.

    잘린 1건만 보면 LLM 도 그 날의 유일한 슬롯으로 읽는다. "가장 빠른 시간
    몇 신데?" 에 09:30 하나만 돌려주면 에이전트가 그 자리에서 확인 질문을
    던지고, 고객이 "그 뒤는?" 으로 되물으면 commit 이 staging_mismatch 로
    되돌아간다.
    """
    if total_slot_count > 1:
        result["earliest_only_truncated"] = {
            "returned": 1,
            "omitted_later_slots": total_slot_count - 1,
            "instruction": (
                "같은 조건에 더 늦은 시각이 남아 있습니다. 이 슬롯을 그 날의 "
                "유일한 선택지로 말하지 마세요. 고객이 다른 시각을 물으면 "
                "strictly_after로 다시 조회하세요."
            ),
        }
    return result


def add_range_adjustment(
    result: dict[str, Any],
    *,
    requested_end_date: str,
    applied_end_date: str,
    reason: str = "booking_horizon",
) -> dict[str, Any]:
    if requested_end_date != applied_end_date:
        result["range_adjustment"] = {
            "reason": reason,
            "requested_end_date": requested_end_date,
            "applied_end_date": applied_end_date,
        }
    return result


def is_hhmm(value: str) -> bool:
    return (
        len(value) == 5
        and value[2] == ":"
        and value[:2].isdigit()
        and value[3:].isdigit()
        and 0 <= int(value[:2]) <= 23
        and 0 <= int(value[3:]) <= 59
    )


def validate_time_filters(
    *,
    time_from: str | None,
    time_to: str | None,
    target_time: str | None,
) -> dict[str, Any] | None:
    for name, value in (
        ("time_from", time_from),
        ("time_to", time_to),
        ("target_time", target_time),
    ):
        if value is not None and not is_hhmm(value):
            return error_result(
                "invalid_time",
                f"{name}은 HH:MM 형식이어야 합니다.",
                retry="silent",
            )
    if time_from and time_to and time_from > time_to:
        return error_result(
            "invalid_time_range",
            "time_from must not be later than time_to. Preserve the caller's "
            "constraints and clarify the intended window before retrying.",
            retry="clarify",
        )
    if target_time is not None and (
        (time_from is not None and target_time < time_from)
        or (time_to is not None and target_time > time_to)
    ):
        return error_result(
            "conflicting_time_filters",
            "target_time falls outside the caller's time window. Preserve "
            "both constraints and ask which time condition the caller intends "
            "to change before retrying. Do not silently drop a constraint.",
            retry="clarify",
        )
    return None


def searched_time_window(
    time_from: str | None,
    time_to: str | None,
    target_time: str | None,
) -> dict[str, str] | None:
    """The clock range a time-filtered search verified; None for whole days.

    A non-empty partial-day result otherwise reads as the whole day's
    schedule, and the model answers a later out-of-range time ("오전 11시는
    요?" after an afternoon search) as unavailable without searching it.
    Nearby offers around target_time are alternatives, not verified range.
    """
    if target_time is not None:
        start = end = target_time
    elif _is_whole_day(time_from, time_to):
        return None
    else:
        start, end = time_from or "00:00", time_to or "23:59"
    return {
        "from": start,
        "to": end,
        "instruction": (
            "이 결과는 위 시각 범위만 확인했습니다. 범위 밖 시각을 고객이 "
            "물으면 가능·불가를 말하기 전에 그 시각으로 search_schedules를 "
            "다시 호출하세요."
        ),
    }


def filter_time_constraints(
    slots: dict[str, list[int]],
    *,
    time_from: str | None,
    time_to: str | None,
    target_time: str | None,
) -> dict[str, list[int]]:
    if time_from is not None:
        slots = {w: ids for w, ids in slots.items() if w[11:16] >= time_from}
    if time_to is not None:
        slots = {w: ids for w, ids in slots.items() if w[11:16] <= time_to}
    if target_time is not None:
        slots = {w: ids for w, ids in slots.items() if w[11:16] == target_time}
    return slots


def add_time_constraint_facts(
    result: dict[str, Any],
    *,
    time_from: str | None,
    time_to: str | None,
    target_time: str | None,
) -> dict[str, Any]:
    if target_time is not None:
        result["target_time"] = target_time
    if time_from is not None or time_to is not None:
        result["time_from"] = time_from
        result["time_to"] = time_to
    return result


def has_nearby_time_offer(result: dict[str, Any]) -> bool:
    target_time = result.get("target_time")
    slots = result.get("slots")
    return bool(
        isinstance(target_time, str)
        and isinstance(slots, list)
        and slots
        and not any(
            isinstance(slot, dict) and slot.get("datetime", "")[11:16] == target_time
            for slot in slots
        )
    )


def add_latest_time_comparison(
    result: dict[str, Any], latest_times: dict[str, str]
) -> None:
    """Compare the latest bookable time across the returned (displayed) dates."""
    displayed_dates = result["available_dates"]
    if displayed_dates:
        for item in displayed_dates:
            item["latest_appointment_time"] = latest_times[item["date"]]
        latest_time = max(item["latest_appointment_time"] for item in displayed_dates)
        result["latest_time_comparison"] = {
            "scope": "returned_dates",
            "time": latest_time,
            "dates": [
                item["date"]
                for item in displayed_dates
                if item["latest_appointment_time"] == latest_time
            ],
        }
