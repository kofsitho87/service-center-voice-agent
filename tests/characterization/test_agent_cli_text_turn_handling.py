"""Text console must not initialize an audio turn detector."""

import ast
from pathlib import Path


def test_text_console_uses_manual_turn_detection():
    source_path = Path(__file__).resolve().parents[2] / "src" / "agent-cli-text.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    options_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "TurnHandlingOptions"
    ]

    assert len(options_calls) == 1
    turn_detection = next(
        keyword.value
        for keyword in options_calls[0].keywords
        if keyword.arg == "turn_detection"
    )
    assert isinstance(turn_detection, ast.Constant)
    assert turn_detection.value == "manual"
