"""Characterization tests for BaseAgent._handle_cold_transfer_call.

The SIP REFER is faked; these tests pin the chat-history events, the
TransferRecord and the error follow-up that post-call analysis relies on.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from livekit import api
from loguru import logger

import agents.base_agent as base_agent_module
from agents.base_agent import BaseAgent

ERROR_INSTRUCTIONS = (
    "Inform that an error occurred during the representative connection process. "
    "Apologize and inform that you will end the call."
)


@pytest.fixture
def log_records():
    records: list[dict] = []
    sink_id = logger.add(lambda message: records.append(message.record), level="DEBUG")
    yield records
    logger.remove(sink_id)


def _speech_handle(wait_side_effect=None):
    return SimpleNamespace(wait_for_playout=AsyncMock(side_effect=wait_side_effect))


def _cold_agent(monkeypatch, *, transfer_side_effect=None, transfer_message=None):
    error_handle = _speech_handle()
    session = SimpleNamespace(
        input=SimpleNamespace(set_audio_enabled=Mock()),
        say=Mock(return_value=_speech_handle()),
        history=SimpleNamespace(add_message=Mock()),
        generate_reply=AsyncMock(return_value=error_handle),
    )
    monkeypatch.setattr(BaseAgent, "session", property(lambda _: session))
    transfer = AsyncMock(side_effect=transfer_side_effect)
    # The SIP REFER goes through services.fake_transfer_gateway
    # (called as ``transfer_sip_participant(job_ctx, request)``) instead of
    # ``job_ctx.api.sip``; fake that boundary with the same mock.
    job_ctx = SimpleNamespace(room=SimpleNamespace(name="room-1"))
    monkeypatch.setattr(
        base_agent_module,
        "fake_transfer_gateway",
        SimpleNamespace(transfer_sip_participant=transfer),
    )
    monkeypatch.setattr(base_agent_module, "get_job_context", lambda: job_ctx)
    monkeypatch.setattr(base_agent_module, "COLD_TRANSFER_RETRY_DELAY_SECONDS", 0)
    data = SimpleNamespace(
        room_name="room-1",
        recipient_data=SimpleNamespace(phone="01000000000", sip_identity="sip-caller"),
        business_data=SimpleNamespace(is_work_time=True),
        call_config_data=SimpleNamespace(transfer_message=transfer_message),
        transfer_record=None,
    )
    agent = object.__new__(BaseAgent)
    return agent, session, data, transfer, error_handle


def _history_extras(session):
    return [
        (call.kwargs["content"], call.kwargs["extra"])
        for call in session.history.add_message.call_args_list
    ]


def _forbidden(code="permission_denied"):
    return api.TwirpError(
        code,
        "forbidden",
        status=403,
        metadata={"sip_status_code": "403", "sip_status": "Forbidden"},
    )


def _started(transfer_type="COLD"):
    return (
        f"[{transfer_type}_TRANSFER_CALL: STATUS=STARTED, TARGET=1004]",
        {
            "agent_event": {
                "name": f"{transfer_type}_TRANSFER_CALL",
                "details": {"target": "1004"},
                "status": "STARTED",
            }
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("transfer_type", ["COLD", "DIRECT_COLD"])
async def test_success_records_started_and_success(monkeypatch, transfer_type):
    agent, session, data, transfer, _ = _cold_agent(monkeypatch)

    await agent._handle_cold_transfer_call(data, "1004", transfer_type)

    session.input.set_audio_enabled.assert_called_once_with(False)
    called_job_ctx, request = transfer.await_args.args
    assert called_job_ctx.room.name == "room-1"
    assert request.room_name == "room-1"
    assert request.participant_identity == "sip-caller"
    # The transfer target uses the reserved transfer.invalid domain.
    assert request.transfer_to == "sip:1004@transfer.invalid"
    assert request.play_dialtone is True
    assert _history_extras(session) == [
        _started(transfer_type),
        (
            "",
            {
                "agent_event": {
                    "name": f"{transfer_type}_TRANSFER_CALL",
                    "details": {"target": "1004", "business_hours": True},
                    "status": "SUCCESS",
                }
            },
        ),
    ]
    record = data.transfer_record
    assert record.type == "cold_transfer"
    assert record.action_type == (
        "transfer_direct" if transfer_type == "DIRECT_COLD" else "transfer"
    )
    assert record.transfer_status == "requested"
    assert record.transfer_target_phone == "1004"
    assert len(record.transfer_requested_at) == len("2026-10-20 10:00:00")
    session.generate_reply.assert_not_awaited()
    session.say.assert_not_called()


@pytest.mark.asyncio
async def test_transfer_message_is_played_before_refer(monkeypatch):
    agent, session, data, _, _ = _cold_agent(monkeypatch, transfer_message="잠시만요")

    await agent._handle_cold_transfer_call(data, "1004")

    session.say.assert_called_once_with("잠시만요", allow_interruptions=False)


@pytest.mark.asyncio
async def test_sip_403_is_retried_then_succeeds(monkeypatch, log_records):
    agent, session, data, transfer, _ = _cold_agent(
        monkeypatch, transfer_side_effect=[_forbidden(), None]
    )

    await agent._handle_cold_transfer_call(data, "1004")

    assert transfer.await_count == 2
    retry_logs = [
        r["extra"] for r in log_records if r["extra"].get("stage") == "transfer_retry"
    ]
    assert retry_logs == [
        {
            "event": "COLD_TRANSFER_CALL",
            "stage": "transfer_retry",
            "room_name": "room-1",
            "phone": "01000000000",
            "attempt": 1,
            "max_attempts": 3,
            "retry_delay_seconds": 0,
            "twirp_code": "permission_denied",
            "twirp_message": "forbidden",
            "twirp_status": 403,
            "sip_status_code": "403",
            "sip_status": "Forbidden",
        }
    ]
    assert _history_extras(session)[-1][1]["agent_event"]["status"] == "SUCCESS"
    assert data.transfer_record.transfer_status == "requested"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_twirp_fields"),
    [
        (
            api.TwirpError("internal", "boom", status=500),
            {"twirp_code": "internal", "twirp_message": "boom", "twirp_status": 500},
        ),
        (RuntimeError("network down"), {}),
    ],
)
async def test_failure_records_error_and_apologizes(
    monkeypatch, log_records, error, expected_twirp_fields
):
    agent, session, data, transfer, error_handle = _cold_agent(
        monkeypatch, transfer_side_effect=error
    )

    await agent._handle_cold_transfer_call(data, "1004")

    assert transfer.await_count == 1
    assert data.transfer_record.transfer_status == "error"
    assert _history_extras(session) == [
        _started(),
        (
            "",
            {
                "agent_event": {
                    "name": "COLD_TRANSFER_CALL",
                    "details": {"reason": str(error)},
                    "status": "ERROR",
                }
            },
        ),
    ]
    session.generate_reply.assert_awaited_once_with(instructions=ERROR_INSTRUCTIONS)
    error_handle.wait_for_playout.assert_awaited_once()
    error_logs = [r for r in log_records if r["extra"].get("stage") == "transfer_error"]
    assert len(error_logs) == 1
    assert error_logs[0]["level"].name == "ERROR"
    assert error_logs[0]["message"] == "Error transferring SIP participant"
    assert error_logs[0]["extra"] == {
        "event": "COLD_TRANSFER_CALL",
        "stage": "transfer_error",
        "room_name": "room-1",
        "phone": "01000000000",
        **expected_twirp_fields,
    }


@pytest.mark.asyncio
async def test_exhausted_403_retries_fall_into_error_path(monkeypatch):
    agent, session, data, transfer, _ = _cold_agent(
        monkeypatch, transfer_side_effect=[_forbidden(), _forbidden(), _forbidden()]
    )

    await agent._handle_cold_transfer_call(data, "1004")

    assert transfer.await_count == 3
    assert data.transfer_record.transfer_status == "error"
    assert _history_extras(session)[-1][1]["agent_event"]["status"] == "ERROR"


@pytest.mark.asyncio
async def test_error_follow_up_playout_failure_is_swallowed(monkeypatch):
    agent, _session, data, _, error_handle = _cold_agent(
        monkeypatch, transfer_side_effect=RuntimeError("down")
    )
    error_handle.wait_for_playout.side_effect = RuntimeError("playout")

    assert await agent._handle_cold_transfer_call(data, "1004") is None
