"""
TOOL: backfill_source_material.py
Dual-mode source-material-data lifecycle owner.

Mirrors tools/backfill_subtypes.py in shape, policy, and three-state
semantics. Source material (manga / light novel / web novel / original /
etc.) is pulled from Jikan's `/anime/{id}` payload via the shared
`get_anime_detail` method so the same round-trip can serve subtype and
source backfills — no duplicate Jikan hits.

Modes:
    --initial (default)  Fill `source` where NULL. One-shot first run,
                         resumable on crash, idempotent on re-invocation.
    --refresh            Re-check every row against Jikan, UPDATE on diff,
                         append each diff to data/source_drift_log.csv.
                         Runs quarterly via the seasonal-ingest workflow.

Three-state write semantics (matches the empty-string-vs-NULL contract
established by the rating + subtype retrofits):
    Jikan returns a source string  -> store verbatim
    Jikan returns null             -> store "" (checked, MAL has nothing)
    HTTP/network error             -> skip (leave NULL, next run retries)

Observed `source` values (empirically 17 distinct, full-vault
backfill against Jikan on 2026-10-10):
    "Manga" (2459), "Original" (1467), "Light novel" (793),
    "Game" (445), "Web manga" (300), "Visual novel" (255),
    "Novel" (210), "4-koma manga" (176), "Other" (159),
    "Web novel" (123), "Unknown" (84), "Mixed media" (79),
    "Card game" (47), "Book" (29), "Picture book" (21),
    "Music" (20), "Radio" (2)
"Other" and "Unknown" are Jikan's catch-all buckets — not
synonyms; "Other" means "we know what it is and it's not any of
the above", "Unknown" means "we don't know."

Usage:
    python tools/backfill_source_material.py                 # initial, full
    python tools/backfill_source_material.py --test          # initial, 5 shows
    python tools/backfill_source_material.py --refresh       # drift check, full
    python tools/backfill_source_material.py --refresh --test  # drift, 5 shows
"""

import argparse
import csv
import datetime
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
DRIFT_LOG_PATH = os.path.join(ROOT_DIR, "data", "source_drift_log.csv")
DRIFT_LOG_HEADER = ["timestamp", "mal_id", "title", "old_source", "new_source"]


def ensure_source_column(conn):
    """Idempotent: adds source TEXT column if not present."""
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(anime_info)")
    columns = {row[1] for row in cursor.fetchall()}
    if "source" not in columns:
        cursor.execute("ALTER TABLE anime_info ADD COLUMN source TEXT")
        conn.commit()
        logger.info("Added 'source' column to anime_info.")


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
    """First-fill mode: Jikan-fill all rows with NULL `source`."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, english_title FROM anime_info "
        "WHERE source IS NULL ORDER BY id"
    )
    rows = cursor.fetchall()
    total = len(rows)

    if total == 0:
        logger.info("Initial backfill: all rows already have source populated.")
        return

    if test_mode:
        rows = rows[:5]
        logger.info(f"TEST MODE: processing first 5 of {total} shows missing source.")
    else:
        logger.info(f"Found {total} shows missing source data.")

    if client is None:
        client = JikanClient()
        client.rate_limit_delay = 0.5

    success = 0
    failed = 0
    no_source = 0

    for i, (mal_id, title) in enumerate(rows, 1):
        result = client.get_anime_detail(mal_id)

        if result.is_error:
            failed += 1
            logger.warning(f"  [{i}/{len(rows)}] {title}: FETCH FAILED (ID {mal_id}) — {result.summary}")
            continue

        source = result.value.source
        cursor.execute(
            "UPDATE anime_info SET source = ? WHERE id = ?",
            (source, mal_id),
        )
        conn.commit()
        success += 1

        if source == "":
            no_source += 1
            display = "(no source set on MAL)"
        else:
            display = source
        logger.info(f"  [{i}/{len(rows)}] {title}: {display}")

        if i % 100 == 0:
            logger.info(
                f"--- Progress: {i}/{len(rows)} | "
                f"Success: {success} | Failed: {failed} | No source: {no_source} ---"
            )

    logger.info(
        f"Initial backfill complete. Success: {success}, Failed: {failed}, "
        f"No source: {no_source}, Total processed: {len(rows)}."
    )


def run_refresh(conn, test_mode=False, client=None, log_path=DRIFT_LOG_PATH):
    """Drift check: re-check ALL rows, UPDATE on diff, append CSV log."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, english_title, source FROM anime_info ORDER BY id"
    )
    rows = cursor.fetchall()
    total = len(rows)

    if test_mode:
        rows = rows[:5]
        logger.info(f"TEST MODE: refreshing first 5 of {total} shows.")
    else:
        logger.info(f"Refreshing source data for {total} shows.")

    if client is None:
        client = JikanClient()
        client.rate_limit_delay = 0.5

    checked = 0
    updated = 0
    failed = 0

    for i, (mal_id, title, old_source) in enumerate(rows, 1):
        result = client.get_anime_detail(mal_id)

        if result.is_error:
            failed += 1
            logger.warning(f"  [{i}/{len(rows)}] {title}: FETCH FAILED (ID {mal_id}) — {result.summary}")
            continue

        new_source = result.value.source
        checked += 1

        # NULL in DB and "" both mean "no source known." Treat them
        # identically so a NULL->"" transition is not a spurious drift.
        old_normalized = old_source if old_source is not None else ""

        if old_normalized != new_source:
            cursor.execute(
                "UPDATE anime_info SET source = ? WHERE id = ?",
                (new_source, mal_id),
            )
            conn.commit()
            updated += 1
            _append_drift_log(mal_id, title, old_normalized, new_source, log_path=log_path)
            logger.info(
                f"  [{i}/{len(rows)}] DRIFT: {title} (#{mal_id}): "
                f"'{old_normalized}' -> '{new_source}'"
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
        description="MAL source-material backfill (initial fill) or drift refresh (quarterly check)."
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
    ensure_source_column(conn)

    if args.refresh:
        run_refresh(conn, test_mode=args.test)
    else:
        run_initial(conn, test_mode=args.test)

    conn.close()


if __name__ == "__main__":
    main()
