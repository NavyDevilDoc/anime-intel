"""Item 2 Phase C.2 — Staff-data enrichment tool.

Backfills the `people` and `anime_staff` tables from Jikan's
`/anime/{mal_id}/staff` endpoint for every show in `anime_info`.

Phase A empirical findings inform the design:
  - Coverage varies by tier: ~87% for Director, ~72-75% for Music /
    Original Creator / Character Design, ~48% for Series Composition.
    2020s obscure titles are the weakest slice.
  - Names uniformly return as "Family, Given" — normalize to "Given
    Family" for display.
  - `person.mal_id` is stable — use it as the primary key rather than
    fuzzy name matching.
  - Multi-value cardinality: a person can hold multiple roles per show
    (Watanabe on Bebop: Director + Episode Director + Script + Storyboard),
    and a role can have multiple people per show (max=9 Character
    Designers observed).

Usage:
    python tools/backfill_staff.py                    # Full backfill
    python tools/backfill_staff.py --dry-run          # Fetch + parse, no writes
    python tools/backfill_staff.py --limit 10         # Smoke test
    python tools/backfill_staff.py --only-missing     # Skip shows with staff rows
    python tools/backfill_staff.py --delay 100        # Milliseconds between calls

Failure handling:
  - HTTP errors and timeouts are logged to stderr with mal_id and error kind
  - Failed shows are not quarantined in a table; re-run with --only-missing
    after fixing whatever caused the initial failure
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(str(ROOT / "env_variables.env"))

DEFAULT_DB = ROOT / "data" / "anime_intelligence_v2.db"
# JIKAN_BASE_URL is required — the public api.jikan.moe fallback was
# retired because the upstream is in maintenance mode / brownout. Empty
# string here means "no default"; validated at main() entry below. See
# src/jikan_client.py for the equivalent fail-fast on the client side.
DEFAULT_JIKAN_BASE = (os.environ.get("JIKAN_BASE_URL") or "").strip()
DEFAULT_DELAY_MS = 200  # Phase B decision

# Abort tripwire defaults. Phase A baseline was 1/240 errors + 6/240
# empty = 2.9% combined. A 30% threshold in a rolling 50-show window is
# ~10× baseline — very generous while still catching a Jikan brownout
# scenario (the Spring 2026 outage returned HTTP 200 with empty data,
# which is why errors + empties are combined into one signal).
DEFAULT_ABORT_WINDOW = 50
DEFAULT_ABORT_THRESHOLD = 0.30
ABORT_WARMUP_SHOWS = 50  # Don't fire before this many shows

TARGET_ROLES = frozenset({
    "Director",
    "Music",
    "Original Creator",
    "Series Composition",
    "Character Design",
})


# ------------------------------------------------------------------------
# Name normalization
# ------------------------------------------------------------------------

def normalize_name(jikan_name: str) -> tuple[str, str, str]:
    """Transform a Jikan-returned name into (original, display, normalized).

    Jikan returns "Family, Given" (e.g., "Watanabe, Shinichirou").
    Display flips to Western order ("Shinichirou Watanabe").
    Normalized lowercases + strips non-word characters, for entity
    detection in `_extract_filters` later (Item 4).

    Edge cases:
      - No comma present (rare — single-name or Western artists): display = original stripped
      - Extra whitespace collapsed
      - Punctuation dropped in normalized form only (apostrophes, dots, hyphens)
    """
    original = jikan_name.strip()
    if not original:
        return "", "", ""

    if "," in original:
        family, given = [p.strip() for p in original.split(",", 1)]
        display = f"{given} {family}".strip()
    else:
        display = original

    normalized = re.sub(r"[^\w\s]", "", display.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return original, display, normalized


# ------------------------------------------------------------------------
# Jikan response parsing
# ------------------------------------------------------------------------

@dataclass
class StaffEntry:
    """A single (person, role) pair extracted from Jikan's response."""
    person_mal_id: int
    name_original: str
    name_display: str
    name_normalized: str
    role: str


def parse_staff_response(payload: dict) -> list[StaffEntry]:
    """Extract only the target-role entries from a Jikan /staff response.

    Jikan's shape:
      { "data": [
          {
            "person": {"mal_id": N, "name": "Family, Given"},
            "positions": ["Director", "Episode Director", "Script"]
          },
          ...
      ]}

    Each person's `positions` array is expanded into one StaffEntry per
    role that intersects TARGET_ROLES. Positions outside the target set
    (Producer, Storyboard, etc.) are silently dropped.
    """
    entries: list[StaffEntry] = []
    for staff in payload.get("data") or []:
        person = staff.get("person") or {}
        mal_id = person.get("mal_id")
        raw_name = person.get("name") or ""
        positions = staff.get("positions") or []
        if not mal_id or not raw_name:
            continue
        original, display, normalized = normalize_name(raw_name)
        for pos in positions:
            if pos in TARGET_ROLES:
                entries.append(StaffEntry(
                    person_mal_id=mal_id,
                    name_original=original,
                    name_display=display,
                    name_normalized=normalized,
                    role=pos,
                ))
    return entries


# ------------------------------------------------------------------------
# Jikan HTTP client
# ------------------------------------------------------------------------

@dataclass
class FetchResult:
    """Structured return from a single staff fetch — mirrors the pattern
    from PR #93's ReviewFetchResult so failures are diagnosable."""
    entries: list[StaffEntry] | None  # None on error, [] on empty response
    status_code: int | None
    error_kind: str | None            # 'http_5xx' / 'http_4xx' / 'network' / 'unexpected' / None
    error_detail: str | None


def fetch_staff(base_url: str, mal_id: int, session: requests.Session,
                retries: int = 2) -> FetchResult:
    """Fetch and parse staff for a single anime. Retries 5xx with
    exponential backoff (2, 4s) — same pattern as PR #93."""
    url = f"{base_url}/anime/{mal_id}/staff"
    last_detail: str | None = None
    for attempt in range(retries + 1):
        try:
            r = session.get(url, timeout=15)
            if r.status_code == 200:
                try:
                    return FetchResult(
                        entries=parse_staff_response(r.json()),
                        status_code=200,
                        error_kind=None,
                        error_detail=None,
                    )
                except ValueError as e:
                    return FetchResult(
                        entries=None, status_code=200,
                        error_kind="unexpected",
                        error_detail=f"JSON decode failure: {e}",
                    )
            if 500 <= r.status_code < 600 and attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            kind = "http_5xx" if r.status_code >= 500 else "http_4xx"
            return FetchResult(
                entries=None, status_code=r.status_code,
                error_kind=kind, error_detail=f"HTTP {r.status_code}",
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            last_detail = f"{type(e).__name__}: {e}"
            if attempt < retries:
                time.sleep(1 + attempt)
                continue
            return FetchResult(
                entries=None, status_code=None,
                error_kind="network", error_detail=last_detail,
            )
        except Exception as e:
            return FetchResult(
                entries=None, status_code=None,
                error_kind="unexpected",
                error_detail=f"{type(e).__name__}: {e}",
            )
    return FetchResult(
        entries=None, status_code=None,
        error_kind="network", error_detail=last_detail or "exhausted retries",
    )


# ------------------------------------------------------------------------
# DB writes
# ------------------------------------------------------------------------

def upsert_person(cur: sqlite3.Cursor, entry: StaffEntry) -> None:
    """Insert-or-ignore the person. If we've seen this mal_id before,
    keep the earlier row — Jikan is consistent enough that we don't need
    conflict resolution. We could later add an UPDATE if we ever
    discover cases where names improve over time."""
    cur.execute(
        "INSERT OR IGNORE INTO people "
        "(person_mal_id, name_original, name_display, name_normalized) "
        "VALUES (?, ?, ?, ?)",
        (entry.person_mal_id, entry.name_original,
         entry.name_display, entry.name_normalized),
    )


def insert_staff_junction(cur: sqlite3.Cursor, anime_id: int,
                          entry: StaffEntry) -> None:
    """Insert-or-ignore the junction row. The composite PK
    (anime_id, person_mal_id, role) enforces uniqueness at the DB
    layer — re-running on the same show simply no-ops."""
    cur.execute(
        "INSERT OR IGNORE INTO anime_staff "
        "(anime_id, person_mal_id, role) VALUES (?, ?, ?)",
        (anime_id, entry.person_mal_id, entry.role),
    )


def write_entries(conn: sqlite3.Connection, anime_id: int,
                  entries: list[StaffEntry]) -> tuple[int, int]:
    """Write a show's entries. Returns (people_new, staff_new) counts."""
    cur = conn.cursor()

    people_before = cur.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    staff_before = cur.execute("SELECT COUNT(*) FROM anime_staff").fetchone()[0]

    for entry in entries:
        upsert_person(cur, entry)
        insert_staff_junction(cur, anime_id, entry)

    conn.commit()

    people_after = cur.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    staff_after = cur.execute("SELECT COUNT(*) FROM anime_staff").fetchone()[0]
    return people_after - people_before, staff_after - staff_before


# ------------------------------------------------------------------------
# Show enumeration
# ------------------------------------------------------------------------

def check_tripwire(
    outcomes: list[str],
    window: int,
    threshold: float,
    warmup: int = ABORT_WARMUP_SHOWS,
) -> tuple[bool, dict]:
    """Sliding-window abort check.

    `outcomes` is a list of per-show outcome strings — 'ok', 'empty',
    or 'error'. Returns (should_abort, diagnostic).

    Fires when the last `window` shows show a combined error+empty rate
    exceeding `threshold`. Does not fire during the warmup period so
    transient early failures don't kill a healthy run.
    """
    if len(outcomes) < warmup:
        return False, {}
    recent = outcomes[-window:]
    n_error = sum(1 for o in recent if o == "error")
    n_empty = sum(1 for o in recent if o == "empty")
    n_bad = n_error + n_empty
    rate = n_bad / len(recent)
    if rate > threshold:
        return True, {
            "window_size": len(recent),
            "recent_errors": n_error,
            "recent_empty": n_empty,
            "combined_bad_rate": round(rate, 3),
            "threshold": threshold,
        }
    return False, {}


def enumerate_shows(conn: sqlite3.Connection, only_missing: bool) -> list[tuple[int, str]]:
    """Return (mal_id, english_title) for shows to process. When
    only_missing is True, skip any show that already has ≥1 anime_staff
    row (idempotent re-run pattern)."""
    cur = conn.cursor()
    if only_missing:
        cur.execute("""
            SELECT ai.id, ai.english_title
            FROM anime_info AS ai
            LEFT JOIN anime_staff AS ast ON ast.anime_id = ai.id
            WHERE ast.anime_id IS NULL
            ORDER BY ai.id
        """)
    else:
        cur.execute(
            "SELECT id, english_title FROM anime_info ORDER BY id"
        )
    return [(row[0], row[1] or "") for row in cur.fetchall()]


# ------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help=f"SQLite path. Default: {DEFAULT_DB}")
    parser.add_argument("--jikan-base", type=str, default=DEFAULT_JIKAN_BASE,
                        help=f"Jikan base URL. Default: {DEFAULT_JIKAN_BASE}")
    parser.add_argument("--delay", type=int, default=DEFAULT_DELAY_MS,
                        help=f"Delay between Jikan calls (ms). Default: {DEFAULT_DELAY_MS}")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only process the first N shows. For smoke testing.")
    parser.add_argument("--only-missing", action="store_true",
                        help="Skip shows that already have anime_staff rows.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and parse but don't write to the DB.")
    parser.add_argument("--abort-window", type=int, default=DEFAULT_ABORT_WINDOW,
                        help=f"Tripwire sliding window size. Default: {DEFAULT_ABORT_WINDOW}")
    parser.add_argument("--abort-threshold", type=float, default=DEFAULT_ABORT_THRESHOLD,
                        help=f"Tripwire error+empty rate threshold. Default: {DEFAULT_ABORT_THRESHOLD}")
    parser.add_argument("--no-abort", action="store_true",
                        help="Disable the tripwire and run through degraded conditions.")
    args = parser.parse_args()

    # Phase G: JIKAN_BASE_URL is required (public api.jikan.moe fallback
    # retired). Either the env var or the --jikan-base flag must supply a
    # value — otherwise fail fast with an actionable message.
    if not (args.jikan_base or "").strip():
        print(
            "ERROR: JIKAN_BASE_URL env var or --jikan-base flag is required.\n"
            "  The public api.jikan.moe fallback was retired. Set\n"
            "  JIKAN_BASE_URL to the self-hosted Railway instance (e.g.,\n"
            "  https://jikan-rest-production-d553.up.railway.app/v4) or\n"
            "  pass --jikan-base explicitly.",
            file=sys.stderr,
        )
        return 2

    if not args.db.exists():
        print(f"ERROR: DB not found at {args.db}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(args.db))
    shows = enumerate_shows(conn, only_missing=args.only_missing)
    if args.limit is not None:
        shows = shows[: args.limit]

    if not shows:
        print("[backfill] Nothing to do — all shows already have staff rows "
              "(or vault is empty).")
        return 0

    delay_s = args.delay / 1000.0
    dry_tag = " [DRY RUN]" if args.dry_run else ""
    print(f"[backfill]{dry_tag} Processing {len(shows)} shows against {args.jikan_base}")
    print(f"[backfill] Delay between calls: {args.delay}ms")

    session = requests.Session()
    n_ok = 0
    n_err = 0
    n_empty = 0
    n_people_added = 0
    n_staff_added = 0
    errors_by_kind: dict[str, int] = {}
    outcomes: list[str] = []  # For tripwire
    aborted = False
    abort_diag: dict = {}

    for i, (anime_id, title) in enumerate(shows, 1):
        result = fetch_staff(args.jikan_base, anime_id, session)

        if result.error_kind:
            n_err += 1
            errors_by_kind[result.error_kind] = errors_by_kind.get(result.error_kind, 0) + 1
            outcomes.append("error")
            print(f"[backfill] ERROR mal_id={anime_id} title={title[:40]!r} "
                  f"kind={result.error_kind} detail={result.error_detail}",
                  file=sys.stderr)
        else:
            n_ok += 1
            entries = result.entries or []
            if not entries:
                n_empty += 1
                outcomes.append("empty")
            else:
                outcomes.append("ok")
                if not args.dry_run:
                    p_new, s_new = write_entries(conn, anime_id, entries)
                    n_people_added += p_new
                    n_staff_added += s_new

        if i % 50 == 0 or i == len(shows):
            print(f"[backfill] {i}/{len(shows)} ok={n_ok} err={n_err} "
                  f"empty={n_empty} people+={n_people_added} staff+={n_staff_added}")

        # Tripwire — abort if rolling window shows too many bad outcomes
        if not args.no_abort:
            should_abort, abort_diag = check_tripwire(
                outcomes, args.abort_window, args.abort_threshold
            )
            if should_abort:
                aborted = True
                print(f"\n[backfill] ABORT — tripwire fired at show {i}/{len(shows)}",
                      file=sys.stderr)
                print(f"  Recent window ({abort_diag['window_size']} shows): "
                      f"errors={abort_diag['recent_errors']} "
                      f"empty={abort_diag['recent_empty']} "
                      f"combined_bad_rate={abort_diag['combined_bad_rate']:.1%} "
                      f"threshold={abort_diag['threshold']:.1%}",
                      file=sys.stderr)
                print("  This is the same fingerprint as the Spring 2026 Jikan brownout — "
                      "investigate before resuming.", file=sys.stderr)
                print("  Resume with `--only-missing` after the upstream is verified healthy.",
                      file=sys.stderr)
                break

        time.sleep(delay_s)

    conn.close()

    print("\n" + "=" * 70)
    status = "ABORTED" if aborted else "COMPLETE"
    processed = len(outcomes)
    print(f"{status}. Processed {processed}/{len(shows)} shows in {'dry-run' if args.dry_run else 'write'} mode.")
    print(f"  Successful:              {n_ok}")
    print(f"  Errors:                  {n_err}  {errors_by_kind or ''}")
    print(f"  Empty (no target roles): {n_empty}")
    if not args.dry_run:
        print(f"  People rows added:       {n_people_added}")
        print(f"  Anime-staff rows added:  {n_staff_added}")
    else:
        print("  [dry-run — no DB writes]")

    if aborted:
        return 2
    return 0 if n_err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
