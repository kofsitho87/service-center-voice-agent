"""Local event-log consumer that reads call-end events from JSONL files."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from .analysis import analyze_call
from .repository import Repository


@dataclass
class ConsumeStats:
    read: int = 0
    saved: int = 0
    skipped_duplicate: int = 0
    malformed: int = 0


def custom_message_handler(payload: dict[str, Any], repo: Repository) -> str:
    """Source stage order: skip saved room → normalize → analyze → save."""
    session = payload.get("call_session_data") or {}
    room_name = session.get("room_name")
    if not room_name:
        raise ValueError("call_session_data.room_name missing")
    # The source keyed results by room name, which is unique per production call.
    # Local console calls all use the room "console", so add the call start time.
    started = (session.get("call_config_data") or {}).get("call_start_time")
    call_key = f"{room_name}@{started}" if started else room_name
    if repo.has_result_for_room_name(call_key):
        logger.bind(event="ANALYSIS_SKIPPED", room_name=room_name).info(
            "Result already saved for this room"
        )
        return "skipped_duplicate"
    result = analyze_call(payload)
    repo.save_result(call_key, result.resolution, result.to_dict())
    logger.bind(
        event="ANALYSIS_SAVED", room_name=room_name, resolution=result.resolution
    ).info("Call analysis saved")
    return "saved"


def consume(events_file: Path, repo: Repository) -> ConsumeStats:
    """Process new lines since the committed offset (at-least-once, idempotent by room)."""
    stats = ConsumeStats()
    if not events_file.exists():
        return stats
    source = str(events_file.resolve())
    lines = events_file.read_text(encoding="utf-8").splitlines()
    start = repo.offset(source)
    for index in range(start, len(lines)):
        stats.read += 1
        try:
            message = json.loads(lines[index])
            outcome = custom_message_handler(message["value"], repo)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            stats.malformed += 1
            logger.bind(event="ANALYSIS_MALFORMED", line=index).warning(
                f"Skipping malformed event: {error}"
            )
        else:
            if outcome == "saved":
                stats.saved += 1
            else:
                stats.skipped_duplicate += 1
        repo.commit_offset(source, index + 1)
    return stats
