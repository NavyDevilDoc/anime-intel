"""Landing Redesign PR #1 — Schema setup for cover art columns.

Adds two nullable TEXT columns to `anime_info` to hold MAL v2 poster URLs:

  poster_url_large   — main_picture.large from /anime/{id}?fields=main_picture
  poster_url_medium  — main_picture.medium

Idempotent — checks PRAGMA table_info before issuing ALTER TABLE, since
SQLite doesn't support `ADD COLUMN IF NOT EXISTS`. Safe to re-run against
an already-migrated DB. Separated from the data-backfill tool
(`tools/backfill_poster_urls.py`) so schema changes are reviewable as
their own commit (matches the setup_staff_schema.py / backfill_staff.py
pattern from Item 2).

Usage:
    python tools/setup_poster_schema.py [--db PATH]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "anime_intelligence_v2.db"

NEW_COLUMNS: tuple[tuple[str, str], ...] = (
    ("poster_url_large", "TEXT"),
    ("poster_url_medium", "TEXT"),
)


def _existing_columns(cur: sqlite3.Cursor, table: str) -> set[str]:
    cur.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall()}


def apply_schema(db_path: Path) -> dict[str, bool]:
    """Apply the schema. Returns {column_name: existed_before} so callers
    can distinguish "added" from "already present"."""
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    present = _existing_columns(cur, "anime_info")
    existed = {name: (name in present) for name, _ in NEW_COLUMNS}

    for name, col_type in NEW_COLUMNS:
        if name not in present:
            cur.execute(f"ALTER TABLE anime_info ADD COLUMN {name} {col_type}")

    conn.commit()
    conn.close()
    return existed


def verify_schema(db_path: Path) -> dict:
    """Confirm both columns exist. Returns a diagnostic dict."""
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    present = _existing_columns(cur, "anime_info")
    conn.close()
    return {
        "columns_present": sorted(name for name, _ in NEW_COLUMNS if name in present),
        "columns_missing": sorted(name for name, _ in NEW_COLUMNS if name not in present),
        "all_present": all(name in present for name, _ in NEW_COLUMNS),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help=f"SQLite path. Default: {DEFAULT_DB}")
    args = parser.parse_args()

    if not args.db.exists():
        print(f"ERROR: DB not found at {args.db}", file=sys.stderr)
        return 2

    print(f"[schema] Applying poster-URL schema to {args.db}")
    existed = apply_schema(args.db)
    for col, was_present in existed.items():
        status = "already existed" if was_present else "ADDED"
        print(f"  {col:<20} {status}")

    print("\n[schema] Verification:")
    diag = verify_schema(args.db)
    print(f"  Present: {diag['columns_present']}")
    print(f"  Missing: {diag['columns_missing']}")

    if not diag["all_present"]:
        print("\nERROR: Some columns are still missing after apply.",
              file=sys.stderr)
        return 1

    print("\n[schema] OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
