"""
TOOL: backfill_subtypes.py
Dual-mode subtype-data lifecycle owner.

Modes:
    --initial (default)  Fill subtype where NULL. One-shot first run,
                         resumable on crash, idempotent on re-invocation.
                         Seeds from data/series_subtype_cache.json before
                         hitting Jikan to skip ~863 already-known rows.
    --refresh            Re-check every row against Jikan, UPDATE on diff,
                         append each diff to data/subtype_drift_log.csv.
                         Runs quarterly via the seasonal-ingest GitHub
                         Actions workflow.

Three-state write semantics (matches the empty-string-vs-NULL contract
established by the rating retrofit):
    Jikan returns a subtype string -> store verbatim
    Jikan returns null             -> store "" (checked, MAL has nothing)
    HTTP/network error             -> skip (leave NULL, next run retries)

Usage:
    python tools/backfill_subtypes.py                 # initial mode, full
    python tools/backfill_subtypes.py --test          # initial mode, 5 shows
    python tools/backfill_subtypes.py --refresh       # drift check, full
    python tools/backfill_subtypes.py --refresh --test  # drift check, 5 shows
"""

import argparse
import csv
import datetime
import json
import logging
import os
import sqlite3
import sys

SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from src.jikan_client import JikanClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

DB_PATH = os.path.join(ROOT_DIR, "data", "anime_intelligence_v2.db")
CACHE_PATH = os.path.join(ROOT_DIR, "data", "series_subtype_cache.json")
DRIFT_LOG_PATH = os.path.join(ROOT_DIR, "data", "subtype_drift_log.csv")
DRIFT_LOG_HEADER = ["timestamp", "mal_id", "title", "old_subtype", "new_subtype"]


def ensure_subtype_column(conn):
    """Idempotent: adds subtype TEXT column if not present."""
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(anime_info)")
    columns = {row[1] for row in cursor.fetchall()}
    if "subtype" not in columns:
        cursor.execute("ALTER TABLE anime_info ADD COLUMN subtype TEXT")
        conn.commit()
        logger.info("Added 'subtype' column to anime_info.")


def _load_subtype_cache():
    """Load the clustering-time subtype cache. Empty dict if missing."""
    if not os.path.exists(CACHE_PATH):
        return {}
    with open(CACHE_PATH, encoding="utf-8") as fh:
        raw = json.load(fh)
    # Cache keys may be strings (post-PR-#77) or ints (legacy). Normalize.
    return {int(k): v for k, v in raw.items()}


def _seed_from_cache(conn):
    """Apply cache values to any NULL subtype rows whose mal_id is cached.

    The cache is the clustering algorithm's view of subtype — already
    vetted during PR #77 enrichment. Seeding from it before Jikan ensures
    the new column agrees with the clustering view AND saves ~863 Jikan
    calls during initial backfill.
    """
    cache = _load_subtype_cache()
    if not cache:
        logger.info("Subtype cache empty or missing; skipping seed step.")
        return 0

    cursor = conn.cursor()
    cursor.execute("SELECT id FROM anime_info WHERE subtype IS NULL")
    null_ids = {row[0] for row in cursor.fetchall()}
    overlap = null_ids & set(cache.keys())

    if not overlap:
        logger.info(f"Cache has {len(cache)} entries; 0 overlap with NULL rows.")
        return 0

    for mal_id in overlap:
        cursor.execute(
            "UPDATE anime_info SET subtype = ? WHERE id = ?",
            (cache[mal_id], mal_id),
        )
    conn.commit()
    logger.info(f"Seeded subtype from cache: {len(overlap)} rows.")
    return len(overlap)


def _append_drift_log(mal_id, title, old, new, log_path=DRIFT_LOG_PATH):
    """Append one drift event to the CSV, writing the header on first use."""
    file_exists = os.path.exists(log_path)
    with open(log_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(DRIFT_LOG_HEADER)
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        writer.writerow([ts, mal_id, title, old, new])


def run_initial(conn, test_mode=False, client=None):
    """First-fill mode: seed from cache, then Jikan-fill the remaining NULLs."""
    _seed_from_cache(conn)

    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, english_title FROM anime_info "
        "WHERE subtype IS NULL ORDER BY id"
    )
    rows = cursor.fetchall()
    total = len(rows)

    if total == 0:
        logger.info("Initial backfill: all rows already have subtype populated.")
        return

    if test_mode:
        rows = rows[:5]
        logger.info(f"TEST MODE: processing first 5 of {total} shows missing subtype.")
    else:
        logger.info(f"Found {total} shows missing subtype data after cache seed.")

    if client is None:
        client = JikanClient()
        client.rate_limit_delay = 0.5

    success = 0
    failed = 0
    no_subtype = 0

    for i, (mal_id, title) in enumerate(rows, 1):
        result = client.get_anime_detail(mal_id)

        if result.is_error:
            failed += 1
            logger.warning(f"  [{i}/{len(rows)}] {title}: FETCH FAILED (ID {mal_id}) — {result.summary}")
            continue

        subtype = result.value.media_type
        cursor.execute(
            "UPDATE anime_info SET subtype = ? WHERE id = ?",
            (subtype, mal_id),
        )
        conn.commit()
        success += 1

        if subtype == "":
            no_subtype += 1
            display = "(no subtype set on MAL)"
        else:
            display = subtype
        logger.info(f"  [{i}/{len(rows)}] {title}: {display}")

        if i % 100 == 0:
            logger.info(
                f"--- Progress: {i}/{len(rows)} | "
                f"Success: {success} | Failed: {failed} | No subtype: {no_subtype} ---"
            )

    logger.info(
        f"Initial backfill complete. Success: {success}, Failed: {failed}, "
        f"No subtype: {no_subtype}, Total processed: {len(rows)}."
    )


def run_refresh(conn, test_mode=False, client=None, log_path=DRIFT_LOG_PATH):
    """Drift check: re-check ALL rows, UPDATE on diff, append CSV log."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, english_title, subtype FROM anime_info ORDER BY id"
    )
    rows = cursor.fetchall()
    total = len(rows)

    if test_mode:
        rows = rows[:5]
        logger.info(f"TEST MODE: refreshing first 5 of {total} shows.")
    else:
        logger.info(f"Refreshing subtype data for {total} shows.")

    if client is None:
        client = JikanClient()
        client.rate_limit_delay = 0.5

    checked = 0
    updated = 0
    failed = 0

    for i, (mal_id, title, old_subtype) in enumerate(rows, 1):
        result = client.get_anime_detail(mal_id)

        if result.is_error:
            failed += 1
            logger.warning(f"  [{i}/{len(rows)}] {title}: FETCH FAILED (ID {mal_id}) — {result.summary}")
            continue

        new_subtype = result.value.media_type
        checked += 1

        # NULL in DB and "" both mean "no subtype known." Treat them
        # identically so a NULL->"" transition is not a spurious drift.
        old_normalized = old_subtype if old_subtype is not None else ""

        if old_normalized != new_subtype:
            cursor.execute(
                "UPDATE anime_info SET subtype = ? WHERE id = ?",
                (new_subtype, mal_id),
            )
            conn.commit()
            updated += 1
            _append_drift_log(mal_id, title, old_normalized, new_subtype, log_path=log_path)
            logger.info(
                f"  [{i}/{len(rows)}] DRIFT: {title} (#{mal_id}): "
                f"'{old_normalized}' -> '{new_subtype}'"
            )

        if i % 100 == 0:
            logger.info(
                f"--- Progress: {i}/{len(rows)} | "
                f"Checked: {checked} | Updated: {updated} | Failed: {failed} ---"
            )

    logger.info(
        f"Drift refresh complete: {checked} checked, {updated} updated, {failed} errors."
    )


def main():
    parser = argparse.ArgumentParser(
        description="MAL subtype backfill (initial fill) or drift refresh (quarterly check)."
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Drift-refresh mode: re-check all rows, log diffs. Default is --initial.",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Test mode: process only the first 5 shows.",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    ensure_subtype_column(conn)

    if args.refresh:
        run_refresh(conn, test_mode=args.test)
    else:
        run_initial(conn, test_mode=args.test)

    conn.close()


if __name__ == "__main__":
    main()
