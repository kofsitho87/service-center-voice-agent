from typing import Any, Literal

from livekit.agents import (
    function_tool,
)
from loguru import logger

from data import BookingMemoContext, RunContext_T
from decorators.agent_decorator import action_mode_handler
from tasks.booking.get_my_reservations_task import (
    GetMyReservationsResult,
    GetMyReservationsTask,
)
from tasks.booking.select_reservation_task import SelectReservationTask
from tasks.operator_schedule_selection_task import OperatorScheduleSelectionTask
from tasks.transfer_reason_task import TransferPurpose
from tasks.verify_customer_identity_task import EscalationReason

# The `_announce_and_shutdown_call` / `_format_spoken_datetime` names stay
# importable from this module.
from tools.booking_common import (
    announce_and_shutdown_call as _announce_and_shutdown_call,
)
from tools.booking_common import (
    announce_identity_failure,
    route_task_exit,
    task_chat_ctx,
)
from tools.reservation_submit import submit_current_reservation
from utils.agent_event import (
    agent_event_extra,
    identity_verify_extra,
)
from utils.common import normalize_verification_code
from utils.i18n import t
from utils.spoken_datetime import spoken_datetime as _format_spoken_datetime
from utils.tool_responses import NO_APPOINTMENT_INFO

RESERVATION_FAILURE_NO_RESERVATIONS = "no_reservations"
RESERVATION_FAILURE_OPERATOR_MISSING = "reservation_operator_missing"
RESERVATION_FAILURE_OPERATOR_SCHEDULE_LOOKUP = "operator_schedule_lookup_failed"

_OPERATOR_NOT_FOUND_MESSAGES = frozenset(
    {
        "해당 상담사가 존재하지 않습니다",
        "operator does not exist",
        "advisor does not exist",
        "provider does not exist",
    }
)


async def _escalate_to_human(
    ctx: RunContext_T,
    reason: EscalationReason | None,
) -> None:
    """Announce the identity failure and pass its purpose to the transfer path."""
    # A closing session rejects say(); checking first keeps the tool from failing.
    if getattr(ctx.userdata, "caller_disconnected", False):
        return
    await announce_identity_failure(ctx, reason)
    if getattr(ctx.userdata, "caller_disconnected", False):
        return
    await ctx.session.current_agent._handle_transfer_call(
        ctx.userdata,
        transfer_purpose=TransferPurpose(code="identity_verification_failed"),
    )


def _record_identity_outcome(
    ctx: RunContext_T,
    *,
    action: str,
    result: GetMyReservationsResult,
) -> None:
    """GetMyReservationsTask 결과에서 본인확인 성패를 이벤트로 남긴다.

    Task 는 session 을 들고 있지 않아 tool 쪽에서 기록한다. 본인확인 미완료와
    에스컬레이션은 명시적 result status로 실패를 남기고, 성공은 Task가
    userdata에 확정한 확인번호를 기준으로 기록한다.
    """
    if result.status in {"identity_incomplete", "escalated"}:
        ctx.session.history.add_message(
            role="developer",
            content="",
            extra=identity_verify_extra(
                action=action,
                status="FAILED",
                reason=result.reason,
            ),
        )
        return

    recipient = getattr(ctx.userdata, "recipient_data", None)
    if getattr(recipient, "verification_code", None):
        ctx.session.history.add_message(
            role="developer",
            content="",
            extra=identity_verify_extra(
                action=action,
                status="SUCCESS",
                source=result.identity_source or "verified",
            ),
        )


def _record_reservation_failure(
    ctx: RunContext_T, event_name: str, reason: str
) -> None:
    """Record a reservation handling failure before closing or handing off."""
    ctx.session.history.add_message(
        role="developer",
        content="",
        extra=agent_event_extra(
            event_name,
            status="FAILED",
            details={"reason": reason},
        ),
    )


async def _finish_incomplete_reservation_lookup(
    ctx: RunContext_T,
    *,
    event_name: str,
    result: GetMyReservationsResult,
) -> bool:
    """Close a reservation lookup attempt that did not reach the RECORD response.

    Returning True tells the tool caller to stop. Only `completed` may continue
    into the empty/non-empty reservation handling.
    """
    if result.status == "completed":
        return False

    terminal_status = {
        "identity_incomplete": "EXITED",
        "escalated": "ESCALATED",
        "error": "ERROR",
    }[result.status]
    ctx.session.history.add_message(
        role="developer",
        content="",
        extra=agent_event_extra(
            event_name,
            status=terminal_status,
            details={"reason": result.reason},
        ),
    )

    if result.status == "escalated" and not getattr(
        ctx.userdata, "caller_disconnected", False
    ):
        await _escalate_to_human(ctx, result.escalation_reason)

    return True


def _handoff_channel(ctx: RunContext_T) -> str:
    """Return the configured staff-handoff channel for the current call."""
    call_config = ctx.userdata.call_config_data
    if call_config.transfer_mode == "leave_memo" or not call_config.transfer_to:
        return "memo"
    return "transfer"


async def _handoff_no_reservations(
    ctx: RunContext_T, *, action: Literal["modify", "cancel"]
) -> None:
    """Route an empty change/cancel lookup to configured staff transfer or memo."""
    userdata = ctx.userdata
    language = userdata.call_config_data.language
    action_key = f"booking_{action}"
    event_name = f"BOOKING_{action.upper()}"
    action_label = "변경" if action == "modify" else "취소"
    english_action = "change" if action == "modify" else "cancel"
    detail = (
        f"예약 {action_label} 요청, 등록된 예약 내역 조회 결과 없음"
        if language == "ko-KR"
        else f"Request to {english_action} appointment; no registered appointments found"
    )
    _record_reservation_failure(ctx, event_name, RESERVATION_FAILURE_NO_RESERVATIONS)
    userdata.booking_memo_context = BookingMemoContext(
        detail=detail,
        exit_reason_code=RESERVATION_FAILURE_NO_RESERVATIONS,
    )
    if getattr(userdata, "caller_disconnected", False):
        return
    if _handoff_channel(ctx) == "transfer":
        speech_handle = ctx.session.say(
            t(f"booking.{action}.no_reservations_handoff.transfer", language),
            allow_interruptions=False,
        )
        await speech_handle.wait_for_playout()
        if getattr(userdata, "caller_disconnected", False):
            return
    # The memo task owns its opening so the message request is spoken once.
    await ctx.session.current_agent._handle_transfer_call(
        userdata,
        action_key=action_key,
        transfer_purpose=TransferPurpose(
            code=RESERVATION_FAILURE_NO_RESERVATIONS,
            detail=detail,
        ),
    )


def _selected_reservation_detail(
    selected_reservation: dict[str, Any],
    *,
    action_key: str,
    language: str | None,
    ko_suffix: str,
    en_suffix: str,
) -> str:
    """Staff-facing summary of a change/cancel request for one reservation."""
    action = "change" if action_key == "booking_modify" else "cancel"
    raw_datetime = selected_reservation.get("appointment_date")
    spoken_datetime = (
        _format_spoken_datetime(raw_datetime, language) if raw_datetime else None
    )
    if language == "ko-KR":
        action_label = "변경" if action == "change" else "취소"
        detail = (
            f"{spoken_datetime} 예약 {action_label} 요청"
            if spoken_datetime
            else f"예약 {action_label} 요청"
        )
        return detail + ko_suffix
    detail = f"Request to {action} appointment"
    if spoken_datetime:
        detail += f" on {spoken_datetime}"
    return detail + en_suffix


async def _handoff_selected_reservation(
    ctx: RunContext_T,
    *,
    action_key: str,
    event_name: str,
    selected_reservation: dict[str, Any],
    reason_code: str,
    operator_name: str | None,
    ko_suffix: str,
    en_suffix: str,
    say_key: str,
    log_stage: str,
    log_message: str,
) -> None:
    """Preserve the failure evidence and hand one reservation to staff.

    The failure event and memo context are always recorded; the announcement
    and transfer are skipped once the caller has hung up, so no new activity is
    started on a closing session.
    """
    userdata = ctx.userdata
    language = userdata.call_config_data.language
    channel = _handoff_channel(ctx)
    detail = _selected_reservation_detail(
        selected_reservation,
        action_key=action_key,
        language=language,
        ko_suffix=ko_suffix,
        en_suffix=en_suffix,
    )

    logger.bind(
        event=event_name,
        stage=log_stage,
        room_name=getattr(userdata, "room_name", None),
        reservation_id=selected_reservation.get("reservation_id"),
        handoff_channel=channel,
    ).error(log_message)
    _record_reservation_failure(ctx, event_name, reason_code)
    userdata.booking_memo_context = BookingMemoContext(
        service_name=selected_reservation.get("service_name"),
        operator_name=operator_name,
        time_condition=None,
        searched_ranges=[],
        detail=detail,
        exit_reason_code=reason_code,
    )
    if getattr(userdata, "caller_disconnected", False):
        return
    speech_handle = ctx.session.say(
        t(f"{say_key}.{channel}", language),
        allow_interruptions=False,
    )
    await speech_handle.wait_for_playout()
    if getattr(userdata, "caller_disconnected", False):
        return
    await ctx.session.current_agent._handle_transfer_call(
        userdata,
        action_key=action_key,
        transfer_purpose=TransferPurpose(code=reason_code, detail=detail),
    )


async def _handoff_operator_missing(
    ctx: RunContext_T,
    *,
    action_key: str,
    event_name: str,
    selected_reservation: dict[str, Any],
) -> None:
    """Preserve RECORD-data evidence and hand the request to configured staff flow."""
    await _handoff_selected_reservation(
        ctx,
        action_key=action_key,
        event_name=event_name,
        selected_reservation=selected_reservation,
        reason_code=RESERVATION_FAILURE_OPERATOR_MISSING,
        operator_name=None,
        ko_suffix=", 상담사 정보 없음",
        en_suffix=", advisor information missing",
        say_key=f"booking.{action_key.split('_')[1]}.no_operator_handoff",
        log_stage="missing_operator_id_handoff",
        log_message="No operator_id found in selected reservation; handing off",
    )


async def _handoff_operator_schedule_lookup_failed(
    ctx: RunContext_T,
    *,
    selected_reservation: dict[str, Any],
) -> None:
    """Hand a modify request to staff when provider availability lookup fails."""
    await _handoff_selected_reservation(
        ctx,
        action_key="booking_modify",
        event_name="BOOKING_MODIFY",
        selected_reservation=selected_reservation,
        reason_code=RESERVATION_FAILURE_OPERATOR_SCHEDULE_LOOKUP,
        operator_name=selected_reservation.get("operator_name"),
        ko_suffix=", 상담사 일정 조회 실패",
        en_suffix=", advisor availability lookup failed",
        say_key="booking.modify.schedule_lookup_failed_handoff",
        log_stage="operator_schedule_lookup_failed_handoff",
        log_message="Operator availability lookup failed; handing off",
    )


def _is_operator_not_found_response(message: str | None) -> bool:
    normalized = (message or "").strip().rstrip(".。").casefold()
    return normalized in _OPERATOR_NOT_FOUND_MESSAGES


@function_tool()
@action_mode_handler(action_key="booking_inquiry", agent_name="booking_agent")
async def get_my_appointment_list(ctx: RunContext_T) -> list[str] | str:
    """
    Retrieve the caller's existing appointment history.

    Call this tool immediately when the caller explicitly asks for details of
    their own existing appointment. Do not ask for or confirm any details before
    calling it. The tool handles every required interaction internally.

    Preamble: None

    Returns:
        List[Dict[str, Any]]: A list of appointment objects when appointments are found, where each appointment contains:
            - appointment_date: Scheduled date and time for the appointment(YYYY-MM-DD HH:MM format)
            - reservation_id(optional): Unique identifier for the appointment
            - content(optional): Description or notes about the appointment
    """

    ctx.disallow_interruptions()

    userdata = ctx.userdata

    # 예약조회 시도의 분모. 이 이벤트가 없으면 성공 건만 관측되어
    # 전환율을 계산할 수 없다.
    ctx.session.history.add_message(
        role="developer",
        content="",
        extra=agent_event_extra("BOOKING_INQUIRY", status="STARTED"),
    )

    task = GetMyReservationsTask(
        userdata,
        action="inquiry",
        history=ctx.session.history,
    )
    result = await task.run()
    _record_identity_outcome(
        ctx,
        action="inquiry",
        result=result,
    )

    if await _finish_incomplete_reservation_lookup(
        ctx,
        event_name="BOOKING_INQUIRY",
        result=result,
    ):
        logger.bind(
            event="GET_MY_APPOINTMENT_LIST",
            stage=result.status,
            room_name=userdata.room_name,
        ).info("Reservation lookup did not complete")
        return None

    ctx.session.history.add_message(
        role="developer",
        content="",
        extra=agent_event_extra(
            "BOOKING_INQUIRY",
            status="SUCCESS",
            details={"count": len(result.reservations)},
        ),
    )

    if not result.reservations:
        logger.bind(
            event="GET_MY_APPOINTMENT_LIST",
            stage="no_result",
            room_name=userdata.room_name,
        ).info("No appointments found")
        return NO_APPOINTMENT_INFO

    appointment_dates = [
        reservation.get("appointment_date") for reservation in result.reservations
    ]

    logger.bind(
        event="GET_MY_APPOINTMENT_LIST",
        stage="success",
        appointment_dates=appointment_dates,
        count=len(appointment_dates),
        room_name=userdata.room_name,
    ).debug("Get my appointment list successfully")

    return appointment_dates


# `create_new_appointment` lives in tools.dynamic_booking_tools (registered by
# SingleAgent).


############ MODIFY APPOINTMENT ############
@function_tool()
@action_mode_handler(action_key="booking_modify", agent_name="booking_agent")
async def modify_appointment(ctx: RunContext_T) -> None:
    """
    Reschedule an existing inspection or consultation appointment to a new date and time.

    Call this tool immediately when the caller explicitly wants to change the date
    or time of an existing appointment. Do not ask for or confirm any details
    before calling it. The tool handles every required interaction internally.
    Do not use this tool for cancellations; use cancel_appointment instead.

    Preamble: None

    Returns:
        None: The tool announces or routes every terminal outcome itself.
    """

    ctx.disallow_interruptions()

    userdata = ctx.userdata
    userdata.booking_memo_context = None
    phone = userdata.recipient_data.phone

    logger.bind(
        event="MODIFY_APPOINTMENT",
        stage="start",
        phone=phone,
        room_name=userdata.room_name,
    ).debug("Attempting to modify appointment")

    ctx.session.history.add_message(
        role="developer",
        content="",
        extra=agent_event_extra("BOOKING_MODIFY", status="STARTED"),
    )

    task = GetMyReservationsTask(
        userdata,
        action="modify",
        history=ctx.session.history,
    )
    result = await task.run()
    _record_identity_outcome(
        ctx,
        action="modify",
        result=result,
    )

    if await _finish_incomplete_reservation_lookup(
        ctx,
        event_name="BOOKING_MODIFY",
        result=result,
    ):
        return None

    if not result.reservations:
        await _handoff_no_reservations(ctx, action="modify")
        return None

    select_result = await SelectReservationTask(
        reservations=result.reservations,
        action="변경",
        language_code=ctx.userdata.call_config_data.language,
    )

    if select_result.exited:
        logger.bind(
            event="MODIFY_APPOINTMENT",
            stage="reservation_selection_exited",
            exit_reason=select_result.exit_reason,
            phone=phone,
            room_name=userdata.room_name,
        ).debug("User exited reservation selection")
        await route_task_exit(
            ctx,
            event_name="BOOKING_MODIFY",
            source_action="booking_modify",
            result=select_result,
        )
        return None

    selected_reservation = select_result.selected_reservation

    logger.bind(
        event="MODIFY_APPOINTMENT",
        stage="selected_reservation",
        phone=phone,
        room_name=userdata.room_name,
        selected_reservation_id=selected_reservation.get("reservation_id"),
        selected_appointment_date=selected_reservation.get("appointment_date"),
        operator_id=selected_reservation.get("operator_id"),
    ).debug("Selected reservation")

    # Step 3: 새로운 날짜/시간 선택
    operator_id = selected_reservation.get("operator_id")
    if not operator_id:
        # ! 오디오 입력 비활성화
        ctx.session.input.set_audio_enabled(False)
        await _handoff_operator_missing(
            ctx,
            action_key="booking_modify",
            event_name="BOOKING_MODIFY",
            selected_reservation=selected_reservation,
        )
        return None

    selection_chat_ctx = task_chat_ctx(ctx.session.current_agent.chat_ctx, max_items=6)
    schedule_lookup_days = userdata.workflow_config.booking.schedule_lookup_days

    selection = await OperatorScheduleSelectionTask(
        operator_id=operator_id,
        reservation_data=userdata.reservation_data,
        lookup_days=schedule_lookup_days,
        service_name=selected_reservation.get("service_name"),
        language_code=ctx.userdata.call_config_data.language,
        chat_ctx=selection_chat_ctx,
    )

    if selection.lookup_failed:
        ctx.session.input.set_audio_enabled(False)
        if _is_operator_not_found_response(selection.lookup_failure_message):
            await _handoff_operator_missing(
                ctx,
                action_key="booking_modify",
                event_name="BOOKING_MODIFY",
                selected_reservation=selected_reservation,
            )
        else:
            await _handoff_operator_schedule_lookup_failed(
                ctx,
                selected_reservation=selected_reservation,
            )
        return None

    if selection.exited:
        logger.bind(
            event="MODIFY_APPOINTMENT",
            stage="reservation_selection_exited",
            exit_reason=selection.exit_reason,
            phone=phone,
            room_name=userdata.room_name,
        ).debug("User exited reservation selection")
        await route_task_exit(
            ctx,
            event_name="BOOKING_MODIFY",
            source_action="booking_modify",
            result=selection,
        )
        return None

    # ! 오디오 입력 비활성화
    ctx.session.input.set_audio_enabled(False)

    # 사용자가 최종 선택한 예약 일시
    new_appointment_datetime = selection.appointment_datetime
    # 사용자가 최종 선택한 상담사 아이디
    new_advisor_id = selection.advisor_id
    # 사용자가 최종 선택한 예약 아이디
    reservation_record_id = selected_reservation.get("reservation_id")
    # 사용자가 변경하기 전 예약 일시
    previous_reservation_date_time = selected_reservation.get("appointment_date")

    userdata.reservation_info = {
        "type": "MODIFY_RESERVATION",
        "service_id": selected_reservation.get("service_id"),
        "verification_code": normalize_verification_code(userdata.recipient_data.verification_code),
        "operator_id": new_advisor_id,
        "reservation_request_date_time": new_appointment_datetime,
        "reservation_record_id": reservation_record_id,
        "previous_reservation_date_time": previous_reservation_date_time,
    }

    # Persist the change before announcing success.
    if not (await submit_current_reservation(ctx, "booking_modify")).accepted:
        return None

    logger.bind(
        event="MODIFY_APPOINTMENT",
        stage="new_schedule_selected",
        phone=phone,
        room_name=userdata.room_name,
        reservation_id=reservation_record_id,
        previous_reservation_date_time=previous_reservation_date_time,
        new_datetime=new_appointment_datetime,
        new_advisor_id=new_advisor_id,
    ).debug("New schedule selected for modification")

    ctx.session.history.add_message(
        role="developer",
        content="",
        extra=agent_event_extra(
            "BOOKING_MODIFY",
            status="SUCCESS",
            details={
                "reservation_id": selected_reservation.get("reservation_id"),
                "previous_reservation_date_time": previous_reservation_date_time,
                "reservation_request_date_time": new_appointment_datetime,
            },
        ),
    )

    await _announce_and_shutdown_call(
        ctx,
        instructions=(
            t("booking.modify.success", ctx.userdata.call_config_data.language)
        ),
        log_event="MODIFY_APPOINTMENT",
        phone=phone,
    )
    return None


############ CANCEL APPOINTMENT ############
@function_tool()
@action_mode_handler(action_key="booking_cancel", agent_name="booking_agent")
async def cancel_appointment(ctx: RunContext_T) -> None:
    """
    Cancel an existing inspection or consultation appointment.

    Call this tool immediately when the caller explicitly wants to cancel an
    existing appointment. Do not ask for or confirm any details before calling
    it. The tool handles every required interaction internally.

    Preamble: None

    Returns:
        None: The tool announces or routes every terminal outcome itself.
    """

    ctx.disallow_interruptions()

    ctx.session.history.add_message(
        role="developer",
        content="",
        extra=agent_event_extra("BOOKING_CANCEL", status="STARTED"),
    )

    userdata = ctx.userdata
    userdata.booking_memo_context = None
    phone = userdata.recipient_data.phone

    logger.bind(
        event="CANCEL_APPOINTMENT",
        stage="start",
        phone=phone,
        room_name=userdata.room_name,
    ).debug("Attempting to cancel appointment")

    task = GetMyReservationsTask(
        userdata,
        action="cancel",
        history=ctx.session.history,
    )
    result = await task.run()
    _record_identity_outcome(
        ctx,
        action="cancel",
        result=result,
    )

    if await _finish_incomplete_reservation_lookup(
        ctx,
        event_name="BOOKING_CANCEL",
        result=result,
    ):
        return None

    if not result.reservations:
        await _handoff_no_reservations(ctx, action="cancel")
        return None

    select_result = await SelectReservationTask(
        reservations=result.reservations,
        action="취소",
        language_code=ctx.userdata.call_config_data.language,
    )

    try:
        await ctx.wait_for_playout()
    except Exception:
        logger.bind(
            event="CANCEL_APPOINTMENT",
            stage="reservation_selection_exited",
            exit_reason=select_result.exit_reason,
            phone=phone,
            room_name=userdata.room_name,
        ).error("Error waiting for playout after reservation selection")

    if select_result.exited:
        logger.bind(
            event="CANCEL_APPOINTMENT",
            stage="reservation_selection_exited",
            exit_reason=select_result.exit_reason,
            phone=phone,
            room_name=userdata.room_name,
        ).debug("User exited reservation selection")
        await route_task_exit(
            ctx,
            event_name="BOOKING_CANCEL",
            source_action="booking_cancel",
            result=select_result,
        )
        return None

    selected_reservation = select_result.selected_reservation

    logger.bind(
        event="CANCEL_APPOINTMENT",
        stage="selected_reservation",
        phone=phone,
        room_name=userdata.room_name,
        selected_reservation_id=selected_reservation.get("reservation_id"),
    ).debug("Selected reservation")

    # ! 오디오 입력 비활성화
    ctx.session.input.set_audio_enabled(False)

    # ── Step 3: 예약 취소 정보 확정 ──
    phone = userdata.recipient_data.phone

    reservation_record_id = selected_reservation.get("reservation_id")
    previous_reservation_date_time = selected_reservation.get("appointment_date")
    operator_id = selected_reservation.get("operator_id")
    service_id = selected_reservation.get("service_id")

    if not operator_id:
        logger.bind(
            event="CANCEL_APPOINTMENT",
            stage="missing_operator_id",
            phone=phone,
            room_name=userdata.room_name,
            reservation_id=selected_reservation.get("reservation_id"),
        ).error("No operator_id found in selected reservation")
        await _handoff_operator_missing(
            ctx,
            action_key="booking_cancel",
            event_name="BOOKING_CANCEL",
            selected_reservation=selected_reservation,
        )
        return None

    userdata.reservation_info = {
        "type": "CANCEL_RESERVATION",
        "verification_code": normalize_verification_code(userdata.recipient_data.verification_code),
        "reservation_record_id": reservation_record_id,
        "previous_reservation_date_time": previous_reservation_date_time,
        "operator_id": operator_id,
        "service_id": service_id,
    }

    # Persist the cancellation before announcing success.
    if not (await submit_current_reservation(ctx, "booking_cancel")).accepted:
        return None

    ctx.session.history.add_message(
        role="developer",
        content="",
        extra=agent_event_extra(
            "BOOKING_CANCEL",
            status="SUCCESS",
            details={
                "reservation_record_id": reservation_record_id,
                "previous_reservation_date_time": previous_reservation_date_time,
            },
        ),
    )

    logger.bind(
        event="CANCEL_APPOINTMENT",
        stage="success",
        phone=phone,
        room_name=userdata.room_name,
        reservation_id=selected_reservation.get("reservation_id"),
    ).info("Appointment cancelled successfully")

    await _announce_and_shutdown_call(
        ctx,
        instructions=(
            t("booking.cancel.success", ctx.userdata.call_config_data.language)
        ),
        log_event="CANCEL_APPOINTMENT",
        phone=phone,
    )

    return None
