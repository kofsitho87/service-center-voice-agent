"""Simulated telephony boundary for human handoff.

Real calls would be placed at two points:

- warm transfer: ``api.sip.create_sip_participant`` dials a staff phone into the
  room (``SingleRoomWarmTransferTask._dial_agent``);
- cold transfer: ``api.sip.transfer_sip_participant`` sends a SIP REFER for the
  caller (``BaseAgent._handle_cold_transfer_call``).

Both now call this gateway, which never reaches LiveKit SIP or a carrier. It
records the attempt and, by default, raises ``SimulatedTelephonyError``.
The regular exception paths then run (warm transfer marks the attempt failed
and falls back to leave-message; cold transfer apologizes and ends the call).
Connected/busy/timeout outcomes are not simulated.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from fake_backend import faults, outbox


class SimulatedTelephonyError(RuntimeError):
    """No real telephony exists in the demo; the transfer cannot connect."""


def _redacted_target(value: str | None) -> str | None:
    if not value:
        return value
    return f"***{value[-4:]}" if len(value) > 4 else "***"


async def create_sip_participant(job_ctx: Any, request: Any) -> None:
    """Stand-in for ``job_ctx.api.sip.create_sip_participant`` (warm transfer)."""
    outcome = faults.next_outcome("sip_dial")
    outbox.append(
        "telephony",
        {
            "operation": "create_sip_participant",
            "room": getattr(request, "room_name", None),
            "target": _redacted_target(getattr(request, "sip_call_to", None)),
            "outcome": outcome,
        },
    )
    logger.bind(
        event="SIMULATED_TELEPHONY", operation="create_sip_participant", outcome=outcome
    ).warning("Simulated SIP dial; no call placed")
    raise SimulatedTelephonyError("simulated staff line: no real call placed")


async def transfer_sip_participant(job_ctx: Any, request: Any) -> None:
    """Stand-in for ``job_ctx.api.sip.transfer_sip_participant`` (cold transfer)."""
    outbox.append(
        "telephony",
        {
            "operation": "transfer_sip_participant",
            "room": getattr(request, "room_name", None),
            "target": _redacted_target(getattr(request, "transfer_to", None)),
            "outcome": "unavailable",
        },
    )
    logger.bind(
        event="SIMULATED_TELEPHONY", operation="transfer_sip_participant"
    ).warning("Simulated SIP REFER; no call transferred")
    raise SimulatedTelephonyError("simulated transfer: no real call transferred")
