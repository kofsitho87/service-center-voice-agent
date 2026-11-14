"""Booking handoff domain: grounded blockers, offers, and caller replies.

Plan: docs/plans/2026-09-29-booking-handoff-final-plan.md.

Program control uses `kind`, `reason_code`, `channel` and `revision`.
`requested_label`, `searched_range` and `filters` only ground the model's
explanation; they never select a route. The model speaks the offer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

HandoffChannel: TypeAlias = Literal["memo", "transfer"]
# Terminal reasons already understood by the parent router and analytics.
HandoffReasonCode: TypeAlias = Literal[
    "service_not_offered", "no_slot_memo", "staff_connection"
]
ObstacleSubject: TypeAlias = Literal["service", "advisor", "schedule"]
OfferReply: TypeAlias = Literal["accept", "decline", "other"]
# Why an offer was opened: a grounded blocker, the caller's physical issue,
# or repeatedly declined alternatives (the last two carry no blocker).
OfferBasis: TypeAlias = Literal["blocker", "issue", "declined_alternatives"]


@dataclass(frozen=True)
class ServiceScopeBlocker:
    """A clear request with no match in the phone-booking catalog.

    Catalog absence is not evidence that the center lacks the appointment.
    """

    subject: Literal["service", "advisor"]
    requested_label: str | None
    kind: Literal["service_outside_booking_scope"] = "service_outside_booking_scope"


@dataclass(frozen=True)
class NoMatchingSlotBlocker:
    """A successful search whose exact request matched no slot.

    Evidence is limited to `searched_range` and `filters`. A combined query
    does not prove which single constraint caused the miss.
    """

    searched_range: tuple[str, str]
    filters: dict[str, Any]
    alternative_slot_count: int
    range_complete: bool
    horizon_reached: bool
    kind: Literal["no_matching_slot"] = "no_matching_slot"


Blocker: TypeAlias = ServiceScopeBlocker | NoMatchingSlotBlocker


@dataclass(frozen=True)
class HandoffOffer:
    """A help offer returned to the model, awaiting the caller's reply."""

    id: str
    blocker: Blocker | None
    reason_code: HandoffReasonCode
    channel: HandoffChannel
    revision: int
    # The caller turn the offer was opened in; only a later turn can answer it.
    user_message_id: str | None


def reason_code_for(blocker: Blocker | None) -> HandoffReasonCode:
    if blocker is None:
        return "staff_connection"
    if blocker.kind == "service_outside_booking_scope":
        return "service_not_offered"
    return "no_slot_memo"


def offer_basis(blocker: Blocker | None, reason_code: HandoffReasonCode) -> OfferBasis:
    if blocker is not None:
        return "blocker"
    if reason_code == "staff_connection":
        return "issue"
    return "declined_alternatives"


def blocker_facts(blocker: Blocker | None) -> dict[str, Any] | None:
    """Primitive facts for tool results and analytics (no free-form text)."""
    if blocker is None:
        return None
    if isinstance(blocker, ServiceScopeBlocker):
        return {
            "kind": blocker.kind,
            "subject": blocker.subject,
            "requested_label": blocker.requested_label,
        }
    return {
        "kind": blocker.kind,
        "searched_range": {
            "start_date": blocker.searched_range[0],
            "end_date": blocker.searched_range[1],
        },
        "filters": dict(blocker.filters),
        "matching_slot_count": 0,
        "alternative_slot_count": blocker.alternative_slot_count,
        "range_complete": blocker.range_complete,
        "horizon_reached": blocker.horizon_reached,
    }


def normalize_label(value: str) -> str:
    return "".join(value.split()).casefold()


_ADVISOR_TITLES = ("상담사님", "매니저님", "엔지니어님", "기사님", "상담사", "매니저", "엔지니어", "기사", "님")


def advisor_name_core(value: str) -> str:
    """A caller's advisor reference without spaces or titles ("정 상담사님" → "정")."""
    core = normalize_label(value)
    changed = True
    while changed:
        changed = False
        for title in _ADVISOR_TITLES:
            if core.endswith(title) and len(core) > len(title):
                core = core[: -len(title)]
                changed = True
    return core


def advisor_name_match(reference: str, listed_name: str) -> str | None:
    """How a caller's advisor reference relates to a listed advisor's name.

    "exact": same name. "partial": surname only or a fragment of the name.
    "similar": same length with one differing syllable (misremembered name or
    STT error). None: unrelated. Only "exact" may identify the advisor without a
    confirmation question.
    """
    ref, name = advisor_name_core(reference), advisor_name_core(listed_name)
    if not ref or not name:
        return None
    if ref == name:
        return "exact"
    # A single syllable is a surname and matches only as the name's prefix
    # ("정" → 정진수, not 김정민); longer fragments may be the given name.
    if len(ref) < len(name) and (
        name.startswith(ref) or (len(ref) >= 2 and ref in name)
    ):
        return "partial"
    if (
        len(ref) == len(name) >= 2
        and sum(a != b for a, b in zip(ref, name, strict=True)) == 1
    ):
        return "similar"
    return None
