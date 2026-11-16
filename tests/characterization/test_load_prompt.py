"""Characterization tests for utils.common.load_prompt."""

from datetime import datetime

import pytest

from utils import common
from utils.common import load_prompt


class _FixedDatetime(datetime):
    """2026-10-20 (Tuesday) 09:05 in whatever timezone is requested."""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 20, 9, 5, tzinfo=tz)


@pytest.fixture(autouse=True)
def _freeze_now(monkeypatch):
    monkeypatch.setattr(common, "datetime", _FixedDatetime)


def _write(tmp_path, body: str) -> str:
    path = tmp_path / "prompt.yaml"
    path.write_text(body, encoding="utf-8")
    # os.path.join drops the prompts/ prefix for an absolute filename.
    return str(path)


TEMPLATE = (
    "instructions: '{current_date}|{current_time}|{day_of_week}|"
    "{current_weekday}|{formatted_date_with_weekday}|{formatted_datetime}|"
    "{localized_datetime}'\n"
)


def test_korean_date_variables(tmp_path):
    rendered = load_prompt(_write(tmp_path, TEMPLATE), language_code="ko-KR")
    assert rendered == (
        "2026년 10월 20일|09시 05분|화요일|화요일|2026년 10월 20일 (화요일)|"
        "2026년 10월 20일 09시 05분|2026년 10월 20일 (화요일) 09시 05분"
    )


@pytest.mark.parametrize(
    ("language", "weekday"),
    [
        ("en-US", "Tuesday"),
        ("fr-FR", "Tuesday"),  # unknown languages fall back to English names
    ],
)
def test_non_korean_date_variables(tmp_path, language, weekday):
    rendered = load_prompt(_write(tmp_path, TEMPLATE), language_code=language)
    assert rendered == (
        f"2026-10-20|09:05|{weekday}|{weekday}|2026-10-20 ({weekday})|"
        f"2026-10-20 09:05|2026-10-20 ({weekday}) 09:05"
    )


def test_language_kwarg_is_accepted_as_language_code(tmp_path):
    rendered = load_prompt(
        _write(tmp_path, "instructions: '{day_of_week}'\n"), language="en-US"
    )
    assert rendered == "Tuesday"


def test_variant_and_language_key_fallback_chain(tmp_path):
    path = _write(
        tmp_path,
        "instructions: base\n"
        "en_instructions: english\n"
        "context_instructions: context\n"
        "ko_context_instructions: ko-context\n",
    )
    assert load_prompt(path, variant="context", language_code="ko-KR") == "ko-context"
    assert load_prompt(path, variant="context", language_code="en-US") == "context"
    assert load_prompt(path, language_code="en-US") == "english"
    assert load_prompt(path, language_code="ko-KR") == "base"
    assert load_prompt(path, variant="missing", language_code="fr-FR") == "base"


def test_user_kwargs_override_default_variables(tmp_path):
    path = _write(tmp_path, "instructions: '{current_date} {name}'\n")
    assert load_prompt(path, current_date="X", name="Y") == "X Y"


def test_missing_file_returns_empty_string(tmp_path):
    assert load_prompt(str(tmp_path / "missing.yaml")) == ""


def test_invalid_yaml_returns_empty_string(tmp_path):
    assert load_prompt(_write(tmp_path, "instructions: [unclosed\n")) == ""


def test_missing_template_variable_returns_unformatted_text(tmp_path):
    path = _write(tmp_path, "instructions: 'hello {unknown_var}'\n")
    assert load_prompt(path) == "hello {unknown_var}"


def test_load_failures_emit_structured_log_events(tmp_path):
    from loguru import logger

    events = []
    sink_id = logger.add(
        lambda message: events.append(message.record["extra"].get("event")),
        level="DEBUG",
    )
    try:
        load_prompt(str(tmp_path / "missing.yaml"))
        load_prompt(_write(tmp_path, "instructions: 'hello {unknown_var}'\n"))
    finally:
        logger.remove(sink_id)

    assert events == ["PROMPT_LOAD_ERROR", "PROMPT_TEMPLATE_KEY_MISSING"]
