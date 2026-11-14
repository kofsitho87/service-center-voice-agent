"""The LLM-facing tool schemas must stay byte-identical across the refactor."""

import json
from pathlib import Path

import pytest
from livekit.agents.llm.tool_context import find_function_tools
from livekit.agents.llm.utils import (
    build_legacy_openai_schema,
    build_strict_openai_schema,
)

from tasks.dynamic_booking_task import DynamicBookingTask

GOLDEN_PATH = Path(__file__).with_name("test_b_tool_schema_golden.py")


def _golden() -> dict:
    namespace: dict = {}
    exec(GOLDEN_PATH.read_text(encoding="utf-8"), namespace)
    return json.loads(namespace["GOLDEN_JSON"])


def _schemas() -> dict:
    task = object.__new__(DynamicBookingTask)
    return {
        tool.info.name: {
            "legacy": build_legacy_openai_schema(tool),
            "strict": build_strict_openai_schema(tool),
        }
        for tool in find_function_tools(task)
    }


def test_tool_names_are_unchanged():
    assert sorted(_schemas()) == sorted(_golden())


@pytest.mark.parametrize("name", sorted(_golden()))
def test_tool_schema_is_unchanged(name):
    assert _schemas()[name] == _golden()[name]
