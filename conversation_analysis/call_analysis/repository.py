"""SQLite storage for analysis results and the consumer offset."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS call_results (
    room_name TEXT PRIMARY KEY,
    resolution TEXT NOT NULL,
    result_json TEXT NOT NULL,
    processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS consumer_offsets (
    source TEXT PRIMARY KEY,
    line_offset INTEGER NOT NULL
);
"""


class Repository:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def has_result_for_room_name(self, room_name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM call_results WHERE room_name = ?", (room_name,)
        ).fetchone()
        return row is not None

    def save_result(
        self, room_name: str, resolution: str, result: dict[str, Any]
    ) -> None:
        self.conn.execute(
            "INSERT INTO call_results (room_name, resolution, result_json) VALUES (?, ?, ?)",
            (room_name, resolution, json.dumps(result, ensure_ascii=False)),
        )

    def offset(self, source: str) -> int:
        row = self.conn.execute(
            "SELECT line_offset FROM consumer_offsets WHERE source = ?", (source,)
        ).fetchone()
        return row["line_offset"] if row else 0

    def commit_offset(self, source: str, line_offset: int) -> None:
        self.conn.execute(
            "INSERT INTO consumer_offsets VALUES (?, ?) "
            "ON CONFLICT(source) DO UPDATE SET line_offset = excluded.line_offset",
            (source, line_offset),
        )

    def results(self) -> list[dict[str, Any]]:
        return [
            json.loads(row["result_json"])
            for row in self.conn.execute(
                "SELECT result_json FROM call_results ORDER BY processed_at"
            )
        ]

    def close(self) -> None:
        self.conn.close()
