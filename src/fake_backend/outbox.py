"""Append-only JSONL records of simulated side effects (SMS, trunk, requests)."""

from __future__ import annotations

import json
import threading
from typing import Any

from . import config

_lock = threading.Lock()


def append(channel: str, record: dict[str, Any]) -> None:
    directory = config.outbox_dir()
    directory.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {"at": config.now().isoformat(timespec="seconds"), **record},
        ensure_ascii=False,
        default=str,
    )
    with _lock, (directory / f"{channel}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def read(channel: str) -> list[dict[str, Any]]:
    path = config.outbox_dir() / f"{channel}.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]
