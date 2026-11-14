"""Production AgentSession settings shared by every entrypoint.

agent-server.py defines production behavior; the local CLIs (agent-cli*.py)
import the same values so local and simulated runs cannot silently drift from
production. Change a value here only when production should change too.
"""

from collections.abc import Callable

from livekit.agents import (
    EndpointingOptions,
    InterruptionOptions,
    PreemptiveGenerationOptions,
    TurnHandlingOptions,
    inference,
)

from utils.agent_interruption import InterruptionSettings, resolve_interruption_settings
from utils.tts_text_filters import normalize_korean_hours, strip_markdown_symbols

PRODUCTION_VAD_ACTIVATION_THRESHOLD = 0.65

# Service-center terms. Biases STT toward the demo's service and device words.
PRODUCTION_STT_CUSTOM_VOCABULARY = (
    "휴대폰 점검",
    "노트북 상담",
    "소형기기 점검",
    "액정",
    "배터리",
    "스마트워치",
    "상담원 연결",
)

PRODUCTION_NON_KO_GOOGLE_VOICE = "Aoede"
PRODUCTION_USER_AWAY_TIMEOUT = 20
PRODUCTION_MAX_TOOL_STEPS = 3


def load_production_vad() -> inference.VAD:
    return inference.VAD(
        model="silero",
        activation_threshold=PRODUCTION_VAD_ACTIVATION_THRESHOLD,
    )


def resolve_production_interruption_settings() -> InterruptionSettings:
    return resolve_interruption_settings(default_min_duration=0.2, default_min_words=1)


def production_turn_handling(
    interrupt_settings: InterruptionSettings,
) -> TurnHandlingOptions:
    return TurnHandlingOptions(
        turn_detection=inference.TurnDetector(version="v1-mini"),
        endpointing=EndpointingOptions(
            mode="fixed",
            min_delay=0.5,
            max_delay=2.0,
        ),
        preemptive_generation=PreemptiveGenerationOptions(
            enabled=True,
            preemptive_tts=True,
        ),
        interruption=InterruptionOptions(
            enabled=True,
            mode="vad",
            min_duration=interrupt_settings.min_duration,
            min_words=interrupt_settings.min_words,
            false_interruption_timeout=0.5,
            resume_false_interruption=True,
            discard_audio_if_uninterruptible=True,
        ),
    )


def production_tts_text_transforms() -> list[str | Callable]:
    return [
        # 내장 필터로 헤더/리스트/링크/코드 및 정상 강조를 먼저 처리하고,
        "filter_markdown",
        # 한글 조사에 붙어 새어나온 강조 심볼(**날짜와 시간**은 …)을 후처리로 제거,
        strip_markdown_symbols,
        # Cartesia 등 TTS에 넘기기 전 한국어 시각을 자연스러운 발음으로 변환.
        # 예: "오후 1시부터 2시" -> "오후 한 시부터 두 시"
        normalize_korean_hours,
        # 마지막으로 이모지 제거.
        "filter_emoji",
    ]
