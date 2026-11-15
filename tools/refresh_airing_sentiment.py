"""Weekly in-season sentiment refresh for the current airing cohort.

For each show in the current season, fetches the latest episode reviews
from Jikan, computes a fresh VADER-weighted avg_sentiment, and distills
a trajectory-aware consensus via Gemini's rolling-context prompt.

Write targets (intentionally split):
  - anime_info.avg_sentiment       ← updated (keeps the Encyclopedia % fresh)
  - anime_info.consensus_json      ← NOT touched (keeps the neutral ingest-time
                                      summary stable in the Encyclopedia)
  - airing_sentiment_log           ← UPSERT with trajectory-aware consensus_json
                                      (the Season Tracker reads from here)

Runs as STEP 2 in .github/workflows/weekly-airing-predictions.yml, after
refresh_airing_scores.py and before predict_airing_shows.py.

CLI:
    python tools/refresh_airing_sentiment.py
        Default: current season, live Jikan + Gemini, writes to DB.

    python tools/refresh_airing_sentiment.py --season Fall_2026
        Only refresh shows in one specific season.

    python tools/refresh_airing_sentiment.py --dry-run
        Fetch + distill, log output, do NOT write to DB.

    python tools/refresh_airing_sentiment.py --test
        First 3 shows only. Implies --dry-run unless --no-dry-run is passed.

    python tools/refresh_airing_sentiment.py --min-reviews 5
        Override the minimum review threshold (default: 3).

Exit codes:
    0 — all shows processed; skipped shows (< min-reviews) are normal, not errors
    1 — one or more Jikan or Gemini errors
    2 — zero shows matched the target season
    3 — GOOGLE_API_KEY missing; Gemini distillation impossible
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(str(ROOT_DIR / "env_variables.env"))

from analysis.prediction_log import (  # noqa: E402
    ensure_airing_sentiment_log_table,
    monday_of_week,
    season_tag_for_date,
    upsert_sentiment_log,
)
from analysis.sentiment_distiller import ReviewDistiller  # noqa: E402
from src.jikan_client import JikanClient  # noqa: E402
from src.seasonal_ingestor_v2 import SentimentEngine  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

DB_PATH = ROOT_DIR / "data" / "anime_intelligence_v2.db"
DEFAULT_DELAY_MS = 400
MIN_REVIEWS = 3


# ---------------------------------------------------------------------------
# Cohort query
# ---------------------------------------------------------------------------

def _fetch_current_cohort(conn: sqlite3.Connection, seasons: list[str]) -> list[dict]:
    """Return shows in target seasons that have consensus_json (predictor gate)."""
    placeholders = ",".join("?" for _ in seasons)
    rows = conn.execute(
        f"""
        SELECT id, english_title, season, mal_synopsis, avg_sentiment, consensus_json
        FROM anime_info
        WHERE season IN ({placeholders})
          AND consensus_json IS NOT NULL
        ORDER BY id
        """,
        seasons,
    ).fetchall()
    return [
        {
            "mal_id": row[0],
            "title": row[1],
            "season": row[2],
            "synopsis": row[3] or "",
            "avg_sentiment": row[4],
            "consensus_json": row[5],
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Prior-consensus loader
# ---------------------------------------------------------------------------

def _load_prior_consensus(conn: sqlite3.Connection, anime_id: int) -> dict | None:
    """Return the most recent airing_sentiment_log consensus for this show.

    Falls back to anime_info.consensus_json if no log row exists yet
    (first-week behaviour). Returns None if neither is parseable.
    """
    row = conn.execute(
        "SELECT consensus_json FROM airing_sentiment_log "
        "WHERE anime_id = ? ORDER BY week_date DESC LIMIT 1",
        (anime_id,),
    ).fetchone()
    if row and row[0]:
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            pass

    # Fall back to current anime_info.consensus_json
    row2 = conn.execute(
        "SELECT consensus_json FROM anime_info WHERE id = ?",
        (anime_id,),
    ).fetchone()
    if row2 and row2[0]:
        try:
            return json.loads(row2[0])
        except (json.JSONDecodeError, TypeError):
            pass

    return None


# ---------------------------------------------------------------------------
# Core refresh logic (importable for unit tests)
# ---------------------------------------------------------------------------

async def _run_refresh_async(
    conn: sqlite3.Connection,
    jikan_client: JikanClient,
    distiller: ReviewDistiller,
    seasons: list[str],
    *,
    week_date: str,
    dry_run: bool = False,
    limit: int | None = None,
    delay_ms: int = DEFAULT_DELAY_MS,
    min_reviews: int = MIN_REVIEWS,
) -> int:
    """Async core — runs the full per-show loop inside one event loop.

    Called by run_refresh() via a single asyncio.run(). Keeping the loop
    inside one async context prevents the Google GenAI SDK's AFC machinery
    from binding internal async state to a loop that gets closed between
    per-show asyncio.run() calls (which caused "Event loop is closed" on
    the second show).
    """
    ensure_airing_sentiment_log_table(conn)
    shows = _fetch_current_cohort(conn, seasons)

    if not shows:
        logger.warning("Zero shows matched seasons %s — nothing to refresh.", seasons)
        return 2

    if limit is not None:
        shows = shows[:limit]
        logger.info("Limiting to first %d shows.", limit)

    jikan_client.rate_limit_delay = delay_ms / 1000.0
    sentiment_engine = SentimentEngine()

    n_updated = 0
    n_skipped = 0
    n_errors = 0
    total = len(shows)

    for i, show in enumerate(shows, 1):
        mal_id = show["mal_id"]
        title = show["title"]

        # --- Jikan: fetch reviews ---
        result = jikan_client.get_anime_reviews(mal_id)
        if result.is_error:
            logger.warning(
                "  [%d/%d] %s (MAL %d): Jikan error — %s",
                i, total, title, mal_id, result.summary,
            )
            n_errors += 1
            continue

        reviews = result.reviews
        if len(reviews) < min_reviews:
            logger.info(
                "  [%d/%d] %s (MAL %d): skipped — %d review(s) (< %d minimum)",
                i, total, title, mal_id, len(reviews), min_reviews,
            )
            n_skipped += 1
            continue

        # --- VADER sentiment ---
        # JikanClient.get_anime_reviews() already normalizes to {"content": ..., "score": ..., "reactions": ...}
        new_sentiment = sentiment_engine.calculate_jit_sentiment(reviews)

        # --- Gemini: rolling distillation ---
        prior_consensus = _load_prior_consensus(conn, mal_id)
        context_data = {
            "title": title,
            "synopsis": show["synopsis"],
            "reviews": [r.get("content", "") for r in reviews],
        }
        consensus = await distiller.distill_sentiment_rolling(context_data, prior_consensus)
        if consensus is None:
            logger.warning(
                "  [%d/%d] %s (MAL %d): Gemini distillation failed",
                i, total, title, mal_id,
            )
            n_errors += 1
            continue

        # --- Log line ---
        old_sent = show["avg_sentiment"]
        old_str = f"{old_sent:.3f}" if old_sent is not None else "N/A"
        delta = f"  Δ{new_sentiment - old_sent:+.3f}" if old_sent is not None else ""
        controversy = consensus.get("controversy_score", "?")
        logger.info(
            "  [%d/%d] %s (MAL %d): sentiment %s → %.3f%s, controversy %s/10",
            i, total, title, mal_id, old_str, new_sentiment, delta, controversy,
        )

        # --- Writes ---
        if not dry_run:
            conn.execute(
                "UPDATE anime_info SET avg_sentiment = ? WHERE id = ?",
                (new_sentiment, mal_id),
            )
            conn.commit()
            upsert_sentiment_log(conn, mal_id, week_date, new_sentiment, consensus)

        n_updated += 1

    action = "WOULD UPDATE" if dry_run else "UPDATED"
    logger.info(
        "\n%s: %d  SKIPPED: %d  ERRORS: %d  (of %d shows)",
        action, n_updated, n_skipped, n_errors, total,
    )

    return 1 if n_errors > 0 else 0


def run_refresh(
    conn: sqlite3.Connection,
    jikan_client: JikanClient,
    distiller: ReviewDistiller,
    seasons: list[str],
    *,
    week_date: str,
    dry_run: bool = False,
    limit: int | None = None,
    delay_ms: int = DEFAULT_DELAY_MS,
    min_reviews: int = MIN_REVIEWS,
) -> int:
    """Refresh avg_sentiment + write trajectory consensus for the airing cohort.

    Returns an exit code:
        0 — all shows processed (skips due to < min_reviews are normal)
        1 — one or more Jikan or Gemini errors
        2 — zero shows matched the cohort

    week_date is passed explicitly so tests can freeze it; callers use
    monday_of_week() to compute the canonical production value.
    """
    return asyncio.run(
        _run_refresh_async(
            conn, jikan_client, distiller, seasons,
            week_date=week_date,
            dry_run=dry_run,
            limit=limit,
            delay_ms=delay_ms,
            min_reviews=min_reviews,
        )
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Weekly in-season sentiment refresh for the current airing cohort."
    )
    parser.add_argument(
        "--season",
        help="Explicit season tag (e.g., Fall_2026). Default: current season only.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch + distill, log output, do NOT write to DB.")
    parser.add_argument("--no-dry-run", action="store_true",
                        help="Force writes even with --test.")
    parser.add_argument("--test", action="store_true",
                        help="First 3 shows only. Implies --dry-run unless --no-dry-run.")
    parser.add_argument("--db", default=str(DB_PATH), help="Path to vault DB.")
    parser.add_argument("--delay", type=int, default=DEFAULT_DELAY_MS,
                        help="Milliseconds between Jikan calls (default: 400).")
    parser.add_argument("--min-reviews", type=int, default=MIN_REVIEWS,
                        help="Minimum reviews required to trigger distillation (default: 3).")
    args = parser.parse_args()

    dry_run = args.dry_run or (args.test and not args.no_dry_run)
    limit = 3 if args.test else None

    api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if not api_key:
        logger.error(
            "GOOGLE_API_KEY is not set. Cannot initialise Gemini distiller."
        )
        return 3

    as_of = datetime.now(timezone.utc)
    seasons = [args.season] if args.season else [season_tag_for_date(as_of)]
    week_date = monday_of_week(as_of)
    logger.info(
        "Target seasons: %s  week_date: %s  dry_run=%s",
        seasons, week_date, dry_run,
    )

    try:
        jikan_client = JikanClient()
    except RuntimeError as e:
        logger.error("Cannot create JikanClient: %s", e)
        return 3

    distiller = ReviewDistiller(api_key=api_key)

    conn = sqlite3.connect(args.db)
    try:
        exit_code = run_refresh(
            conn, jikan_client, distiller, seasons,
            week_date=week_date,
            dry_run=dry_run,
            limit=limit,
            delay_ms=args.delay,
            min_reviews=args.min_reviews,
        )
    finally:
        conn.close()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
