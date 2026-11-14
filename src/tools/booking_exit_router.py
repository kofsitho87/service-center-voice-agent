"""Bridge booking-tool exits to the parent agent's deterministic router."""

from __future__ import annotations

from typing import Protocol, cast

from data import RunContext_T
from tasks.booking.exit_reasons import BookingExitRouteTarget


class _BookingExitHandler(Protocol):
    async def handle_booking_exit(
        self,
        ctx: RunContext_T,
        *,
        source_action: str,
        reason_code: str | None,
        detail: str = "",
        route_target: BookingExitRouteTarget | None = None,
        handoff_accepted: bool | None = None,
        handoff_channel: str | None = None,
        stopped_before_selection: bool = False,
    ) -> None: ...


async def route_booking_exit(
    ctx: RunContext_T,
    *,
    source_action: str,
    reason_code: str | None,
    detail: str = "",
    route_target: BookingExitRouteTarget | None = None,
    handoff_accepted: bool | None = None,
    handoff_channel: str | None = None,
    stopped_before_selection: bool = False,
) -> None:
    """Execute a booking exit through the active parent agent.

    A missing handler is a programming error.  Falling back to a textual tool
    result would reintroduce the LLM-readable routing protocol this bridge
    replaces.
    """
    current_agent = ctx.session.current_agent
    handler = getattr(current_agent, "handle_booking_exit", None)
    if not callable(handler):
        raise RuntimeError("current agent does not support booking exit routing")

    await cast(_BookingExitHandler, current_agent).handle_booking_exit(
        ctx,
        source_action=source_action,
        reason_code=reason_code,
        detail=detail,
        route_target=route_target,
        handoff_accepted=handoff_accepted,
        handoff_channel=handoff_channel,
        stopped_before_selection=stopped_before_selection,
    )
