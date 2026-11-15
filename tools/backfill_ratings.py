"""
TOOL: backfill_ratings.py
Dual-mode rating-data lifecycle owner.

Modes:
    --initial (default)  Fill mal_rating where NULL. One-shot first run,
                         resumable on crash, idempotent on re-invocation.
    --refresh            Re-check every row against Jikan, UPDATE on diff,
                         append each diff to data/rating_drift_log.csv.
                         Runs quarterly via the seasonal-ingest GitHub
                         Actions workflow.

Three-state write semantics (matches the empty-string-vs-NULL contract
locked in Phase A of the rating retrofit):
    Jikan returns a rating string  -> store verbatim
    Jikan returns null             -> store "" (checked, MAL has nothing)
    HTTP/network error             -> skip (leave NULL, next run retries)

Usage:
    python tools/backfill_ratings.py                 # initial mode, full
    python tools/backfill_ratings.py --test          # initial mode, 5 shows
    python tools/backfill_ratings.py --refresh       # drift check, full
    python tools/backfill_ratings.py --refresh --test  # drift check, 5 shows
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
DRIFT_LOG_PATH = os.path.join(ROOT_DIR, "data", "rating_drift_log.csv")
DRIFT_LOG_HEADER = ["timestamp", "mal_id", "title", "old_rating", "new_rating"]


def ensure_rating_column(conn):
    """Idempotent: adds mal_rating TEXT column if not present."""
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(anime_info)")
    columns = {row[1] for row in cursor.fetchall()}
    if "mal_rating" not in columns:
        cursor.execute("ALTER TABLE anime_info ADD COLUMN mal_rating TEXT")
        conn.commit()
        logger.info("Added 'mal_rating' column to anime_info.")


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
    """First-fill mode: target NULL rows only. Idempotent across re-runs."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, english_title FROM anime_info "
        "WHERE mal_rating IS NULL ORDER BY id"
    )
    rows = cursor.fetchall()
    total = len(rows)

    if total == 0:
        logger.info("Initial backfill: all rows already have mal_rating populated.")
        return

    if test_mode:
        rows = rows[:5]
        logger.info(f"TEST MODE: processing first 5 of {total} shows missing rating.")
    else:
        logger.info(f"Found {total} shows missing rating data.")

    if client is None:
        client = JikanClient()
        client.rate_limit_delay = 0.5

    success = 0
    failed = 0
    no_rating = 0

    for i, (mal_id, title) in enumerate(rows, 1):
        result = client.get_anime_rating(mal_id)

        if result.is_error:
            failed += 1
            logger.warning(f"  [{i}/{len(rows)}] {title}: FETCH FAILED (ID {mal_id}) — {result.summary}")
            continue

        rating = result.value
        cursor.execute(
            "UPDATE anime_info SET mal_rating = ? WHERE id = ?",
            (rating, mal_id),
        )
        conn.commit()
        success += 1

        if rating == "":
            no_rating += 1
            display = "(no rating set on MAL)"
        else:
            display = rating
        logger.info(f"  [{i}/{len(rows)}] {title}: {display}")

        if i % 100 == 0:
            logger.info(
                f"--- Progress: {i}/{len(rows)} | "
                f"Success: {success} | Failed: {failed} | No rating: {no_rating} ---"
            )

    logger.info(
        f"Initial backfill complete. Success: {success}, Failed: {failed}, "
        f"No rating: {no_rating}, Total processed: {len(rows)}."
    )


def run_refresh(conn, test_mode=False, client=None, log_path=DRIFT_LOG_PATH):
    """Drift check: re-check ALL rows, UPDATE on diff, append CSV log."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, english_title, mal_rating FROM anime_info ORDER BY id"
    )
    rows = cursor.fetchall()
    total = len(rows)

    if test_mode:
        rows = rows[:5]
        logger.info(f"TEST MODE: refreshing first 5 of {total} shows.")
    else:
        logger.info(f"Refreshing rating data for {total} shows.")

    if client is None:
        client = JikanClient()
        client.rate_limit_delay = 0.5

    checked = 0
    updated = 0
    failed = 0

    for i, (mal_id, title, old_rating) in enumerate(rows, 1):
        result = client.get_anime_rating(mal_id)

        if result.is_error:
            failed += 1
            logger.warning(f"  [{i}/{len(rows)}] {title}: FETCH FAILED (ID {mal_id}) — {result.summary}")
            continue

        new_rating = result.value
        checked += 1

        # Normalize for diff. NULL in DB and "" both mean "no rating known."
        # Treat them identically so a NULL->"" transition is not a spurious drift.
        old_normalized = old_rating if old_rating is not None else ""

        if old_normalized != new_rating:
            cursor.execute(
                "UPDATE anime_info SET mal_rating = ? WHERE id = ?",
                (new_rating, mal_id),
            )
            conn.commit()
            updated += 1
            _append_drift_log(mal_id, title, old_normalized, new_rating, log_path=log_path)
            logger.info(
                f"  [{i}/{len(rows)}] DRIFT: {title} (#{mal_id}): "
                f"'{old_normalized}' -> '{new_rating}'"
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
        description="MAL rating backfill (initial fill) or drift refresh (quarterly check)."
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
    ensure_rating_column(conn)

    if args.refresh:
        run_refresh(conn, test_mode=args.test)
    else:
        run_initial(conn, test_mode=args.test)

    conn.close()


if __name__ == "__main__":
    main()
