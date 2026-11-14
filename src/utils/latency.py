"""Analyze STT -> LLM -> TTS latency from conversation transcript JSON files."""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich import box
from rich.console import Console
from rich.table import Table

console = Console()


@dataclass(frozen=True)
class TurnLatency:
    turn_index: int
    user_text: str | None
    assistant_text: str
    stt_transcription_delay: float | None
    stt_end_of_turn_delay: float | None
    llm_ttft: float | None
    tts_ttfb: float | None
    e2e_latency: float | None
    e2e_from_timestamps: float | None
    playback_latency: float | None
    interrupted: bool


@dataclass(frozen=True)
class ConversationLatencyReport:
    source: Path
    turns: list[TurnLatency]

    @property
    def interactive_turns(self) -> list[TurnLatency]:
        return [turn for turn in self.turns if turn.user_text is not None]


def _message_text(item: dict[str, Any]) -> str:
    content = item.get("content") or []
    if not content:
        return ""
    return str(content[0])


def _metric(metrics: dict[str, Any], key: str) -> float | None:
    value = metrics.get(key)
    if value is None:
        return None
    return float(value)


def _compute_e2e_from_timestamps(
    user_metrics: dict[str, Any], assistant_metrics: dict[str, Any]
) -> float | None:
    user_stopped = user_metrics.get("stopped_speaking_at")
    assistant_started = assistant_metrics.get("started_speaking_at")
    if user_stopped is None or assistant_started is None:
        return None
    return float(assistant_started) - float(user_stopped)


def analyze_conversation(items: list[dict[str, Any]]) -> list[TurnLatency]:
    turns: list[TurnLatency] = []
    pending_user: dict[str, Any] | None = None
    turn_index = 0

    for item in items:
        if item.get("type") != "message":
            continue

        role = item.get("role")
        metrics = item.get("metrics") or {}

        if role == "user":
            pending_user = item
            continue

        if role != "assistant":
            continue

        turn_index += 1
        user_metrics = (pending_user or {}).get("metrics") or {}
        user_text = _message_text(pending_user) if pending_user else None

        turns.append(
            TurnLatency(
                turn_index=turn_index,
                user_text=user_text or None,
                assistant_text=_message_text(item),
                stt_transcription_delay=_metric(user_metrics, "transcription_delay"),
                stt_end_of_turn_delay=_metric(user_metrics, "end_of_turn_delay"),
                llm_ttft=_metric(metrics, "llm_node_ttft"),
                tts_ttfb=_metric(metrics, "tts_node_ttfb"),
                e2e_latency=_metric(metrics, "e2e_latency"),
                e2e_from_timestamps=_compute_e2e_from_timestamps(user_metrics, metrics)
                if pending_user
                else None,
                playback_latency=_metric(metrics, "playback_latency"),
                interrupted=bool(item.get("interrupted")),
            )
        )
        pending_user = None

    return turns


def load_conversation(path: Path) -> ConversationLatencyReport:
    with path.open(encoding="utf-8") as file:
        items = json.load(file)

    if not isinstance(items, list):
        raise ValueError(f"Expected a JSON array in {path}")

    return ConversationLatencyReport(
        source=path,
        turns=analyze_conversation(items),
    )


def _format_seconds(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.3f}s"


def _truncate(text: str, max_length: int = 36) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + "..."


def render_report(report: ConversationLatencyReport) -> None:
    table = Table(
        title=f"[bold blue]Latency Report[/bold blue] — {report.source.name}",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Turn", justify="right", style="bold")
    table.add_column("User", style="green")
    table.add_column("Assistant", style="blue")
    table.add_column("STT", justify="right")
    table.add_column("Turn Det.", justify="right")
    table.add_column("LLM", justify="right")
    table.add_column("TTS", justify="right")
    table.add_column("E2E", justify="right", style="bold yellow")
    table.add_column("Notes")

    for turn in report.turns:
        notes: list[str] = []
        if turn.interrupted:
            notes.append("interrupted")
        if (
            turn.e2e_latency is not None
            and turn.e2e_from_timestamps is not None
            and abs(turn.e2e_latency - turn.e2e_from_timestamps) > 0.05
        ):
            notes.append("e2e mismatch")

        table.add_row(
            str(turn.turn_index),
            _truncate(turn.user_text or "(greeting)"),
            _truncate(turn.assistant_text),
            _format_seconds(turn.stt_transcription_delay),
            _format_seconds(turn.stt_end_of_turn_delay),
            _format_seconds(turn.llm_ttft),
            _format_seconds(turn.tts_ttfb),
            _format_seconds(turn.e2e_latency),
            ", ".join(notes) if notes else "-",
        )

    console.print()
    console.print(table)
    console.print()


def _average(values: list[float]) -> float | None:
    if not values:
        return None
    return statistics.mean(values)


def render_summary(reports: list[ConversationLatencyReport]) -> None:
    interactive_turns = [
        turn for report in reports for turn in report.interactive_turns
    ]
    if not interactive_turns:
        console.print("[yellow]No interactive turns found.[/yellow]")
        return

    metrics = {
        "STT": [turn.stt_transcription_delay for turn in interactive_turns],
        "Turn detection": [turn.stt_end_of_turn_delay for turn in interactive_turns],
        "LLM TTFT": [turn.llm_ttft for turn in interactive_turns],
        "TTS TTFB": [turn.tts_ttfb for turn in interactive_turns],
        "E2E": [turn.e2e_latency for turn in interactive_turns],
    }

    table = Table(
        title=f"[bold blue]Aggregate Latency[/bold blue] — {len(reports)} conversation(s)",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Metric", style="bold green")
    table.add_column("Avg", justify="right")
    table.add_column("Min", justify="right")
    table.add_column("Max", justify="right")
    table.add_column("Samples", justify="right")

    for name, values in metrics.items():
        filtered = [value for value in values if value is not None]
        if not filtered:
            continue
        table.add_row(
            name,
            _format_seconds(_average(filtered)),
            _format_seconds(min(filtered)),
            _format_seconds(max(filtered)),
            str(len(filtered)),
        )

    console.print()
    console.print(table)
    console.print()


def collect_reports(path: Path) -> list[ConversationLatencyReport]:
    if path.is_file():
        return [load_conversation(path)]

    if not path.is_dir():
        raise FileNotFoundError(f"Path not found: {path}")

    reports: list[ConversationLatencyReport] = []
    for file_path in sorted(path.glob("conversation_*.json")):
        try:
            reports.append(load_conversation(file_path))
        except (json.JSONDecodeError, ValueError) as error:
            console.print(f"[yellow]Skipping {file_path.name}: {error}[/yellow]")
    return reports


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze STT -> LLM -> TTS latency from conversation transcripts."
    )
    parser.add_argument(
        "path",
        type=Path,
        help="Conversation JSON file or transcripts/conversations directory",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Show aggregate stats when analyzing a directory",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    reports = collect_reports(args.path)
    if not reports:
        console.print(f"[yellow]No conversation files found in {args.path}[/yellow]")
        return

    for report in reports:
        render_report(report)

    if args.summary or (args.path.is_dir() and len(reports) > 1):
        render_summary(reports)


if __name__ == "__main__":
    main()
