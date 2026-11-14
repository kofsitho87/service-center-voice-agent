"""The HTTP clients work against the fake backend.

These call the real ``external_apis`` / ``services`` client functions; only the
transport underneath is fake. Clock: Monday 2026-11-16 09:30 KST (conftest).
"""

from __future__ import annotations

import httpx
import pytest

from external_apis import booking_api, fastapi_app_api, sms_api
from external_apis.types.request_types import (
    GetConfirmedAppointmentsRequest,
    GetCustomerRequest,
)
from fake_backend import outbox
from fake_backend.transport import OutboundRequestBlocked
from services.trunk_allocation_client import TrunkAllocationClient, TrunkStatus
from tasks.operator_schedule_selection_task import _operator_times_to_schedules
from utils.http_client import create_async_http_client

SITE = "demo-device-care"


async def test_reference_data_matches_source_shapes():
    services = await booking_api.get_services(SITE, "ALLOW_ONLY_REVISIT")
    operators = await booking_api.get_site_operators(SITE, "ALLOW_ONLY_REVISIT")

    assert services.success and operators.success
    assert [d.name for d in services.data.services] == [
        "휴대폰 점검",
        "노트북 상담",
        "소형기기 점검",
    ]
    by_id = {op.operatorId: op for op in operators.data}
    assert {d.serviceId for d in by_id[401].services} == {301, 302}
    assert {d.serviceId for d in by_id[402].services} == {301, 303}


async def test_day_slots_respect_hours_bookings_and_today_floor():
    # Thursday: advisor A has a seeded 10:00 booking; advisor B works all day.
    thursday = await booking_api.get_site_available_schedules(
        SITE,
        "2026-11-19",
        "2026-11-19",
        "ALLOW_ONLY_REVISIT",
        schedule_type="DATE_TIME",
        size=100,
    )
    slots = {row.time: row.operator_ids for row in thursday.data.schedules}
    assert slots["2026-11-19 10:00:00"] == [402]
    assert slots["2026-11-19 09:00:00"] == [401, 402]
    assert "2026-11-19 17:30:00" in slots and "2026-11-19 18:00:00" not in slots
    assert len(thursday.data.schedules) < 100  # below the 100-row page tripwire

    # Today (Monday 09:30): nothing at or before now; advisor B starts at 13:00.
    today = await booking_api.get_site_available_schedules(
        SITE,
        "2026-11-16",
        "2026-11-16",
        "ALLOW_ONLY_REVISIT",
        schedule_type="DATE_TIME",
        size=100,
    )
    today_slots = {row.time: row.operator_ids for row in today.data.schedules}
    assert min(today_slots) == "2026-11-16 10:00:00"
    assert today_slots["2026-11-16 12:30:00"] == [401]
    assert today_slots["2026-11-16 13:00:00"] == [401, 402]


async def test_date_probe_skips_weekends_and_horizon():
    response = await booking_api.get_site_available_schedules(
        SITE,
        "2026-11-16",
        "2026-12-31",
        "ALLOW_ONLY_NEW",
        schedule_type="DATE",
        size=100,
    )
    dates = [row.time for row in response.data.schedules]
    assert "2026-11-21" not in dates and "2026-11-22" not in dates  # weekend
    assert dates[-1] <= "2026-11-30"  # 14-day horizon
    assert dates[0] == "2026-11-16"


async def test_service_filter_limits_advisors():
    response = await booking_api.get_site_available_schedules(
        SITE,
        "2026-11-17",
        "2026-11-17",
        "ALLOW_ONLY_NEW",
        schedule_type="DATE_TIME",
        size=100,
        service_id=303,
    )
    assert {i for row in response.data.schedules for i in row.operator_ids} == {402}


async def test_operator_times_parse_with_source_helper():
    response = await booking_api.get_operator_available_times(
        402, "2026-11-16", "2026-11-16"
    )
    schedules = _operator_times_to_schedules(list(response.data.availableTimes), 402)
    assert schedules[0] == {"time": "2026-11-16 13:00:00", "advisor_id": 402}


async def test_customer_lookup_and_owned_appointments():
    single = await booking_api.get_customer_by_phone(
        GetCustomerRequest(site_id=SITE, phone="01000000001")
    )
    unknown = await booking_api.get_customer_by_phone(
        GetCustomerRequest(site_id=SITE, phone="01000009999")
    )
    assert [p.customerNo for p in single.data.customers] == ["DC-1001"]
    assert unknown.success and unknown.data.customers == []

    mine = await booking_api.get_confirmed_appointments(
        GetConfirmedAppointmentsRequest(site_id=SITE, phone="01000000002")
    )
    assert [a.reservationRecordId for a in mine.data] == ["DC-R-SEED-2", "DC-R-SEED-3"]
    assert mine.data[0].reservationDateTime == "2026-11-18 14:00:00"


async def test_read_fault_maps_to_source_failure_shape(monkeypatch):
    monkeypatch.setenv("DEMO_FAULTS", '{"reservation_read": "error"}')
    response = await booking_api.get_services(SITE, "ALLOW_ONLY_NEW")
    assert response.success is False and response.data is None
    assert response.message  # server message surfaced by the client


async def test_sms_is_recorded_not_delivered():
    result = await sms_api.send_sms(
        site_id=SITE, receiver_phone_number="01000000001", content="테스트"
    )
    assert result.success is True
    record = outbox.read("sms")[-1]
    assert record["delivered"] is False and record["content"] == "테스트"


async def test_call_context_comes_from_local_fixture():
    context = await fastapi_app_api.get_call_context(9001)
    assert "가상" in context.data.center_name
    assert "운영 시간" in context.data.context


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("available", TrunkStatus.AVAILABLE),
        ("busy", TrunkStatus.BUSY),
        ("fatal", TrunkStatus.FATAL),
        ("network", TrunkStatus.NETWORK_ERROR),
    ],
)
async def test_trunk_client_hmac_contract(monkeypatch, script, expected):
    monkeypatch.setenv("DEMO_FAULTS", f'{{"trunk": "{script}"}}')
    client = await TrunkAllocationClient.from_env_async()
    try:
        assert (await client.check(9001)).status is expected
    finally:
        await client.aclose()


async def test_trunk_rejects_bad_signature(monkeypatch):
    monkeypatch.setenv("WARM_TRANSFER_HMAC_SECRET", "wrong-secret")
    client = await TrunkAllocationClient.from_env_async()
    try:
        result = await client.check(9001)
    finally:
        await client.aclose()
    assert result.status is TrunkStatus.FATAL and result.status_code == 401


async def test_non_fake_hosts_are_blocked():
    async with await create_async_http_client() as client:
        with pytest.raises(OutboundRequestBlocked):
            await client.get("https://example.org/")
    assert issubclass(OutboundRequestBlocked, httpx.ConnectError)
