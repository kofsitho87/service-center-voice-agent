"""예약 플로우 하위 Task의 '구조화된 종료 사유'.

서비스/상담사/일정/예약 선택 Task에서 사용자가 선택을 중단하거나 다른 요청을
할 때 호출하는 exit_task 의 사유를 자유 텍스트가 아니라 아래 정의된 코드로만
받도록 제한한다.

자유 텍스트 사유는 LLM 이 실제로 언급되지 않은 내용을 지어내는 문제가 있었다.
대표적으로 docstring 예시였던 "주차장 정보 문의" 문자열이, 사용자가 주차 얘기를
한 적이 없는데도 종료 사유로 그대로 복사되어(예: 목록에 없는 서비스 발화 시)
메인 에이전트가 엉뚱하게 주차장 정보를 조회하는 오작동이 발생했다.

이제 exit_reason 문구는 LLM 이 아니라 아래 코드→문구 매핑에서 파생한다.
"""

from __future__ import annotations

from typing import Literal, cast, get_args

from utils.i18n import t

# exit_task 의 reason_code 인자로 허용되는 값 (function tool 스키마에서 enum 으로 강제됨).
ExitReasonCode = Literal[
    "change_service",  # 다른 서비스로 변경하고 싶어함
    "change_advisor",  # 다른 상담사로 변경하고 싶어함
    "no_slot_memo",  # 조건에 맞는 일정이 없어 담당자 메모 남기기에 동의함
    "staff_connection",  # 상담원(사람) 연결 요청
    "service_not_offered",  # AI 예약 목록 밖 서비스만 원함
    "cancel",  # 예약 진행 중단
    "unrelated",  # 예약과 무관한 요청 (정보 문의 등)
    "other",  # 그 외
]

# `reason_code` remains the analytics-compatible exit category.  A separate
# route target captures the caller's next business intent without asking the
# parent LLM to infer it from `detail`.
BookingExitRouteTarget = Literal[
    "booking_inquiry",
    "booking_modify",
    "booking_cancel",
    "info_request",
    "end_call",
    "other",
]

# Function-tool input is intentionally broader than the internal result type.
# Models sometimes echo the selected reason into route_target. Accept those
# values at schema parsing time, then normalize them to None/"other" below.
# ``transfer`` is another observed semantic alias for ``staff_connection``.
# It must pass schema validation so non-routing exit reasons can discard it in
# ``route_target_for_exit`` instead of failing before the tool body runs.
BookingExitRouteTargetInput = Literal[
    "booking_inquiry",
    "booking_modify",
    "booking_cancel",
    "info_request",
    "end_call",
    "transfer",
    "change_service",
    "change_advisor",
    "no_slot_memo",
    "staff_connection",
    "service_not_offered",
    "cancel",
    "unrelated",
    "other",
]

VALID_BOOKING_EXIT_ROUTE_TARGETS: frozenset[str] = frozenset(
    get_args(BookingExitRouteTarget)
)

# 알 수 없는 코드가 들어오면 안전하게 'other' 로 취급하기 위한 화이트리스트.
VALID_EXIT_REASON_CODES: frozenset[str] = frozenset(get_args(ExitReasonCode))

# 코드 → developer 이벤트/메모에 사용하는 표준 문구는
# registry(exit_reason.<code>)에서 통화 언어에 맞춰 가져온다. 실제 후속 라우팅은
# 이 문구나 detail을 파싱하지 않고 reason_code와 route_target으로 수행한다.


def normalize_exit_reason_code(reason_code: str | None) -> str:
    """허용 코드가 아니면 'other' 로 정규화한다."""
    if reason_code in VALID_EXIT_REASON_CODES:
        return reason_code  # type: ignore[return-value]
    return "other"


def normalize_booking_exit_route_target(
    route_target: str | None,
) -> BookingExitRouteTarget:
    """Normalize an unknown routing target to the safe non-terminal fallback."""
    if route_target in VALID_BOOKING_EXIT_ROUTE_TARGETS:
        return cast(BookingExitRouteTarget, route_target)
    return "other"


def route_target_for_exit(
    reason_code: str | None,
    route_target: str | None,
) -> BookingExitRouteTarget | None:
    """Keep route metadata only for generic analytics exit categories."""
    if reason_code not in VALID_EXIT_REASON_CODES:
        return "other"
    if reason_code not in {"unrelated", "other"}:
        return None
    return normalize_booking_exit_route_target(route_target)


def exit_event_details(
    reason_code: str | None,
    exit_reason_text_value: str,
    route_target: str | None = None,
) -> dict[str, str]:
    """Return structured details for a booking exit developer event."""
    details = {
        "reason": exit_reason_text_value,
        "reason_code": normalize_exit_reason_code(reason_code),
    }
    if route_target is not None:
        normalized_target = route_target_for_exit(reason_code, route_target)
        if normalized_target is not None:
            details["route_target"] = normalized_target
    return details


def exit_reason_text(
    reason_code: str | None, detail: str = "", language: str | None = None
) -> str:
    """구조화된 종료 사유 코드를 통화 언어의 표준 문구로 변환한다.

    detail(사용자가 실제로 말한 내용)이 있으면 괄호로 덧붙인다.
    """
    code = normalize_exit_reason_code(reason_code)
    base = t(f"exit_reason.{code}", language)
    detail = (detail or "").strip()
    if detail:
        return t("exit_reason.with_detail", language, base=base, detail=detail)
    return base
