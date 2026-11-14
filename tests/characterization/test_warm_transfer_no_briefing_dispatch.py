"""warm_transfer_no_briefing: config, dispatch, warm handler and monitor.

Plan: docs/plans/2026-09-29-warm-transfer-no-briefing-plan.md (Sections 1-3, 5).
SingleRoomWarmTransferTask is always replaced by a fake so these tests only
cover what BaseAgent passes to it and how it handles the result.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import fakeredis.aioredis
import pytest
from loguru import logger

import agents.base_agent as base_agent_module
from agents.base_agent import BaseAgent
from data import (
    TRANSFER_MODES,
    WARM_TRANSFER_MODES,
    CallSessionData,
    normalize_transfer_mode,
)
from services.trunk_allocation_client import TrunkCheckResult, TrunkStatus
from services.warm_transfer_queue import WarmTransferQueue
from tasks.single_room_warm_transfer_task import (
    WarmTransferResult,
    WarmTransferStatus,
)
from tasks.transfer_reason_task import TransferPurpose, TransferReasonResult

COMPANY_ID = 42
SIP_IDENTITY = "sip-caller"
AGENT_IDENTITY = "human-agent-1004"


@pytest.fixture
def warnings_sink():
    records: list[dict] = []
    sink_id = logger.add(
        lambda message: records.append(message.record), level="WARNING"
    )
    yield records
    logger.remove(sink_id)


# --- Section 1: config value and validation ---------------------------------


@pytest.mark.parametrize("mode", TRANSFER_MODES)
def test_normalize_transfer_mode_keeps_known_values(mode, warnings_sink):
    assert normalize_transfer_mode(mode) == mode
    assert warnings_sink == []


@pytest.mark.parametrize("value", [None, ""])
def test_normalize_transfer_mode_defaults_missing_values(value, warnings_sink):
    assert normalize_transfer_mode(value) == "warm_transfer"
    assert normalize_transfer_mode(value, default="cold_transfer") == "cold_transfer"
    assert warnings_sink == []


def test_normalize_transfer_mode_warns_and_defaults_unknown_value(warnings_sink):
    assert normalize_transfer_mode("warm_transfer_fast") == "warm_transfer"

    assert len(warnings_sink) == 1
    assert warnings_sink[0]["extra"]["event"] == "TRANSFER_MODE_UNKNOWN"


def test_transfer_mode_tuples():
    assert TRANSFER_MODES == (
        "warm_transfer",
        "warm_transfer_no_briefing",
        "cold_transfer",
        "leave_memo",
    )
    assert WARM_TRANSFER_MODES == ("warm_transfer", "warm_transfer_no_briefing")


def _from_dial_info(call_config: dict) -> CallSessionData:
    ctx = SimpleNamespace(room=SimpleNamespace(name="synthetic-room"))
    return CallSessionData.from_dial_info(ctx, {"call_config_data": call_config})


def test_from_dial_info_parses_no_briefing_mode():
    data = _from_dial_info({"transfer_mode": "warm_transfer_no_briefing"})

    assert data.call_config_data.transfer_mode == "warm_transfer_no_briefing"


def test_from_dial_info_defaults_missing_and_unknown_modes(warnings_sink):
    assert _from_dial_info({}).call_config_data.transfer_mode == "warm_transfer"
    assert warnings_sink == []

    unknown = _from_dial_info({"transfer_mode": "bogus"})

    assert unknown.call_config_data.transfer_mode == "warm_transfer"
    assert [r["extra"]["event"] for r in warnings_sink] == ["TRANSFER_MODE_UNKNOWN"]


# --- Section 2: dispatch ----------------------------------------------------


def _dispatch_agent(monkeypatch):
    session = SimpleNamespace(input=SimpleNamespace(set_audio_enabled=Mock()))
    monkeypatch.setattr(BaseAgent, "session", property(lambda _: session))
    agent = object.__new__(BaseAgent)
    agent._handle_cold_transfer_call = AsyncMock()
    agent._handle_warm_transfer_call = AsyncMock()
    data = SimpleNamespace(
        recipient_data=SimpleNamespace(sip_identity="caller"),
        call_config_data=SimpleNamespace(language="ko-KR"),
    )
    return agent, session, data


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("direct", "transfer_type"), [(False, "WARM"), (True, "DIRECT_WARM")]
)
async def test_no_briefing_dispatch_skips_reason_and_disables_briefing(
    monkeypatch, direct, transfer_type
):
    agent, session, data = _dispatch_agent(monkeypatch)
    reason_task = AsyncMock()
    monkeypatch.setattr("agents.base_agent.TransferReasonTask", reason_task)

    await agent._dispatch_transfer(
        data, "1004", "warm_transfer_no_briefing", direct=direct
    )

    reason_task.assert_not_called()
    session.input.set_audio_enabled.assert_called_once_with(False)
    agent._handle_cold_transfer_call.assert_not_awaited()
    agent._handle_warm_transfer_call.assert_awaited_once_with(
        call_session_data=data,
        transfer_to="1004",
        transfer_type=transfer_type,
        action_key=None,
        transfer_purpose=None,
        briefing_enabled=False,
    )


@pytest.mark.asyncio
async def test_no_briefing_dispatch_forwards_known_purpose(monkeypatch):
    agent, _, data = _dispatch_agent(monkeypatch)
    reason_task = AsyncMock()
    monkeypatch.setattr("agents.base_agent.TransferReasonTask", reason_task)
    purpose = TransferPurpose(code="urgent_handoff")

    await agent._dispatch_transfer(
        data,
        "1004",
        "warm_transfer_no_briefing",
        direct=False,
        action_key="booking_create",
        transfer_purpose=purpose,
    )

    reason_task.assert_not_called()
    kwargs = agent._handle_warm_transfer_call.await_args.kwargs
    assert kwargs["transfer_purpose"] == purpose
    assert kwargs["action_key"] == "booking_create"
    assert kwargs["briefing_enabled"] is False


@pytest.mark.asyncio
async def test_briefed_dispatch_still_asks_reason_and_enables_briefing(monkeypatch):
    agent, _, data = _dispatch_agent(monkeypatch)
    purpose = TransferPurpose(code="caller_stated", detail="예약 변경")
    reason_task = AsyncMock(return_value=TransferReasonResult(purpose=purpose))
    monkeypatch.setattr("agents.base_agent.TransferReasonTask", reason_task)

    await agent._dispatch_transfer(data, "1004", "warm_transfer", direct=False)

    reason_task.assert_awaited_once_with(caller_identity="caller", language="ko-KR")
    kwargs = agent._handle_warm_transfer_call.await_args.kwargs
    assert kwargs["transfer_type"] == "WARM"
    assert kwargs["transfer_purpose"] == purpose
    assert kwargs["briefing_enabled"] is True


@pytest.mark.asyncio
async def test_unknown_mode_dispatches_as_briefed_warm_transfer(
    monkeypatch, warnings_sink
):
    agent, _, data = _dispatch_agent(monkeypatch)
    reason_task = AsyncMock(
        return_value=TransferReasonResult(purpose=TransferPurpose(code="unprovided"))
    )
    monkeypatch.setattr("agents.base_agent.TransferReasonTask", reason_task)

    await agent._dispatch_transfer(data, "1004", "bogus", direct=False)

    reason_task.assert_awaited_once()
    assert agent._handle_warm_transfer_call.await_args.kwargs["briefing_enabled"]
    assert [r["extra"]["event"] for r in warnings_sink] == ["TRANSFER_MODE_UNKNOWN"]


# --- Section 3: warm handler (fakeredis queue, fake single-room task) --------


class _FakeRoom:
    def __init__(self) -> None:
        self.name = "synthetic-room"
        self.remote_participants = {SIP_IDENTITY: object()}
        self.local_participant = SimpleNamespace(identity="ai-agent")
        self.handlers: dict[str, list] = {}

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def off(self, event, handler):
        handlers = self.handlers.get(event, [])
        if handler in handlers:
            handlers.remove(handler)


class _FakeHoldMusicPlayer:
    def __init__(self) -> None:
        self.start = AsyncMock()
        self.play = Mock(return_value=SimpleNamespace(stop=Mock()))
        self.aclose = AsyncMock()


class _FakeTrunkClient:
    async def check(self, company_id):
        return TrunkCheckResult(status=TrunkStatus.AVAILABLE, status_code=200)


@pytest.fixture
async def handler_env(monkeypatch):
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = WarmTransferQueue(redis_client)
    room = _FakeRoom()
    job_ctx = SimpleNamespace(
        room=room,
        api=SimpleNamespace(
            room=SimpleNamespace(
                remove_participant=AsyncMock(), delete_room=AsyncMock()
            )
        ),
    )
    speech_handle = SimpleNamespace(wait_for_playout=AsyncMock())
    session = SimpleNamespace(
        input=SimpleNamespace(set_audio_enabled=Mock()),
        history=SimpleNamespace(add_message=Mock()),
        say=Mock(return_value=speech_handle),
    )
    chat_ctx = object()
    monkeypatch.setattr(BaseAgent, "session", property(lambda _: session))
    monkeypatch.setattr(BaseAgent, "chat_ctx", property(lambda _: chat_ctx))
    monkeypatch.setattr(base_agent_module, "get_job_context", lambda: job_ctx)
    monkeypatch.setattr(
        base_agent_module, "get_warm_transfer_queue", AsyncMock(return_value=queue)
    )
    monkeypatch.setattr(
        base_agent_module,
        "get_trunk_allocation_client",
        AsyncMock(return_value=_FakeTrunkClient()),
    )
    monkeypatch.setattr(
        base_agent_module, "BackgroundAudioPlayer", _FakeHoldMusicPlayer
    )
    monkeypatch.setattr(base_agent_module, "WT_RETRY_COOLDOWN_SECONDS", 0)
    generate_briefing = AsyncMock(return_value="브리핑")
    extract_history = Mock(return_value="history")
    monkeypatch.setattr(
        base_agent_module, "generate_warm_transfer_briefing_text", generate_briefing
    )
    monkeypatch.setattr(
        base_agent_module,
        "extract_warm_transfer_conversation_history",
        extract_history,
    )
    task_cls = Mock()
    monkeypatch.setattr(base_agent_module, "SingleRoomWarmTransferTask", task_cls)

    agent = object.__new__(BaseAgent)
    agent._handle_leave_message = AsyncMock()
    agent.hangup = AsyncMock()

    call_session_data = SimpleNamespace(
        business_data=SimpleNamespace(client_id=COMPANY_ID, is_work_time=True),
        recipient_data=SimpleNamespace(
            phone="01000000000",
            sip_identity=SIP_IDENTITY,
            user_name=None,
            verification_code=None,
            customer_no=None,
            customer_visit="NEW",
        ),
        room_name=room.name,
        call_config_data=SimpleNamespace(
            transfer_message=None,
            language="ko-KR",
            sip_outbound_trunk_id="trunk",
        ),
        booking_memo_context=None,
        transfer_record=None,
        is_warm_transferring=False,
    )
    env = SimpleNamespace(
        agent=agent,
        data=call_session_data,
        queue=queue,
        redis=redis_client,
        room=room,
        job_ctx=job_ctx,
        session=session,
        chat_ctx=chat_ctx,
        task_cls=task_cls,
        generate_briefing=generate_briefing,
        extract_history=extract_history,
    )
    try:
        yield env
    finally:
        await redis_client.aclose()


async def _stream_events(env) -> list[str]:
    entries = await env.redis.xrange(env.queue.event_stream_key(COMPANY_ID))
    return [fields["event"] for _, fields in entries]


def _started_details(env) -> dict:
    for call in env.session.history.add_message.call_args_list:
        event = (call.kwargs.get("extra") or {}).get("agent_event", {})
        if event.get("status") == "STARTED":
            return event["details"]
    raise AssertionError("STARTED event not recorded")


def _install_monitor(env) -> dict:
    """Replace the monitor and record queue occupancy while it runs."""
    state: dict = {}

    async def _monitor(*, caller_identity, agent_identity):
        state["rank_during_monitor"] = await env.queue.get_rank(
            COMPANY_ID, SIP_IDENTITY
        )
        state["args"] = (caller_identity, agent_identity)

    env.agent._monitor_transfer_conversation = _monitor
    return state


def _success() -> WarmTransferResult:
    return WarmTransferResult(
        status=WarmTransferStatus.SUCCESS, agent_identity=AGENT_IDENTITY
    )


@pytest.mark.asyncio
async def test_no_briefing_handler_success_skips_briefing(handler_env):
    env = handler_env
    env.task_cls.return_value = _success()
    monitor_state = _install_monitor(env)

    await env.agent._handle_warm_transfer_call(
        env.data, transfer_to="1004", briefing_enabled=False
    )

    env.generate_briefing.assert_not_called()
    env.extract_history.assert_not_called()
    env.task_cls.assert_called_once()
    kwargs = env.task_cls.call_args.kwargs
    assert kwargs["briefing_enabled"] is False
    assert kwargs["queued_briefing_future"] is None
    assert kwargs["chat_ctx"] is None

    events = await _stream_events(env)
    assert "briefing_ready" not in events
    assert "agent_answered" not in events
    assert events.count("connected") == 1
    assert events[-1] == "dequeued"

    entry = await env.queue.get_entry(COMPANY_ID, SIP_IDENTITY)
    assert entry["briefing_text"] == ""
    assert entry["status"] == "completed"

    # Slot held while the conversation runs, released only after the monitor.
    assert monitor_state["rank_during_monitor"] == 0
    assert monitor_state["args"] == (SIP_IDENTITY, AGENT_IDENTITY)
    assert await env.queue.get_rank(COMPANY_ID, SIP_IDENTITY) is None

    assert env.data.transfer_record.transfer_status == "success"
    assert env.data.transfer_record.briefing_text is None
    assert _started_details(env) == {"target": "1004", "briefing": False}
    env.agent._handle_leave_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_briefing_handler_retry_keeps_front_slot(handler_env):
    env = handler_env
    env.task_cls.side_effect = [
        WarmTransferResult(status=WarmTransferStatus.ERROR),
        _success(),
    ]
    monitor_state = _install_monitor(env)

    await env.agent._handle_warm_transfer_call(
        env.data, transfer_to="1004", briefing_enabled=False
    )

    assert env.task_cls.call_count == 2
    for call in env.task_cls.call_args_list:
        assert call.kwargs["briefing_enabled"] is False
        assert call.kwargs["queued_briefing_future"] is None
        assert call.kwargs["chat_ctx"] is None

    events = await _stream_events(env)
    assert events.count("dial_started") == 2
    assert "dial_failed" in events
    assert "briefing_ready" not in events
    assert "agent_answered" not in events
    # No dequeue between the ERROR and the second dial: the slot was kept.
    assert "dequeued" not in events[: events.index("connected")]
    assert events[-1] == "dequeued"
    assert monitor_state["rank_during_monitor"] == 0
    assert await env.queue.get_rank(COMPANY_ID, SIP_IDENTITY) is None
    env.generate_briefing.assert_not_called()


@pytest.mark.asyncio
async def test_briefed_handler_still_generates_briefing(handler_env):
    env = handler_env
    env.task_cls.return_value = _success()
    _install_monitor(env)

    await env.agent._handle_warm_transfer_call(env.data, transfer_to="1004")

    env.extract_history.assert_called_once_with(env.chat_ctx)
    env.generate_briefing.assert_awaited_once()
    kwargs = env.task_cls.call_args.kwargs
    assert kwargs["briefing_enabled"] is True
    assert kwargs["queued_briefing_future"] is not None
    assert kwargs["chat_ctx"] is env.chat_ctx
    assert "briefing_ready" in await _stream_events(env)
    assert _started_details(env) == {"target": "1004", "briefing": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "purpose", [None, TransferPurpose(code="urgent_handoff")], ids=["none", "known"]
)
async def test_no_briefing_failure_falls_back_to_leave_message(
    handler_env, monkeypatch, purpose
):
    env = handler_env
    reason_task = AsyncMock()
    monkeypatch.setattr(base_agent_module, "TransferReasonTask", reason_task)
    env.task_cls.return_value = WarmTransferResult(status=WarmTransferStatus.DECLINED)

    await env.agent._dispatch_transfer(
        env.data,
        "1004",
        "warm_transfer_no_briefing",
        direct=False,
        transfer_purpose=purpose,
    )

    reason_task.assert_not_called()
    env.generate_briefing.assert_not_called()
    env.agent._handle_leave_message.assert_awaited_once_with(
        env.data,
        is_after_transfer_failure=True,
        entry_reason="final_failure",
        action_key=None,
        transfer_purpose=purpose,
        booking_context=None,
    )
    assert await env.queue.get_rank(COMPANY_ID, SIP_IDENTITY) is None


@pytest.mark.asyncio
async def test_agent_cleanup_failure_ends_call_without_retry(handler_env):
    env = handler_env
    env.task_cls.return_value = WarmTransferResult(
        status=WarmTransferStatus.ERROR,
        agent_identity=AGENT_IDENTITY,
        error_message="agent_cleanup_failed",
        terminate_call=True,
    )
    env.agent._restore_caller_ai_audio = AsyncMock()

    await asyncio.wait_for(
        env.agent._handle_warm_transfer_call(
            env.data, transfer_to="1004", briefing_enabled=False
        ),
        timeout=5,
    )

    env.task_cls.assert_called_once()
    events = await asyncio.wait_for(_stream_events(env), timeout=5)
    assert events.count("dial_started") == 1
    assert "dial_failed" not in events
    assert events[-1] == "dequeued"
    assert await env.queue.get_rank(COMPANY_ID, SIP_IDENTITY) is None

    env.agent.hangup.assert_awaited_once()
    env.agent._handle_leave_message.assert_not_awaited()
    env.agent._restore_caller_ai_audio.assert_not_awaited()
    env.session.say.assert_not_called()
    assert env.data.is_warm_transferring is False
    assert env.data.transfer_record.transfer_status == "agent_cleanup_failed"

    failed = [
        call
        for call in env.session.history.add_message.call_args_list
        if (call.kwargs.get("extra") or {}).get("agent_event", {}).get("status")
        == "FAILED"
    ]
    assert len(failed) == 1
    assert failed[0].kwargs["content"] == "[WARM_TRANSFER_CALL: STATUS=FAILED]"
    details = failed[0].kwargs["extra"]["agent_event"]["details"]
    assert details["reason"] == "agent_cleanup_failed"
    assert details["target"] == "1004"
    assert "wait_seconds" in details


async def _bounded(coro):
    return await asyncio.wait_for(coro, timeout=5)


def _cleanup_failed(
    status: WarmTransferStatus = WarmTransferStatus.ERROR,
) -> WarmTransferResult:
    return WarmTransferResult(
        status=status,
        agent_identity=AGENT_IDENTITY,
        error_message="agent_cleanup_failed",
        terminate_call=True,
    )


def _record_order(env, monkeypatch) -> list[str]:
    """Record delete_room / dequeue / hangup in the order they happen."""
    calls: list[str] = []
    original_dequeue = env.queue.dequeue

    async def _dequeue(*args, **kwargs):
        calls.append(f"dequeue:{kwargs.get('reason')}")
        return await original_dequeue(*args, **kwargs)

    async def _delete_room(request):
        calls.append(f"delete_room:{request.room}")

    async def _hangup():
        calls.append("hangup")

    monkeypatch.setattr(env.queue, "dequeue", _dequeue)
    env.job_ctx.api.room.delete_room.side_effect = _delete_room
    env.agent.hangup = AsyncMock(side_effect=_hangup)
    return calls


def _caller_leaves_during_dial(env, result: WarmTransferResult, state: dict):
    """Fake task: fire the caller-disconnect listener mid-dial, then finish."""

    async def _dial():
        (handler,) = env.room.handlers["participant_disconnected"]
        handler(SimpleNamespace(identity=SIP_IDENTITY))
        for _ in range(3):
            await asyncio.sleep(0)
        state["rank_after_listener"] = await env.queue.get_rank(
            COMPANY_ID, SIP_IDENTITY
        )
        return result

    return lambda **_: _dial()


def _pending_refresh_tasks() -> list[asyncio.Task]:
    return [
        task
        for task in asyncio.all_tasks()
        if not task.done() and "_refresh_entry_ttl" in task.get_coro().__qualname__
    ]


def _failed_status(env) -> str:
    return env.data.transfer_record.transfer_status


@pytest.mark.asyncio
async def test_agent_cleanup_failure_deletes_room_before_dequeue(
    handler_env, monkeypatch
):
    env = handler_env
    env.task_cls.return_value = _cleanup_failed()
    calls = _record_order(env, monkeypatch)

    await asyncio.wait_for(
        env.agent._handle_warm_transfer_call(
            env.data, transfer_to="1004", briefing_enabled=False
        ),
        timeout=5,
    )

    assert calls == ["delete_room:synthetic-room", "dequeue:error", "hangup"]
    env.task_cls.assert_called_once()
    env.job_ctx.api.room.delete_room.assert_awaited_once()
    env.agent.hangup.assert_awaited_once()
    env.agent._handle_leave_message.assert_not_awaited()
    assert await _bounded(env.queue.get_rank(COMPANY_ID, SIP_IDENTITY)) is None
    assert _failed_status(env) == "agent_cleanup_failed"


@pytest.mark.asyncio
async def test_caller_leaving_during_cleanup_still_terminates(handler_env, monkeypatch):
    env = handler_env
    state: dict = {}
    env.task_cls.side_effect = _caller_leaves_during_dial(
        env, _cleanup_failed(WarmTransferStatus.CALLER_DISCONNECTED), state
    )
    calls = _record_order(env, monkeypatch)

    await asyncio.wait_for(
        env.agent._handle_warm_transfer_call(
            env.data, transfer_to="1004", briefing_enabled=False
        ),
        timeout=5,
    )

    # The listener did not release the slot while the dial was in flight.
    assert state["rank_after_listener"] == 0
    assert calls == ["delete_room:synthetic-room", "dequeue:error", "hangup"]
    env.agent.hangup.assert_awaited_once()
    env.agent._handle_leave_message.assert_not_awaited()
    assert env.data.is_warm_transferring is False
    assert _failed_status(env) == "agent_cleanup_failed"
    statuses = [
        (call.kwargs.get("extra") or {}).get("agent_event", {}).get("status")
        for call in env.session.history.add_message.call_args_list
    ]
    assert "FAILED" in statuses
    assert "CALLER_DISCONNECTED" not in statuses


@pytest.mark.asyncio
async def test_room_delete_failure_holds_queue_slot(handler_env, monkeypatch):
    env = handler_env
    monkeypatch.setattr(base_agent_module, "WT_ROOM_DELETE_TIMEOUT_SECONDS", 0.01)
    refresh_during_dial: list[asyncio.Task] = []

    async def _dial():
        refresh_during_dial.extend(_pending_refresh_tasks())
        return _cleanup_failed()

    env.task_cls.side_effect = lambda **_: _dial()
    calls = _record_order(env, monkeypatch)
    attempts: list[str] = []

    async def _delete_room(request):
        attempts.append(request.room)
        if len(attempts) == 1:
            raise RuntimeError("room API down")
        await asyncio.Event().wait()  # second attempt hangs until timeout

    env.job_ctx.api.room.delete_room.side_effect = _delete_room

    await asyncio.wait_for(
        env.agent._handle_warm_transfer_call(
            env.data, transfer_to="1004", briefing_enabled=False
        ),
        timeout=5,
    )

    assert attempts == ["synthetic-room", "synthetic-room"]
    assert calls == ["hangup"]  # no dequeue by this call
    assert await _bounded(env.queue.get_rank(COMPANY_ID, SIP_IDENTITY)) == 0
    entry = await _bounded(env.queue.get_entry(COMPANY_ID, SIP_IDENTITY))
    assert entry["status"] != "completed"
    assert len(refresh_during_dial) == 1
    assert refresh_during_dial[0].cancelled()
    assert _pending_refresh_tasks() == []
    env.agent.hangup.assert_awaited_once()
    env.agent._handle_leave_message.assert_not_awaited()
    assert _failed_status(env) == "agent_cleanup_failed"


@pytest.mark.asyncio
async def test_caller_leaving_during_normal_dial_dequeues_after_task(
    handler_env, monkeypatch
):
    env = handler_env
    state: dict = {}
    env.task_cls.side_effect = _caller_leaves_during_dial(
        env, WarmTransferResult(status=WarmTransferStatus.CALLER_DISCONNECTED), state
    )
    calls = _record_order(env, monkeypatch)

    await asyncio.wait_for(
        env.agent._handle_warm_transfer_call(
            env.data, transfer_to="1004", briefing_enabled=False
        ),
        timeout=5,
    )

    assert state["rank_after_listener"] == 0
    assert calls == ["dequeue:disconnect"]
    assert await _bounded(env.queue.get_rank(COMPANY_ID, SIP_IDENTITY)) is None
    env.job_ctx.api.room.delete_room.assert_not_awaited()
    env.agent.hangup.assert_not_awaited()
    env.agent._handle_leave_message.assert_not_awaited()
    assert _failed_status(env) == "caller_disconnected"


# --- Section 3: monitor presence re-check -----------------------------------


def _monitor_env(monkeypatch, present: set[str]):
    room = _FakeRoom()
    room.remote_participants = {identity: object() for identity in present}
    remove_participant = AsyncMock()
    job_ctx = SimpleNamespace(
        room=room,
        api=SimpleNamespace(
            room=SimpleNamespace(remove_participant=remove_participant)
        ),
    )
    monkeypatch.setattr(base_agent_module, "get_job_context", lambda: job_ctx)
    return object.__new__(BaseAgent), room, remove_participant


def _removed_identities(remove_participant: AsyncMock) -> list[str]:
    return [call.args[0].identity for call in remove_participant.await_args_list]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("absent", "remaining"),
    [(SIP_IDENTITY, AGENT_IDENTITY), (AGENT_IDENTITY, SIP_IDENTITY)],
    ids=["caller-absent", "agent-absent"],
)
async def test_monitor_treats_already_absent_participant_as_disconnect(
    monkeypatch, absent, remaining
):
    agent, room, remove_participant = _monitor_env(
        monkeypatch, {SIP_IDENTITY, AGENT_IDENTITY} - {absent}
    )

    await asyncio.wait_for(
        agent._monitor_transfer_conversation(
            caller_identity=SIP_IDENTITY, agent_identity=AGENT_IDENTITY
        ),
        timeout=1.0,
    )

    assert _removed_identities(remove_participant) == [remaining, "ai-agent"]
    assert room.handlers["participant_disconnected"] == []


@pytest.mark.asyncio
async def test_monitor_waits_for_disconnect_when_both_present(monkeypatch):
    agent, room, remove_participant = _monitor_env(
        monkeypatch, {SIP_IDENTITY, AGENT_IDENTITY}
    )

    monitor = asyncio.create_task(
        agent._monitor_transfer_conversation(
            caller_identity=SIP_IDENTITY, agent_identity=AGENT_IDENTITY
        )
    )
    await asyncio.sleep(0)
    assert not monitor.done()
    remove_participant.assert_not_awaited()

    (handler,) = room.handlers["participant_disconnected"]
    handler(
        SimpleNamespace(
            identity=AGENT_IDENTITY,
            metadata="",
            attributes={},
            name="",
            kind=None,
            sid="PA_1",
        )
    )
    await asyncio.wait_for(monitor, timeout=1.0)

    assert _removed_identities(remove_participant) == [SIP_IDENTITY, "ai-agent"]
