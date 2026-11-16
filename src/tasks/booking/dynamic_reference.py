"""Dynamic booking reference data: bookable services and advisors for a call.

Loaded once per call by the caller (load_booking_reference) and injected into
DynamicBookingTask; the task never looks it up itself.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from loguru import logger

from data import CallSessionData
from external_apis.booking_api import (
    get_services,
    get_site_operators,
)
from utils.agent_event import record_api_result
from utils.common import flatten_services


@dataclass(frozen=True)
class BookingReference:
    """사전 조회된 서비스/상담사 참조 데이터 — 태스크 생성 전에 주입한다.

    통화 내에서 변하지 않으므로 호출자가 load_booking_reference()로 한 번
    조회해 캐싱·재사용한다(예: CallSessionData.dynamic_booking_reference).
    """

    services: list[dict[str, Any]]
    advisors: list[dict[str, Any]]


def is_operator_selection_enabled(call_session_data: CallSessionData) -> bool:
    """Return the booking-create operator selection flag with its config default."""
    try:
        return bool(
            call_session_data.workflow_config.booking.booking_create.select_operator
        )
    except AttributeError:
        return False


def advisor_service_ids(advisor: dict[str, Any]) -> set[int]:
    """The appointments a reference advisor covers, as ints."""
    return {int(value) for value in advisor["service_ids"]}


def advisor_is_eligible(
    advisor_id: int,
    advisor: dict[str, Any],
    *,
    service_id: int | None,
    operator_id: int | None,
) -> bool:
    """Whether a listed advisor fits the requested advisor and appointment filters."""
    if operator_id is not None and advisor_id != operator_id:
        return False
    return service_id is None or service_id in advisor_service_ids(advisor)


def filter_bookable_services(
    services: list[dict[str, Any]], advisors: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """커버 상담사 0명인 서비스 제외 — 예약 불가 서비스 원천 차단."""
    covered: set[int] = set()
    for advisor in advisors:
        covered |= advisor_service_ids(advisor)
    return [d for d in services if int(d["id"]) in covered]


def filter_practicing_advisors(
    advisors: list[dict[str, Any]], services: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """예약 가능 서비스를 하나도 커버하지 않는 상담사 제외 — 배정 불가 상담사 원천 차단."""
    service_ids = {int(d["id"]) for d in services}
    return [
        advisor for advisor in advisors if service_ids & advisor_service_ids(advisor)
    ]


def _known_operator_service_ids(operator: Any) -> list[int] | None:
    """서비스 매핑이 알려진 상담사만 통과 — 매핑이 없으면 None."""
    services = getattr(operator, "services", None)
    if not services:
        return None
    return [int(item.serviceId) for item in services]


def _service_display_mode(call_session_data: CallSessionData) -> str:
    try:
        return call_session_data.workflow_config.booking.booking_create.service_selection.display_mode
    except AttributeError:
        return "parent_with_child"


def _service_free_utterance(call_session_data: CallSessionData) -> bool:
    try:
        return bool(
            call_session_data.workflow_config.booking.booking_create.service_selection.free_utterance
        )
    except AttributeError:
        return False


async def load_booking_reference(
    call_session_data: CallSessionData,
    history: Any = None,
) -> BookingReference:
    """서비스·상담사 참조 데이터 조회 — DynamicBookingTask 생성 전에 호출한다.

    통화 내에서 결과가 변하지 않으므로 호출자가 캐싱해 재사용한다.
    조회/응답 실패 시 RuntimeError.
    """
    site_id = call_session_data.business_data.site_id
    client_id = call_session_data.business_data.client_id
    services_response, operators_response = await asyncio.gather(
        get_services(site_id, client_id=client_id),
        get_site_operators(site_id, client_id=client_id),
    )
    record_api_result(history, "GET_SERVICES", services_response)
    record_api_result(history, "GET_SITE_OPERATORS", operators_response)
    if not services_response.success or not services_response.data:
        raise RuntimeError(services_response.message or "service lookup failed")
    if not operators_response.success or operators_response.data is None:
        raise RuntimeError(operators_response.message or "operator lookup failed")
    flattened = flatten_services(
        services_response.data.services,
        display_mode=_service_display_mode(call_session_data),
    )
    services = [
        {
            "id": item["service_id"],
            "name": item["service_name"],
            "keywords": [str(value) for value in item.get("keywords", [])],
        }
        for item in flattened
    ]
    advisors: list[dict[str, Any]] = []
    for operator in operators_response.data:
        service_ids = _known_operator_service_ids(operator)
        if service_ids is None:
            continue  # 서비스 매핑이 없는 상담사는 배정 대상에서 제외
        advisors.append(
            {
                "id": operator.operatorId,
                "name": operator.displayName,
                "service_ids": service_ids,
            }
        )
    advisors = filter_practicing_advisors(advisors, services)
    services = filter_bookable_services(services, advisors)
    logger.bind(
        event="DYNAMIC_BOOKING_TASK",
        stage="static_loaded",
        room_name=call_session_data.room_name,
        services=services,
        advisors=advisors,
    ).debug(
        "Booking reference data loaded: {} services, {} advisors",
        len(services),
        len(advisors),
    )
    return BookingReference(services=services, advisors=advisors)
