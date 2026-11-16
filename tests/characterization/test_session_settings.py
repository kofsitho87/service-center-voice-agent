"""Production session settings are shared by every entrypoint."""

import ast
from pathlib import Path

import pytest
from livekit.agents import inference

from utils.agent_interruption import InterruptionSettings
from utils.session_settings import (
    PRODUCTION_MAX_TOOL_STEPS,
    PRODUCTION_NON_KO_GOOGLE_VOICE,
    PRODUCTION_STT_CUSTOM_VOCABULARY,
    PRODUCTION_VAD_ACTIVATION_THRESHOLD,
    production_tts_text_transforms,
    production_turn_handling,
    resolve_production_interruption_settings,
)
from utils.tts_text_filters import normalize_korean_hours, strip_markdown_symbols

SRC = Path(__file__).resolve().parents[2] / "src"


def test_production_values_are_unchanged():
    assert PRODUCTION_VAD_ACTIVATION_THRESHOLD == 0.65
    # Service-center service/device terms.
    assert PRODUCTION_STT_CUSTOM_VOCABULARY == (
        "휴대폰 점검",
        "노트북 상담",
        "소형기기 점검",
        "액정",
        "배터리",
        "스마트워치",
        "상담원 연결",
    )
    assert PRODUCTION_NON_KO_GOOGLE_VOICE == "Aoede"
    assert PRODUCTION_MAX_TOOL_STEPS == 3


def test_tts_text_transforms_keep_production_order():
    assert production_tts_text_transforms() == [
        "filter_markdown",
        strip_markdown_symbols,
        normalize_korean_hours,
        "filter_emoji",
    ]


def test_interruption_defaults_and_env_override(monkeypatch):
    monkeypatch.delenv("AGENT_INTERRUPT_MIN_DURATION", raising=False)
    monkeypatch.delenv("AGENT_INTERRUPT_MIN_WORDS", raising=False)
    settings = resolve_production_interruption_settings()
    assert (settings.min_duration, settings.min_words, settings.source) == (
        0.2,
        1,
        "default",
    )


def test_turn_handling_matches_production():
    options = production_turn_handling(
        InterruptionSettings(min_duration=0.2, min_words=1, source="default")
    )
    assert isinstance(options["turn_detection"], inference.TurnDetector)
    assert options["endpointing"] == {
        "mode": "fixed",
        "min_delay": 0.5,
        "max_delay": 2.0,
    }
    assert options["preemptive_generation"] == {
        "enabled": True,
        "preemptive_tts": True,
    }
    assert options["interruption"] == {
        "enabled": True,
        "mode": "vad",
        "min_duration": 0.2,
        "min_words": 1,
        "false_interruption_timeout": 0.5,
        "resume_false_interruption": True,
        "discard_audio_if_uninterruptible": True,
    }


def _called_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


@pytest.mark.parametrize(
    ("entrypoint", "required"),
    [
        (
            "agent-server.py",
            {
                "load_production_vad",
                "production_turn_handling",
                "production_tts_text_transforms",
                "resolve_production_interruption_settings",
            },
        ),
        (
            "agent-cli.py",
            {
                "load_production_vad",
                "production_turn_handling",
                "production_tts_text_transforms",
                "resolve_production_interruption_settings",
            },
        ),
        # Text console: no audio pipeline, so only VAD and session limits apply.
        ("agent-cli-text.py", {"load_production_vad"}),
    ],
)
def test_entrypoints_use_production_settings(entrypoint, required):
    called = _called_names(SRC / entrypoint)
    assert required <= called
    assert "apply_stt_future_timestamp_clamp_patch" not in called
