"""에이전트 발화 끊김(인터럽트) 추적과 인터럽트 임계값 설정.

AgentSession 이벤트로 "에이전트 발화가 끊긴 순간"을 재구성해 끊김마다 NC_INTERRUPTION
로그 한 건을 남긴다. NC A/B 비교에서 오탐 인터럽트와 실제 끊김을 세션 시각(t_s)과
NC 입력 덤프 WAV 시각(t_dump_s)으로 짚어 볼 수 있게 하는 것이 목적이다.

VAD 모드 + pause/resume 에서 SDK 는 에이전트 오디오를 멈추는 순간 agent_state 를
speaking → listening 으로 바꾸고(pause), 이후 agent_false_interruption(resumed) 또는
interrupted=True 인 assistant 메시지 추가 중 하나로 해소한다. 해소 이벤트가 오면 레코드를
낸다. 발화가 자연스럽게 끝나도 speaking → listening 이지만 해소 이벤트가 없어 남지 않는다.
재개할 때 SDK 는 listening → speaking 상태 변경을 false interruption 이벤트보다 먼저
내므로, 끊긴 발화 길이는 pause 시점에 고정해 둔다. 사용자가 재생 전에 말을 시작하면 SDK 는
speaking → listening 없이 멈추므로(on_start_of_speech), 사용자 발화가 pause 보다 뒤에
시작했으면 그 시작을 pause 로 본다.

모든 세션 핸들러는 예외를 삼킨다. 이 모듈의 실패는 통화 경로로 번지지 않는다.
사용자 발화 텍스트는 log_text=True 일 때만 레코드에 넣는다(prd 금지).
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

from loguru import logger

INTERRUPT_MIN_DURATION_ENV = "AGENT_INTERRUPT_MIN_DURATION"
INTERRUPT_MIN_WORDS_ENV = "AGENT_INTERRUPT_MIN_WORDS"
INTERRUPTION_EVENT = "NC_INTERRUPTION"

# 해소를 기다리는 final 전사 보관 상한. 해소되지 않은 final 이 계속 쌓이지 않게 한다.
_MAX_FINALS = 20
# pause 이 시간(초) 전부터의 final 전사와 사용자 발화도 그 pause 의 것으로 본다.
# final 은 상태 변경보다 수 ms 먼저 도착할 수 있다.
_LOOKBACK_S = 1.0

_T = TypeVar("_T")


# ---------------------------------------------------------------- settings


@dataclass(frozen=True)
class InterruptionSettings:
    """InterruptionOptions 에 넣을 인터럽트 임계값.

    source 는 env 값이 하나라도 적용됐으면 "env", 아니면 "default" 다.
    """

    min_duration: float
    min_words: int
    source: str


def _parse_min_duration(raw: str) -> float | None:
    try:
        value = float(raw)
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _parse_min_words(raw: str) -> int | None:
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _read_override(
    environ: Mapping[str, str], name: str, parse: Callable[[str], _T | None]
) -> _T | None:
    """env 값을 parse 로 읽는다. 없거나 비면 None, 해석할 수 없으면 경고 후 None."""
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return None
    value = parse(raw.strip())
    if value is None:
        logger.bind(event="INTERRUPT_SETTINGS_INVALID", name=name, value=raw).warning(
            "Invalid interruption setting; using default"
        )
    return value


def resolve_interruption_settings(
    *,
    default_min_duration: float,
    default_min_words: int,
    environ: Mapping[str, str] | None = None,
) -> InterruptionSettings:
    """인터럽트 임계값을 env 로 덮어쓴다. 값이 없거나 잘못되면 그 항목만 기본값이다.

    min_duration 은 0 이상의 유한한 실수, min_words 는 0 이상의 정수여야 한다.
    """
    env = os.environ if environ is None else environ
    min_duration = _read_override(env, INTERRUPT_MIN_DURATION_ENV, _parse_min_duration)
    min_words = _read_override(env, INTERRUPT_MIN_WORDS_ENV, _parse_min_words)
    return InterruptionSettings(
        min_duration=default_min_duration if min_duration is None else min_duration,
        min_words=default_min_words if min_words is None else min_words,
        source="default" if min_duration is None and min_words is None else "env",
    )


# ----------------------------------------------------------------- tracker


def _words(text: str) -> int:
    """SDK min_words 와 같은 기준. 한국어는 공백으로 나눈 토큰 수다."""
    return len(text.split())


def _chars(text: str) -> int:
    """공백을 뺀 글자 수(한국어는 음절 수)."""
    return len("".join(text.split()))


@dataclass(frozen=True)
class _Final:
    at: float
    text: str
    speaker_id: str | None


def _log_record(record: dict[str, Any]) -> None:
    logger.bind(event=INTERRUPTION_EVENT, **record).info("Agent speech interrupted")


class InterruptionTracker:
    """세션 이벤트로 에이전트 발화 끊김을 재구성해 끊김마다 레코드 하나를 낸다.

    시각은 모두 clock()(기본 time.monotonic) 기준 초다. started_at 은 세션 기준 시각,
    dump_origin 은 NC 입력 덤프 WAV 의 0초에 해당하는 시각을 준다(아직 없으면 None).
    context 는 레코드에 그대로 합칠 값(room_name, nc_engine 등)이다.
    """

    def __init__(
        self,
        *,
        started_at: float,
        dump_origin: Callable[[], float | None] | None = None,
        log_text: bool = False,
        context: Mapping[str, Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        emit: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._started_at = started_at
        self._dump_origin = dump_origin
        self._log_text = log_text
        self._context = dict(context or {})
        self._clock = clock
        self._emit = emit or _log_record
        self._count = 0

        self._agent_speaking_since: float | None = None
        self._user_speaking_since: float | None = None
        self._last_user_speech: tuple[float, float] | None = None  # (시작, 끝)
        self._interim_text = ""
        self._finals: list[_Final] = []

        # 열린 pause(에이전트가 speaking 을 벗어난 시각)와 그 순간의 스냅샷.
        self._pause_at: float | None = None
        self._pause_interim_words = 0
        self._pause_agent_since: float | None = None
        # 마지막 레코드 시각. 그 전에 시작한 사용자 발화는 이미 소비된 것으로 본다.
        self._resolved_at: float | None = None

    @property
    def count(self) -> int:
        """지금까지 낸 레코드 수."""
        return self._count

    # -- 세션 이벤트 ------------------------------------------------------------

    def on_agent_state(self, old_state: str, new_state: str) -> None:
        now = self._clock()
        if new_state == "speaking":
            self._agent_speaking_since = now
        if old_state == "speaking" and new_state != "speaking":
            self._pause_at = now
            self._pause_interim_words = _words(self._interim_text)
            self._pause_agent_since = self._agent_speaking_since

    def on_user_state(self, old_state: str, new_state: str) -> None:
        now = self._clock()
        if new_state == "speaking":
            self._user_speaking_since = now
            # 새 발화가 시작됐다. final 없이 버려진 이전 발화의 중간 전사는 이 발화 것이 아니다.
            self._interim_text = ""
        elif old_state == "speaking":
            started = self._user_speaking_since
            self._last_user_speech = (now if started is None else started, now)
            self._user_speaking_since = None

    def on_transcript(
        self, transcript: str, is_final: bool, speaker_id: str | None = None
    ) -> None:
        if not is_final:
            self._interim_text = transcript
            return
        self._finals.append(_Final(self._clock(), transcript, speaker_id))
        del self._finals[:-_MAX_FINALS]
        self._interim_text = ""

    def on_false_interruption(self, resumed: bool) -> None:
        self._resolve("false_resumed" if resumed else "false_dropped")

    def on_item_added(
        self, item_type: str | None, role: str | None, interrupted: bool
    ) -> None:
        if item_type == "message" and role == "assistant" and interrupted:
            self._resolve("committed")

    # -- 레코드 -----------------------------------------------------------------

    def _resolve(self, outcome: str) -> None:
        """열린 pause 를 해소해 레코드를 낸다.

        정상 경로(VAD 끊김)에서는 사용자 발화가 pause 보다 먼저 시작한다. 사용자 발화가
        pause 뒤에 시작했거나 pause 를 못 봤으면, SDK 가 재생 전(agent_state != speaking)에
        멈춘 경우라 사용자 발화 시작을 pause 로 보고 에이전트 발화 길이는 0 이다.
        둘 다 없으면 지금을 pause 로 본다.
        """
        now = self._clock()
        paused = self._pause_at is not None
        pause = self._pause_at if self._pause_at is not None else now
        agent_since = self._pause_agent_since if paused else self._agent_speaking_since
        interim_words = self._pause_interim_words
        user_start = self._user_speech_start(pause - _LOOKBACK_S)
        if user_start is not None and (not paused or user_start > pause):
            # 저장된 pause 는 이번 끊김의 것이 아니다. 그 스냅샷 대신 지금의 중간 전사를 쓴다.
            pause = user_start
            agent_since = None
            interim_words = _words(self._interim_text)
        cutoff = pause - _LOOKBACK_S
        finals = [final for final in self._finals if final.at >= cutoff]

        record: dict[str, Any] = {"seq": self._count + 1, **self._context}
        record.update(
            outcome=outcome,
            t_s=round(pause - self._started_at, 3),
            t_dump_s=self._dump_offset(pause),
            agent_said_s=(
                round(pause - agent_since, 3) if agent_since is not None else 0.0
            ),
            user_speech_s=self._user_speech_s(now, cutoff),
            user_speaking=self._user_speaking_since is not None,
            interim_words=interim_words,
            final_words=sum(_words(final.text) for final in finals),
            final_chars=sum(_chars(final.text) for final in finals),
            speaker_id=finals[-1].speaker_id if finals else None,
        )
        if self._log_text:
            record["text"] = " ".join(final.text for final in finals)

        # emit 이 실패해도 다음 끊김이 이 pause·전사를 다시 쓰지 않게 먼저 비운다.
        self._pause_at = None
        self._pause_interim_words = 0
        self._pause_agent_since = None
        self._finals = [final for final in self._finals if final.at < cutoff]
        self._resolved_at = now

        self._emit(record)
        self._count += 1

    def _dump_offset(self, pause: float) -> float | None:
        if self._dump_origin is None:
            return None
        origin = self._dump_origin()
        return None if origin is None else round(pause - origin, 3)

    def _user_speech_start(self, cutoff: float) -> float | None:
        """진행 중이거나 cutoff 뒤에 끝난 사용자 발화의 시작 시각. 없으면 None.

        마지막 레코드보다 먼저 시작한 발화는 그 레코드가 소비했으므로 제외한다.
        """
        started: float | None = None
        if self._user_speaking_since is not None:
            started = self._user_speaking_since
        elif self._last_user_speech is not None:
            last_started, ended = self._last_user_speech
            if ended >= cutoff:
                started = last_started
        if started is None:
            return None
        if self._resolved_at is not None and started <= self._resolved_at:
            return None
        return started

    def _user_speech_s(self, now: float, cutoff: float) -> float:
        if self._user_speaking_since is not None:
            return round(now - self._user_speaking_since, 3)
        if self._last_user_speech is not None:
            started, ended = self._last_user_speech
            if ended >= cutoff:
                return round(ended - started, 3)
        return 0.0


# -------------------------------------------------------------------- bind


def _never_raise(handler: Callable[[Any], None]) -> Callable[[Any], None]:
    def wrapper(event: Any) -> None:
        try:
            handler(event)
        except Exception as exc:
            logger.bind(event="NC_INTERRUPTION_HANDLER_ERROR", error=repr(exc)).debug(
                "Interruption tracker handler failed"
            )

    return wrapper


def bind_interruption_tracker(session: Any, tracker: InterruptionTracker) -> None:
    """AgentSession 이벤트를 tracker 에 연결한다. 핸들러는 예외를 내지 않는다."""

    @session.on("agent_state_changed")
    @_never_raise
    def _on_agent_state_changed(event: Any) -> None:
        tracker.on_agent_state(
            getattr(event, "old_state", ""), getattr(event, "new_state", "")
        )

    @session.on("user_state_changed")
    @_never_raise
    def _on_user_state_changed(event: Any) -> None:
        tracker.on_user_state(
            getattr(event, "old_state", ""), getattr(event, "new_state", "")
        )

    @session.on("user_input_transcribed")
    @_never_raise
    def _on_user_input_transcribed(event: Any) -> None:
        tracker.on_transcript(
            getattr(event, "transcript", "") or "",
            bool(getattr(event, "is_final", False)),
            getattr(event, "speaker_id", None),
        )

    @session.on("agent_false_interruption")
    @_never_raise
    def _on_agent_false_interruption(event: Any) -> None:
        tracker.on_false_interruption(bool(getattr(event, "resumed", False)))

    @session.on("conversation_item_added")
    @_never_raise
    def _on_conversation_item_added(event: Any) -> None:
        item = getattr(event, "item", None)
        tracker.on_item_added(
            getattr(item, "type", None),
            getattr(item, "role", None),
            bool(getattr(item, "interrupted", False)),
        )
