"""ai-coustics NC 트라이얼 헬퍼(utils.agent_nc) 테스트.

세션 단위 배정(sha1 버킷), Redis 킬 스위치, NCTee 래퍼(훅 위임·타이밍·bypass 감지·덤프),
warmup, 세션 카운터, finalize(요약 로그 + S3 업로드)를 검증한다.
"""

from __future__ import annotations

import asyncio
import io
import time
import wave
from types import SimpleNamespace

from livekit import rtc

from utils import agent_nc

SAMPLE_RATE = 24000
FRAME_SAMPLES = SAMPLE_RATE // 100  # 10 ms


def make_frame(fill: int = 0) -> rtc.AudioFrame:
    data = bytes([fill & 0xFF, (fill >> 8) & 0xFF]) * FRAME_SAMPLES
    return rtc.AudioFrame(
        data=data,
        sample_rate=SAMPLE_RATE,
        num_channels=1,
        samples_per_channel=FRAME_SAMPLES,
    )


class FakeInner(rtc.FrameProcessor[rtc.AudioFrame]):
    """내부 프로세서 대역: 호출 기록 + 동작 주입."""

    def __init__(
        self, *, transform=None, raise_exc: Exception | None = None, sleep: float = 0.0
    ):
        self._enabled = True
        self.calls: list[tuple[str, dict]] = []
        self.processed = 0
        self.closed = 0
        self._transform = transform
        self._raise = raise_exc
        self._sleep = sleep

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    def _on_stream_info_updated(
        self, *, room_name, participant_identity, publication_sid
    ):
        self.calls.append(("stream_info", {"room_name": room_name}))

    def _on_stream_info_cleared(self):
        self.calls.append(("stream_info_cleared", {}))

    def _on_credentials_updated(self, *, token, url):
        self.calls.append(("credentials", {"url": url}))

    def _on_credentials_cleared(self):
        self.calls.append(("credentials_cleared", {}))

    def _process(self, frame):
        if self._sleep:
            time.sleep(self._sleep)
        if self._raise is not None:
            raise self._raise
        self.processed += 1
        if self._transform is None:
            return frame
        return self._transform(frame)

    def _close(self):
        self.closed += 1


def invert(frame: rtc.AudioFrame) -> rtc.AudioFrame:
    raw = bytes(frame.data)
    return rtc.AudioFrame(
        data=bytes(b ^ 0xFF for b in raw),
        sample_rate=frame.sample_rate,
        num_channels=frame.num_channels,
        samples_per_channel=frame.samples_per_channel,
    )


# ---------------------------------------------------------------- assignment


def test_room_bucket_is_deterministic_and_in_range():
    a = agent_nc.room_bucket("room-07070754168-abc")
    b = agent_nc.room_bucket("room-07070754168-abc")
    assert a == b
    assert 0 <= a < 100
    assert (
        agent_nc.room_bucket("other-room") != a or True
    )  # 다른 값일 수 있음(결정성만 보장)


def test_resolve_assignment_defaults_to_none_without_env(monkeypatch):
    for name in (
        agent_nc.NC_ENGINE_ENV,
        agent_nc.NC_MODEL_ENV,
        agent_nc.NC_CANARY_PCT_ENV,
        agent_nc.NC_DUMP_PCT_ENV,
    ):
        monkeypatch.delenv(name, raising=False)

    assignment = agent_nc.resolve_nc_assignment("room-a")

    assert assignment.engine == "none"
    assert assignment.model == agent_nc.DEFAULT_NC_MODEL
    assert assignment.dump is False
    assert assignment.source == "env"


def test_resolve_assignment_canary_pct_boundaries(monkeypatch):
    monkeypatch.setenv(agent_nc.NC_ENGINE_ENV, "aic")
    rooms = [f"room-{i}" for i in range(50)]

    monkeypatch.setenv(agent_nc.NC_CANARY_PCT_ENV, "0")
    assert all(agent_nc.resolve_nc_assignment(r).engine == "none" for r in rooms)

    monkeypatch.setenv(agent_nc.NC_CANARY_PCT_ENV, "100")
    assert all(agent_nc.resolve_nc_assignment(r).engine == "aic" for r in rooms)

    monkeypatch.setenv(agent_nc.NC_CANARY_PCT_ENV, "30")
    for r in rooms:
        a = agent_nc.resolve_nc_assignment(r)
        assert (a.engine == "aic") == (agent_nc.room_bucket(r) < 30)


def test_resolve_assignment_prefers_redis_override(monkeypatch):
    monkeypatch.setenv(agent_nc.NC_ENGINE_ENV, "aic")
    monkeypatch.setenv(agent_nc.NC_CANARY_PCT_ENV, "100")

    assignment = agent_nc.resolve_nc_assignment("room-a", canary_pct_override=0)

    assert assignment.engine == "none"
    assert assignment.canary_pct == 0
    assert assignment.source == "redis"


def test_resolve_assignment_unknown_engine_or_model_fails_open(monkeypatch):
    monkeypatch.setenv(agent_nc.NC_ENGINE_ENV, "krisp")
    assert agent_nc.resolve_nc_assignment("room-a").engine == "none"

    monkeypatch.setenv(agent_nc.NC_ENGINE_ENV, "aic")
    monkeypatch.setenv(agent_nc.NC_CANARY_PCT_ENV, "100")
    monkeypatch.setenv(agent_nc.NC_MODEL_ENV, "not-a-model")
    assert agent_nc.resolve_nc_assignment("room-a").model == agent_nc.DEFAULT_NC_MODEL


def test_resolve_assignment_invalid_canary_pct_fails_closed(monkeypatch):
    """카나리 비율이 해석 불가면 전량 ON(100)이 아니라 0 으로 닫혀야 한다."""
    monkeypatch.setenv(agent_nc.NC_ENGINE_ENV, "aic")
    monkeypatch.setenv(agent_nc.NC_CANARY_PCT_ENV, "ten percent")

    assignments = [agent_nc.resolve_nc_assignment(f"room-{i}") for i in range(50)]

    assert all(a.engine == "none" for a in assignments)
    assert all(a.canary_pct == 0 for a in assignments)


def test_resolve_assignment_dump_uses_independent_bucket(monkeypatch):
    monkeypatch.setenv(agent_nc.NC_ENGINE_ENV, "aic")
    monkeypatch.setenv(agent_nc.NC_CANARY_PCT_ENV, "10")
    monkeypatch.setenv(agent_nc.NC_DUMP_PCT_ENV, "10")
    rooms = [f"room-{i}" for i in range(400)]
    assignments = [agent_nc.resolve_nc_assignment(r) for r in rooms]

    dumped_off = [a for a in assignments if a.dump and a.engine == "none"]
    assert dumped_off, "덤프는 OFF 그룹에서도 나와야 한다(카나리 버킷과 독립)"


# ---------------------------------------------------------- redis override


async def test_canary_override_parses_int_from_redis():
    class FakeClient:
        async def get(self, key):
            assert key == "demo:qa:agent_nc:canary_pct"  # demo key prefix
            return "25"

    async def get_client():
        return FakeClient()

    assert (
        await agent_nc.resolve_canary_pct_override(env="qa", get_client=get_client)
        == 25
    )


async def test_canary_override_returns_none_on_timeout_or_error():
    async def slow_client():
        await asyncio.sleep(1.0)

    assert (
        await agent_nc.resolve_canary_pct_override(
            env="qa", get_client=slow_client, timeout=0.05
        )
        is None
    )

    async def broken_client():
        raise ConnectionError("redis down")

    assert (
        await agent_nc.resolve_canary_pct_override(env="qa", get_client=broken_client)
        is None
    )


async def test_canary_override_ignores_garbage_and_clamps():
    class FakeClient:
        def __init__(self, value):
            self.value = value

        async def get(self, key):
            return self.value

    async def garbage():
        return FakeClient("abc")

    async def too_big():
        return FakeClient("250")

    assert (
        await agent_nc.resolve_canary_pct_override(env="qa", get_client=garbage) is None
    )
    assert (
        await agent_nc.resolve_canary_pct_override(env="qa", get_client=too_big) == 100
    )


# ------------------------------------------------------------------- NCTee


def test_nctee_delegates_hooks_enabled_and_close():
    inner = FakeInner()
    tee = agent_nc.NCTee(inner)

    tee._on_stream_info_updated(
        room_name="r", participant_identity="p", publication_sid="s"
    )
    tee._on_credentials_updated(token="t", url="ws://x")
    tee._on_stream_info_cleared()
    tee._on_credentials_cleared()
    assert [c[0] for c in inner.calls] == [
        "stream_info",
        "credentials",
        "stream_info_cleared",
        "credentials_cleared",
    ]

    assert tee.enabled is True
    inner.enabled = False
    assert tee.enabled is False  # 내부가 꺼지면 래퍼도 꺼진 것으로 보임(bypass 노출)
    inner.enabled = True
    tee.enabled = False
    assert tee.enabled is False

    tee._close()
    tee._close()
    assert inner.closed == 1


def test_nctee_returns_original_frame_when_inner_raises():
    tee = agent_nc.NCTee(FakeInner(raise_exc=RuntimeError("boom")))
    frame = make_frame(7)

    out = tee._process(frame)

    assert out is frame
    stats = tee.stats()
    assert stats.errors == 1
    assert stats.frames == 1


def test_nctee_counts_identical_frames_as_bypass():
    # 무음이 아닌 입력(피크 3000)이어야 검사 대상이 된다.
    passthrough = agent_nc.NCTee(FakeInner(), identical_check_every=1)
    for _ in range(10):
        passthrough._process(make_frame(3000))
    assert passthrough.stats().identical_checks == 10
    assert passthrough.stats().identical_ratio == 1.0

    changing = agent_nc.NCTee(FakeInner(transform=invert), identical_check_every=1)
    for _ in range(10):
        changing._process(make_frame(3000))
    assert changing.stats().identical_ratio == 0.0


def test_nctee_identical_check_ignores_silent_input():
    """무음 입력은 NC 를 거쳐도 그대로라 bypass 증거가 못 된다 — 검사에서 제외한다."""
    tee = agent_nc.NCTee(FakeInner(), identical_check_every=1)
    for _ in range(5):
        tee._process(make_frame(0))  # 디지털 무음
    for _ in range(5):
        tee._process(make_frame(2000))  # 신호 있음, inner 는 passthrough
    stats = tee.stats()
    assert stats.identical_checks == 5
    assert stats.identical_hits == 5
    assert stats.identical_ratio == 1.0


def test_nctee_counts_frames_slower_than_their_own_duration():
    """실시간 위반 = 처리 시간이 프레임 길이(10 ms 프레임이면 10 ms)를 넘긴 프레임 수."""
    slow = agent_nc.NCTee(FakeInner(sleep=0.02))
    slow._process(make_frame(1))
    assert slow.stats().over_budget == 1

    fast = agent_nc.NCTee(FakeInner())
    fast._process(make_frame(1))
    assert fast.stats().over_budget == 0


def test_nctee_measures_processing_time():
    tee = agent_nc.NCTee(FakeInner(sleep=0.002))
    for _ in range(5):
        tee._process(make_frame(1))

    stats = tee.stats()
    assert stats.frames == 5
    assert stats.p50_ms >= 1.5
    assert stats.p95_ms >= stats.p50_ms
    assert stats.max_ms >= stats.p95_ms


def test_nctee_dump_collects_input_as_wav_and_caps_length():
    tee = agent_nc.NCTee(FakeInner(transform=invert), dump=True, dump_max_seconds=0.03)
    for i in range(5):  # 50 ms 입력, 상한 30 ms
        tee._process(make_frame(i))

    stats = tee.stats()
    assert stats.dump_truncated is True
    wav_bytes = tee.dump_wav_bytes()
    assert wav_bytes is not None
    with wave.open(io.BytesIO(wav_bytes)) as w:
        assert w.getframerate() == SAMPLE_RATE
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getnframes() == FRAME_SAMPLES * 3
        first = w.readframes(FRAME_SAMPLES)
    assert first == bytes(
        make_frame(0).data
    )  # 입력(원음)이 저장돼야 함, 변환 결과가 아님

    no_dump = agent_nc.NCTee(FakeInner())
    no_dump._process(make_frame(0))
    assert no_dump.dump_wav_bytes() is None


def test_nctee_records_monotonic_time_of_first_frame_only():
    """덤프 WAV 의 0초 = 첫 프레임 처리 시각. 인터럽트 로그가 이 기준으로 t_dump_s 를 낸다."""
    tee = agent_nc.NCTee(FakeInner())
    assert tee.first_frame_at is None

    before = time.monotonic()
    tee._process(make_frame(1))
    after = time.monotonic()
    first = tee.first_frame_at
    assert first is not None
    assert before <= first <= after

    tee._process(make_frame(1))
    assert tee.first_frame_at == first


# ------------------------------------------------------------------ warmup


async def test_warmup_marks_init_failed_on_timeout():
    tee = agent_nc.NCTee(FakeInner(sleep=0.5))

    ok = await agent_nc.warmup_nc(tee, timeout=0.05)

    assert ok is False
    assert tee.stats().init_failed is True
    assert tee.stats().frames == 0  # warmup 프레임은 통계에 섞지 않음


async def test_warmup_returns_false_when_inner_disables_itself():
    class DisablingInner(FakeInner):
        def _process(self, frame):
            self.enabled = False
            return frame

    tee = agent_nc.NCTee(DisablingInner())
    assert await agent_nc.warmup_nc(tee) is False
    assert tee.stats().init_failed is True


async def test_warmup_succeeds_with_healthy_inner():
    tee = agent_nc.NCTee(FakeInner(transform=invert))
    assert await agent_nc.warmup_nc(tee) is True
    assert tee.stats().init_failed is False


async def test_warmup_frame_matches_room_input_format():
    """warmup 프레임은 실입력(24 kHz, mono, 50 ms = 1200 샘플)과 같아야 한다.

    플러그인은 samples_per_channel 이 바뀌면 Enhancer 를 다시 만들어 첫 실프레임에서
    이벤트 루프를 막으므로, warmup 이 다른 크기로 초기화하면 의미가 없다.
    """
    seen: list[rtc.AudioFrame] = []

    class RecordingInner(FakeInner):
        def _process(self, frame):
            seen.append(frame)
            return frame

    tee = agent_nc.NCTee(RecordingInner())
    assert await agent_nc.warmup_nc(tee) is True

    assert len(seen) == 1
    assert seen[0].sample_rate == 24000
    assert seen[0].num_channels == 1
    assert seen[0].samples_per_channel == 1200


# ------------------------------------------------------------ build / bind


def test_build_nc_processor_returns_none_for_engine_none_or_missing_key(monkeypatch):
    monkeypatch.delenv(agent_nc.NC_LICENSE_ENV, raising=False)
    off = agent_nc.NCAssignment(
        engine="none", model="quail_l", canary_pct=0, dump=False, source="env", bucket=0
    )
    assert agent_nc.build_nc_processor(off) is None

    on = agent_nc.NCAssignment(
        engine="aic",
        model="quail_l",
        canary_pct=100,
        dump=False,
        source="env",
        bucket=0,
    )
    assert agent_nc.build_nc_processor(on) is None  # 키 없음 → fail-open


def test_build_nc_processor_wraps_plugin_with_tee(monkeypatch):
    monkeypatch.setenv(agent_nc.NC_LICENSE_ENV, "test-key")
    captured: dict = {}

    def fake_audio_enhancement(**kwargs):
        captured.update(kwargs)
        return FakeInner()

    monkeypatch.setattr(
        agent_nc, "_load_audio_enhancement", lambda: fake_audio_enhancement
    )
    on = agent_nc.NCAssignment(
        engine="aic", model="quail_l", canary_pct=100, dump=True, source="env", bucket=0
    )

    tee = agent_nc.build_nc_processor(on)

    assert isinstance(tee, agent_nc.NCTee)
    assert captured["model"] == agent_nc.PLUGIN_MODEL_NAMES["quail_l"]
    assert captured["auth"] == ("ai_coustics_api", "test-key")


def test_bind_nc_session_counts_events():
    handlers: dict[str, list] = {}

    class FakeSession:
        def on(self, name):
            def deco(fn):
                handlers.setdefault(name, []).append(fn)
                return fn

            return deco

    counters = agent_nc.NCSessionCounters()
    agent_nc.bind_nc_session(FakeSession(), counters)

    for fn in handlers["agent_false_interruption"]:
        fn(SimpleNamespace())
    for fn in handlers["conversation_item_added"]:
        fn(
            SimpleNamespace(
                item=SimpleNamespace(type="message", role="user", interrupted=False)
            )
        )
        fn(
            SimpleNamespace(
                item=SimpleNamespace(type="message", role="assistant", interrupted=True)
            )
        )
        fn(
            SimpleNamespace(
                item=SimpleNamespace(
                    type="message", role="assistant", interrupted=False
                )
            )
        )
        fn(
            SimpleNamespace(
                item=SimpleNamespace(
                    type="function_call", role="assistant", interrupted=True
                )
            )
        )
    for fn in handlers["user_input_transcribed"]:
        fn(SimpleNamespace(is_final=True))
        fn(SimpleNamespace(is_final=False))

    assert counters.false_interruptions == 1
    assert counters.user_turns == 1
    assert counters.agent_interrupted == 1
    assert counters.user_final_transcripts == 1


# ---------------------------------------------------------------- finalize


async def test_finalize_nc_uploads_dump_logs_summary_and_closes():
    inner = FakeInner(transform=invert)
    tee = agent_nc.NCTee(inner, dump=True)
    for i in range(3):
        tee._process(make_frame(i))
    assignment = agent_nc.NCAssignment(
        engine="aic",
        model="quail_l",
        canary_pct=20,
        dump=True,
        source="redis",
        bucket=5,
    )
    counters = agent_nc.NCSessionCounters(false_interruptions=2, user_turns=4)
    uploads: list[tuple[str, int]] = []

    async def upload(key: str, data: bytes) -> None:
        uploads.append((key, len(data)))

    summary = await agent_nc.finalize_nc(
        assignment=assignment,
        tee=tee,
        counters=counters,
        room_name="room-x",
        env="dev",
        started_at=time.monotonic() - 12.0,
        upload=upload,
    )

    assert uploads and uploads[0][0].startswith("nc-ab/dev/")
    assert uploads[0][0].endswith("/room-x.aic.wav")
    assert summary["dump_key"] == uploads[0][0]
    assert summary["nc_engine"] == "aic"
    assert summary["source"] == "redis"
    assert summary["frames"] == 3
    assert summary["false_interruptions"] == 2
    assert summary["user_turns"] == 4
    assert summary["duration_s"] >= 12
    assert inner.closed == 1


async def test_finalize_nc_swallows_upload_failure():
    tee = agent_nc.NCTee(FakeInner(transform=invert), dump=True)
    tee._process(make_frame(1))
    assignment = agent_nc.NCAssignment(
        engine="aic", model="quail_l", canary_pct=100, dump=True, source="env", bucket=1
    )

    async def failing_upload(key: str, data: bytes) -> None:
        raise OSError("s3 down")

    summary = await agent_nc.finalize_nc(
        assignment=assignment,
        tee=tee,
        counters=agent_nc.NCSessionCounters(),
        room_name="room-y",
        env="dev",
        started_at=time.monotonic(),
        upload=failing_upload,
    )

    assert "dump_key" not in summary
    assert "s3 down" in summary["dump_error"]


async def test_finalize_nc_without_tee_still_logs_engine_none():
    assignment = agent_nc.NCAssignment(
        engine="none",
        model="quail_l",
        canary_pct=10,
        dump=False,
        source="env",
        bucket=77,
    )
    summary = await agent_nc.finalize_nc(
        assignment=assignment,
        tee=None,
        counters=agent_nc.NCSessionCounters(),
        room_name="room-z",
        env="prd",
        started_at=time.monotonic(),
    )
    assert summary["nc_engine"] == "none"
    assert summary["bucket"] == 77
    assert "frames" not in summary


async def test_finalize_nc_merges_extra_fields_into_summary():
    tee = agent_nc.NCTee(FakeInner(transform=invert), dump=True)
    tee._process(make_frame(1))
    assignment = agent_nc.NCAssignment(
        engine="aic", model="quail_l", canary_pct=100, dump=True, source="env", bucket=3
    )

    async def upload(key: str, data: bytes) -> None:
        return None

    summary = await agent_nc.finalize_nc(
        assignment=assignment,
        tee=tee,
        counters=agent_nc.NCSessionCounters(agent_interrupted=1),
        room_name="room-e",
        env="dev",
        started_at=time.monotonic(),
        upload=upload,
        extra={
            "interruption_min_duration": 0.5,
            "interruption_min_words": 2,
            "interruptions_logged": 3,
        },
    )

    assert summary["interruption_min_duration"] == 0.5
    assert summary["interruption_min_words"] == 2
    assert summary["interruptions_logged"] == 3
    assert summary["agent_interrupted"] == 1
    assert summary["frames"] == 1
    keys = list(summary)
    assert keys.index("interruption_min_words") > keys.index("user_final_transcripts")
