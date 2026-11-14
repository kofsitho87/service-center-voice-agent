from __future__ import annotations

import pytest

from utils import agent_stt


@pytest.mark.parametrize("selection", ["aws:transcribe", None])
def test_build_agent_stt_injects_aws_credentials_from_environment(
    monkeypatch, selection
) -> None:
    captured_credentials: dict[str, object] = {}
    captured_stt: dict[str, object] = {}
    expected_credentials = object()
    expected_stt = object()

    def fake_credentials(**options):
        captured_credentials.update(options)
        return expected_credentials

    def fake_filtered_aws_stt(**options):
        captured_stt.update(options)
        return expected_stt

    monkeypatch.setattr(agent_stt.aws.stt, "Credentials", fake_credentials)
    monkeypatch.setattr(agent_stt, "FilteredAWSSTT", fake_filtered_aws_stt)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "other-access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "other-secret-key")
    monkeypatch.setenv("AWS_SPEECH_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("AWS_SPEECH_SECRET_ACCESS_KEY", "test-secret-key")
    monkeypatch.setenv("AGENT_STT_MODEL", "gov:stt")
    monkeypatch.delenv("GOV_SPEECH_API_KEY", raising=False)
    monkeypatch.delenv("EXAONE_API_KEY", raising=False)

    result = agent_stt.build_agent_stt(selection, language="ko-KR")

    assert result is expected_stt
    assert captured_credentials == {
        "access_key_id": "test-access-key",
        "secret_access_key": "test-secret-key",
    }
    assert captured_stt["credentials"] is expected_credentials


def test_build_agent_stt_requires_speech_credentials(monkeypatch) -> None:
    monkeypatch.delenv("AWS_SPEECH_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SPEECH_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "other-access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "other-secret-key")

    with pytest.raises(ValueError, match="AWS_SPEECH_ACCESS_KEY_ID"):
        agent_stt.build_agent_stt("aws:transcribe", language="ko-KR")


def test_build_agent_stt_uses_vertex_for_gemini_live_preview(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}
    expected_stt = object()

    def fake_gemini_stt(**options):
        captured.update(options)
        return expected_stt

    monkeypatch.setattr(agent_stt, "GeminiSTT", fake_gemini_stt)
    monkeypatch.setenv(
        "GOOGLE_APPLICATION_CREDENTIALS",
        "/tmp/google-credentials.json",
    )
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")

    result = agent_stt.build_agent_stt(
        "google:gemini-3.5-transcribe-live-preview",
        language="ko-KR",
        custom_vocabulary=["휴대폰 점검", "노트북 상담"],
    )

    assert result is expected_stt
    assert captured == {
        "model": "gemini-3.5-transcribe-live-preview",
        "language": "ko-KR",
        "vertexai": True,
        "credentials_path": "/tmp/google-credentials.json",
        "location": "us-central1",
        "custom_vocabulary": ["휴대폰 점검", "노트북 상담"],
    }


def test_build_agent_stt_keeps_google_cloud_stt_for_other_models(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}
    expected_stt = object()

    def fake_google_stt(**options):
        captured.update(options)
        return expected_stt

    monkeypatch.setattr(agent_stt.google, "STT", fake_google_stt)

    result = agent_stt.build_agent_stt(
        "google:latest_long",
        language="ko-KR",
    )

    assert result is expected_stt
    assert captured == {
        "languages": "ko-KR",
        "detect_language": False,
        "model": "latest_long",
        "use_streaming": True,
    }


@pytest.mark.parametrize(
    ("explicit", "env_value", "expected"),
    [
        (None, "", "aws:transcribe"),
        (None, "gov:stt", "aws:transcribe"),
        ("gov:stt", "aws:transcribe", "gov:stt"),
        (" aws:transcribe ", "gov:stt", "aws:transcribe"),
        ("", "gov:stt", "aws:transcribe"),
        (" ", " gov:stt ", "aws:transcribe"),
    ],
)
def test_resolve_agent_stt_selection_ignores_environment(
    monkeypatch, explicit, env_value, expected
) -> None:
    monkeypatch.setenv("AGENT_STT_MODEL", env_value)
    assert agent_stt.resolve_agent_stt_model(explicit) == expected
