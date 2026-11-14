"""AWS Transcribe STT에 final transcript 게이트를 추가한 커스텀 STT.

기본 ``livekit.plugins.aws.STT``의 ``SpeechStream._process_transcript_event``는
``resp.is_partial``이 False이면 항상 ``FINAL_TRANSCRIPT``를 내보내고
(livekit/plugins/aws/stt.py ``_process_transcript_event``), 이 final transcript가 turn 커밋의 근거가
된다. 이 모듈은 두 클래스를 상속해 final transcript에 두 가지 게이트를 추가한다:

1. **backchannel 게이트** — "음", "어" 같은 추임새(맞장구)만으로 이뤄진 발화를
   commit하지 않는다. 에이전트 발화 중 사용자의 backchannel에 에이전트가
   반응(턴 생성)하지 않게 하는 것이 목적. ``is_agent_speaking``을 설정하면
   "에이전트가 발화 턴 중(끼어들기)"일 때만 드롭한다. 단, STT가 무음 등을
   ``"."`` 하나로 인식한 final은 발화 턴과 관계없이 항상 드롭한다(아래 참고).
2. **confidence 게이트** — 단어 신뢰도가 임계값 미만인 발화를 commit하지 않는다.
   이 게이트도 ``is_agent_speaking``이 True인 에이전트 발화 턴 중에만
   적용해, 에이전트가 응답을 기다리는 동안의 사용자 발화는 낮은 신뢰도여도 유지한다.

두 게이트 모두 ``FINAL_TRANSCRIPT``를 전송하지 않는 방식이라, 해당 발화는
turn에 누적되지 않는다(=commit되지 않음). START/END_OF_SPEECH·INTERIM_TRANSCRIPT
흐름은 livekit-plugins-aws 구현과 동일하게 둔다.

────────────────────────────────────────────────────────────────────────
final을 드롭하면 turn 커밋이 막히는 이유 (livekit-agents 1.8.6, voice/audio_recognition.py)

- 커밋용 transcript(``_audio_transcript``)는 FINAL_TRANSCRIPT로만 누적된다
  (``_process_stt_event``). INTERIM은 누적되지 않는다. 예외는 수동
  ``commit_user_turn``(``_commit_user_turn``)으로, 이때만 interim을 덧붙인다.
- turn 종료(EOU) 판정은 ``_audio_transcript``가 비어 있으면 즉시 return한다
  (``_run_eou_detection``, STT 사용 + non-manual turn detection 기준).
- 커밋되는 텍스트도 ``_audio_transcript``(interim 미포함)이다(``_run_eou_detection``).
→ 따라서 final을 드롭하면 해당 발화는 turn으로 커밋되지 않는다.

────────────────────────────────────────────────────────────────────────
final 게이트를 "에이전트 발화 중(끼어들기)"일 때 적용하기

응답 대기 중 사용자의 "음/네"는 실제 답변일 수 있어 버리면 안 된다. 그래서
``is_agent_speaking`` 콜러블이 설정돼 있으면 그게 True일 때만 backchannel과
저신뢰도 final을 드롭한다. 단, ``"."`` final은 유효한 사용자 답변이 아니므로
에이전트 발화 여부와 관계없이 드롭한다. final 시점엔 끼어들기로 인해
agent_state가 이미 "listening"으로 바뀌어 있지만, ``session.current_speech``는
일시정지 구간에도 살아있고 ``done()``이 False이므로(완료 시에만 None) 이를
신호로 쓴다.

STT는 AgentSession보다 먼저 생성되므로 세션 생성 직후 ``bind_session()``으로
연결한다:

    filtered_stt = FilteredAWSSTT(..., backchannel_words=DEFAULT_BACKCHANNEL_WORDS)
    session = AgentSession(..., stt=filtered_stt)
    filtered_stt.bind_session(session)

``is_agent_speaking``을 설정하지 않으면 두 게이트는 상태와 무관하게 항상
적용된다(기존 동작 호환). 보통 ``bind_session(session)``으로 연결한다.

────────────────────────────────────────────────────────────────────────
confidence 지표(confidence_metric)에 관한 주의

커밋된 item의 ``transcript_confidence``는 final별 ``SpeechData.confidence``의
평균인데, AWS 플러그인은 ``SpeechData.confidence``를 **첫 단어**의 confidence로
채운다(``_streaming_recognize_response_to_speech_data``). 즉 보통(턴당 final 1개) 화면의 ``transcript_confidence``
= 첫 단어 신뢰도다. 게이트 지표를 이와 다르게 잡으면 "보이는 값은 낮은데 안
걸리는" 혼동이 생기므로 confidence_metric을 둔다:
  - "first" (기본): 첫 단어 = 화면의 transcript_confidence와 동일 기준
  - "mean" : 단어별 confidence 평균
  - "min"  : 단어별 confidence 최소값(가장 엄격)

────────────────────────────────────────────────────────────────────────
⚠️ backchannel 게이트의 한계 (self-host / mode="vad" 기준)
끼어들기(interruption) 자체는 막지 못한다. mode="vad", min_words=0에서는 STT
전사보다 먼저 VAD가 에이전트를 멈추기 때문이다(agent_activity.py
``_interrupt_by_audio_activity``). 대신 final을 드롭하면 에이전트가 backchannel을
응답/기록하지 않고, false-interruption 타이머가 만료되면
resume_false_interruption=True인 경우 원래 발화를 이어서 재개한다. 즉
잠깐 멈췄다가 이어 말한다(stutter). 멈춤을 줄이려면 min_duration↑ /
false_interruption_timeout↓로 보완한다.

참조 구현: livekit-plugins-aws 1.8.6 / livekit/plugins/aws/stt.py
``SpeechStream._process_transcript_event`` (1.6.3 이후 로직 변경 없음 확인).
플러그인 업그레이드 시 ``_process_transcript_event`` 변경 여부를 확인하고
이 오버라이드를 재동기화할 것.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    APIConnectOptions,
    stt,
)
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.plugins.aws import stt as aws_stt
from loguru import logger

# 기본 backchannel(추임새) 단어 집합. 일반 단어는 소문자/무구두점으로
# 정규화하고, "."과 같은 구두점 전용 항목은 exact match용으로 그대로 보존한다.
# 반복 음절은 _is_backchannel_only에서 축약 비교하므로 기본형만 넣으면 된다
# (예: "음음음"→"음", "어어"→"어").
#
# ⚠️ "네", "예", "응", "아니", "맞아요" 등 단독으로 실제 답변이 될 수 있는
# 단어는 의도적으로 제외했다. 이 단어들까지 넣으면 에이전트가 질문한 뒤
# 사용자의 "네" 답변까지 버려질 수 있다.
DEFAULT_BACKCHANNEL_WORDS: frozenset[str] = frozenset(
    {
        # 한국어 머뭇거림/추임새
        "음",
        "흠",
        "아",
        "아하",
        "어",
        "그",
        "저",
        "응",
        "응.",
        ".",
        # 영어 filler
        "um",
        "uh",
        "mm",
        "mhm",
        "hmm",
        "er",
        "erm",
    }
)

# 지원하는 confidence 집계 방식
CONFIDENCE_METRICS: tuple[str, ...] = ("first", "mean", "min")

_NON_WORD_RE = re.compile(r"[^\w]", re.UNICODE)  # \w는 한글 포함 → 구두점/기호만 제거
_REPEAT_RE = re.compile(r"(.)\1+")  # 연속 반복 문자 축약: 음음음→음


def _normalize_token(token: str) -> str:
    """구두점/공백 제거 + 소문자화. (한글은 ``\\w``에 포함되어 보존된다.)"""
    return _NON_WORD_RE.sub("", token).lower()


def _normalize_backchannel_entry(entry: str) -> str:
    """일반 단어는 정규화하고, 구두점 전용 항목은 원문을 보존한다."""
    stripped = entry.strip().lower()
    return _normalize_token(stripped) or stripped


def _is_backchannel_only(text: str, words: frozenset[str]) -> bool:
    """text의 모든 토큰이 backchannel 단어이면 True.

    각 토큰은 정규화 후 그대로 또는 반복 축약형(음음→음)으로 집합과 대조한다.
    토큰이 하나도 없으면(빈 문자열) False. ``words``가 비면 항상 False(비활성).
    """
    if not words:
        return False
    if text.strip().lower() in words:
        return True
    tokens = [t for t in (_normalize_token(w) for w in text.split()) if t]
    if not tokens:
        return False
    for token in tokens:
        if token in words or _REPEAT_RE.sub(r"\1", token) in words:
            continue
        return False
    return True


def _word_confidence(resp: object, metric: str) -> float | None:
    """final 결과의 단어 confidence를 metric에 따라 집계. 측정 불가 시 None.

    - "first": 첫 단어 confidence. AWS 플러그인의 ``SpeechData.confidence`` 및
      커밋된 item의 ``transcript_confidence``(단일 final 기준)와 동일 기준.
    - "mean" : 단어별 confidence 평균.
    - "min"  : 단어별 confidence 최소값(가장 엄격).

    confidence가 None인 item(구두점 등)은 제외한다. 측정 가능한 단어가 하나도
    없으면 None을 반환하고, 호출부는 이를 "측정 불가 → 통과"로 처리한다.
    """
    alternatives = getattr(resp, "alternatives", None)
    if not alternatives:
        return None
    items = alternatives[0].items
    if not items:
        return None
    scores = [item.confidence for item in items if item.confidence is not None]
    if not scores:
        return None
    if metric == "first":
        return scores[0]
    if metric == "min":
        return min(scores)
    return sum(scores) / len(scores)


class FilteredSpeechStream(aws_stt.SpeechStream):
    """backchannel / 저신뢰도 final을 조건에 따라 드롭한다."""

    def __init__(
        self,
        *args: object,
        confidence_threshold: float,
        confidence_metric: str,
        backchannel_words: frozenset[str],
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._confidence_threshold = confidence_threshold
        self._confidence_metric = confidence_metric
        self._backchannel_words = backchannel_words
        # 부모 STT(FilteredAWSSTT) 참조. is_agent_speaking을 런타임에 동적으로 읽기 위함
        # (앱이 stream 생성 후 bind_session으로 설정해도 반영되도록).
        self._owner_stt = kwargs.get("stt")

    def _agent_is_speaking(self) -> bool:
        """backchannel / confidence 게이트 적용 여부.

        is_agent_speaking 미설정 시 True(=상태 무관하게 항상 드롭, 기존 동작).
        설정 시 그 결과. 예외는 안전하게 False(드롭 안 함=commit)로 처리해
        실제 사용자 발화를 잃지 않도록 한다.
        """
        predicate = getattr(self._owner_stt, "is_agent_speaking", None)
        if predicate is None:
            return True
        try:
            return bool(predicate())
        except Exception as e:
            logger.bind(event="STT_AGENT_SPEAKING_PREDICATE_ERROR").debug(
                "is_agent_speaking predicate raised: {err}", err=e
            )
            return False

    def _process_transcript_event(self, transcript_event: object) -> None:
        # 플러그인 구현(livekit/plugins/aws/stt.py, 1.8.6)과 동일하되, final transcript
        # 전송 직전에 게이트(_emit_final_if_allowed)만 추가했다.
        transcript = getattr(transcript_event, "transcript", None)
        if not transcript or not transcript.results:
            return

        for resp in transcript.results:
            if resp.start_time is not None and resp.start_time == 0.0:
                self._event_ch.send_nowait(
                    stt.SpeechEvent(type=stt.SpeechEventType.START_OF_SPEECH)
                )

            if resp.end_time is not None and resp.end_time > 0.0:
                if resp.is_partial:
                    self._event_ch.send_nowait(
                        stt.SpeechEvent(
                            type=stt.SpeechEventType.INTERIM_TRANSCRIPT,
                            alternatives=[
                                self._streaming_recognize_response_to_speech_data(resp)
                            ],
                        )
                    )
                else:
                    self._emit_final_if_allowed(resp)

            if not resp.is_partial:
                self._event_ch.send_nowait(
                    stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH)
                )

    def _emit_final_if_allowed(self, resp: object) -> None:
        speech_data = self._streaming_recognize_response_to_speech_data(resp)
        text = (speech_data.text or "").strip()

        # 1) backchannel 게이트: "음/어" 같은 추임새만으로 된 발화를 commit 안 함.
        #    "."은 유효한 사용자 답변이 아니므로 발화 턴과 관계없이 항상 드롭한다.
        #    그 외 항목은 "에이전트 발화 중(끼어들기)"일 때만 드롭해 응답 대기
        #    중의 "음" 같은 실제 답변은 유지한다.
        if (
            text
            and _is_backchannel_only(text, self._backchannel_words)
            and (text == "." or self._agent_is_speaking())
        ):
            logger.bind(event="STT_BACKCHANNEL_DROPPED", text=text).info(
                "Dropping backchannel final transcript (not committed)"
            )
            return

        # 2) confidence 게이트: 에이전트 발화 턴 중에만 적용.
        #    임계값 0.0이면 비활성. 텍스트가 비었거나 confidence를 측정할
        #    수 없으면 통과(플러그인 구현과 동일).
        if self._confidence_threshold > 0.0 and text and self._agent_is_speaking():
            confidence = _word_confidence(resp, self._confidence_metric)
            if confidence is not None and confidence < self._confidence_threshold:
                logger.bind(
                    event="STT_LOW_CONFIDENCE_DROPPED",
                    confidence=round(confidence, 4),
                    threshold=self._confidence_threshold,
                    metric=self._confidence_metric,
                    text=text,
                ).warning("Dropping low-confidence final transcript (not committed)")
                return

        self._event_ch.send_nowait(
            stt.SpeechEvent(
                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                alternatives=[speech_data],
            )
        )


class FilteredAWSSTT(aws_stt.STT):
    """``aws.STT`` + 에이전트 발화 중 final transcript 게이트.

    Args:
        backchannel_words: 추임새로 간주해 commit하지 않을 단어 집합. None이거나
            빈 집합이면 backchannel 게이트 비활성화. 편의를 위해
            ``DEFAULT_BACKCHANNEL_WORDS`` 제공. 전달된 단어는 내부에서 소문자/
            무구두점으로 정규화된다.
        is_agent_speaking: () -> bool 콜러블. 설정 시 이게 True일 때만
            backchannel과 저신뢰도 final을 드롭한다(에이전트 발화 중에만).
            단, ``"."`` final은 결과와 관계없이 항상 드롭한다. None(기본)이면
            상태와 무관하게 두 게이트를 항상 적용한다. 보통
            ``bind_session(session)``으로 설정한다.
        confidence_threshold: 0.0~1.0. 이 값 미만의 단어 신뢰도(집계 방식은
            ``confidence_metric``)를 가진 final transcript는 commit되지 않는다.
            0.0(기본)이면 비활성화. 에이전트 발화 턴 중에만 적용된다.
        confidence_metric: confidence 집계 방식. "first"(기본, 첫 단어 =
            커밋 item의 transcript_confidence와 동일 기준) / "mean" / "min".
        **kwargs: ``aws.STT``의 모든 인자(region, language, credentials 등) 그대로.
    """

    def __init__(
        self,
        *,
        backchannel_words: set[str] | frozenset[str] | None = None,
        is_agent_speaking: Callable[[], bool] | None = None,
        confidence_threshold: float = 0.0,
        confidence_metric: str = "first",
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        if not (0.0 <= confidence_threshold <= 1.0):
            raise ValueError(
                f"confidence_threshold must be in [0.0, 1.0], got {confidence_threshold}"
            )
        if confidence_metric not in CONFIDENCE_METRICS:
            raise ValueError(
                f"confidence_metric must be one of {CONFIDENCE_METRICS}, "
                f"got {confidence_metric!r}"
            )
        self._confidence_threshold = confidence_threshold
        self._confidence_metric = confidence_metric
        # 공개 속성: 런타임에 교체/설정 가능 (stream 생성 후 bind_session 호출 허용)
        self.is_agent_speaking: Callable[[], bool] | None = is_agent_speaking
        self._backchannel_words: frozenset[str] = (
            frozenset(
                entry
                for entry in (
                    _normalize_backchannel_entry(word) for word in backchannel_words
                )
                if entry
            )
            if backchannel_words
            else frozenset()
        )

    def bind_session(self, session: object) -> None:
        """``session.current_speech`` 기준으로 ``is_agent_speaking``을 설정.

        에이전트가 발화 턴 중(재생 중이거나 끼어들기로 일시정지)일 때 True.
        backchannel과 confidence 게이트가 이 판정을 공유하되, ``"."`` final은
        판정과 관계없이 드롭한다.
        STT는 AgentSession보다 먼저 생성되므로 세션 생성 직후 호출한다::

            filtered_stt = FilteredAWSSTT(..., backchannel_words=...)
            session = AgentSession(..., stt=filtered_stt)
            filtered_stt.bind_session(session)

        커스텀 판정이 필요하면 ``is_agent_speaking``에 직접 콜러블을 할당해도 된다.
        """

        def _is_speaking() -> bool:
            speech = getattr(session, "current_speech", None)
            return speech is not None and not speech.done()

        self.is_agent_speaking = _is_speaking

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> FilteredSpeechStream:
        return FilteredSpeechStream(
            stt=self,
            conn_options=conn_options,
            opts=self._config,
            credentials=self._credentials,
            confidence_threshold=self._confidence_threshold,
            confidence_metric=self._confidence_metric,
            backchannel_words=self._backchannel_words,
        )
