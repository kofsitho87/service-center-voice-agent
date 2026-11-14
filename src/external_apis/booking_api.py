import os
from collections.abc import Callable
from typing import Any, Literal

import httpx
from loguru import logger

from fake_backend.config import FAKE_RESERVATION_BASEURL
from utils.http_client import create_async_http_client

from .api_logging import log_api_exception, log_api_response, response_elapsed_ms
from .types.request_types import (
    GetConfirmedAppointmentsRequest,
    GetCustomerRequest,
)
from .types.response_types import (
    AvailableSchedulesAPIResponse,
    ConfirmedAppointmentsAPIResponse,
    CustomerAPIResponse,
    CustomerContents,
    OperatorAPIResponse,
    OperatorAvailableTimesAPIResponse,
    ReservationSubmitAPIResponse,
    ReservationSubmitContents,
    ServiceAPIResponse,
    SiteAvailableSchedulesAPIResponse,
)


def _normalize_api_response(raw: dict[str, Any]) -> dict[str, Any]:
    """Coerce heterogeneous API payloads into the structure APIResponse expects."""

    normalized: dict[str, Any] = {}

    success = raw.get("success")
    if success is None:
        result_code = raw.get("resultCode")
        if isinstance(result_code, str):
            success = result_code.startswith("2")
        elif isinstance(result_code, int):
            success = 200 <= result_code < 300
        else:
            success = True

    normalized["success"] = bool(success)
    normalized["resultMsg"] = raw.get("resultMsg") or raw.get("message")
    normalized["contents"] = raw.get("contents", raw.get("data"))

    return normalized


# Defaults to the in-process fake backend; other hosts are blocked.
RESERVATION_API_BASEURL = os.getenv("RESERVATION_API_BASEURL", FAKE_RESERVATION_BASEURL)

ServiceReservationSetting = Literal[
    "ALLOW_ONLY_NEW",
    "ALLOW_ONLY_REVISIT",
]

# 특정 고객사(client_id)는 기본 RESERVATION_API_BASEURL 대신 별도의 예약 API 서버를 사용한다.
_CLIENT_BASEURL_OVERRIDES: dict[int, str] = {}  # no overrides configured


def _resolve_base_url(client_id: int | None = None) -> str | None:
    """client_id에 매핑된 전용 base URL이 있으면 반환하고, 없으면 기본값을 사용한다."""
    if client_id is not None and client_id in _CLIENT_BASEURL_OVERRIDES:
        return _CLIENT_BASEURL_OVERRIDES[client_id]
    return RESERVATION_API_BASEURL


def _log_http_error(
    event: str,
    response: httpx.Response,
    *,
    request_params: dict[str, Any] | None = None,
) -> Any:
    try:
        data = response.json()
    except Exception:
        data = None
    log_api_response(event, response, data, request_params=request_params)
    return data


def _server_result_message(data: Any) -> str | None:
    return data.get("resultMsg")


async def _get_reservation_api(
    event: str,
    url: str,
    response_model: Any,
    *,
    http_error_message: str,
    unexpected_message: str,
    params: dict[str, Any] | None = None,
    build: Callable[[Any, httpx.Response], Any] | None = None,
    server_message: Callable[[Any], str | None] | None = _server_result_message,
    timeout_message: str | None = None,
    client_options: dict[str, Any] | None = None,
    request_params: dict[str, Any] | None = None,
    failure_fields: Callable[[httpx.Response | None], dict[str, Any]] | None = None,
) -> Any:
    """GET a reservation API endpoint and map every failure to `success=False`.

    - HTTP 4xx/5xx: the server `resultMsg` (via `server_message`) when present,
      otherwise `http_error_message`. `server_message=None` always uses the
      default message.
    - Timeout: `timeout_message` when given, otherwise handled as unexpected.
    - Any other error (transport, JSON, validation): `unexpected_message`.
    """
    response: httpx.Response | None = None

    def failure(message: str) -> Any:
        extra = failure_fields(response) if failure_fields is not None else {}
        return response_model(
            resultMsg=message,
            contents=None,
            success=False,
            **extra,
        )

    try:
        async with await create_async_http_client(**(client_options or {})) as client:
            response = await client.get(
                url,
                headers={"Content-Type": "application/json"},
                params=params,
            )

            response.raise_for_status()
            data = response.json()
            if build is None:
                result = response_model(**data, success=True)
            else:
                result = build(data, response)
            log_api_response(event, response, data, request_params=request_params)
            return result

    except httpx.HTTPStatusError:
        # HTTP 4xx/5xx 에러 - 서버에서 반환한 에러 메시지 사용
        data = _log_http_error(event, response, request_params=request_params)
        message = http_error_message
        if server_message is not None:
            try:
                message = server_message(data) or http_error_message
            except Exception:
                message = http_error_message
        return failure(message)

    except Exception as e:
        log_api_exception(event, e, request_params=request_params)
        if timeout_message is not None and isinstance(e, httpx.TimeoutException):
            return failure(timeout_message)
        return failure(unexpected_message)


async def get_operators(
    service_id: int, client_id: int | None = None
) -> OperatorAPIResponse:
    """
    Get operator list.
    """
    return await _get_reservation_api(
        "GET_OPERATORS",
        f"{_resolve_base_url(client_id)}/reservation/{service_id}",
        OperatorAPIResponse,
        http_error_message="상담사 조회 중 오류가 발생했습니다.",
        unexpected_message="예약 조회 중 예상치 못한 오류가 발생했습니다.",
    )


async def get_site_operators(
    site_id: str | int,
    service_reservation_setting: ServiceReservationSetting,
    client_id: int | None = None,
) -> OperatorAPIResponse:
    """사이트에 등록된 상담사 목록을 조회합니다."""
    return await _get_reservation_api(
        "GET_SITE_OPERATORS",
        f"{_resolve_base_url(client_id)}/reservation/{site_id}/operators",
        OperatorAPIResponse,
        params={"serviceReservationSetting": service_reservation_setting},
        http_error_message="상담사 조회 중 오류가 발생했습니다.",
        unexpected_message="상담사 조회 중 예상치 못한 오류가 발생했습니다.",
    )


async def get_operator_available_times(
    operator_id: int,
    start_date: str,
    end_date: str,
    client_id: int | None = None,
) -> OperatorAvailableTimesAPIResponse:
    """
    상담사 예약 가능 시간을 조회합니다.

    Args:
        operator_id: 상담사 ID
        start_date: 시작 날짜 (YYYY-MM-DD)
        end_date: 종료 날짜 (YYYY-MM-DD)

    Returns:
        상담사 예약 가능 시간 API 응답
    """
    return await _get_reservation_api(
        "GET_OPERATOR_AVAILABLE_TIMES",
        f"{_resolve_base_url(client_id)}/reservation/operators/{operator_id}/schedules/available",
        OperatorAvailableTimesAPIResponse,
        params={
            "startDate": start_date,
            "endDate": end_date,
        },
        http_error_message="상담사 예약 가능 시간 조회 중 오류가 발생했습니다.",
        unexpected_message=(
            "상담사 예약 가능 시간 조회 중 예상치 못한 오류가 발생했습니다."
        ),
    )


async def get_site_available_schedules(
    site_id: str,
    start_date: str,
    end_date: str,
    service_reservation_setting: ServiceReservationSetting,
    schedule_type: Literal["DATE", "DATE_TIME"] = "DATE",
    size: int = 10,
    operator_id: int | None = None,
    service_id: int | None = None,
    client_id: int | None = None,
) -> SiteAvailableSchedulesAPIResponse:
    """사이트 기준 예약 가능 일정을 조회합니다."""
    request_params = {
        "site_id": site_id,
        "start_date": start_date,
        "end_date": end_date,
        "service_reservation_setting": service_reservation_setting,
        "schedule_type": schedule_type,
        "size": size,
        "operator_id": operator_id,
        "service_id": service_id,
        "client_id": client_id,
    }
    params = {
        "startDate": start_date,
        "endDate": end_date,
        "type": schedule_type,
        "size": size,
        "serviceReservationSetting": service_reservation_setting,
    }
    if operator_id is not None:
        params["operatorId"] = operator_id
    if service_id is not None:
        params["serviceId"] = service_id

    def build(data: Any, response: httpx.Response) -> SiteAvailableSchedulesAPIResponse:
        return SiteAvailableSchedulesAPIResponse(
            **data,
            success=True,
            elapsed_ms=response_elapsed_ms(response),
            request_params=request_params,
        )

    def failure_fields(response: httpx.Response | None) -> dict[str, Any]:
        return {
            "elapsed_ms": (
                response_elapsed_ms(response) if response is not None else None
            ),
            "request_params": request_params,
        }

    return await _get_reservation_api(
        "GET_SITE_AVAILABLE_SCHEDULES",
        f"{_resolve_base_url(client_id)}/reservation/{site_id}/schedules/available",
        SiteAvailableSchedulesAPIResponse,
        params=params,
        build=build,
        http_error_message="사이트 예약 가능 일정 조회 중 오류가 발생했습니다.",
        unexpected_message="사이트 예약 가능 일정 조회 중 예상치 못한 오류가 발생했습니다.",
        request_params=request_params,
        failure_fields=failure_fields,
    )


async def get_services(
    site_id: str,
    service_reservation_setting: ServiceReservationSetting,
    client_id: int | None = None,
) -> ServiceAPIResponse:
    """
    Get service list.
    """
    return await _get_reservation_api(
        "GET_SERVICES",
        f"{_resolve_base_url(client_id)}/reservation/{site_id}/services",
        ServiceAPIResponse,
        # ALLOW_ONLY_NEW, ALLOW_ONLY_REVISIT
        params={"serviceReservationSetting": service_reservation_setting},
        http_error_message="서비스 조회 중 오류가 발생했습니다.",
        unexpected_message="예약 조회 중 예상치 못한 오류가 발생했습니다.",
    )


def _build_available_schedule_params(
    start_date: str,
    end_date: str,
    operator_id: int | None = None,
) -> dict[str, str | int]:
    params: dict[str, str | int] = {
        "startDate": start_date,
        "endDate": end_date,
    }

    if operator_id is not None:
        params["operatorId"] = operator_id

    return params


def _select_schedule_advisor_id(
    advisor_ids: list[int],
    operator_id: int | None = None,
) -> int:
    if operator_id is not None and operator_id in advisor_ids:
        return operator_id

    return advisor_ids[0]


def _map_service_schedules(
    raw_data: dict[str, Any], operator_id: int | None
) -> None:
    """Map each slot's `advisorIds` to one `advisor_id` in place.

    A slot without any advisor id cannot be booked, so it is skipped instead of
    failing the whole lookup.
    """
    raw_data["contents"] = raw_data.get("contents", {})
    schedules: list[dict[str, Any]] = []
    for item in raw_data["contents"].get("schedules", []):
        advisor_ids = item.get("advisorIds")
        if not advisor_ids:
            logger.bind(
                event="GET_AVAILABLE_SCHEDULES",
                stage="schedule_without_advisor_ids",
                time=item.get("time"),
            ).warning("Skipping available schedule without advisor ids")
            continue
        schedules.append(
            {
                "time": item.get("time"),
                "advisor_id": _select_schedule_advisor_id(advisor_ids, operator_id),
            }
        )
    raw_data["contents"]["schedules"] = schedules


async def get_available_schedules(
    service_id: int,
    start_date: str,
    end_date: str,
    operator_id: int | None = None,
    client_id: int | None = None,
) -> AvailableSchedulesAPIResponse:
    """
    Get available appointment schedules for a service.

    Args:
        service_id: The service ID to query schedules for.
        start_date: Start date in YYYY-MM-DD format.
        end_date: End date in YYYY-MM-DD format.
        operator_id: Optional operator ID to filter schedules.

    Returns:
        AvailableSchedulesAPIResponse with list of datetime strings in "YYYY-MM-DD HH:MM:SS" format.
    """

    def build(
        raw_data: Any, _response: httpx.Response
    ) -> AvailableSchedulesAPIResponse:
        # The mapped payload is also what gets logged.
        _map_service_schedules(raw_data, operator_id)
        return AvailableSchedulesAPIResponse(**raw_data, success=True)

    return await _get_reservation_api(
        "GET_AVAILABLE_SCHEDULES",
        f"{_resolve_base_url(client_id)}/reservation/services/{service_id}/schedules/available",
        AvailableSchedulesAPIResponse,
        params=_build_available_schedule_params(start_date, end_date, operator_id),
        build=build,
        http_error_message="예약 가능 일정 조회 중 오류가 발생했습니다.",
        unexpected_message="예약 가능 일정 조회 중 예상치 못한 오류가 발생했습니다.",
    )


async def get_confirmed_appointments(
    request: GetConfirmedAppointmentsRequest,
    client_id: int | None = None,
) -> ConfirmedAppointmentsAPIResponse:
    """
    RECORD 예약 확정 예약을 조회합니다.
    - customerNumber가 있으면 기존 고객 조회 (customerNumber + phoneNumber)
    - customerNumber가 없으면 신규 고객 조회 (phoneNumber + verificationCode)

    Args:
        request: GetConfirmedAppointmentsRequest containing site_id, phone, customer_no, verification_code

    Returns:
        RECORD 예약 확정 예약 API 응답
    """
    params = {
        "phoneNumber": request.phone,
    }

    if request.customer_no:
        params["customerNumber"] = request.customer_no

    if request.verification_code:
        params["verificationCode"] = request.verification_code

    return await _get_reservation_api(
        "GET_CONFIRMED_APPOINTMENTS",
        f"{_resolve_base_url(client_id)}/record/{request.site_id}/appointment/confirm",
        ConfirmedAppointmentsAPIResponse,
        params=params,
        client_options={"timeout": 3.0},
        build=lambda data, _response: ConfirmedAppointmentsAPIResponse(
            resultMsg=None,
            contents=data,
            success=True,
        ),
        # This endpoint never surfaces the server message.
        server_message=None,
        http_error_message="RECORD 예약 확정 조회 중 오류가 발생했습니다.",
        timeout_message=(
            "RECORD 예약 확정 조회 요청이 시간 초과되었습니다. 잠시 후 다시 시도해주세요."
        ),
        unexpected_message="RECORD 예약 확정 조회 중 예상치 못한 오류가 발생했습니다.",
    )


def _build_customer_response(data: Any, _response: httpx.Response) -> CustomerAPIResponse:
    # API 응답을 CustomerContents로 변환
    normalized = _normalize_api_response(data)
    # contents 필드에 CustomerContents 객체 생성
    normalized["contents"] = CustomerContents(**data)
    return CustomerAPIResponse(**normalized)


async def get_customer_by_phone(
    request: GetCustomerRequest,
    client_id: int | None = None,
) -> CustomerAPIResponse:
    """
    전화번호로 고객 정보를 조회합니다.

    Args:
        request: GetCustomerRequest containing site_id and phone

    Returns:
        고객 정보 API 응답
    """
    return await _get_reservation_api(
        "GET_CUSTOMER_BY_PHONE",
        f"{_resolve_base_url(client_id)}/record/v2/{request.site_id}/customer",
        CustomerAPIResponse,
        params={
            "guardianFallback": "true",
            "telNum": request.phone,
        },
        build=_build_customer_response,
        server_message=lambda data: _normalize_api_response(data).get("resultMsg"),
        http_error_message="고객 조회 중 오류가 발생했습니다.",
        timeout_message=(
            "고객 조회 요청이 시간 초과되었습니다. 잠시 후 다시 시도해주세요."
        ),
        unexpected_message="고객 조회 중 예상치 못한 오류가 발생했습니다.",
    )


async def submit_reservation(
    site_id: str,
    reservation_info: dict[str, Any],
    *,
    customer: dict[str, Any],
    idempotency_key: str,
    client_id: int | None = None,
) -> ReservationSubmitAPIResponse:
    """Submit one NEW/MODIFY/CANCEL reservation request. Never raises.

    The reservation is persisted at the tool completion point so
    capacity, ownership and replay are enforced before success is announced.
    The same idempotency key replays the prior result instead of writing twice.
    """
    event = "SUBMIT_RESERVATION"
    request_params = {
        "site_id": site_id,
        "type": reservation_info.get("type"),
        "idempotency_key": idempotency_key,
    }
    payload = {**reservation_info, "customer": customer}
    response: httpx.Response | None = None

    def failure(status: str, message: str) -> ReservationSubmitAPIResponse:
        return ReservationSubmitAPIResponse(
            success=False,
            resultMsg=message,
            contents=ReservationSubmitContents(status=status),
        )

    try:
        async with await create_async_http_client(timeout=5.0) as client:
            response = await client.post(
                f"{_resolve_base_url(client_id)}/reservation/{site_id}/requests",
                headers={"Idempotency-Key": idempotency_key},
                json=payload,
            )
        data = response.json()
        log_api_response(event, response, data, request_params=request_params)
        result = ReservationSubmitAPIResponse(**data)
        if result.data is None:
            return failure("ERROR", result.message or "예약 저장 응답이 비어 있습니다.")
        return result
    except Exception as e:
        log_api_exception(event, e, request_params=request_params)
        if isinstance(e, httpx.TimeoutException):
            return failure("ERROR", "예약 저장 요청이 시간 초과되었습니다.")
        return failure("ERROR", "예약 저장 중 예상치 못한 오류가 발생했습니다.")
