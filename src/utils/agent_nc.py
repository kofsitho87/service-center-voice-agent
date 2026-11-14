"""ai-coustics 노이즈 캔슬링(NC) 트라이얼 헬퍼.

세션 단위 배정(room 이름 sha1 버킷과 Redis 킬 스위치), 플러그인 래퍼 NCTee(훅 위임,
처리 시간 계측, bypass 감지, 입력 덤프), warmup, 세션 카운터, 종료 요약 로그와 덤프
S3 업로드를 담당한다.

모든 경로는 fail-open 이다. 이 모듈의 어떤 실패도 통화 경로로 예외를 올리지 않고
"NC 없음"으로 수렴한다.

warmup 프레임 형식은 AudioInputOptions 기본값(24 kHz, mono, 50 ms)과 같아야 한다.
플러그인이 samples_per_channel 변경 시 Enhancer 를 재생성해 이벤트 루프를 막기 때문이다.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import math
import os
import time
import wave
from array import array
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import metadata
from typing import Any

from livekit import rtc
from loguru import logger

NC_ENGINE_ENV = "AGENT_NC_ENGINE"
NC_MODEL_ENV = "AGENT_NC_MODEL"
NC_CANARY_PCT_ENV = "AGENT_NC_CANARY_PCT"
NC_DUMP_PCT_ENV = "AGENT_NC_DUMP_PCT"
NC_LICENSE_ENV = "AI_COUSTICS_API_KEY"

DEFAULT_NC_ENGINE = "none"
DEFAULT_NC_MODEL = "quail_l"
DEFAULT_CANARY_PCT = 100
DEFAULT_DUMP_PCT = 0
DEFAULT_DUMP_MAX_SECONDS = 480
REDIS_OVERRIDE_TIMEOUT = 0.3

SUPPORTED_NC_ENGINES = {"none", "aic"}
PLUGIN_MODEL_NAMES = {
    "quail_l": "QUAIL_L",
    "quail_vf_l": "QUAIL_VF_L",
    "quail_vf_s": "QUAIL_VF_S",
    "rook_s": "ROOK_S",
}

_PLUGIN_PACKAGE = "livekit-plugins-ai-coustics"
_PLUGIN_MODULE = "livekit.plugins.ai_coustics"
_CANARY_OVERRIDE_KEY = "demo:{env}:agent_nc:canary_pct"

# NCTee 처리 시간 히스토그램: 0.25 ms 버킷 400개(0~100 ms)와 overflow 버킷 1개.
_HIST_BUCKET_NS = 250_000
_HIST_BUCKETS = 400
_SAMPLE_WIDTH = 2  # 16-bit PCM
# identical(bypass) 검사에서 제외할 "무음" 입력의 피크 임계값(int16, ≈ -54 dBFS).
# 무음은 NC 를 거쳐도 그대로라 bypass 증거가 못 된다(dev 실측: 무음 구간 identical 1.0).
_SILENCE_PEAK = 64

# Redis override 조회 실패는 장애 구간마다 첫 1회만 warning, 이후는 debug 로 남긴다.
_override_failure_logged = False


# ---------------------------------------------------------------- assignment


@dataclass(frozen=True)
class NCAssignment:
    """세션 단위 NC 배정 결과. engine 은 카나리 반영 후의 실효 엔진이다."""

    engine: str
    model: str
    canary_pct: int
    dump: bool
    source: str
    bucket: int


def _room_digest(room_name: str) -> str:
    return hashlib.sha1(room_name.encode("utf-8"), usedforsecurity=False).hexdigest()


def room_bucket(room_name: str) -> int:
    """카나리 배정 버킷(0~99). 같은 room 이름은 항상 같은 버킷이다."""
    return int(_room_digest(room_name)[:8], 16) % 100


def _dump_bucket(room_name: str) -> int:
    """덤프 샘플링 버킷(0~99). 다이제스트의 다른 구간을 써서 카나리 버킷과 독립이다."""
    return int(_room_digest(room_name)[8:16], 16) % 100


def _clamp_pct(value: int) -> int:
    return max(0, min(100, value))


def _parse_int(raw: object) -> int | None:
    """정수로 관대하게 파싱한다(앞뒤 공백, bytes, "30.0" 허용). 실패하면 None."""
    if isinstance(raw, int):
        return raw
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        return int(float(str(raw).strip()))
    except (UnicodeDecodeError, ValueError, OverflowError):
        return None


def _resolve_choice(
    explicit: str | None, env_name: str, allowed: Collection[str], default: str
) -> str:
    raw = explicit if explicit is not None else os.getenv(env_name)
    value = (raw or "").strip().lower()
    if not value:
        return default
    if value not in allowed:
        logger.bind(
            event="NC_CONFIG_INVALID", setting=env_name, value=raw, fallback=default
        ).warning("Unsupported NC setting; using fallback")
        return default
    return value


def _resolve_pct(
    explicit: int | str | None,
    env_name: str,
    default: int,
    *,
    invalid_default: int | None = None,
) -> int:
    """비율 설정을 읽는다. 값이 없으면 default, 해석 불가면 invalid_default(없으면 default)."""
    raw = explicit if explicit is not None else os.getenv(env_name)
    if raw is None or not str(raw).strip():
        return default
    value = _parse_int(raw)
    if value is None:
        fallback = default if invalid_default is None else invalid_default
        logger.bind(
            event="NC_CONFIG_INVALID", setting=env_name, value=raw, fallback=fallback
        ).warning("Invalid NC percentage; using fallback")
        return fallback
    return _clamp_pct(value)


def resolve_nc_assignment(
    room_name: str,
    *,
    engine: str | None = None,
    model: str | None = None,
    canary_pct: int | str | None = None,
    dump_pct: int | str | None = None,
    canary_pct_override: int | None = None,
) -> NCAssignment:
    """세션의 NC 배정을 결정한다. 인자가 env 보다 우선하고, 잘못된 값은 기본값으로 돌린다.

    canary_pct_override(Redis 킬 스위치)가 있으면 env 카나리 비율 대신 쓴다.
    실효 엔진은 설정 엔진이 "aic" 이고 room 버킷이 카나리 비율 미만일 때만 "aic" 다.
    덤프 여부는 카나리와 독립된 버킷으로 정해 OFF 그룹에서도 나온다.
    """
    configured_engine = _resolve_choice(
        engine, NC_ENGINE_ENV, SUPPORTED_NC_ENGINES, DEFAULT_NC_ENGINE
    )
    resolved_model = _resolve_choice(
        model, NC_MODEL_ENV, PLUGIN_MODEL_NAMES, DEFAULT_NC_MODEL
    )

    override = (
        _parse_int(canary_pct_override) if canary_pct_override is not None else None
    )
    if override is not None:
        pct = _clamp_pct(override)
        source = "redis"
    else:
        # 값이 없으면 전량(100), 오타 등 해석 불가면 0 으로 닫는다 — prd 에서 잘못된
        # 값이 전량 ON 으로 번지지 않게.
        pct = _resolve_pct(
            canary_pct, NC_CANARY_PCT_ENV, DEFAULT_CANARY_PCT, invalid_default=0
        )
        source = "env"

    bucket = room_bucket(room_name)
    effective_engine = "aic" if configured_engine == "aic" and bucket < pct else "none"
    dump_threshold = _resolve_pct(dump_pct, NC_DUMP_PCT_ENV, DEFAULT_DUMP_PCT)

    return NCAssignment(
        engine=effective_engine,
        model=resolved_model,
        canary_pct=pct,
        dump=_dump_bucket(room_name) < dump_threshold,
        source=source,
        bucket=bucket,
    )


# ---------------------------------------------------------- redis override


def _default_redis_client_factory() -> Callable[[], Awaitable[Any]]:
    from services.redis_client import get_client

    return get_client


async def resolve_canary_pct_override(
    *,
    env: str,
    get_client: Callable[[], Awaitable[Any]] | None = None,
    timeout: float = REDIS_OVERRIDE_TIMEOUT,
) -> int | None:
    """Redis 의 카나리 비율 override(킬 스위치)를 읽는다.

    클라이언트 획득과 GET 을 합쳐 timeout 안에 끝내야 한다. 실패, 타임아웃, 키 없음,
    해석 불가 값은 모두 None 이고 호출부는 env 값을 쓴다.
    """
    global _override_failure_logged
    key = _CANARY_OVERRIDE_KEY.format(env=env)

    async def _fetch() -> Any:
        factory = get_client or _default_redis_client_factory()
        client = await factory()
        return await client.get(key)

    try:
        raw = await asyncio.wait_for(_fetch(), timeout)
    except Exception as exc:
        log = logger.bind(event="NC_OVERRIDE_UNAVAILABLE", key=key, error=repr(exc))
        if _override_failure_logged:
            log.debug("NC canary override unavailable; using env")
        else:
            _override_failure_logged = True
            log.warning("NC canary override unavailable; using env")
        return None
    _override_failure_logged = False

    if raw is None:
        return None
    value = _parse_int(raw)
    if value is None:
        logger.bind(event="NC_OVERRIDE_INVALID", key=key, value=repr(raw)).warning(
            "Ignoring invalid NC canary override"
        )
        return None
    return _clamp_pct(value)


# ------------------------------------------------------------------- NCTee


@dataclass(frozen=True)
class NCStats:
    """NCTee 계측 스냅샷. 처리 시간 백분위는 히스토그램 버킷 상단 경계(최대값 이하)다."""

    frames: int
    errors: int
    p50_ms: float
    p95_ms: float
    max_ms: float
    over_budget: int  # 처리 시간이 프레임 길이를 넘긴(실시간 위반) 프레임 수
    identical_checks: int
    identical_hits: int
    identical_ratio: float
    dump_bytes: int
    dump_truncated: bool
    init_failed: bool


def _frame_bytes(frame: rtc.AudioFrame) -> bytes:
    data = frame.data
    if isinstance(data, memoryview):
        return data.tobytes()
    return bytes(data)


def _frame_duration_ns(frame: rtc.AudioFrame) -> int:
    if not frame.sample_rate:
        return 0
    return int(frame.samples_per_channel * 1_000_000_000 / frame.sample_rate)


def _is_silent(pcm: bytes) -> bool:
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % _SAMPLE_WIDTH])
    if not samples:
        return True
    return max(max(samples), -min(samples)) < _SILENCE_PEAK


class NCTee(rtc.FrameProcessor[rtc.AudioFrame]):
    """ai-coustics 프로세서 래퍼.

    LiveKit 훅과 enabled 를 내부 프로세서에 위임하면서 처리 시간, 예외, 입출력 동일
    프레임 비율(bypass 의심)을 계측하고, dump=True 면 처리 전 입력을 WAV 용으로 모은다.
    내부 프로세서가 예외를 내면 원본 프레임을 그대로 돌려준다.
    """

    def __init__(
        self,
        inner: rtc.FrameProcessor[rtc.AudioFrame],
        *,
        dump: bool = False,
        dump_max_seconds: float = DEFAULT_DUMP_MAX_SECONDS,
        identical_check_every: int = 20,
    ) -> None:
        self._inner = inner
        self._enabled = True
        self._closed = False
        self.init_failed = False

        self._frames = 0
        self._first_frame_at: float | None = None
        self._errors = 0
        self._hist = [0] * (_HIST_BUCKETS + 1)
        self._max_ns = 0
        self._over_budget = 0
        self._identical_every = max(1, int(identical_check_every))
        self._identical_checks = 0
        self._identical_hits = 0
        self._bookkeeping_error_logged = False

        self._dump = dump
        self._dump_max_seconds = dump_max_seconds
        self._dump_buf = bytearray()
        self._dump_limit = 0
        self._dump_format: tuple[int, int] | None = None  # (sample_rate, channels)
        self._dump_truncated = False

    # -- FrameProcessor 인터페이스 ------------------------------------------

    @property
    def enabled(self) -> bool:
        if not self._enabled:
            return False
        try:
            return bool(self._inner.enabled)
        except Exception:
            return False

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    def _on_stream_info_updated(
        self, *, room_name: str, participant_identity: str, publication_sid: str
    ) -> None:
        try:
            self._inner._on_stream_info_updated(
                room_name=room_name,
                participant_identity=participant_identity,
                publication_sid=publication_sid,
            )
        except Exception as exc:
            _log_hook_error("stream_info_updated", exc)

    def _on_stream_info_cleared(self) -> None:
        try:
            self._inner._on_stream_info_cleared()
        except Exception as exc:
            _log_hook_error("stream_info_cleared", exc)

    def _on_credentials_updated(self, *, token: str, url: str) -> None:
        try:
            self._inner._on_credentials_updated(token=token, url=url)
        except Exception as exc:
            _log_hook_error("credentials_updated", exc)

    def _on_credentials_cleared(self) -> None:
        try:
            self._inner._on_credentials_cleared()
        except Exception as exc:
            _log_hook_error("credentials_cleared", exc)

    def _process(self, frame: rtc.AudioFrame) -> rtc.AudioFrame:
        if self._first_frame_at is None:
            self._first_frame_at = time.monotonic()
        self._frames += 1
        check = self._frames % self._identical_every == 0
        in_bytes = self._capture_input(frame, check)

        failed = False
        start = time.perf_counter_ns()
        try:
            out = self._inner._process(frame)
        except Exception as exc:
            failed = True
            out = frame
            self._errors += 1
            if self._errors == 1:
                logger.bind(event="NC_PROCESS_ERROR", error=repr(exc)).warning(
                    "NC processing failed; passing original frame"
                )
        elapsed_ns = time.perf_counter_ns() - start

        self._record(
            elapsed_ns,
            _frame_duration_ns(frame),
            out,
            in_bytes if check and not failed else None,
        )
        return out

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._inner._close()
        except Exception as exc:
            logger.bind(event="NC_CLOSE_ERROR", error=repr(exc)).warning(
                "NC processor close failed"
            )

    # -- 계측 -----------------------------------------------------------------

    def _capture_input(self, frame: rtc.AudioFrame, check: bool) -> bytes | None:
        """identical 검사와 덤프에 쓸 입력 바이트를 inner 호출 전에 확보한다."""
        dumping = self._dump and not self._dump_truncated
        if not (check or dumping):
            return None
        try:
            data = _frame_bytes(frame)
            if dumping:
                self._append_dump(frame, data)
            return data
        except Exception as exc:
            self._log_bookkeeping_error(exc)
            return None

    def _append_dump(self, frame: rtc.AudioFrame, data: bytes) -> None:
        frame_format = (frame.sample_rate, frame.num_channels)
        if self._dump_format is None:
            self._dump_format = frame_format
            max_samples = max(0, round(self._dump_max_seconds * frame.sample_rate))
            self._dump_limit = max_samples * frame.num_channels * _SAMPLE_WIDTH
        elif frame_format != self._dump_format:
            # 형식이 섞이면 WAV 가 깨지므로 여기서 덤프를 멈춘다.
            self._dump_truncated = True
            return

        remaining = self._dump_limit - len(self._dump_buf)
        if remaining <= 0:
            self._dump_truncated = True
            return
        if len(data) > remaining:
            self._dump_buf += data[:remaining]
            self._dump_truncated = True
        else:
            self._dump_buf += data

    def _record(
        self,
        elapsed_ns: int,
        frame_ns: int,
        out: rtc.AudioFrame,
        in_bytes: bytes | None,
    ) -> None:
        try:
            self._hist[min(elapsed_ns // _HIST_BUCKET_NS, _HIST_BUCKETS)] += 1
            self._max_ns = max(self._max_ns, elapsed_ns)
            if frame_ns and elapsed_ns > frame_ns:
                self._over_budget += 1
            # 무음 입력은 처리해도 그대로라 bypass 판정에서 뺀다.
            if in_bytes is not None and not _is_silent(in_bytes):
                self._identical_checks += 1
                if _frame_bytes(out) == in_bytes:
                    self._identical_hits += 1
        except Exception as exc:
            self._log_bookkeeping_error(exc)

    def _log_bookkeeping_error(self, exc: Exception) -> None:
        if self._bookkeeping_error_logged:
            return
        self._bookkeeping_error_logged = True
        logger.bind(event="NC_TEE_ERROR", error=repr(exc)).warning(
            "NC instrumentation failed; audio path unaffected"
        )

    def _percentile_ms(self, pct: float) -> float:
        total = sum(self._hist)
        if total == 0:
            return 0.0
        max_ms = self._max_ns / 1_000_000
        rank = max(1, math.ceil(total * pct / 100))
        seen = 0
        for index, count in enumerate(self._hist):
            seen += count
            if seen >= rank:
                if index >= _HIST_BUCKETS:
                    return round(max_ms, 3)
                upper_ms = (index + 1) * _HIST_BUCKET_NS / 1_000_000
                return round(min(upper_ms, max_ms), 3)
        return round(max_ms, 3)

    # -- 결과 -----------------------------------------------------------------

    @property
    def first_frame_at(self) -> float | None:
        """첫 _process 호출 시각(time.monotonic). 입력 덤프 WAV 의 0초에 해당한다."""
        return self._first_frame_at

    def stats(self) -> NCStats:
        checks = self._identical_checks
        return NCStats(
            frames=self._frames,
            errors=self._errors,
            p50_ms=self._percentile_ms(50),
            p95_ms=self._percentile_ms(95),
            max_ms=round(self._max_ns / 1_000_000, 3),
            over_budget=self._over_budget,
            identical_checks=checks,
            identical_hits=self._identical_hits,
            identical_ratio=round(self._identical_hits / checks, 4) if checks else 0.0,
            dump_bytes=len(self._dump_buf),
            dump_truncated=self._dump_truncated,
            init_failed=self.init_failed,
        )

    def dump_wav_bytes(self) -> bytes | None:
        """모은 입력 오디오를 16-bit PCM WAV 로 돌려준다. 덤프가 없으면 None."""
        if not self._dump or not self._dump_buf or self._dump_format is None:
            return None
        sample_rate, num_channels = self._dump_format
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(num_channels)
            wav.setsampwidth(_SAMPLE_WIDTH)
            wav.setframerate(sample_rate)
            wav.writeframes(self._dump_buf)
        return buffer.getvalue()


def _log_hook_error(hook: str, exc: Exception) -> None:
    logger.bind(event="NC_HOOK_ERROR", hook=hook, error=repr(exc)).warning(
        "NC processor hook failed"
    )


# ------------------------------------------------------------ build / warmup


def _load_audio_enhancement() -> Callable[..., Any]:
    """ai-coustics 플러그인 팩토리를 지연 import 한다. 테스트는 이 함수를 교체한다."""
    from livekit.plugins.ai_coustics import audio_enhancement

    return audio_enhancement


def _build_plugin_kwargs(assignment: NCAssignment, license_key: str) -> dict[str, Any]:
    """플러그인 인자를 평이한 값으로 만든다. 실제 객체 변환은 _to_plugin_objects 몫이다."""
    return {
        "model": PLUGIN_MODEL_NAMES[assignment.model],
        "auth": ("ai_coustics_api", license_key),
    }


def _is_real_plugin(factory: Callable[..., Any]) -> bool:
    module = getattr(factory, "__module__", None) or ""
    return module.startswith(_PLUGIN_MODULE)


def _to_plugin_objects(kwargs: dict[str, Any]) -> dict[str, Any]:
    from livekit.plugins.ai_coustics import Auth, EnhancerModel

    _, license_key = kwargs["auth"]
    return {
        "model": EnhancerModel[kwargs["model"]],
        "auth": Auth.ai_coustics_api(license_key=license_key),
    }


def build_nc_processor(assignment: NCAssignment) -> NCTee | None:
    """배정이 "aic" 이고 라이선스가 있으면 NCTee 로 감싼 플러그인을, 아니면 None 을 준다."""
    if assignment.engine != "aic":
        return None
    license_key = (os.getenv(NC_LICENSE_ENV) or "").strip()
    if not license_key:
        logger.bind(
            event="NC_PROCESSOR_SKIPPED",
            reason="missing_license",
            setting=NC_LICENSE_ENV,
        ).warning("ai-coustics license key missing; NC disabled")
        return None

    try:
        factory = _load_audio_enhancement()
        kwargs = _build_plugin_kwargs(assignment, license_key)
        if _is_real_plugin(factory):
            kwargs = _to_plugin_objects(kwargs)
        return NCTee(factory(**kwargs), dump=assignment.dump)
    except Exception as exc:
        # traceback 대신 repr 만 남긴다(지역 변수에 라이선스 키가 있다).
        logger.bind(
            event="NC_PROCESSOR_FAILED", model=assignment.model, error=repr(exc)
        ).warning("Failed to build ai-coustics processor; NC disabled")
        return None


async def warmup_nc(
    tee: NCTee,
    *,
    sample_rate: int = 24000,
    frame_size_ms: int = 50,
    timeout: float = 3.0,
) -> bool:
    """무음 프레임 1개를 내부 프로세서에 직접 흘려 init 실패를 통화 전에 감지한다.

    프레임 형식은 AudioInputOptions 기본값(24 kHz, mono, 50 ms = 1200 샘플)과 같아야
    한다. 플러그인은 sample_rate·channels·samples_per_channel 이 바뀌면 Enhancer 를
    다시 만들어(plugin.py "implicitly recreate") 첫 실프레임에서 이벤트 루프를 막기
    때문이다. tee._process 를 거치지 않으므로 통계와 덤프에 섞이지 않는다. 타임아웃이
    나도 워커 스레드는 취소되지 않고 끝까지 돌므로, False 를 받은 tee 는 오디오 경로에
    붙이지 않는다.
    """
    if tee is None:
        return False
    failure: str | None = None
    try:
        samples = sample_rate * frame_size_ms // 1000
        frame = rtc.AudioFrame(
            data=b"\x00\x00" * samples,
            sample_rate=sample_rate,
            num_channels=1,
            samples_per_channel=samples,
        )
        await asyncio.wait_for(asyncio.to_thread(tee._inner._process, frame), timeout)
        if not tee.enabled:
            failure = "disabled"
    except TimeoutError:
        failure = "timeout"
    except Exception as exc:
        failure = repr(exc)

    if failure is None:
        return True
    tee.init_failed = True
    logger.bind(event="NC_WARMUP_FAILED", reason=failure, timeout_s=timeout).warning(
        "ai-coustics warmup failed; NC disabled for this session"
    )
    return False


# ---------------------------------------------------------- session counters


@dataclass
class NCSessionCounters:
    """NC 효과 비교용 세션 지표."""

    false_interruptions: int = 0
    agent_interrupted: int = 0
    user_turns: int = 0
    user_final_transcripts: int = 0


def _never_raise(handler: Callable[[Any], None]) -> Callable[[Any], None]:
    def wrapper(event: Any) -> None:
        try:
            handler(event)
        except Exception as exc:
            logger.bind(event="NC_COUNTER_ERROR", error=repr(exc)).debug(
                "NC counter handler failed"
            )

    return wrapper


def bind_nc_session(session: Any, counters: NCSessionCounters) -> None:
    """AgentSession 이벤트로 오탐 인터럽트, 에이전트 끊김, 사용자 턴을 센다."""

    @session.on("agent_false_interruption")
    @_never_raise
    def _on_false_interruption(_event: Any) -> None:
        counters.false_interruptions += 1

    @session.on("conversation_item_added")
    @_never_raise
    def _on_conversation_item_added(event: Any) -> None:
        item = getattr(event, "item", None)
        if getattr(item, "type", None) != "message":
            return
        role = getattr(item, "role", None)
        if role == "user":
            counters.user_turns += 1
        elif role == "assistant" and getattr(item, "interrupted", False):
            counters.agent_interrupted += 1

    @session.on("user_input_transcribed")
    @_never_raise
    def _on_user_input_transcribed(event: Any) -> None:
        if getattr(event, "is_final", False):
            counters.user_final_transcripts += 1


# ---------------------------------------------------------------- finalize


def _plugin_version() -> str | None:
    try:
        return metadata.version(_PLUGIN_PACKAGE)
    except Exception:
        return None


async def _upload_s3(key: str, data: bytes) -> None:
    """Caller-audio dumps are never persisted or uploaded.

    Raw audio storage is out of scope for the demo, so the dump is discarded.
    """
    logger.bind(event="NC_DUMP_DISCARDED", key=key, bytes=len(data)).info(
        "Caller-audio dump discarded (demo does not persist audio)"
    )


async def finalize_nc(
    *,
    assignment: NCAssignment,
    tee: NCTee | None,
    counters: NCSessionCounters,
    room_name: str,
    env: str,
    started_at: float,
    upload: Callable[[str, bytes], Awaitable[None]] | None = None,
    timeout: float = 5.0,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """세션 종료 시 NC 요약을 남기고 덤프를 올린 뒤 tee 를 닫는다. 예외를 내지 않는다.

    요약은 NC_SESSION_SUMMARY 이벤트 로그로 남기고 그대로 반환한다. extra 는 세션
    카운터 뒤에 그대로 합친다. 업로드 실패는 summary["dump_error"] 로만 기록한다.
    """
    summary: dict[str, Any] = {}
    try:
        summary.update(
            nc_engine=assignment.engine,
            model=assignment.model,
            source=assignment.source,
            canary_pct=assignment.canary_pct,
            bucket=assignment.bucket,
            plugin_version=_plugin_version(),
            git_sha=os.getenv("GIT_SHA"),
            duration_s=round(time.monotonic() - started_at, 1),
            false_interruptions=counters.false_interruptions,
            agent_interrupted=counters.agent_interrupted,
            user_turns=counters.user_turns,
            user_final_transcripts=counters.user_final_transcripts,
        )
        if extra:
            summary.update(extra)
        if tee is not None:
            try:
                stats = tee.stats()
                summary.update(
                    frames=stats.frames,
                    errors=stats.errors,
                    p50_ms=stats.p50_ms,
                    p95_ms=stats.p95_ms,
                    max_ms=stats.max_ms,
                    over_budget=stats.over_budget,
                    identical_checks=stats.identical_checks,
                    identical_ratio=stats.identical_ratio,
                    init_failed=stats.init_failed,
                    dump_bytes=stats.dump_bytes,
                    dump_truncated=stats.dump_truncated,
                )
                data = tee.dump_wav_bytes()
                if data is not None:
                    key = (
                        f"nc-ab/{env}/{datetime.now(timezone.utc):%Y-%m-%d}/"
                        f"{room_name}.{assignment.engine}.wav"
                    )
                    try:
                        await asyncio.wait_for(
                            (upload or _upload_s3)(key, data), timeout
                        )
                        summary["dump_key"] = key
                    except Exception as exc:
                        summary["dump_error"] = str(exc) or type(exc).__name__
            finally:
                tee._close()

        logger.bind(event="NC_SESSION_SUMMARY", room_name=room_name, **summary).info(
            "NC session summary"
        )
    except Exception as exc:
        logger.bind(
            event="NC_FINALIZE_ERROR", room_name=room_name, error=repr(exc)
        ).warning("NC finalize failed")
    return summary
