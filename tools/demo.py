"""Demo data utility for the fake backend.

    uv run python tools/demo.py reset   # delete and reseed artifacts/demo.sqlite3
    uv run python tools/demo.py show    # list appointments in the demo database

Only the configured demo database (DEMO_DB_PATH, default artifacts/demo.sqlite3)
is touched.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fake_backend import config, store


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["reset", "show"])
    args = parser.parse_args()
    if args.command == "reset":
        path = store.reset_database()
        print(f"Demo database reset and reseeded: {path}")
        return 0
    with store.connection() as conn:
        rows = conn.execute(
            """SELECT a.reservation_record_id, a.start_at, a.state, a.source,
                      d.name AS service, o.display_name AS advisor, c.customer_no
               FROM appointments a
               JOIN services d USING (service_id)
               JOIN operators o USING (operator_id)
               JOIN customers c USING (customer_no)
               ORDER BY a.start_at"""
        ).fetchall()
    print(f"{config.db_path()}  (now={config.now():%Y-%m-%d %H:%M %Z})")
    for row in rows:
        print(
            f"{row['start_at']}  {row['state']:<9}  {row['service']:<8}  {row['advisor']}  "
            f"{row['customer_no']}  {row['reservation_record_id']}  ({row['source']})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
