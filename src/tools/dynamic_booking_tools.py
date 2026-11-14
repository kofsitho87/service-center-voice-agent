"""다이나믹 예약(DynamicBookingTask) 기반 create_new_appointment.

tools/booking_tools.py 의 create_new_appointment(단계별 플로우, 롤백용 보존)와
같은 이름·시그니처로, SingleAgent 에서 import 만 바꾸면 서로 교체된다.

플로우:
1. 고객 조회/검증 (verification_code 없을 때, VerifyCustomerIdentityTask + DTMF)
   — 1단계는 tools/booking_common.py 를 단계별 플로우와 공유한다.
2. 서비스 + 상담사 + 일시 선택 — DynamicBookingTask 하나로 대화형 진행

단계별 플로우의 동일 상담사 제안(same_operator)과 change_advisor/change_service
재시작(restart_hint)은 태스크 내부 대화로 대체되므로 없다.

서비스/상담사 참조 데이터는 load_booking_reference()로 조회 후
CallSessionData.dynamic_booking_reference 에 캐싱한다 — 통화 중 이 툴이
재호출돼도 API 를 다시 조회하지 않는다.
"""

from livekit.agents import ToolError, function_tool, llm
from loguru import logger

from data import CallSessionData, DynamicBookingResumeContext, RunContext_T
from decorators.agent_decorator import action_mode_handler
from tasks.dynamic_booking_task import (
    NO_PROGRESS_ANNOUNCEMENT,
    REFERENCE_LOAD_FAILED_ANNOUNCEMENT,
    DynamicBookingTask,
    load_booking_reference,
)
from tasks.transfer_reason_task import TransferPurpose
from tasks.verify_customer_identity_task import EscalationReason
from tools.booking_common import (
    announce_and_shutdown_call,
    announce_identity_failure,
    resolve_create_identity,
    task_chat_ctx,
)
from tools.booking_exit_router import route_booking_exit
from tools.reservation_submit import submit_current_reservation
from utils.agent_event import agent_event_extra
from utils.booking_resume import booking_preferences_message, latest_user_message_id
from utils.common import normalize_verification_code
from utils.i18n import t
from utils.spoken_datetime import spoken_datetime

# DynamicBookingTask exits that keep the caller's search conditions for a later
# resume in the same call. Every other exit discards them.
_RESUMABLE_EXIT_REASON_CODES = frozenset({"cancel", "unrelated", "other"})


async def _handoff_identity_failure(
    ctx: RunContext_T,
    reason: EscalationReason | None,
) -> None:
    """Announce an identity failure, then route to staff without an LLM turn."""

    def _caller_left() -> bool:
        if not getattr(ctx.userdata, "caller_disconnected", False):
            return False
        logger.bind(
            event="IDENTITY_VERIFY_HANDOFF",
            stage="caller_disconnected",
            reason=reason,
            room_name=ctx.userdata.room_name,
        ).info("Caller disconnected before identity failure handoff")
        return True

    # A closing session rejects say(); checking first keeps the tool from failing.
    if _caller_left():
        return

    await announce_identity_failure(ctx, reason)

    if _caller_left():
        return

    await ctx.session.current_agent._handle_transfer_call(
        ctx.userdata,
        action_key="booking_create",
        transfer_purpose=TransferPurpose(code="identity_verification_failed"),
    )


def _prepare_booking_chat_ctx(
    userdata: CallSessionData, chat_ctx: llm.ChatContext
) -> llm.ChatContext:
    booking_ctx = task_chat_ctx(chat_ctx, max_items=10)
    resume = getattr(userdata, "dynamic_booking_resume", None)
    if resume is not None and resume.paused:
        # Conditions survive truncation, but candidate confirmation never does.
        userdata.dynamic_booking_pending = None
        booking_ctx.add_message(
            role="system", content=booking_preferences_message(resume)
        )
        resume.paused = False
    return booking_ctx


def _ensure_resume_context(userdata: CallSessionData) -> DynamicBookingResumeContext:
    resume = getattr(userdata, "dynamic_booking_resume", None)
    if resume is None:
        resume = DynamicBookingResumeContext()
        userdata.dynamic_booking_resume = resume
    return resume


def _pause_booking_context(
    userdata: CallSessionData, reason_code: str | None, chat_ctx: llm.ChatContext
) -> None:
    if reason_code not in _RESUMABLE_EXIT_REASON_CODES:
        userdata.dynamic_booking_resume = None
        return
    resume = _ensure_resume_context(userdata)
    resume.paused = True
    resume.last_exit_reason = reason_code
    resume.last_handled_user_message_id = latest_user_message_id(chat_ctx)
    if reason_code == "cancel":
        # A confirmed whole-booking stop ends this attempt's handoff history; a
        # later new request may be offered help again.
        resume.handoff_declined_evidence = None
        resume.handoff_offer_delivered = False
        resume.advisor_alternative_presented = False
        resume.declined_alternatives = 0


############ CREATE NEW APPOINTMENT (DYNAMIC) ############
@function_tool()
@action_mode_handler(action_key="booking_create", agent_name="booking_agent")
async def create_new_appointment(ctx: RunContext_T) -> None:
    """
    Create a new inspection or consultation appointment.

    Call this tool immediately when the caller explicitly requests a new
    appointment OR asks which appointments/services are available on a date
    or in a time window, even without selecting a service. General questions
    about offered services or opening hours remain service center information.
    Do not ask for or confirm any booking details before calling it.
    This tool handles every required interaction internally.

    Preamble: None

    Returns:
        None: The tool announces or routes every terminal outcome itself.
    """

    ctx.disallow_interruptions()

    userdata = ctx.userdata
    center_context = getattr(ctx.session.current_agent, "_center_context", "")
    userdata.booking_memo_context = None
    phone = userdata.recipient_data.phone
    site_id = userdata.business_data.site_id

    logger.bind(
        event="CREATE_NEW_APPOINTMENT",
        stage="start",
        variant="dynamic",
        phone=phone,
        room_name=userdata.room_name,
    ).debug("Starting create new appointment (dynamic)")

    ctx.session.history.add_message(
        role="developer",
        content="",
        extra=agent_event_extra("BOOKING_CREATE", status="STARTED"),
    )

    # ── Step 1: 고객 식별 ──
    if not await resolve_create_identity(ctx, escalate=_handoff_identity_failure):
        return None
    # 확정된 본인 정보 (기존/신규/검증 경로 모두 recipient_data에 반영됨)
    verification_code = userdata.recipient_data.verification_code
    phone = userdata.recipient_data.phone
    name = userdata.recipient_data.user_name
    customer_no = userdata.recipient_data.customer_no

    # ── Step 3: 서비스/상담사 참조 데이터 (통화 내 캐싱) ──
    if userdata.dynamic_booking_reference is None:
        try:
            userdata.dynamic_booking_reference = await load_booking_reference(
                userdata,
                history=ctx.session.history,
            )
        except Exception as error:
            logger.bind(
                event="CREATE_NEW_APPOINTMENT",
                stage="reference_load_failed",
                phone=phone,
                room_name=userdata.room_name,
            ).opt(exception=error).error("Failed to load booking reference data")
            ctx.session.history.add_message(
                role="developer",
                content="",
                extra=agent_event_extra(
                    "BOOKING_CREATE",
                    status="ERROR",
                    details={"reason": "reference_load_failed"},
                ),
            )
            raise ToolError(REFERENCE_LOAD_FAILED_ANNOUNCEMENT)

    # ── Step 4: 서비스 + 상담사 + 일시 선택 (다이나믹 태스크) ──
    # ! 오디오 입력 활성화 (태스크 대화 음성 수집)
    ctx.session.input.set_audio_enabled(True)

    result = await DynamicBookingTask(
        call_session_data=userdata,
        reference=userdata.dynamic_booking_reference,
        center_context=center_context,
        chat_ctx=_prepare_booking_chat_ctx(
            userdata, ctx.session.current_agent.chat_ctx
        ),
    )

    # ── Step 5: 결과 라우팅 (완료는 안내 후 종료, 이탈은 SingleAgent로 반환) ──
    # 무진행 가드 종료는 SingleAgent 로 돌려보내지 않는다. 툴 결과로 반환하면
    # 진행이 안 되던 그 통화를 상위 LLM 루프가 그대로 이어받는다 — 태스크가
    # 판정한 사실(대화 무진행)을 코드가 통화 종료로 집행한다.
    if result.outcome == "exited" and result.exit_reason_code == "no_progress":
        userdata.dynamic_booking_resume = None
        logger.bind(
            event="CREATE_NEW_APPOINTMENT",
            stage="no_progress_terminated",
            detail=result.detail,
            phone=phone,
            room_name=userdata.room_name,
        ).warning("No-progress guard terminating the call")
        ctx.session.history.add_message(
            role="developer",
            content="",
            extra=agent_event_extra(
                "BOOKING_CREATE",
                status="EXITED",
                details={"reason_code": "no_progress", "detail": result.detail},
            ),
        )
        ctx.session.history.add_message(
            role="developer",
            content="",
            extra=agent_event_extra(
                "CALL_TERMINATION",
                details={"actor": "SYSTEM", "reason": "NO_PROGRESS"},
            ),
        )
        ctx.session.input.set_audio_enabled(False)
        await announce_and_shutdown_call(
            ctx,
            instructions=NO_PROGRESS_ANNOUNCEMENT,
            log_event="CREATE_NEW_APPOINTMENT",
            phone=phone,
        )
        return None

    if result.outcome == "exited":
        exit_reason_code = result.exit_reason_code
        _pause_booking_context(userdata, exit_reason_code, ctx.session.history)
        logger.bind(
            event="CREATE_NEW_APPOINTMENT",
            stage="exited",
            exit_reason_code=exit_reason_code,
            detail=result.detail,
            phone=phone,
            room_name=userdata.room_name,
        ).info("Customer exited dynamic booking task")
        event_details = {
            "reason_code": exit_reason_code,
            "detail": result.detail,
            "handoff_accepted": result.handoff_accepted,
            "handoff_channel": result.handoff_channel,
        }
        if result.route_target is not None:
            event_details["route_target"] = result.route_target
        ctx.session.history.add_message(
            role="developer",
            content="",
            extra=agent_event_extra(
                "BOOKING_CREATE",
                status="EXITED",
                details=event_details,
            ),
        )
        await route_booking_exit(
            ctx,
            source_action="booking_create",
            reason_code=exit_reason_code,
            detail=result.detail,
            route_target=result.route_target,
            handoff_accepted=result.handoff_accepted,
            handoff_channel=result.handoff_channel,
            stopped_before_selection=result.stopped_before_selection,
        )
        return None

    userdata.dynamic_booking_resume = None
    staged = result.reservation
    if staged is None:
        # 방어: DynamicBookingResult 불변식을 우회한 잘못된 결과.
        ctx.session.history.add_message(
            role="developer",
            content="",
            extra=agent_event_extra(
                "BOOKING_CREATE",
                status="ERROR",
                details={"reason": "empty_task_result"},
            ),
        )
        raise ToolError("Completed booking task ended without a reservation")

    # ── Step 6: 정규화 + reservation_info 병합 ──
    # 기본 필드(type/service_id/operator_id/일시)는 태스크 commit_booking이
    # 이미 채웠다. 여기서는 상위 플로우가 아는 정보만 병합한다.
    try:
        verification_code = normalize_verification_code(verification_code, separator="-")
    except ValueError as e:
        ctx.session.history.add_message(
            role="developer",
            content="",
            extra=agent_event_extra(
                "BOOKING_CREATE",
                status="ERROR",
                details={
                    "service_id": staged.service_id,
                    "reason": "normalization_failed",
                    "detail": str(e),
                },
            ),
        )
        raise ToolError(f"Error occurred while normalizing phone or verification code: {e!s}")

    userdata.reservation_info.update(
        {
            "verification_code": verification_code,
            "customer_lookup_phone_source": bool(
                userdata.reservation_data.get("customer_lookup_phone_source")
            ),
        }
    )

    # ── Step 6b: persist before announcing success ──
    if not (await submit_current_reservation(ctx, "booking_create")).accepted:
        return None

    ctx.session.history.add_message(
        role="developer",
        content="",
        extra=agent_event_extra(
            "BOOKING_CREATE",
            status="SUCCESS",
            details={
                "service_id": staged.service_id,
                "datetime": f"{staged.datetime}:00",
                "customer_no": customer_no,
                "verification_code": verification_code,
                "advisor_id": staged.operator_id,
            },
        ),
    )

    logger.bind(
        event="CREATE_NEW_APPOINTMENT",
        stage="success",
        variant="dynamic",
        site_id=site_id,
        service_id=staged.service_id,
        advisor_id=staged.operator_id,
        verification_code=verification_code,
        phone=phone,
        appointment_datetime=staged.datetime,
        name=name,
        customer_no=customer_no,
        reservation_info=userdata.reservation_info,
        room_name=userdata.room_name,
    ).info("Appointment created successfully")

    spoken_reservation_request_date_time = spoken_datetime(
        f"{staged.datetime}:00",
        ctx.userdata.call_config_data.language,
    )
    success_instructions = t(
        "booking.create.success",
        ctx.userdata.call_config_data.language,
        datetime=spoken_reservation_request_date_time,
        service=staged.service_name,
    )

    await announce_and_shutdown_call(
        ctx,
        instructions=success_instructions,
        log_event="CREATE_NEW_APPOINTMENT",
        phone=phone,
    )
    return None
