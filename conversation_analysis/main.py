"""Run the local post-call consumer once over the event log.

uv run python conversation_analysis/main.py            # process new events
uv run python conversation_analysis/main.py --show     # also print results
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from call_analysis.consumer import consume
from call_analysis.repository import Repository

ARTIFACTS = Path(os.getenv("DEMO_ARTIFACTS_DIR", str(ROOT / "artifacts")))
TOPIC = os.getenv("KAFKA_TOPIC", "service-center.call-analysis")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--events",
        type=Path,
        default=Path(os.getenv("DEMO_EVENTS_DIR", str(ARTIFACTS / "events")))
        / f"{TOPIC}.jsonl",
    )
    parser.add_argument("--db", type=Path, default=ARTIFACTS / "analysis.sqlite3")
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args()

    repo = Repository(args.db)
    try:
        stats = consume(args.events, repo)
        print(json.dumps(stats.__dict__, ensure_ascii=False))
        if args.show:
            for result in repo.results():
                print(json.dumps(result, ensure_ascii=False))
    finally:
        repo.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
