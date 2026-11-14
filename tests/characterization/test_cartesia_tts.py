"""Cartesia TTS selector checks."""

from unittest.mock import Mock

from utils import agent_tts


def test_cartesia_tts_selection_uses_call_config(monkeypatch):
    constructor = Mock(return_value=object())
    monkeypatch.setattr(agent_tts.cartesia, "TTS", constructor)

    selected = agent_tts.build_agent_tts(
        "cartesia:sonic-3",
        language="ko-KR",
        voice="cartesia-voice-id",
        speaking_rate=1,
    )

    assert selected is constructor.return_value
    constructor.assert_called_once_with(
        model="sonic-3",
        voice="cartesia-voice-id",
        language="ko",
        speed=1.0,
    )


def test_cartesia_tts_provider_is_supported():
    assert agent_tts.parse_agent_tts_model("cartesia:sonic-3") == (
        "cartesia",
        "sonic-3",
    )
