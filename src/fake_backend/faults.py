"""Scripted fault injection for fake integrations.

Faults are selected explicitly through ``DEMO_FAULTS`` (JSON), never by hidden
customer-ID branches. Each key maps to one outcome or a list consumed in order;
the last list item repeats. Example::

    DEMO_FAULTS='{"trunk": ["busy", "busy", "available"], "sms": "fail"}'

Keys and outcomes:

- ``trunk``: ``available`` | ``busy`` | ``fatal`` | ``network``
- ``sms``: ``ok`` | ``fail`` | ``timeout``
- ``call_context``: ``ok`` | ``error``
- ``reservation_read``: ``ok`` | ``error`` | ``timeout``
- ``reservation_submit``: ``ok`` | ``error`` | ``timeout``
- ``sip_dial``: ``unavailable`` (default; the gateway records the outcome and always fails the dial)
"""

from __future__ import annotations

import json
import os
import threading

_lock = threading.Lock()
_cursor: dict[str, int] = {}
_loaded_from: str | None = None
_script: dict[str, list[str]] = {}

DEFAULTS = {
    "trunk": "available",
    "sms": "ok",
    "call_context": "ok",
    "reservation_read": "ok",
    "reservation_submit": "ok",
    "sip_dial": "unavailable",
}


def _load() -> None:
    global _loaded_from, _script
    raw = os.getenv("DEMO_FAULTS", "")
    if raw == _loaded_from:
        return
    parsed = json.loads(raw) if raw.strip() else {}
    if not isinstance(parsed, dict):
        raise ValueError("DEMO_FAULTS must be a JSON object")
    _script = {
        key: [str(v) for v in (value if isinstance(value, list) else [value])]
        for key, value in parsed.items()
    }
    _cursor.clear()
    _loaded_from = raw


def next_outcome(key: str) -> str:
    with _lock:
        _load()
        sequence = _script.get(key)
        if not sequence:
            return DEFAULTS[key]
        index = _cursor.get(key, 0)
        _cursor[key] = index + 1
        return sequence[min(index, len(sequence) - 1)]


def reset() -> None:
    global _loaded_from
    with _lock:
        _cursor.clear()
        _loaded_from = None
