"""Reservation write path: capacity, replay, ownership, and calendar validity.

Covers the backend half of scenarios S01, S04, S05, S08, S09, S11, S15, S17.
Clock: Monday 2026-11-16 09:30 KST (conftest).
"""

from __future__ import annotations

import asyncio

from external_apis import booking_api
from fake_backend import store

SITE = "demo-device-care"
ME = {"phone": "01000000001", "customer_no": "DC-1001"}
OTHER = {"phone": "01000000002", "customer_no": "DC-1002"}


def new(when: str, operator_id: int = 401, service_id: int = 301) -> dict:
    return {
        "type": "NEW_RESERVATION",
        "service_id": service_id,
        "operator_id": operator_id,
        "reservation_request_date_time": when,
    }


async def submit(info: dict, customer: dict, key: str):
    return await booking_api.submit_reservation(
        SITE, info, customer=customer, idempotency_key=key
    )


def confirmed_count(when: str, operator_id: int) -> int:
    with store.connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM appointments WHERE start_at = ? AND operator_id = ? AND state = 'CONFIRMED'",
            (when, operator_id),
        ).fetchone()[0]


async def test_new_booking_is_saved_once_and_leaves_availability():
    first = await submit(new("2026-11-17 11:00:00"), ME, "k1")
    replay = await submit(new("2026-11-17 11:00:00"), ME, "k1")

    assert first.success and first.data.status == "ACCEPTED"
    assert replay.data.replayed is True
    assert replay.data.reservationRecordId == first.data.reservationRecordId
    assert confirmed_count("2026-11-17 11:00", 401) == 1

    day = await booking_api.get_site_available_schedules(
        SITE,
        "2026-11-17",
        "2026-11-17",
        schedule_type="DATE_TIME",
        size=100,
    )
    slots = {row.time: row.operator_ids for row in day.data.schedules}
    assert slots["2026-11-17 11:00:00"] == [402]


async def test_second_request_for_taken_slot_conflicts():
    await submit(new("2026-11-17 11:00:00"), ME, "a")
    second = await submit(new("2026-11-17 11:00:00"), OTHER, "b")
    assert second.success is False and second.data.status == "CONFLICT"
    assert confirmed_count("2026-11-17 11:00", 401) == 1


async def test_concurrent_requests_for_one_slot_book_exactly_once():
    results = await asyncio.gather(
        *(
            submit(new("2026-11-18 16:00:00"), ME if i % 2 else OTHER, f"c{i}")
            for i in range(6)
        )
    )
    statuses = sorted(r.data.status for r in results)
    assert statuses.count("ACCEPTED") == 1 and statuses.count("CONFLICT") == 5
    assert confirmed_count("2026-11-18 16:00", 401) == 1


async def test_reused_key_with_different_request_is_rejected():
    await submit(new("2026-11-17 11:00:00"), ME, "same")
    other = await submit(new("2026-11-17 11:30:00"), ME, "same")
    assert other.data.status == "INVALID"
    assert confirmed_count("2026-11-17 11:30", 401) == 0


async def test_calendar_violations_are_invalid_not_conflicts():
    cases = [
        new("2026-11-21 10:00:00"),  # Saturday
        new("2026-11-17 18:00:00"),  # after last start
        new("2026-11-16 09:00:00"),  # already past
        new("2026-12-15 10:00:00"),  # beyond the 14-day horizon
        new("2026-11-17 10:15:00"),  # not on the 30-minute grid
        new("2026-11-16 10:00:00", operator_id=402),  # advisor B off on Monday mornings
        new("2026-11-17 10:00:00", operator_id=402, service_id=302),  # wrong skill
    ]
    for i, info in enumerate(cases):
        response = await submit(info, ME, f"inv{i}")
        assert response.data.status == "INVALID", info


async def test_modify_moves_only_an_owned_appointment():
    foreign = await submit(
        {
            "type": "MODIFY_RESERVATION",
            "reservation_record_id": "DC-R-SEED-2",
            "operator_id": 401,
            "reservation_request_date_time": "2026-11-17 15:00:00",
        },
        ME,
        "m-foreign",
    )
    assert foreign.data.status == "NOT_FOUND"

    moved = await submit(
        {
            "type": "MODIFY_RESERVATION",
            "reservation_record_id": "DC-R-SEED-1",
            "service_id": 301,
            "operator_id": 401,
            "reservation_request_date_time": "2026-11-17 15:00:00",
        },
        ME,
        "m-own",
    )
    assert moved.data.status == "ACCEPTED"
    assert confirmed_count("2026-11-17 15:00", 401) == 1
    assert confirmed_count("2026-11-19 10:00", 401) == 0  # old slot released


async def test_cancel_is_owned_audited_and_not_repeatable():
    foreign = await submit(
        {"type": "CANCEL_RESERVATION", "reservation_record_id": "DC-R-SEED-1"}, OTHER, "x1"
    )
    assert foreign.data.status == "NOT_FOUND"

    cancelled = await submit(
        {"type": "CANCEL_RESERVATION", "reservation_record_id": "DC-R-SEED-1"}, ME, "x2"
    )
    again = await submit(
        {"type": "CANCEL_RESERVATION", "reservation_record_id": "DC-R-SEED-1"}, ME, "x3"
    )
    assert cancelled.data.status == "ACCEPTED"
    assert again.data.status == "NOT_FOUND"

    with store.connection() as conn:
        row = conn.execute(
            "SELECT state FROM appointments WHERE reservation_record_id = 'DC-R-SEED-1'"
        ).fetchone()
    assert row["state"] == "CANCELLED"  # kept for audit, not deleted


async def test_submit_faults_never_raise(monkeypatch):
    monkeypatch.setenv("DEMO_FAULTS", '{"reservation_submit": ["error", "timeout"]}')
    error = await submit(new("2026-11-17 11:00:00"), ME, "f1")
    timeout = await submit(new("2026-11-17 11:00:00"), ME, "f2")
    assert (error.success, error.data.status) == (False, "ERROR")
    assert (timeout.success, timeout.data.status) == (False, "ERROR")
    assert confirmed_count("2026-11-17 11:00", 401) == 0


async def test_concurrent_first_requests_seed_the_database_once():
    """Regression: concurrent first connections used to double-insert seed rows."""
    results = await asyncio.gather(
        *(booking_api.get_services(SITE) for _ in range(8))
    )
    assert all(r.success for r in results)
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM services").fetchone()[0] == 3
