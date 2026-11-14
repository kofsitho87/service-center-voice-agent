"""Build a clean submission directory from tracked files (positive list).

    uv run python tools/release_export.py <empty-or-new-target-dir>

- Exports only files tracked by Git at HEAD (``git ls-files``), so untracked
  local data (.env, artifacts/, transcripts/, logs/) can never be included.
- Scans the exported text for credential shapes, private identifiers, absolute
  workstation paths, and non-placeholder phone numbers. It fails (exit 1) on
  any hit.
- Private identifiers are read from ``.release-denylist`` (one term per line,
  ``#`` comments allowed). The file is gitignored so the terms themselves are
  never published; the export refuses to run without it.
- Does not copy Git history; initialize a fresh repository in the target if needed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE_NAMES = {".env"}

PATTERNS = {
    "credential": re.compile(
        r"(sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|"
        r"BEGIN [A-Z ]*PRIVATE KEY|xox[bp]-[A-Za-z0-9-]{10,})"
    ),
    "workstation path": re.compile(r"/Users/[A-Za-z0-9._-]+/|/home/[A-Za-z0-9._-]+/"),
    "phone number": re.compile(r"(?<!\d)01[016789]-?(?!0000)\d{3,4}-?\d{4}(?!\d)"),
}


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files"], check=True, capture_output=True, text=True
    ).stdout
    return [
        f
        for f in out.splitlines()
        if Path(f).name not in EXCLUDE_NAMES
    ]


DENYLIST = ROOT / ".release-denylist"
SKIP_PATTERN = {("uv.lock", "phone number")}  # package hashes contain digit runs


def load_denylist() -> re.Pattern[str]:
    terms = [
        line.strip()
        for line in DENYLIST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not terms:
        raise ValueError(f"{DENYLIST.name} has no terms")
    return re.compile("|".join(map(re.escape, terms)), re.IGNORECASE)


def scan(target: Path, files: list[str]) -> list[str]:
    patterns = {**PATTERNS, "private identifier": load_denylist()}
    hits = []
    for rel in files:
        path = target / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for label, pattern in patterns.items():
            if (rel, label) in SKIP_PATTERN:
                continue
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                hits.append(f"{rel}:{line}: {label}")
    return hits


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    target = Path(sys.argv[1]).resolve()
    if not DENYLIST.is_file():
        print(f"Missing {DENYLIST.name}; refusing to export.", file=sys.stderr)
        return 2
    if target.exists() and any(target.iterdir()):
        print(f"Target is not empty: {target}", file=sys.stderr)
        return 2
    files = tracked_files()
    for rel in files:
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dest)
    hits = scan(target, files)
    print(f"Exported {len(files)} files to {target}")
    if hits:
        print("Release scan FAILED:", *hits, sep="\n  ")
        return 1
    print("Release scan passed (credentials, private identifiers, paths, phones).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
