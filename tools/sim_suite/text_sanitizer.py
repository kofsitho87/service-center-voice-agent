"""Pure text cleanup shared by customer-sim parsing and suite evaluation."""

from __future__ import annotations

import re

CONSOLE_LOG_SUFFIX_RE = re.compile(
    r"\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+"
    r"(?:DEBUG|INFO|WARNING|ERROR|CRITICAL)\b.*$",
    re.DOTALL,
)
# Long box-drawing runs mark the LiveKit terminal redraw, never spoken text.
CONSOLE_REDRAW_SUFFIX_RE = re.compile(r"[─━]{8,}.*$", re.DOTALL)
TRAILING_STATUS_RE = re.compile(r"\s*✓\s*$")


def clean_agent_message(content: str) -> str:
    """Remove console-only status/log suffixes without touching spoken clocks."""
    normalized = " ".join((content or "").split())
    normalized = CONSOLE_REDRAW_SUFFIX_RE.sub("", normalized)
    normalized = CONSOLE_LOG_SUFFIX_RE.sub("", normalized)
    normalized = TRAILING_STATUS_RE.sub("", normalized)
    return normalized.strip()


def normalized_message_key(content: str) -> str:
    """Whitespace-insensitive key used only for duplicate suppression."""
    return "".join(clean_agent_message(content).split())
