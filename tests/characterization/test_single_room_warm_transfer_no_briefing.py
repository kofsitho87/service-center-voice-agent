"""SingleRoomWarmTransferTask ``briefing_enabled=False`` (auto-connect) tests."""

import asyncio
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from livekit import rtc

import tasks.single_room_warm_transfer_task as warm_transfer_module
from tasks.single_room_warm_transfer_task import (
    SingleRoomWarmTransferTask,
    WarmTransferStatus,
)

CALLER = "caller-test"
TARGET_PHONE = "01000000000"
AGENT = f"human-agent-{TARGET_PHONE}"
# Upper bound per awaited step so a regression fails instead of hanging the suite.
TEST_TIMEOUT_SECONDS = 5.0


async def _guard(coro):
    return await asyncio.wait_for(coro, timeout=TEST_TIMEOUT_SECONDS)


class _FakeRoom:
    def __init__(self) -> None:
        self.name = "test-room"
        self.remote_participants: dict[str, SimpleNamespace] = {}
        self.local_participant = SimpleNamespace(
            set_track_subscription_permissions=MagicMock(),
            track_publications={},
        )
        self.handlers: dict[str, list] = defaultdict(list)

    def on(self, event, handler=None):
        self.handlers[event].append(handler)
        return handler

    def off(self, event, handler) -> None:
        if handler in self.handlers[event]:
            self.handlers[event].remove(handler)

    def emit(self, event, *args) -> None:
        for handler in list(self.handlers[event]):
            handler(*args)

    def add_participant(self, identity: str, audio_sids: list[str]):
        participant = SimpleNamespace(
            identity=identity,
            track_publications={
                sid: SimpleNamespace(
                    sid=sid, kind=rtc.TrackKind.KIND_AUDIO, set_subscribed=MagicMock()
                )
                for sid in audio_sids
            },
        )
        self.remote_participants[identity] = participant
        return participant

    def disconnect(self, identity: str) -> None:
        participant = self.remote_participants.pop(identity)
        self.emit("participant_disconnected", participant)


class _Harness:
    def __init__(self) -> None:
        self.room = _FakeRoom()
        self.api_calls: list[tuple] = []
        self.room_api = SimpleNamespace(
            mute_published_track=AsyncMock(side_effect=self._record("mute")),
            update_subscriptions=AsyncMock(side_effect=self._record("subscriptions")),
            remove_participant=AsyncMock(side_effect=self._record("remove")),
        )
        self.sip_api = SimpleNamespace(create_sip_participant=AsyncMock())
        self.job_ctx = SimpleNamespace(
            room=self.room, api=SimpleNamespace(room=self.room_api, sip=self.sip_api)
        )
        self.session = SimpleNamespace(
            input=SimpleNamespace(set_audio_enabled=MagicMock()),
            say=MagicMock(),
            interrupt=MagicMock(),
        )
        self.task = SingleRoomWarmTransferTask(
            target_phone_number=TARGET_PHONE,
            sip_trunk_id="trunk",
            caller_identity=CALLER,
            external_hold_music_player=SimpleNamespace(publication=None),
            restore_caller_ai_audio_on_failure=False,
            briefing_enabled=False,
        )
        self.task._get_activity_or_raise = lambda: SimpleNamespace(session=self.session)
        self.complete_once_calls = 0
        original_complete_once = self.task._complete_once

        def _counting_complete_once(result):
            self.complete_once_calls += 1
            return original_complete_once(result)

        self.task._complete_once = _counting_complete_once

    def _record(self, name: str):
        def _side_effect(request):
            self.api_calls.append((name, request))

        return _side_effect

    def claim(self) -> None:
        """Set up the state that on_enter leaves right before auto-connect."""
        self.task._room = self.room
        self.task._job_ctx = self.job_ctx
        self.task._agent_identity = AGENT
        self.task._agent_answered = True
        self.task._room.on(
            "participant_disconnected", self.task._on_participant_disconnected
        )
        self.task._start_blocking_agent_tracks_from_caller()
        self.task._auto_connect_claimed = True
        self.task._dtmf_completed = True

    @property
    def result(self):
        return self.task._AgentTask__fut.result()

    def subscription_calls(self) -> list:
        return [req for name, req in self.api_calls if name == "subscriptions"]

    async def drain_background(self) -> None:
        while self.task._background_tasks:
            await asyncio.gather(*self.task._background_tasks, return_exceptions=True)


@pytest.fixture
async def harness(monkeypatch) -> _Harness:
    # Async so AgentTask's internal Future binds to the test's running loop.
    h = _Harness()
    monkeypatch.setattr(warm_transfer_module, "get_job_context", lambda: h.job_ctx)
    # The staff dial goes through services.fake_transfer_gateway
    # (``create_sip_participant(job_ctx, request)``) instead of job_ctx.api.sip.
    # Route that boundary to the harness's SIP mock so the scenarios can run.
    h.gateway_job_ctxs = []

    async def _gateway_create_sip_participant(job_ctx, request):
        h.gateway_job_ctxs.append(job_ctx)
        return await h.sip_api.create_sip_participant(request)

    monkeypatch.setattr(
        warm_transfer_module,
        "fake_transfer_gateway",
        SimpleNamespace(create_sip_participant=_gateway_create_sip_participant),
    )
    return h


def _assert_single_completion(h: _Harness, status: WarmTransferStatus) -> None:
    assert h.task._AgentTask__fut.done()
    assert h.result.status is status
    assert h.complete_once_calls == 1


def _assert_agent_removed(h: _Harness) -> None:
    removed = [req.identity for name, req in h.api_calls if name == "remove"]
    assert removed == [AGENT]


# ── Constructor ──────────────────────────────────────────────────────


async def test_constructor_skips_history_extraction_when_briefing_disabled(
    monkeypatch,
):
    extract = MagicMock(return_value="history")
    monkeypatch.setattr(
        SingleRoomWarmTransferTask,
        "_extract_conversation_history",
        staticmethod(extract),
    )
    chat_ctx = object()

    task = SingleRoomWarmTransferTask(
        target_phone_number=TARGET_PHONE,
        sip_trunk_id="trunk",
        caller_identity=CALLER,
        chat_ctx=chat_ctx,
        briefing_enabled=False,
    )

    extract.assert_not_called()
    assert task._conversation_history == ""
    assert task._briefing_enabled is False

    briefed = SingleRoomWarmTransferTask(
        target_phone_number=TARGET_PHONE,
        sip_trunk_id="trunk",
        caller_identity=CALLER,
        chat_ctx=chat_ctx,
    )
    extract.assert_called_once_with(chat_ctx)
    assert briefed._conversation_history == "history"
    assert briefed._briefing_enabled is True


# ── on_enter ─────────────────────────────────────────────────────────


async def test_on_enter_no_briefing_skips_briefing_and_dtmf(harness):
    h = harness
    task = h.task
    order: list[tuple[str, str | None]] = []

    task._prepare_briefing_text = AsyncMock()
    task._setup_briefing_audio = AsyncMock()
    task._notify_realtime_update = AsyncMock()
    task._isolate_caller = AsyncMock()
    task._mute_caller_audio = AsyncMock()
    task._start_hold_music = AsyncMock()
    task._start_blocking_agent_tracks_from_caller = MagicMock(
        side_effect=lambda: order.append(("listen", task._agent_identity))
    )
    task._dial_agent = AsyncMock(side_effect=lambda: order.append(("dial", None)))
    task._wait_for_agent_connection = AsyncMock(return_value=True)

    async def _auto_connect():
        order.append(("auto_connect", None))
        assert task._auto_connect_claimed is True
        assert task._dtmf_completed is True

    task._execute_auto_connect = AsyncMock(side_effect=_auto_connect)

    await _guard(task.on_enter())

    assert order == [("listen", AGENT), ("dial", None), ("auto_connect", None)]
    task._prepare_briefing_text.assert_not_called()
    task._setup_briefing_audio.assert_not_called()
    task._notify_realtime_update.assert_not_called()
    h.session.say.assert_not_called()
    assert h.room.handlers["sip_dtmf_received"] == []
    assert task._dtmf_timeout_task is None


async def test_on_enter_no_briefing_end_to_end_success(harness, monkeypatch):
    h = harness
    h.room.add_participant(CALLER, ["TR_caller"])
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def _spy_sleep(delay, *args, **kwargs):
        sleeps.append(delay)
        return await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", _spy_sleep)

    async def _create_sip_participant(request):
        # A track published during ringing must hit the pre-registered listener.
        agent = h.room.add_participant(AGENT, ["TR_agent"])
        h.room.emit("participant_connected", agent)
        h.room.emit("track_published", agent.track_publications["TR_agent"], agent)

    h.sip_api.create_sip_participant.side_effect = _create_sip_participant
    h.task._prepare_briefing_text = AsyncMock()

    await _guard(h.task.on_enter())

    _assert_single_completion(h, WarmTransferStatus.SUCCESS)
    assert h.result.agent_identity == AGENT
    assert h.gateway_job_ctxs == [h.job_ctx]
    dial_request = h.sip_api.create_sip_participant.await_args.args[0]
    assert dial_request.sip_call_to == TARGET_PHONE
    assert dial_request.participant_identity == AGENT
    h.task._prepare_briefing_text.assert_not_called()
    h.session.say.assert_not_called()
    assert h.room.handlers["sip_dtmf_received"] == []
    assert 0.8 not in sleeps
    unmutes = [req for name, req in h.api_calls if name == "mute" and not req.muted]
    assert [req.track_sid for req in unmutes] == ["TR_caller"]
    subscribes = [req for req in h.subscription_calls() if req.subscribe]
    assert [(r.identity, list(r.track_sids)) for r in subscribes] == [
        (CALLER, ["TR_agent"]),
        (AGENT, ["TR_caller"]),
    ]


async def test_on_enter_exception_after_claim_fails_auto_connect(harness):
    h = harness
    h.room.add_participant(CALLER, ["TR_caller"])
    h.room.add_participant(AGENT, ["TR_agent"])
    task = h.task
    task._isolate_caller = AsyncMock()
    task._mute_caller_audio = AsyncMock()
    task._start_hold_music = AsyncMock()
    task._dial_agent = AsyncMock()
    task._wait_for_agent_connection = AsyncMock(return_value=True)
    task._execute_auto_connect = AsyncMock(side_effect=RuntimeError("boom"))

    await _guard(task.on_enter())

    _assert_single_completion(h, WarmTransferStatus.ERROR)
    assert h.result.error_message == "setup_exception"
    _assert_agent_removed(h)


# ── _execute_auto_connect outcomes ───────────────────────────────────


async def test_auto_connect_success_bridges_and_releases_ai(harness, monkeypatch):
    h = harness
    caller = h.room.add_participant(CALLER, ["TR_caller"])
    agent = h.room.add_participant(AGENT, ["TR_agent"])
    h.claim()
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def _spy_sleep(delay, *args, **kwargs):
        sleeps.append(delay)
        return await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", _spy_sleep)

    await _guard(h.task._execute_auto_connect())

    _assert_single_completion(h, WarmTransferStatus.SUCCESS)
    assert 0.8 not in sleeps
    assert not any(name == "remove" for name, _ in h.api_calls)
    h.room.local_participant.set_track_subscription_permissions.assert_called_with(
        allow_all_participants=False, participant_permissions=[]
    )
    caller.track_publications["TR_caller"].set_subscribed.assert_called_with(False)
    agent.track_publications["TR_agent"].set_subscribed.assert_called_with(False)
    assert h.room.handlers["track_published"] == []


async def test_auto_connect_agent_hangup_during_audio_wait_is_declined(harness):
    h = harness
    h.room.add_participant(CALLER, ["TR_caller"])
    h.room.add_participant(AGENT, [])
    h.claim()

    async def _agent_hangs_up():
        h.room.disconnect(AGENT)
        return False

    h.task._wait_for_agent_audio_ready = _agent_hangs_up

    await _guard(h.task._execute_auto_connect())
    await _guard(h.drain_background())

    _assert_single_completion(h, WarmTransferStatus.DECLINED)
    assert h.task._agent_left_during_auto_connect is True
    assert not any(name == "remove" for name, _ in h.api_calls)


async def test_auto_connect_caller_disconnect_during_bridge(harness):
    h = harness
    h.room.add_participant(CALLER, ["TR_caller"])
    h.room.add_participant(AGENT, ["TR_agent"])
    h.claim()

    async def _update_subscriptions(request):
        h.api_calls.append(("subscriptions", request))
        if request.subscribe and request.identity == CALLER:
            h.room.disconnect(CALLER)

    h.room_api.update_subscriptions.side_effect = _update_subscriptions

    await _guard(h.task._execute_auto_connect())
    await _guard(h.drain_background())

    _assert_single_completion(h, WarmTransferStatus.CALLER_DISCONNECTED)
    _assert_agent_removed(h)


async def test_auto_connect_drains_block_tasks_before_subscribe(harness):
    h = harness
    h.room.add_participant(CALLER, ["TR_caller"])
    agent = h.room.add_participant(AGENT, ["TR_agent"])
    h.claim()
    events: list[str] = []

    async def _update_subscriptions(request):
        if not request.subscribe:
            await asyncio.sleep(0.05)
            events.append(f"block:{list(request.track_sids)}")
        else:
            events.append(f"subscribe:{request.identity}")

    h.room_api.update_subscriptions.side_effect = _update_subscriptions
    # A late-published track spawns a delayed block task via the listener.
    h.room.emit("track_published", agent.track_publications["TR_agent"], agent)

    await _guard(h.task._execute_auto_connect())

    _assert_single_completion(h, WarmTransferStatus.SUCCESS)
    first_subscribe = events.index(f"subscribe:{CALLER}")
    blocks = [i for i, e in enumerate(events) if e.startswith("block:")]
    assert len(blocks) == 2  # listener task + step-0 task
    assert max(blocks) < first_subscribe


async def test_auto_connect_empty_block_set_succeeds(harness):
    h = harness
    h.room.add_participant(CALLER, ["TR_caller"])
    agent = h.room.add_participant(AGENT, [])
    h.claim()

    async def _audio_ready():
        # Track appears without a track_published event: no block task in flight.
        agent.track_publications["TR_agent"] = SimpleNamespace(
            sid="TR_agent", kind=rtc.TrackKind.KIND_AUDIO, set_subscribed=MagicMock()
        )
        return True

    h.task._wait_for_agent_audio_ready = _audio_ready

    await _guard(h.task._execute_auto_connect())

    _assert_single_completion(h, WarmTransferStatus.SUCCESS)
    assert h.task._agent_block_tasks == set()


async def test_auto_connect_ai_release_exception_still_succeeds(harness):
    h = harness
    h.room.add_participant(CALLER, ["TR_caller"])
    h.room.add_participant(AGENT, ["TR_agent"])
    h.claim()
    h.room.local_participant.set_track_subscription_permissions.side_effect = (
        RuntimeError("permission API down")
    )

    await _guard(h.task._execute_auto_connect())

    _assert_single_completion(h, WarmTransferStatus.SUCCESS)
    assert not any(name == "remove" for name, _ in h.api_calls)


# ── Failure paths → exactly one ERROR, agent removed ─────────────────


def _hang_forever():
    async def _side_effect(request):
        await asyncio.Event().wait()

    return _side_effect


@pytest.mark.parametrize(
    ("scenario", "reason"),
    [
        ("audio_not_ready", "agent_audio_not_ready"),
        ("caller_sids_missing", "audio_track_missing"),
        ("agent_sids_missing", "audio_track_missing"),
        ("unmute_raises", "unmute_failed"),
        ("subscribe_caller_side_raises", "subscription_failed"),
        ("subscribe_agent_side_raises", "subscription_failed"),
        ("block_drain_timeout", "block_drain_timeout"),
        ("api_timeout", "unmute_failed"),
        ("unexpected_exception", "auto_connect_exception"),
    ],
)
async def test_auto_connect_failure_paths_complete_once_with_error(
    harness, monkeypatch, scenario, reason
):
    h = harness
    h.room.add_participant(
        CALLER, [] if scenario == "caller_sids_missing" else ["TR_c"]
    )
    agent = h.room.add_participant(
        AGENT, [] if scenario == "agent_sids_missing" else ["TR_a"]
    )
    h.claim()

    if scenario == "audio_not_ready":
        h.task._wait_for_agent_audio_ready = AsyncMock(return_value=False)
    elif scenario == "agent_sids_missing":
        h.task._wait_for_agent_audio_ready = AsyncMock(return_value=True)
    elif scenario in ("unmute_raises", "api_timeout"):
        if scenario == "api_timeout":
            monkeypatch.setattr(
                warm_transfer_module, "AUTO_CONNECT_API_TIMEOUT_SECONDS", 0.01
            )
            unmute_effect = _hang_forever()
        else:
            unmute_effect = RuntimeError("mute API down")

        async def _mute(request):
            h.api_calls.append(("mute", request))
            if not request.muted:
                if isinstance(unmute_effect, Exception):
                    raise unmute_effect
                await unmute_effect(request)

        h.room_api.mute_published_track.side_effect = _mute
    elif scenario.startswith("subscribe_"):
        failing_identity = CALLER if "caller_side" in scenario else AGENT

        async def _subscribe(request):
            h.api_calls.append(("subscriptions", request))
            if request.subscribe and request.identity == failing_identity:
                raise RuntimeError("subscription API down")

        h.room_api.update_subscriptions.side_effect = _subscribe
    elif scenario == "block_drain_timeout":
        monkeypatch.setattr(
            warm_transfer_module, "AUTO_CONNECT_BLOCK_DRAIN_TIMEOUT_SECONDS", 0.01
        )
        h.room_api.update_subscriptions.side_effect = _hang_forever()
        h.room.emit("track_published", agent.track_publications["TR_a"], agent)
    elif scenario == "unexpected_exception":
        h.task._wait_for_agent_audio_ready = AsyncMock(
            side_effect=RuntimeError("unexpected")
        )

    await _guard(h.task._execute_auto_connect())
    await _guard(h.drain_background())

    _assert_single_completion(h, WarmTransferStatus.ERROR)
    assert h.result.error_message == reason
    _assert_agent_removed(h)
    if scenario == "subscribe_agent_side_raises":
        # Caller was unmuted in step 4, so the failure path re-mutes it.
        remutes = [r for n, r in h.api_calls if n == "mute" and r.muted]
        assert [r.track_sid for r in remutes] == ["TR_c"]


async def test_intentional_agent_removal_is_error_not_declined(harness):
    h = harness
    h.room.add_participant(CALLER, ["TR_caller"])
    h.room.add_participant(AGENT, ["TR_agent"])
    h.claim()
    h.task._wait_for_agent_audio_ready = AsyncMock(return_value=False)

    async def _remove(request):
        h.api_calls.append(("remove", request))
        h.room.disconnect(request.identity)  # fires participant_disconnected

    h.room_api.remove_participant.side_effect = _remove

    await _guard(h.task._execute_auto_connect())
    await _guard(h.drain_background())

    _assert_single_completion(h, WarmTransferStatus.ERROR)
    assert h.task._agent_left_during_auto_connect is False
    h.session.interrupt.assert_not_called()


async def test_caller_disconnect_during_fail_auto_connect(harness):
    h = harness
    h.room.add_participant(CALLER, ["TR_caller"])
    h.room.add_participant(AGENT, ["TR_agent"])
    h.claim()
    h.task._wait_for_agent_audio_ready = AsyncMock(return_value=False)

    async def _remove(request):
        h.api_calls.append(("remove", request))
        h.room.disconnect(CALLER)

    h.room_api.remove_participant.side_effect = _remove

    await _guard(h.task._execute_auto_connect())
    await _guard(h.drain_background())

    _assert_single_completion(h, WarmTransferStatus.CALLER_DISCONNECTED)
    _assert_agent_removed(h)


# ── Briefed path unchanged ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("identity", "expected"),
    [
        (AGENT, WarmTransferStatus.DECLINED),
        (CALLER, WarmTransferStatus.CALLER_DISCONNECTED),
    ],
)
async def test_briefed_disconnect_handler_unchanged_when_not_claimed(
    harness, identity, expected
):
    h = harness
    h.task._briefing_enabled = True
    h.task._room = h.room
    h.task._job_ctx = h.job_ctx
    h.task._agent_identity = AGENT
    h.task._agent_answered = True

    h.task._on_participant_disconnected(SimpleNamespace(identity=identity))
    await _guard(h.drain_background())

    assert h.task._auto_connect_claimed is False
    _assert_single_completion(h, expected)


# ── Agent removal after auto-connect failure ─────────────────────────


def _install_removal(h: _Harness, effects: list[str]) -> None:
    """Model remove_participant per attempt; success really removes the agent.

    Effects: "ok" removes the participant, "raise" fails with it still present,
    "hang" never returns (bounded by the API timeout), "raise_absent" drops the
    participant from the room but still raises.
    """
    pending = list(effects)

    async def _remove(request):
        h.api_calls.append(("remove", request))
        effect = pending.pop(0)
        if effect == "ok":
            h.room.remote_participants.pop(request.identity, None)
        elif effect == "raise":
            raise RuntimeError("remove API down")
        elif effect == "hang":
            await asyncio.Event().wait()
        elif effect == "raise_absent":
            h.room.remote_participants.pop(request.identity, None)
            raise RuntimeError("participant not found")

    h.room_api.remove_participant.side_effect = _remove


def _remove_attempts(h: _Harness) -> list[str]:
    return [req.identity for name, req in h.api_calls if name == "remove"]


async def _run_audio_not_ready_failure(h: _Harness) -> None:
    async def _audio_not_ready():
        # Let the step-0 block task finish so later unsubscribes are attributable.
        await h.task._drain_agent_block_tasks()
        return False

    h.task._wait_for_agent_audio_ready = _audio_not_ready
    await _guard(h.task._execute_auto_connect())
    await _guard(h.drain_background())


async def test_removal_retry_success_is_retriable_error(harness):
    h = harness
    h.room.add_participant(CALLER, ["TR_caller"])
    h.room.add_participant(AGENT, ["TR_agent"])
    h.claim()
    _install_removal(h, ["raise", "ok"])

    await _run_audio_not_ready_failure(h)

    _assert_single_completion(h, WarmTransferStatus.ERROR)
    assert h.result.error_message == "agent_audio_not_ready"
    assert h.result.terminate_call is False
    assert _remove_attempts(h) == [AGENT, AGENT]
    assert AGENT not in h.room.remote_participants


async def test_removal_failure_with_agent_present_terminates_call(harness, monkeypatch):
    h = harness
    monkeypatch.setattr(warm_transfer_module, "AUTO_CONNECT_API_TIMEOUT_SECONDS", 0.01)
    h.room.add_participant(CALLER, ["TR_caller"])
    h.room.add_participant(AGENT, ["TR_agent"])
    h.claim()
    _install_removal(h, ["raise", "hang"])

    await _run_audio_not_ready_failure(h)

    _assert_single_completion(h, WarmTransferStatus.ERROR)
    assert h.result.error_message == "agent_cleanup_failed"
    assert h.result.terminate_call is True
    assert _remove_attempts(h) == [AGENT, AGENT]
    assert AGENT in h.room.remote_participants
    last_remove = max(i for i, (name, _) in enumerate(h.api_calls) if name == "remove")
    unsubscribes = [
        (r.identity, list(r.track_sids))
        for name, r in h.api_calls[last_remove + 1 :]
        if name == "subscriptions" and not r.subscribe
    ]
    assert unsubscribes == [(CALLER, ["TR_agent"])]


async def test_removal_raises_but_agent_absent_is_retriable_error(harness):
    h = harness
    h.room.add_participant(CALLER, ["TR_caller"])
    h.room.add_participant(AGENT, ["TR_agent"])
    h.claim()
    _install_removal(h, ["raise_absent"])

    await _run_audio_not_ready_failure(h)

    _assert_single_completion(h, WarmTransferStatus.ERROR)
    assert h.result.error_message == "agent_audio_not_ready"
    assert h.result.terminate_call is False
    assert _remove_attempts(h) == [AGENT]


async def test_partial_subscribe_then_removal_failure_terminates_call(harness):
    h = harness
    h.room.add_participant(CALLER, ["TR_caller"])
    h.room.add_participant(AGENT, ["TR_agent"])
    h.claim()
    _install_removal(h, ["raise", "raise"])

    async def _subscribe(request):
        h.api_calls.append(("subscriptions", request))
        # Caller-side subscribe succeeds, agent-side fails.
        if request.subscribe and request.identity == AGENT:
            raise RuntimeError("subscription API down")

    h.room_api.update_subscriptions.side_effect = _subscribe

    await _guard(h.task._execute_auto_connect())
    await _guard(h.drain_background())

    _assert_single_completion(h, WarmTransferStatus.ERROR)
    assert h.result.error_message == "agent_cleanup_failed"
    assert h.result.terminate_call is True
    assert _remove_attempts(h) == [AGENT, AGENT]
    caller_calls = [
        (r.subscribe, list(r.track_sids))
        for r in h.subscription_calls()
        if r.identity == CALLER
    ]
    # The caller was bridged to the agent, then unsubscribed after cleanup failed.
    assert caller_calls[-2:] == [(True, ["TR_agent"]), (False, ["TR_agent"])]


async def test_caller_leaving_with_unconfirmed_removal_terminates_call(harness):
    h = harness
    h.room.add_participant(CALLER, ["TR_caller"])
    h.room.add_participant(AGENT, ["TR_agent"])
    h.claim()
    effects = ["raise", "raise_caller_left"]

    async def _remove(request):
        h.api_calls.append(("remove", request))
        if effects.pop(0) == "raise_caller_left":
            h.room.disconnect(CALLER)
        raise RuntimeError("remove API down")

    h.room_api.remove_participant.side_effect = _remove

    await _run_audio_not_ready_failure(h)

    # Caller left during cleanup, but the agent leg is still up: the room
    # must still be torn down, so terminate_call wins.
    _assert_single_completion(h, WarmTransferStatus.CALLER_DISCONNECTED)
    assert h.result.terminate_call is True
    assert _remove_attempts(h) == [AGENT, AGENT]
    assert AGENT in h.room.remote_participants


async def test_caller_leaving_after_confirmed_removal_does_not_terminate(harness):
    h = harness
    h.room.add_participant(CALLER, ["TR_caller"])
    h.room.add_participant(AGENT, ["TR_agent"])
    h.claim()

    async def _remove(request):
        h.api_calls.append(("remove", request))
        h.room.remote_participants.pop(request.identity, None)
        h.room.disconnect(CALLER)

    h.room_api.remove_participant.side_effect = _remove

    await _run_audio_not_ready_failure(h)

    _assert_single_completion(h, WarmTransferStatus.CALLER_DISCONNECTED)
    assert h.result.terminate_call is False
    assert _remove_attempts(h) == [AGENT]
