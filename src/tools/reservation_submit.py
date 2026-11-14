"""Reservation write hook at the booking-tool completion points.

Create/modify/cancel fill ``userdata.reservation_info``, and that
``reservation_info`` is submitted to the fake backend *before* the success
announcement, so an occupied slot, a missing or foreign appointment, or a
backend failure is handled during the call:

- ACCEPTED: continue with the original success path unchanged.
- CONFLICT (create/modify): say so, keep the caller's search preferences, and
  re-enter the same tool. A new ``DynamicBookingTask`` starts with an empty
  schedule cache, so the taken slot is no longer offered. Stale consent cannot
  carry over: the pending candidate is cleared, and the booking task already
  requires a re-staged candidate to be revalidated.
- NOT_FOUND (modify/cancel): say nothing was changed and return to the agent.
- ERROR / INVALID: say the request was not completed and hand off through the
  existing booking-exit router (``staff_connection``).

On every non-accepted outcome ``reservation_info`` is cleared, so the call-end
event never reports an unsaved request as a reservation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from loguru import logger

from data import RunContext_T
from external_apis.booking_api import submit_reservation
from tools.booking_exit_router import route_booking_exit
from utils.agent_event import agent_event_extra, record_api_result
from utils.i18n import t

BookingAction = Literal["booking_create", "booking_modify", "booking_cancel"]

_EVENT_NAMES: dict[str, str] = {
    "booking_create": "BOOKING_CREATE",
    "booking_modify": "BOOKING_MODIFY",
    "booking_cancel": "BOOKING_CANCEL",
}
_RETRY_TOOLS: dict[str, str] = {
    "booking_create": "create_new_appointment",
    "booking_modify": "modify_appointment",
}


@dataclass(frozen=True)
class SubmitOutcome:
    status: str
    reservation_record_id: str | None = None
    replayed: bool = False

    @property
    def accepted(self) -> bool:
        return self.status == "ACCEPTED"


def idempotency_key(room_name: str | None, reservation_info: dict) -> str:
    digest = hashlib.sha256(
        json.dumps(
            reservation_info, sort_keys=True, ensure_ascii=False, default=str
        ).encode()
    ).hexdigest()[:24]
    return f"{room_name or 'no-room'}:{digest}"


async def submit_current_reservation(
    ctx: RunContext_T, action: BookingAction
) -> SubmitOutcome:
    """Persist ``userdata.reservation_info``; handle every non-accepted outcome.

    Returns the outcome. When it is not accepted the caller must ``return``
    without running its success path; this function already spoke and routed.
    """
    userdata = ctx.userdata
    info = dict(userdata.reservation_info or {})
    recipient = userdata.recipient_data
    customer = {
        "phone": recipient.phone,
        "customer_no": recipient.customer_no,
        "name": recipient.user_name,
        "verification_code": info.get("verification_code"),
    }
    response = await submit_reservation(
        userdata.business_data.site_id,
        info,
        customer=customer,
        idempotency_key=idempotency_key(userdata.room_name, info),
        client_id=userdata.business_data.client_id,
    )
    record_api_result(ctx.session.history, "SUBMIT_RESERVATION", response)
    contents = response.data
    outcome = SubmitOutcome(
        status=contents.status if contents is not None else "ERROR",
        reservation_record_id=contents.reservationRecordId if contents is not None else None,
        replayed=bool(contents and contents.replayed),
    )
    logger.bind(
        event="SUBMIT_RESERVATION",
        action=action,
        status=outcome.status,
        replayed=outcome.replayed,
        room_name=userdata.room_name,
    ).info("Reservation submission finished")

    if outcome.accepted:
        if outcome.reservation_record_id and action == "booking_create":
            userdata.reservation_info["reservation_record_id"] = outcome.reservation_record_id
        return outcome

    userdata.reservation_info = {}
    language = userdata.call_config_data.language
    ctx.session.history.add_message(
        role="developer",
        content="",
        extra=agent_event_extra(
            _EVENT_NAMES[action],
            status=outcome.status,
            details={"reason": "submit_rejected", "message": response.message},
        ),
    )

    if outcome.status == "CONFLICT" and action in _RETRY_TOOLS:
        if action == "booking_create":
            # The resume context (search preferences) is kept; the candidate and
            # any consent attached to it are not.
            userdata.dynamic_booking_pending = None
        ctx.session.say(
            t("booking.submit.slot_taken", language), allow_interruptions=False
        )
        ctx.session.input.set_audio_enabled(True)
        _request_tool_reentry(ctx, _RETRY_TOOLS[action])
        return outcome

    if outcome.status == "NOT_FOUND":
        ctx.session.say(
            t("booking.submit.not_found", language), allow_interruptions=True
        )
        ctx.session.input.set_audio_enabled(True)
        return outcome

    ctx.session.say(t("booking.submit.failed", language), allow_interruptions=False)
    await route_booking_exit(
        ctx,
        source_action=action,
        reason_code="staff_connection",
        detail="reservation_submit_failed",
    )
    return outcome


def _request_tool_reentry(ctx: RunContext_T, tool_name: str) -> None:
    """Re-enter a booking tool the way the booking-exit router does.

    ``SingleAgent.handle_booking_exit`` uses ``_request_exact_tool_call`` for
    ``change_service``/``change_advisor``; this reuses that mechanism instead
    of adding a new LLM-visible exit reason.
    """
    agent = ctx.session.current_agent
    request = getattr(agent, "_request_exact_tool_call", None)
    if not callable(request):
        raise RuntimeError("current agent cannot re-enter booking tools")
    request(tool_name)
