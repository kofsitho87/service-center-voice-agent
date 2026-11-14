import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from livekit import api

from utils import common


def _egress_info(status: int, *, started_at: int = 0):
    return SimpleNamespace(
        egress_id="egress-1",
        room_name="room-1",
        status=status,
        started_at=started_at,
    )


@pytest.mark.asyncio
async def test_wait_for_egress_active_returns_observed_active_status():
    starting = _egress_info(api.EgressStatus.EGRESS_STARTING)
    active = _egress_info(api.EgressStatus.EGRESS_ACTIVE)
    list_egress = AsyncMock(return_value=SimpleNamespace(items=[active]))
    lkapi = SimpleNamespace(egress=SimpleNamespace(list_egress=list_egress))

    result = await common.wait_for_egress_active(
        lkapi,
        starting,
        timeout=1.0,
        poll_interval=0.01,
    )

    assert result is active
    list_egress.assert_awaited_once()


def test_capture_recording_started_at_uses_local_clock(monkeypatch):
    active = _egress_info(
        api.EgressStatus.EGRESS_ACTIVE,
        started_at=122_900_000_000,
    )
    monkeypatch.setattr(common.time, "time", lambda: 123.0)

    result = common.capture_recording_started_at(active, room_name="room-1")

    assert result == 123.0


def test_capture_recording_started_at_returns_none_before_active():
    starting = _egress_info(api.EgressStatus.EGRESS_STARTING)

    result = common.capture_recording_started_at(starting, room_name="room-1")

    assert result is None


@pytest.mark.asyncio
async def test_observer_updates_late_recording_start(monkeypatch):
    starting = _egress_info(api.EgressStatus.EGRESS_STARTING)
    active = _egress_info(
        api.EgressStatus.EGRESS_ACTIVE,
        started_at=122_900_000_000,
    )
    lkapi = SimpleNamespace(egress=SimpleNamespace())
    call_session_data = SimpleNamespace(
        lkapi_ref={"instance": lkapi, "egress_info": starting},
        recording_started_at=None,
    )
    ctx = SimpleNamespace(room=SimpleNamespace(name="room-1"))
    wait_for_active = AsyncMock(return_value=active)
    monkeypatch.setattr(common, "wait_for_egress_active", wait_for_active)
    monkeypatch.setattr(common.time, "time", lambda: 123.0)

    await common.observe_recording_start(ctx, call_session_data)

    assert call_session_data.lkapi_ref["egress_info"] is active
    assert call_session_data.recording_started_at == 123.0
    wait_for_active.assert_awaited_once_with(
        lkapi,
        starting,
        timeout=common.DEFAULT_EGRESS_ACTIVE_OBSERVE_TIMEOUT_SEC,
    )


@pytest.mark.asyncio
async def test_initialize_recording_starts_background_observer_after_timeout(
    monkeypatch,
):
    starting = _egress_info(api.EgressStatus.EGRESS_STARTING)
    lkapi = SimpleNamespace(egress=SimpleNamespace())
    call_session_data = SimpleNamespace(
        lkapi_ref={},
        recording_filepath=None,
        recording_started_at=None,
    )
    ctx = SimpleNamespace(room=SimpleNamespace(name="room-1"))
    setup_egress = AsyncMock(return_value=(lkapi, starting, "recording.ogg", None))

    async def observe_recording_start(_ctx, session_data):
        session_data.recording_started_at = 123.0

    monkeypatch.setattr(common, "setup_egress", setup_egress)
    monkeypatch.setattr(common, "observe_recording_start", observe_recording_start)

    # initialize_recording is a no-op; the egress-based body lives in
    # _initialize_recording_with_egress.
    await common._initialize_recording_with_egress(ctx, call_session_data)
    observer_task = call_session_data.lkapi_ref["recording_observer_task"]
    await observer_task

    assert call_session_data.recording_filepath == "recording.ogg"
    assert call_session_data.recording_started_at == 123.0
    setup_egress.assert_awaited_once_with(
        ctx,
        call_session_data,
        egress_storage_type="s3",
    )


@pytest.mark.asyncio
async def test_wait_for_recording_anchor_joins_background_observer():
    call_session_data = SimpleNamespace(
        lkapi_ref={},
        recording_started_at=None,
        room_name="room-1",
    )

    async def set_recording_anchor():
        await asyncio.sleep(0)
        call_session_data.recording_started_at = 123.0

    observer_task = asyncio.create_task(set_recording_anchor())
    call_session_data.lkapi_ref["recording_observer_task"] = observer_task

    await common.wait_for_recording_anchor(call_session_data, timeout=1.0)

    assert observer_task.done()
    assert call_session_data.recording_started_at == 123.0


@pytest.mark.asyncio
@pytest.mark.parametrize("storage_type", ["s3", "local"])
@pytest.mark.parametrize("observed_active", [True, False])
async def test_setup_egress_uses_unified_api_and_preserves_recording_contract(
    monkeypatch, tmp_path, storage_type, observed_active
):
    ctx = SimpleNamespace(room=SimpleNamespace(name="room+1"))
    data = SimpleNamespace(business_data=SimpleNamespace(client_id="client-1"))
    starting = _egress_info(api.EgressStatus.EGRESS_STARTING)
    active = _egress_info(api.EgressStatus.EGRESS_ACTIVE)
    latest = active if observed_active else starting
    start_egress = AsyncMock(return_value=starting)
    legacy_start = AsyncMock(side_effect=AssertionError("Legacy API must not be used"))
    lkapi = SimpleNamespace(
        egress=SimpleNamespace(
            start_egress=start_egress,
            start_room_composite_egress=legacy_start,
        )
    )
    wait_for_active = AsyncMock(return_value=latest)
    monkeypatch.setattr(common.api, "LiveKitAPI", lambda: lkapi)
    monkeypatch.setattr(common, "wait_for_egress_active", wait_for_active)
    monkeypatch.setattr(common.time, "time", lambda: 123.0)
    monkeypatch.setattr(
        common, "__file__", str(tmp_path / "src" / "utils" / "common.py")
    )
    monkeypatch.setenv("ENV", "test")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret-key")
    monkeypatch.setenv("AWS_BUCKET", "test-recordings")
    monkeypatch.setenv("AWS_REGION", "ap-northeast-2")

    client, info, filepath, anchor = await common.setup_egress(
        ctx, data, egress_storage_type=storage_type
    )

    start_egress.assert_awaited_once()
    legacy_start.assert_not_called()
    wait_for_active.assert_awaited_once_with(lkapi, starting)
    assert client is lkapi
    assert info is latest
    assert anchor == (123.0 if observed_active else None)

    request = start_egress.await_args.args[0]
    assert isinstance(request, api.StartEgressRequest)
    request = api.StartEgressRequest.FromString(request.SerializeToString())
    assert request.room_name == "room+1"
    assert request.WhichOneof("source") == "template"
    assert request.template.audio_only
    assert not request.template.video_only
    assert request.template.layout == ""
    assert request.template.custom_base_url == ""
    assert len(request.outputs) == 1
    assert request.outputs[0].WhichOneof("config") == "file"
    assert not request.outputs[0].HasField("storage")
    assert request.outputs[0].file.file_type == api.EncodedFileType.OGG
    assert request.outputs[0].file.filepath == filepath
    assert not request.outputs[0].file.disable_manifest

    if storage_type == "s3":
        assert filepath == "inbound-agent/test/client-1/room_1.ogg"
        assert request.storage.WhichOneof("provider") == "s3"
        assert request.storage.s3.access_key == "test-access-key"
        assert request.storage.s3.secret == "test-secret-key"
        assert request.storage.s3.bucket == "test-recordings"
        assert request.storage.s3.region == "ap-northeast-2"
    else:
        assert not request.HasField("storage")
        path = Path(filepath)
        assert path.parent == tmp_path / "recordings" / "room_1"
        assert path.parent.is_dir()
        assert path.name.startswith("recording-")
        assert path.suffix == ".ogg"


@pytest.mark.asyncio
async def test_demo_initialize_recording_makes_no_egress_request(monkeypatch):
    """The public entry point never starts egress or an observer."""
    call_session_data = SimpleNamespace(
        lkapi_ref={},
        recording_filepath=None,
        recording_started_at=None,
    )
    ctx = SimpleNamespace(room=SimpleNamespace(name="room-1"))
    setup_egress = AsyncMock()
    monkeypatch.setattr(common, "setup_egress", setup_egress)

    await common.initialize_recording(ctx, call_session_data)

    setup_egress.assert_not_awaited()
    assert call_session_data.lkapi_ref == {}
    assert call_session_data.recording_filepath is None

