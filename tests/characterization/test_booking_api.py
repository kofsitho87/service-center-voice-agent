"""HTTP-level characterization tests for the reservation API wrappers."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from external_apis import booking_api
from external_apis.types.request_types import (
    GetConfirmedAppointmentsRequest,
    GetCustomerRequest,
)

BASE_URL = "https://reservation.test"


@dataclass
class ApiCase:
    name: str
    event: str
    call: Callable[[], Awaitable[Any]]
    path: str
    params: dict[str, str]
    payload: Any
    check: Callable[[Any], None]
    http_error_message: str
    unexpected_message: str
    timeout_message: str | None = None
    uses_server_message: bool = True
    client_options: dict[str, Any] = field(default_factory=dict)
    request_params: dict[str, Any] | None = None
    logged_success_data: Any = None


def _check_operators(result):
    assert result.data[0].operatorId == 1
    assert result.data[0].displayName == "Advisor Kim"


def _check_operator_times(result):
    assert result.data.operatorId == 5
    assert result.data.availableTimes == ["2030-01-02T09:00:00.000Z"]


def _check_site_schedules(result):
    assert result.data.schedules[0].operator_ids == [3]
    assert result.request_params == _SITE_REQUEST_PARAMS


def _check_services(result):
    assert result.data.services[0].serviceId == 1



def _check_available_schedules(result):
    assert [(item.time, item.advisor_id) for item in result.data.schedules] == [
        ("2030-01-02 09:00:00", 3)
    ]


def _check_confirmed_appointments(result):
    assert result.message is None
    assert result.data[0].reservationRecordId == "R1"


def _check_customer(result):
    assert result.message == "ok"
    assert result.data.customerResponseType == "NEW"


_SITE_REQUEST_PARAMS = {
    "site_id": "S1",
    "start_date": "2030-01-01",
    "end_date": "2030-01-31",
    "schedule_type": "DATE",
    "size": 10,
    "operator_id": 3,
    "service_id": 4,
    "client_id": None,
}

_CONFIRMED_ITEM = {
    "reservationDateTime": "2030-01-02 09:00:00",
    "state": "CONFIRMED",
    "reservationRecordId": "R1",
}

CASES = [
    ApiCase(
        name="get_operators",
        event="GET_OPERATORS",
        call=lambda: booking_api.get_operators(12),
        path="/reservation/12",
        params={},
        payload={
            "resultMsg": "ok",
            "contents": [{"operatorId": 1, "displayName": "Advisor Kim"}],
        },
        check=_check_operators,
        http_error_message="상담사 조회 중 오류가 발생했습니다.",
        unexpected_message="예약 조회 중 예상치 못한 오류가 발생했습니다.",
    ),
    ApiCase(
        name="get_site_operators",
        event="GET_SITE_OPERATORS",
        call=lambda: booking_api.get_site_operators("S1"),
        path="/reservation/S1/operators",
        params={},
        payload={
            "resultMsg": "ok",
            "contents": [{"operatorId": 1, "displayName": "Advisor Kim"}],
        },
        check=_check_operators,
        http_error_message="상담사 조회 중 오류가 발생했습니다.",
        unexpected_message="상담사 조회 중 예상치 못한 오류가 발생했습니다.",
    ),
    ApiCase(
        name="get_operator_available_times",
        event="GET_OPERATOR_AVAILABLE_TIMES",
        call=lambda: booking_api.get_operator_available_times(
            5, "2030-01-01", "2030-01-31"
        ),
        path="/reservation/operators/5/schedules/available",
        params={"startDate": "2030-01-01", "endDate": "2030-01-31"},
        payload={
            "resultMsg": "ok",
            "contents": {
                "operatorId": 5,
                "availableTimes": ["2030-01-02T09:00:00.000Z"],
            },
        },
        check=_check_operator_times,
        http_error_message="상담사 예약 가능 시간 조회 중 오류가 발생했습니다.",
        unexpected_message=(
            "상담사 예약 가능 시간 조회 중 예상치 못한 오류가 발생했습니다."
        ),
    ),
    ApiCase(
        name="get_site_available_schedules",
        event="GET_SITE_AVAILABLE_SCHEDULES",
        call=lambda: booking_api.get_site_available_schedules(
            "S1",
            "2030-01-01",
            "2030-01-31",
            operator_id=3,
            service_id=4,
        ),
        path="/reservation/S1/schedules/available",
        params={
            "startDate": "2030-01-01",
            "endDate": "2030-01-31",
            "type": "DATE",
            "size": "10",
            "operatorId": "3",
            "serviceId": "4",
        },
        payload={
            "resultMsg": "ok",
            "contents": {"schedules": [{"time": "2030-01-02", "operatorIds": [3]}]},
        },
        check=_check_site_schedules,
        http_error_message="사이트 예약 가능 일정 조회 중 오류가 발생했습니다.",
        unexpected_message="사이트 예약 가능 일정 조회 중 예상치 못한 오류가 발생했습니다.",
        request_params=_SITE_REQUEST_PARAMS,
    ),
    ApiCase(
        name="get_services",
        event="GET_SERVICES",
        call=lambda: booking_api.get_services("S1"),
        path="/reservation/S1/services",
        params={},
        payload={
            "resultMsg": "ok",
            "contents": {
                "services": [
                    {
                        "serviceId": 1,
                        "name": "Phone Inspection",
                        "operatorCount": 1,
                        "services": None,
                    }
                ]
            },
        },
        check=_check_services,
        http_error_message="서비스 조회 중 오류가 발생했습니다.",
        unexpected_message="예약 조회 중 예상치 못한 오류가 발생했습니다.",
    ),
    ApiCase(
        name="get_available_schedules",
        event="GET_AVAILABLE_SCHEDULES",
        call=lambda: booking_api.get_available_schedules(
            9, "2030-01-01", "2030-01-31", operator_id=3
        ),
        path="/reservation/services/9/schedules/available",
        params={"startDate": "2030-01-01", "endDate": "2030-01-31", "operatorId": "3"},
        payload={
            "resultMsg": "ok",
            "contents": {
                "schedules": [{"time": "2030-01-02 09:00:00", "advisorIds": [2, 3]}]
            },
        },
        check=_check_available_schedules,
        http_error_message="예약 가능 일정 조회 중 오류가 발생했습니다.",
        unexpected_message="예약 가능 일정 조회 중 예상치 못한 오류가 발생했습니다.",
        # The wrapper logs the payload after mapping advisorIds to advisor_id.
        logged_success_data={
            "resultMsg": "ok",
            "contents": {
                "schedules": [{"time": "2030-01-02 09:00:00", "advisor_id": 3}]
            },
        },
    ),
    ApiCase(
        name="get_confirmed_appointments",
        event="GET_CONFIRMED_APPOINTMENTS",
        call=lambda: booking_api.get_confirmed_appointments(
            GetConfirmedAppointmentsRequest(
                site_id="S1", phone="01000000101", customer_no="C1", verification_code="123456"
            )
        ),
        path="/record/S1/appointment/confirm",
        params={
            "phoneNumber": "01000000101",
            "customerNumber": "C1",
            "verificationCode": "123456",
        },
        payload=[_CONFIRMED_ITEM],
        check=_check_confirmed_appointments,
        http_error_message="RECORD 예약 확정 조회 중 오류가 발생했습니다.",
        unexpected_message="RECORD 예약 확정 조회 중 예상치 못한 오류가 발생했습니다.",
        timeout_message=(
            "RECORD 예약 확정 조회 요청이 시간 초과되었습니다. 잠시 후 다시 시도해주세요."
        ),
        uses_server_message=False,
        client_options={"timeout": 3.0},
    ),
    ApiCase(
        name="get_customer_by_phone",
        event="GET_CUSTOMER_BY_PHONE",
        call=lambda: booking_api.get_customer_by_phone(
            GetCustomerRequest(site_id="S1", phone="01000000101")
        ),
        path="/record/v2/S1/customer",
        params={"guardianFallback": "true", "telNum": "01000000101"},
        payload={
            "resultMsg": "ok",
            "customers": [],
            "responseCode": "200",
            "customerResponseType": "NEW",
        },
        check=_check_customer,
        http_error_message="고객 조회 중 오류가 발생했습니다.",
        unexpected_message="고객 조회 중 예상치 못한 오류가 발생했습니다.",
        timeout_message=(
            "고객 조회 요청이 시간 초과되었습니다. 잠시 후 다시 시도해주세요."
        ),
    ),
]

_IDS = [case.name for case in CASES]


@pytest.fixture
def http(monkeypatch):
    state = SimpleNamespace(
        handler=None,
        requests=[],
        client_options=[],
        logs=[],
    )

    async def fake_create_async_http_client(**options):
        state.client_options.append(options)

        def handler(request: httpx.Request) -> httpx.Response:
            state.requests.append(request)
            return state.handler(request)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def fake_log_api_response(event, response, data=None, *, request_params=None):
        state.logs.append(("response", event, data, request_params))

    def fake_log_api_exception(event, exception, *, request_params=None):
        state.logs.append(("exception", event, type(exception), request_params))

    monkeypatch.setattr(
        booking_api, "create_async_http_client", fake_create_async_http_client
    )
    monkeypatch.setattr(booking_api, "RESERVATION_API_BASEURL", BASE_URL)
    monkeypatch.setattr(booking_api, "log_api_response", fake_log_api_response)
    monkeypatch.setattr(booking_api, "log_api_exception", fake_log_api_exception)
    return state


def _assert_failure(result, message):
    assert result.success is False
    assert result.data is None
    assert result.message == message


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=_IDS)
async def test_success_builds_request_and_result(http, case):
    http.handler = lambda request: httpx.Response(200, json=case.payload)

    result = await case.call()

    assert result.success is True
    case.check(result)
    (request,) = http.requests
    assert request.method == "GET"
    assert str(request.url).split("?")[0] == f"{BASE_URL}{case.path}"
    assert dict(request.url.params) == case.params
    assert request.headers["Content-Type"] == "application/json"
    assert http.client_options == [case.client_options]
    logged = (
        case.payload if case.logged_success_data is None else case.logged_success_data
    )
    assert http.logs == [("response", case.event, logged, case.request_params)]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=_IDS)
async def test_http_error_prefers_server_result_message(http, case):
    body = {"resultMsg": "server says no"}
    http.handler = lambda request: httpx.Response(400, json=body)

    result = await case.call()

    expected = "server says no" if case.uses_server_message else case.http_error_message
    _assert_failure(result, expected)
    assert http.logs == [("response", case.event, body, case.request_params)]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=_IDS)
async def test_http_error_message_key_only_customer_api_reads_it(http, case):
    http.handler = lambda request: httpx.Response(404, json={"message": "missing"})

    result = await case.call()

    expected = (
        "missing" if case.name == "get_customer_by_phone" else case.http_error_message
    )
    _assert_failure(result, expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=_IDS)
async def test_http_error_without_message_uses_default(http, case):
    http.handler = lambda request: httpx.Response(500, json={})

    result = await case.call()

    _assert_failure(result, case.http_error_message)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=_IDS)
async def test_http_error_with_non_json_body_uses_default(http, case):
    http.handler = lambda request: httpx.Response(502, text="bad gateway")

    result = await case.call()

    _assert_failure(result, case.http_error_message)
    assert http.logs == [("response", case.event, None, case.request_params)]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=_IDS)
async def test_http_error_with_list_body_uses_default(http, case):
    http.handler = lambda request: httpx.Response(400, json=["unexpected"])

    result = await case.call()

    _assert_failure(result, case.http_error_message)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=_IDS)
async def test_timeout_maps_to_timeout_or_unexpected_message(http, case):
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    http.handler = handler

    result = await case.call()

    _assert_failure(result, case.timeout_message or case.unexpected_message)
    assert http.logs == [
        ("exception", case.event, httpx.ReadTimeout, case.request_params)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=_IDS)
async def test_unparseable_success_body_is_unexpected_error(http, case):
    http.handler = lambda request: httpx.Response(200, text="not json")

    result = await case.call()

    _assert_failure(result, case.unexpected_message)
    assert [entry[:2] for entry in http.logs] == [("exception", case.event)]
    assert http.logs[0][3] == case.request_params


@pytest.mark.asyncio
async def test_site_schedule_failures_keep_request_params(http):
    case = next(case for case in CASES if case.name == "get_site_available_schedules")
    http.handler = lambda request: httpx.Response(400, json={"resultMsg": "bad"})

    http_error = await case.call()

    def handler(request):
        raise httpx.ConnectError("down", request=request)

    http.handler = handler
    unexpected = await case.call()

    assert http_error.request_params == _SITE_REQUEST_PARAMS
    assert unexpected.request_params == _SITE_REQUEST_PARAMS
    assert unexpected.elapsed_ms is None


@pytest.mark.asyncio
async def test_service_schedules_skip_slots_without_advisor_ids(http):
    payload = {
        "resultMsg": "ok",
        "contents": {
            "schedules": [
                {"time": "2030-01-02 09:00:00"},
                {"time": "2030-01-02 09:30:00", "advisorIds": []},
                {"time": "2030-01-02 10:00:00", "advisorIds": [4]},
            ]
        },
    }
    http.handler = lambda request: httpx.Response(200, json=payload)

    result = await booking_api.get_available_schedules(9, "2030-01-01", "2030-01-31")

    assert result.success is True
    assert [(item.time, item.advisor_id) for item in result.data.schedules] == [
        ("2030-01-02 10:00:00", 4)
    ]
