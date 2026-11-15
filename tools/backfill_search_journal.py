"""
One-shot backfill of the local search_journal.jsonl file into the new
Supabase `search_journal` table (Initiative 2 Phase C).

Reads every entry from data/search_journal.jsonl, translates each into
the table's column shape via the same helper the dual-write uses
(`_build_search_journal_payload`), and posts in batches of 50.

Idempotent: pre-fetches every existing `event_at` from Supabase and
skips entries whose timestamp already lives in the table. Safe to re-run
if a previous run crashed mid-backfill or after a partial-batch failure.

Usage:
    python tools/backfill_search_journal.py            # full run
    python tools/backfill_search_journal.py --dry-run  # report counts only
    python tools/backfill_search_journal.py --limit 50 # first 50 entries

Requires SUPABASE_URL and SUPABASE_SERVICE_KEY in the environment
(env_variables.env or shell).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / "env_variables.env")

from analysis.telemetry_logger import _build_search_journal_payload  # noqa: E402
from api.supabase_client import supabase_get  # noqa: E402

JOURNAL_PATH = ROOT / "data" / "search_journal.jsonl"
TABLE = "search_journal"
BATCH_SIZE = 50
PAGE_SIZE = 1000  # Supabase default max per GET


def _direct_headers() -> dict:
    """Service-role headers for direct httpx calls (batch inserts)."""
    return {
        "apikey": os.environ["SUPABASE_SERVICE_KEY"],
        "Authorization": f"Bearer {os.environ['SUPABASE_SERVICE_KEY']}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }


def parse_ts(ts: str | None) -> datetime | None:
    """
    Parse an ISO timestamp string into a datetime for set-membership
    comparison. Tolerates both Python isoformat (`...678901+00:00`) and
    Supabase's returned shape (which may trim trailing zeros on the
    fractional seconds). Using parsed datetimes sidesteps string-format
    drift across the round trip.
    """
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def fetch_existing_event_ats() -> set[datetime]:
    """Pull every event_at currently in search_journal for idempotency."""
    existing: set[datetime] = set()
    offset = 0
    while True:
        rows = supabase_get(TABLE, params={
            "select": "event_at",
            "order": "event_at.asc",
            "limit": str(PAGE_SIZE),
            "offset": str(offset),
        })
        if not rows:
            break
        for r in rows:
            dt = parse_ts(r.get("event_at"))
            if dt:
                existing.add(dt)
        if len(rows) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return existing


def load_local_entries() -> list[dict]:
    if not JOURNAL_PATH.exists():
        print(f"ERROR: local journal not found at {JOURNAL_PATH}", file=sys.stderr)
        return []
    entries = []
    with JOURNAL_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def post_batch(url: str, batch: list[dict]) -> tuple[bool, str]:
    """
    POST a batch as a JSON array. PostgREST treats arrays as multi-row
    inserts. Returns (success, error_text).
    """
    try:
        resp = httpx.post(
            f"{url}/rest/v1/{TABLE}",
            headers=_direct_headers(),
            json=batch,
            timeout=30.0,
        )
        if resp.status_code >= 400:
            return False, f"HTTP {resp.status_code}: {resp.text[:300]}"
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def post_one(url: str, row: dict) -> tuple[bool, str]:
    """Single-row fallback for diagnosing which entry in a failed batch was bad."""
    try:
        resp = httpx.post(
            f"{url}/rest/v1/{TABLE}",
            headers=_direct_headers(),
            json=row,
            timeout=10.0,
        )
        if resp.status_code >= 400:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Count what would be inserted; no writes.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only the first N local entries.")
    args = parser.parse_args()

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set.",
              file=sys.stderr)
        return 1

    print("Loading local journal...")
    entries = load_local_entries()
    if args.limit:
        entries = entries[: args.limit]
    print(f"  Loaded {len(entries)} entries from {JOURNAL_PATH.relative_to(ROOT)}")

    print("Fetching existing event_at values from Supabase...")
    existing = fetch_existing_event_ats()
    print(f"  Supabase already has {len(existing)} rows")

    print("Building payloads + filtering for new entries...")
    new_payloads = []
    skipped = 0
    malformed = 0
    for entry in entries:
        ts = entry.get("timestamp")
        dt = parse_ts(ts)
        if not dt:
            malformed += 1
            continue
        if dt in existing:
            skipped += 1
            continue
        try:
            payload = _build_search_journal_payload(entry)
        except Exception as e:
            malformed += 1
            print(f"  WARN: payload build failed for ts={ts}: "
                  f"{type(e).__name__}: {e}")
            continue
        new_payloads.append(payload)
    print(f"  New: {len(new_payloads)}  Skipped (already in Supabase): "
          f"{skipped}  Malformed: {malformed}")

    if args.dry_run:
        print("\n[dry-run] No writes performed.")
        return 0

    if not new_payloads:
        print("\nNothing to insert.")
        return 0

    print(f"\nPosting {len(new_payloads)} new rows in batches of {BATCH_SIZE}...")
    start = time.perf_counter()
    posted = 0
    failed = 0
    batch_failures = 0
    for i in range(0, len(new_payloads), BATCH_SIZE):
        batch = new_payloads[i:i + BATCH_SIZE]
        ok, err = post_batch(url, batch)
        if ok:
            posted += len(batch)
        else:
            batch_failures += 1
            print(f"  Batch {i // BATCH_SIZE + 1} failed: {err}")
            print("  Retrying batch one-by-one to localize the bad row...")
            for row in batch:
                row_ok, row_err = post_one(url, row)
                if row_ok:
                    posted += 1
                else:
                    failed += 1
                    print(f"    FAIL ts={row.get('event_at')}: {row_err}")
        if (i // BATCH_SIZE + 1) % 5 == 0:
            elapsed = time.perf_counter() - start
            print(f"  Progress: {posted}/{len(new_payloads)} "
                  f"({elapsed:.1f}s elapsed)")
    elapsed = time.perf_counter() - start

    print()
    print(f"Elapsed: {elapsed:.1f}s")
    print(f"Posted: {posted}")
    print(f"Failed: {failed}")
    print(f"Batch failures (fell back to one-by-one): {batch_failures}")

    # Final verification: re-fetch the row count
    print("\nVerifying final row count...")
    final_existing = fetch_existing_event_ats()
    expected = len(existing) + posted
    print(f"  Supabase now reports {len(final_existing)} rows "
          f"(expected ~{expected})")
    if abs(len(final_existing) - expected) > 0:
        print(f"  Discrepancy: {len(final_existing) - expected} rows "
              f"unaccounted (possible event_at collisions in the source).")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
