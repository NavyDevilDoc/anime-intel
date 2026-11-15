"""Item 2 Phase C.1 — Schema setup for staff data (Director, Music,
Original Creator, Series Composition, Character Design).

Adds two tables and their supporting indexes to
`data/anime_intelligence_v2.db`:

  people(person_mal_id PK, name_original, name_display, name_normalized)
  anime_staff(anime_id, person_mal_id, role) with (anime_id, person_mal_id, role) PK
    - CHECK constraint locks role to the five target values
    - Junction supports many-to-many with a person holding multiple
      roles per show (Phase A: Watanabe = Director + Episode Director +
      Script + Storyboard on Cowboy Bebop)

Idempotent — `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS`.
Safe to re-run against an already-migrated DB. Separated from the
data-backfill tool (`tools/backfill_staff.py`) so schema changes are
reviewable as their own commit.

Usage:
    python tools/setup_staff_schema.py [--db PATH]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "anime_intelligence_v2.db"

TARGET_ROLES = (
    "Director",
    "Music",
    "Original Creator",
    "Series Composition",
    "Character Design",
)


PEOPLE_DDL = """
CREATE TABLE IF NOT EXISTS people (
    person_mal_id     INTEGER PRIMARY KEY,
    name_original     TEXT NOT NULL,
    name_display      TEXT NOT NULL,
    name_normalized   TEXT NOT NULL
)
"""

PEOPLE_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_people_name_normalized
    ON people(name_normalized)
"""

# CHECK constraint intentional per Phase B decision — "fail loudly" if
# any backfill or future ingestion tries to insert an unknown role.
# Widening the closed set later is a small ALTER-and-migrate change.
ANIME_STAFF_DDL = """
CREATE TABLE IF NOT EXISTS anime_staff (
    anime_id       INTEGER NOT NULL,
    person_mal_id  INTEGER NOT NULL,
    role           TEXT    NOT NULL
        CHECK (role IN ('Director', 'Music', 'Original Creator',
                        'Series Composition', 'Character Design')),
    PRIMARY KEY (anime_id, person_mal_id, role)
)
"""

ANIME_STAFF_INDEX_PERSON_DDL = """
CREATE INDEX IF NOT EXISTS idx_anime_staff_person
    ON anime_staff(person_mal_id)
"""

ANIME_STAFF_INDEX_ROLE_PERSON_DDL = """
CREATE INDEX IF NOT EXISTS idx_anime_staff_role_person
    ON anime_staff(role, person_mal_id)
"""

ALL_DDL = (
    PEOPLE_DDL,
    PEOPLE_INDEX_DDL,
    ANIME_STAFF_DDL,
    ANIME_STAFF_INDEX_PERSON_DDL,
    ANIME_STAFF_INDEX_ROLE_PERSON_DDL,
)


def apply_schema(db_path: Path) -> dict[str, bool]:
    """Apply the schema. Returns a dict of {table_name: existed_before}
    so callers can distinguish "created fresh" from "already present."""
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    def table_exists(name: str) -> bool:
        cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        )
        return cur.fetchone() is not None

    existed = {
        "people": table_exists("people"),
        "anime_staff": table_exists("anime_staff"),
    }

    for statement in ALL_DDL:
        cur.execute(statement)

    conn.commit()
    conn.close()
    return existed


def verify_schema(db_path: Path) -> dict:
    """Confirm the tables + indexes exist and the CHECK constraint is
    in place. Returns a diagnostic dict."""
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    def get_columns(table: str) -> list[str]:
        cur.execute(f"PRAGMA table_info({table})")
        return [row[1] for row in cur.fetchall()]

    def get_indexes(table: str) -> list[str]:
        cur.execute(f"PRAGMA index_list({table})")
        return [row[1] for row in cur.fetchall()]

    def get_check_sql(table: str) -> str:
        cur.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )
        row = cur.fetchone()
        return row[0] if row else ""

    result = {
        "people_columns": get_columns("people"),
        "people_indexes": get_indexes("people"),
        "anime_staff_columns": get_columns("anime_staff"),
        "anime_staff_indexes": get_indexes("anime_staff"),
        "anime_staff_check_constraint_present": (
            "CHECK" in get_check_sql("anime_staff").upper()
        ),
    }
    conn.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help=f"SQLite path. Default: {DEFAULT_DB}")
    args = parser.parse_args()

    if not args.db.exists():
        print(f"ERROR: DB not found at {args.db}", file=sys.stderr)
        return 2

    print(f"[schema] Applying staff schema to {args.db}")
    existed = apply_schema(args.db)
    for table, was_present in existed.items():
        status = "already existed" if was_present else "CREATED"
        print(f"  {table:<15} {status}")

    print("\n[schema] Verification:")
    diag = verify_schema(args.db)
    print(f"  people columns:       {diag['people_columns']}")
    print(f"  people indexes:       {diag['people_indexes']}")
    print(f"  anime_staff columns:  {diag['anime_staff_columns']}")
    print(f"  anime_staff indexes:  {diag['anime_staff_indexes']}")
    print(f"  CHECK constraint:     {diag['anime_staff_check_constraint_present']}")

    if not diag["anime_staff_check_constraint_present"]:
        print("\nWARNING: anime_staff CHECK constraint not detected. "
              "If the table pre-existed without the constraint, drop and "
              "recreate before running the backfill.", file=sys.stderr)
        return 1

    print(f"\n[schema] OK. Target roles enforced: {TARGET_ROLES}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
