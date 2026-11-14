"""Bug fixes in the legacy booking tools (area C, part 2)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from data import BusinessData, CallConfigData, CallSessionData, RecipientData
from tasks.booking import get_my_reservations_task
from tasks.booking.get_my_reservations_task import (
    GetMyReservationsResult,
    GetMyReservationsTask,
)
from tasks.dtmf_verification_code_task import GetDtmfResult
from tasks.verify_customer_identity_task import VerifyCustomerResult
from tools.booking_tools import (
    _handoff_operator_missing,
    _handoff_operator_schedule_lookup_failed,
    _record_identity_outcome,
)

_RESERVATION = {
    "reservation_id": "R-1",
    "service_name": "내과",
    "operator_name": "홍길동",
    "appointment_date": "2030-01-09 10:00:00",
}


def _handoff_ctx(*, disconnected_before: bool, disconnect_on_playout: bool = False):
    events = []
    userdata = SimpleNamespace(
        call_config_data=SimpleNamespace(
            language="ko-KR", transfer_mode="cold", transfer_to="1004"
        ),
        room_name="c-bugfix",
        caller_disconnected=disconnected_before,
        booking_memo_context=None,
    )

    async def wait_for_playout():
        if disconnect_on_playout:
            userdata.caller_disconnected = True

    session = SimpleNamespace(
        history=SimpleNamespace(
            add_message=lambda role, content, extra: events.append(extra)
        ),
        say=Mock(return_value=SimpleNamespace(wait_for_playout=wait_for_playout)),
        current_agent=SimpleNamespace(_handle_transfer_call=AsyncMock()),
    )
    return SimpleNamespace(session=session, userdata=userdata), events


async def _operator_missing(ctx):
    await _handoff_operator_missing(
        ctx,
        action_key="booking_cancel",
        event_name="BOOKING_CANCEL",
        selected_reservation=dict(_RESERVATION),
    )


async def _lookup_failed(ctx):
    await _handoff_operator_schedule_lookup_failed(
        ctx, selected_reservation=dict(_RESERVATION)
    )


_HANDOFFS = [
    (_operator_missing, "BOOKING_CANCEL", "reservation_operator_missing"),
    (_lookup_failed, "BOOKING_MODIFY", "operator_schedule_lookup_failed"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("handoff", "event_name", "reason"), _HANDOFFS)
async def test_handoff_after_hangup_records_evidence_without_new_activity(
    handoff, event_name, reason
):
    ctx, events = _handoff_ctx(disconnected_before=True)

    await handoff(ctx)

    assert events[0]["agent_event"] == {
        "name": event_name,
        "status": "FAILED",
        "details": {"reason": reason},
    }
    assert ctx.userdata.booking_memo_context.exit_reason_code == reason
    ctx.session.say.assert_not_called()
    ctx.session.current_agent._handle_transfer_call.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("handoff", "event_name", "reason"), _HANDOFFS)
async def test_handoff_skips_transfer_when_caller_hangs_up_during_announcement(
    handoff, event_name, reason
):
    ctx, _ = _handoff_ctx(disconnected_before=False, disconnect_on_playout=True)

    await handoff(ctx)

    ctx.session.say.assert_called_once()
    ctx.session.current_agent._handle_transfer_call.assert_not_awaited()


def _reservations_userdata(verification_code):
    return CallSessionData(
        ctx=None,
        room_name="c-bugfix",
        business_data=BusinessData(site_id="site-1", client_id=1),
        recipient_data=RecipientData(phone="01000000101", verification_code=verification_code),
        call_config_data=CallConfigData(language="ko-KR"),
    )


def _patch_lookup(monkeypatch, *, verify=None, dtmf=None):
    monkeypatch.setattr(
        get_my_reservations_task,
        "get_confirmed_appointments",
        AsyncMock(return_value=SimpleNamespace(success=True, data=[], message=None)),
    )
    if verify is not None:
        monkeypatch.setattr(
            get_my_reservations_task,
            "VerifyCustomerIdentityTask",
            Mock(return_value=SimpleNamespace(run=AsyncMock(side_effect=verify))),
        )
    if dtmf is not None:
        monkeypatch.setattr(
            get_my_reservations_task,
            "GetVerificationCodeDtmfTask",
            AsyncMock(return_value=dtmf),
        )


@pytest.mark.asyncio
async def test_reservation_lookup_reports_known_identity(monkeypatch):
    _patch_lookup(monkeypatch)

    result = await GetMyReservationsTask(_reservations_userdata("1990-01-01")).run()

    assert result.status == "completed"
    assert result.identity_source == "known"


@pytest.mark.asyncio
async def test_reservation_lookup_reports_verified_identity(monkeypatch):
    userdata = _reservations_userdata(None)

    def matched():
        userdata.recipient_data.verification_code = "1985-05-05"
        return VerifyCustomerResult(status="matched")

    _patch_lookup(monkeypatch, verify=matched)

    result = await GetMyReservationsTask(userdata).run()

    assert result.status == "completed"
    assert result.identity_source == "verified"


@pytest.mark.asyncio
async def test_reservation_lookup_reports_new_customer_identity(monkeypatch):
    _patch_lookup(
        monkeypatch,
        verify=lambda: VerifyCustomerResult(status="no_customer"),
        dtmf=GetDtmfResult(
            user_input="900101", normalized_date="1990-01-01", confirmed=True
        ),
    )

    result = await GetMyReservationsTask(_reservations_userdata(None)).run()

    assert result.status == "completed"
    assert result.identity_source == "new_customer"


@pytest.mark.asyncio
async def test_reservation_api_failure_keeps_identity_source(monkeypatch):
    monkeypatch.setattr(
        get_my_reservations_task,
        "get_confirmed_appointments",
        AsyncMock(return_value=SimpleNamespace(success=False, data=None, message="x")),
    )

    result = await GetMyReservationsTask(_reservations_userdata("1990-01-01")).run()

    assert result.status == "error"
    assert result.identity_source == "known"


@pytest.mark.parametrize(
    ("identity_source", "expected"),
    [("known", "known"), ("new_customer", "new_customer"), (None, "verified")],
)
def test_identity_outcome_event_uses_reported_source(identity_source, expected):
    events = []
    ctx = SimpleNamespace(
        session=SimpleNamespace(
            history=SimpleNamespace(
                add_message=lambda role, content, extra: events.append(extra)
            )
        ),
        userdata=SimpleNamespace(recipient_data=SimpleNamespace(verification_code="19900101")),
    )

    _record_identity_outcome(
        ctx,
        action="modify",
        result=GetMyReservationsResult(
            status="completed", identity_source=identity_source
        ),
    )

    assert events == [
        {
            "agent_event": {
                "name": "IDENTITY_VERIFY",
                "status": "SUCCESS",
                "details": {"action": "modify", "source": expected},
            }
        }
    ]
