"""Dynamic booking value types, tool-argument aliases and result contract."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import wraps
from typing import Annotated, Any, Literal, get_args

from loguru import logger
from pydantic import BeforeValidator
from typing_extensions import TypeAliasType

from tasks.booking.exit_reasons import BookingExitRouteTarget


def _log_tool_io(
    func: Callable[..., Awaitable[Any]],
) -> Callable[..., Awaitable[Any]]:
    """Log tool arguments and every returned output without changing its schema."""
    signature = inspect.signature(func)

    @wraps(func)
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        bound = signature.bind(self, *args, **kwargs)
        bound.apply_defaults()
        arguments = {
            name: value
            for name, value in bound.arguments.items()
            if name not in {"self", "ctx"}
        }
        tool_logger = logger.bind(
            event="DYNAMIC_BOOKING_TASK",
            room_name=self._call_session_data.room_name,
            tool_name=func.__name__,
        )
        tool_logger.bind(arguments=arguments).debug(
            "Tool {} called: arguments={}", func.__name__, arguments
        )
        output = await func(self, *args, **kwargs)
        tool_logger.bind(output=output).debug(
            "Tool {} returned: output={}", func.__name__, output
        )
        return output

    return wrapper


def _normalize_nullable_tool_value(value: Any) -> Any:
    """Normalize only null sentinels before normal enum/bool/int validation."""
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized or normalized.lower() in {"none", "null"}:
            return None
    return value


def normalize_optional_tool_text(value: str | None) -> str | None:
    """Treat common LLM null sentinels as omitted optional tool arguments.

    Same sentinels as _normalize_nullable_tool_value, which must return the
    original value for validation; free text is returned stripped instead.
    """
    if value is None:
        return None
    text = _normalize_nullable_tool_value(str(value))
    return None if text is None else text.strip()


# Named aliases preserve the validator through LiveKit's tool-model builder,
# which strips metadata from a top-level Annotated unless it belongs to Field.
NullableToolBool = TypeAliasType(
    "NullableToolBool",
    Annotated[bool | None, BeforeValidator(_normalize_nullable_tool_value)],
)
NullableToolInt = TypeAliasType(
    "NullableToolInt",
    Annotated[int | None, BeforeValidator(_normalize_nullable_tool_value)],
)
NullableToolStr = TypeAliasType(
    "NullableToolStr",
    Annotated[str | None, BeforeValidator(_normalize_nullable_tool_value)],
)
# Keep tolerated model echoes/aliases from the shared tool input while excluding
# info_request: this task now answers center information without exiting.
DynamicBookingRouteTargetInput = Literal[
    "booking_inquiry",
    "booking_modify",
    "booking_cancel",
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
NullableBookingRouteTarget = TypeAliasType(
    "NullableBookingRouteTarget",
    Annotated[
        DynamicBookingRouteTargetInput | None,
        BeforeValidator(_normalize_nullable_tool_value),
    ],
)


@dataclass(frozen=True)
class StagedBooking:
    service_id: int
    service_name: str
    operator_id: int
    operator_name: str
    datetime: str  # "YYYY-MM-DD HH:MM"


@dataclass(frozen=True)
class EarliestOffer:
    """Search-grounded candidate that may use the earliest-offer confirmation."""

    service_id: int
    operator_id: int | None
    datetime: str


BookingOutcome = Literal["completed", "exited"]
# exit_task 로 LLM 이 선택할 수 있는 사유. no_progress 는 여기 없다 —
# 가드 전용 사유를 툴 스키마에 노출하면 LLM 이 스스로 통화 종료 경로를
# 밟는 길이 생긴다(정규화 화이트리스트에도 없어 전달해도 other 가 된다).
DynamicBookingToolExitReasonCode = Literal[
    "no_slot_memo",
    "staff_connection",
    "service_not_offered",
    "cancel",
    "unrelated",
    "other",
]
# 태스크 결과의 사유. no_progress 는 무진행 가드만 만들 수 있으며, 호출자는
# 이 사유를 받으면 상위 에이전트로 돌아가지 않고 안내 후 통화를 종료한다.
DynamicBookingExitReasonCode = Literal[
    "no_slot_memo",
    "staff_connection",
    "service_not_offered",
    "cancel",
    "unrelated",
    "other",
    "no_progress",
]
DYNAMIC_BOOKING_EXIT_REASON_CODES = frozenset(get_args(DynamicBookingExitReasonCode))


@dataclass(frozen=True)
class DynamicBookingResult:
    outcome: BookingOutcome
    reservation: StagedBooking | None = None
    exit_reason_code: DynamicBookingExitReasonCode | None = None
    detail: str = ""
    route_target: BookingExitRouteTarget | None = None
    handoff_accepted: bool | None = None
    handoff_channel: str | None = None
    # cancel only: nothing was chosen or verified, so the parent asks what else
    # the caller needs instead of leaving a bare acknowledgement.
    stopped_before_selection: bool = False

    def __post_init__(self) -> None:
        if self.outcome == "completed":
            if self.reservation is None:
                raise ValueError("completed booking result requires a reservation")
            if (
                self.exit_reason_code is not None
                or self.detail
                or self.route_target is not None
            ):
                raise ValueError("completed booking result cannot contain exit data")
            return

        if self.outcome == "exited":
            if self.reservation is not None:
                raise ValueError("exited booking result cannot contain a reservation")
            if self.exit_reason_code is None:
                raise ValueError("exited booking result requires an exit reason code")
            if self.exit_reason_code not in DYNAMIC_BOOKING_EXIT_REASON_CODES:
                raise ValueError(
                    "exited booking result requires a valid exit reason code"
                )
            return

        raise ValueError(f"unsupported booking outcome: {self.outcome}")


class BookingApiError(RuntimeError):
    """조회 하위 호출 실패 — 부분 결과 없이 전체 오류로 승격한다."""


# 종료 멘트는 태스크가 아니라 호출자(셸/툴)가 발화한다.
CANCEL_ANNOUNCEMENT = (
    "네, 알겠습니다. 예약 없이 마치겠습니다. 필요하실 때 다시 전화 주세요. 감사합니다."
)
REFERENCE_LOAD_FAILED_ANNOUNCEMENT = (
    "지금은 예약 접수가 어렵습니다. 잠시 후 다시 시도해 주세요."
)
NO_PROGRESS_ANNOUNCEMENT = (
    "죄송합니다. 말씀을 정확히 확인하기 어려워 통화를 종료합니다. "
    "예약이 필요하시면 다시 전화해 주세요. 감사합니다."
)
