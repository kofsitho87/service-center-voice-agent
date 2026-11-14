from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger as _base_logger

# ═══════════════════════════════════════════════════════════════════════════════
# ANSI Color Codes for Terminal Pretty Printing
# ═══════════════════════════════════════════════════════════════════════════════


class _Colors:
    """ANSI color codes for terminal output."""

    # Reset
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    ITALIC = "\033[3m"
    UNDERLINE = "\033[4m"

    # Regular colors
    BLACK = "\033[30m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"

    # Bright colors
    BRIGHT_BLACK = "\033[90m"
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_MAGENTA = "\033[95m"
    BRIGHT_CYAN = "\033[96m"
    BRIGHT_WHITE = "\033[97m"

    # Background colors
    BG_BLACK = "\033[40m"
    BG_RED = "\033[41m"
    BG_GREEN = "\033[42m"
    BG_YELLOW = "\033[43m"
    BG_BLUE = "\033[44m"
    BG_MAGENTA = "\033[45m"
    BG_CYAN = "\033[46m"
    BG_WHITE = "\033[47m"


# Level color mapping
_LEVEL_COLORS = {
    "TRACE": _Colors.BRIGHT_BLACK,
    "DEBUG": _Colors.BRIGHT_BLACK,
    "INFO": _Colors.BRIGHT_BLUE,
    "SUCCESS": _Colors.BRIGHT_GREEN,
    "WARNING": _Colors.BRIGHT_YELLOW,
    "ERROR": _Colors.BRIGHT_RED,
    "CRITICAL": f"{_Colors.BOLD}{_Colors.BRIGHT_RED}{_Colors.BG_BLACK}",
}


def _json_default(value: Any) -> Any:
    """Best-effort converter for non-JSON-serializable objects."""
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "dict"):
        return value.dict()
    if isinstance(value, (set, tuple)):
        return list(value)
    return str(value)


def _colorize_json_value(value: Any, indent: int = 0) -> str:
    """Recursively colorize JSON values for pretty printing."""
    c = _Colors
    indent_str = "  " * indent

    if value is None:
        return f"{c.DIM}null{c.RESET}"
    elif isinstance(value, bool):
        color = c.BRIGHT_GREEN if value else c.BRIGHT_RED
        return f"{color}{str(value).lower()}{c.RESET}"
    elif isinstance(value, (int, float)):
        return f"{c.BRIGHT_CYAN}{value}{c.RESET}"
    elif isinstance(value, str):
        # Escape quotes in string for display
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'{c.BRIGHT_YELLOW}"{escaped}"{c.RESET}'
    elif isinstance(value, dict):
        if not value:
            return f"{c.DIM}{{}}{c.RESET}"
        lines = ["{"]
        items = list(value.items())
        for i, (k, v) in enumerate(items):
            comma = "," if i < len(items) - 1 else ""
            key_colored = f'{c.BRIGHT_MAGENTA}"{k}"{c.RESET}'
            val_colored = _colorize_json_value(v, indent + 1)
            lines.append(f"{indent_str}  {key_colored}: {val_colored}{comma}")
        lines.append(f"{indent_str}}}")
        return "\n".join(lines)
    elif isinstance(value, list):
        if not value:
            return f"{c.DIM}[]{c.RESET}"
        lines = ["["]
        for i, item in enumerate(value):
            comma = "," if i < len(value) - 1 else ""
            item_colored = _colorize_json_value(item, indent + 1)
            lines.append(f"{indent_str}  {item_colored}{comma}")
        lines.append(f"{indent_str}]")
        return "\n".join(lines)
    else:
        return f"{c.WHITE}{value}{c.RESET}"


def _pretty_sink(message: Any) -> None:
    """Pretty print sink with colors for terminal readability.

    Format:
    ┌──────────────────────────────────────────────────────────────────────────────
    │ [TIMESTAMP] LEVEL ▸ message
    │ data: { ... pretty formatted JSON ... }
    └──────────────────────────────────────────────────────────────────────────────
    """
    c = _Colors
    record = message.record  # type: ignore[attr-defined]

    ts = record["time"].astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S")
    level_name = record["level"].name
    level_color = _LEVEL_COLORS.get(level_name, c.WHITE)
    msg = record["message"]
    data = record.get("extra", {}) or {}

    # Build the output
    output_lines = []

    # Header line with box drawing
    output_lines.append(f"{c.DIM}┌{'─' * 78}{c.RESET}")

    # Timestamp and level
    level_badge = f"{level_color}{c.BOLD}{level_name:8}{c.RESET}"
    timestamp_str = f"{c.DIM}[{ts}]{c.RESET}"
    message_str = f"{c.WHITE}{c.BOLD}{msg}{c.RESET}"
    output_lines.append(
        f"{c.DIM}│{c.RESET} {timestamp_str} {level_badge} {c.BRIGHT_WHITE}▸{c.RESET} {message_str}"
    )

    # Data section (if exists)
    if data:
        data_colored = _colorize_json_value(data, indent=1)
        # Add prefix to each line
        data_lines = data_colored.split("\n")
        output_lines.append(f"{c.DIM}│{c.RESET} {c.DIM}data:{c.RESET} {data_lines[0]}")
        for line in data_lines[1:]:
            output_lines.append(f"{c.DIM}│{c.RESET}       {line}")

    # Exception handling
    exc = record.get("exception")
    if exc:
        exc_type, exc_value, exc_tb = exc
        output_lines.append(f"{c.DIM}│{c.RESET}")
        output_lines.append(
            f"{c.DIM}│{c.RESET} {c.BRIGHT_RED}{c.BOLD}⚠ Error:{c.RESET} {c.RED}{exc_type.__name__}: {exc_value}{c.RESET}"
        )
        output_lines.append(f"{c.DIM}│{c.RESET} {c.DIM}Stacktrace:{c.RESET}")
        tb_lines = "".join(
            traceback.format_exception(exc_type, exc_value, exc_tb)
        ).split("\n")
        for tb_line in tb_lines:
            if tb_line.strip():
                output_lines.append(
                    f"{c.DIM}│{c.RESET}   {c.BRIGHT_RED}{tb_line}{c.RESET}"
                )

    # Footer line
    output_lines.append(f"{c.DIM}└{'─' * 78}{c.RESET}")

    print("\n".join(output_lines), file=sys.stdout)


def _json_sink(message: Any) -> None:
    """Custom JSON sink that enforces required fields.

    Outputs one JSON line with:
    - timestamp: YYYY-MM-DD HH:MM:SS
    - level
    - message
    - data (from record["extra"])
    - error (only when an exception is present)
    - stacktrace (only when an exception is present)
    """

    record = message.record  # type: ignore[attr-defined]

    # timestamp in the requested format (no timezone suffix)
    ts = record["time"].astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S")

    payload: dict[str, Any] = {
        "timestamp": ts,
        "level": record["level"].name,
        "message": record["message"],
        "data": record.get("extra", {}) or {},
    }

    exc = record.get("exception")
    if exc:
        exc_type, exc_value, exc_tb = exc
        payload["error"] = f"{exc_type.__name__}: {exc_value}"
        payload["stacktrace"] = "".join(
            traceback.format_exception(exc_type, exc_value, exc_tb)
        )

    print(
        json.dumps(payload, ensure_ascii=False, default=_json_default),
        file=sys.stdout,
    )


def setup_json_logging(*, level: str | None = None, pretty: bool | None = None):
    """Configure Loguru to emit JSON lines with fixed schema.

    - level: log level string (defaults to env LOG_LEVEL or INFO)
    - pretty: if True, use colorful pretty-print format (defaults to env LOG_PRETTY)
    - writes to stdout, asynchronous via enqueue=True

    Environment variables:
    - LOG_LEVEL: Set log level (DEBUG, INFO, WARNING, ERROR, etc.)
    - LOG_PRETTY: Set to "1" or "true" to enable pretty colorful output
    - LOG_BACKTRACE: Set to "1" or "true" to enable backtrace
    - LOG_DIAGNOSE: Set to "1" or "true" to enable diagnose
    """

    # Remove any pre-existing handlers to avoid duplicate logs
    _base_logger.remove()

    # Determine if pretty printing should be used
    if pretty is None:
        pretty = os.getenv("LOG_PRETTY", "0") in {"1", "true", "True"}

    # Select the appropriate sink
    sink = _pretty_sink if pretty else _json_sink

    _base_logger.add(
        sink,
        level=level or os.getenv("LOG_LEVEL", "INFO"),
        enqueue=True,
        # We provide our own JSON serialization; do not use serialize=True
        serialize=False,
        backtrace=os.getenv("LOG_BACKTRACE", "0") in {"1", "true", "True"},
        diagnose=os.getenv("LOG_DIAGNOSE", "0") in {"1", "true", "True"},
    )

    return _base_logger
