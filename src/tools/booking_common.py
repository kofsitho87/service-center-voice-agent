"""Steps shared by the booking tools.

Both `create_new_appointment` implementations (the legacy step-by-step flow in
tools/booking_tools.py and the dynamic flow in tools/dynamic_booking_tools.py)
settle the caller's identity the same way before their own booking steps. Each tool keeps its own escalation and exit
routing; these helpers only own the shared steps and their developer events.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, get_args

from livekit.agents import ToolError, llm
from loguru import logger

from data import RunContext_T
from tasks.booking.exit_reasons import exit_event_details
from tasks.dtmf_verification_code_task import GetVerificationCodeDtmfTask
from tasks.verify_customer_identity_task import (
    EscalationReason,
    VerifyCustomerIdentityTask,
)
from tools.booking_exit_router import route_booking_exit
from utils.agent_event import (
    agent_event_extra,
    identity_verify_extra,
)
from utils.i18n import t

# 알 수 없는/누락 사유는 verify_failed(기본 에스컬레이션 멘트)로 폴백한다.
IDENTITY_ESCALATION_REASONS = frozenset(get_args(EscalationReason))

# 신규 고객 확인번호 DTMF 수집 + 확인 시도 횟수 (거부 시 재시도)
MAX_NEW_CUSTOMER_VERIFICATION_CODE_ATTEMPTS = 3

IdentityEscalation = Callable[[RunContext_T, EscalationReason | None], Awaitable[None]]


def task_chat_ctx(chat_ctx: llm.ChatContext, *, max_items: int) -> llm.ChatContext:
    """Recent conversation for a sub-task, without instructions or tool noise."""
    return chat_ctx.copy(
        exclude_instructions=True,
        exclude_function_call=True,
        exclude_empty_message=True,
        exclude_handoff=True,
        exclude_config_update=True,
    ).truncate(max_items=max_items)


async def announce_and_shutdown_call(
    ctx: RunContext_T,
    *,
    instructions: str,
    log_event: str,
    phone: str | None,
) -> None:
    """Speak the final announcement verbatim, then end the call."""
    ctx.session.say(instructions, allow_interruptions=False)

    try:
        await ctx.wait_for_playout()
    except Exception:
        logger.bind(
            event=log_event,
            stage="wait_for_playout_error",
            phone=phone,
            room_name=ctx.userdata.room_name,
        ).error("Error waiting for playout before call shutdown")

    ctx.session.shutdown(drain=True)


async def announce_identity_failure(
    ctx: RunContext_T,
    reason: EscalationReason | None,
) -> str:
    """Speak the identity-failure line and wait for it; return the reason key used."""
    ccd = getattr(ctx.userdata, "call_config_data", None)
    language = getattr(ccd, "language", None)
    sub = reason if reason in IDENTITY_ESCALATION_REASONS else "verify_failed"
    speech_handle = ctx.session.say(
        t(f"verify_customer.escalation.{sub}", language),
        allow_interruptions=False,
    )
    await speech_handle.wait_for_playout()
    return sub


async def route_task_exit(
    ctx: RunContext_T,
    *,
    event_name: str,
    source_action: str,
    result: Any,
    include_route_target: bool = True,
    before_route: Callable[[], None] | None = None,
) -> None:
    """Record a sub-task exit as an EXITED event, then hand it to the router.

    `result` is any task result with `exit_reason`, `exit_reason_code` and
    `route_target`. Restart exits (change_advisor / change_service) pass
    `include_route_target=False`: the router re-invokes the booking tool itself.
    """
    route_target = result.route_target if include_route_target else None
    ctx.session.history.add_message(
        role="developer",
        content="",
        extra=agent_event_extra(
            event_name,
            status="EXITED",
            details=exit_event_details(
                result.exit_reason_code,
                result.exit_reason or "",
                route_target,
            ),
        ),
    )
    if before_route is not None:
        before_route()
    await route_booking_exit(
        ctx,
        source_action=source_action,
        reason_code=result.exit_reason_code,
        detail=result.exit_reason or "",
        route_target=route_target,
    )


async def _collect_new_customer_verification_code(ctx: RunContext_T) -> None:
    """DTMF verification_code collection + confirmation for a caller with no customer record.

    Stores the confirmed verification_code on recipient_data, or raises ToolError after
    every attempt was rejected.
    """
    userdata = ctx.userdata
    for _ in range(MAX_NEW_CUSTOMER_VERIFICATION_CODE_ATTEMPTS):
        verification_code_result = await GetVerificationCodeDtmfTask(
            num_digits=6,
        )

        if not verification_code_result.normalized_date:
            continue

        if verification_code_result.confirmed:
            userdata.recipient_data.verification_code = verification_code_result.normalized_date
            ctx.session.history.add_message(
                role="developer",
                content="",
                extra=identity_verify_extra(
                    action="create",
                    status="SUCCESS",
                    source="new_customer",
                ),
            )
            return

    ctx.session.history.add_message(
        role="developer",
        content="",
        extra=agent_event_extra(
            "BOOKING_CREATE",
            status="ERROR",
            details={"reason": "verification_code_confirmation_rejected"},
        ),
    )
    raise ToolError(
        t(
            "booking.error.verification_code_rejected",
            userdata.call_config_data.language,
        )
    )


async def resolve_create_identity(
    ctx: RunContext_T,
    *,
    escalate: IdentityEscalation,
) -> bool:
    """Settle the caller's identity before a new booking.

    Returns True once recipient_data holds the settled identity. Returns False
    after an escalation, which `escalate` has already announced and routed; the
    tool then returns. Raises ToolError when a new customer rejects every
    verification_code confirmation.
    """
    userdata = ctx.userdata

    if userdata.recipient_data.verification_code:
        # 통화 앞 단계에서 이미 본인확인이 끝나 확인번호를 알고 있는 경우.
        # 여기서 이벤트를 남기지 않으면 IDENTITY_RESOLVED 가 과소 계상되어
        # '본인확인에서 이탈' 이라는 거짓 신호가 된다.
        ctx.session.history.add_message(
            role="developer",
            content="",
            extra=identity_verify_extra(
                action="create", status="SUCCESS", source="known"
            ),
        )
        return True

    verify_task = VerifyCustomerIdentityTask(
        userdata,
        action="create",
        verify_single_customer_identity=(
            userdata.workflow_config.booking.booking_create.verify_single_customer_identity
        ),
        history=ctx.session.history,
    )
    verify_result = await verify_task.run()

    if verify_result.status == "no_customer":
        logger.bind(
            event="CREATE_NEW_APPOINTMENT",
            stage="new_customer_detected",
            phone=userdata.recipient_data.phone,
            room_name=userdata.room_name,
        ).info("No customer found, collecting verification_code for new customer")

        ctx.session.history.add_message(
            role="developer",
            content="",
            extra=agent_event_extra(
                "BOOKING_CREATE",
                status="NEW_CUSTOMER",
                details={"collecting_verification_code": True},
            ),
        )
        await _collect_new_customer_verification_code(ctx)
        return True

    if verify_result.status == "escalated":
        ctx.session.history.add_message(
            role="developer",
            content="",
            extra=identity_verify_extra(
                action="create",
                status="FAILED",
                reason=verify_result.escalation_reason,
            ),
        )
        ctx.session.history.add_message(
            role="developer",
            content="",
            extra=agent_event_extra(
                "BOOKING_CREATE",
                status="ESCALATED",
                details={"reason": verify_result.escalation_reason},
            ),
        )
        await escalate(ctx, verify_result.escalation_reason)
        return False

    # matched → userdata에 이미 세팅됨
    ctx.session.history.add_message(
        role="developer",
        content="",
        extra=identity_verify_extra(
            action="create", status="SUCCESS", source="verified"
        ),
    )
    return True
