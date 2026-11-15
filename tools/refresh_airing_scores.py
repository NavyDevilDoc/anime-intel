"""Refresh mal_score + scored_by for the current airing cohort.

Fetches live values from Jikan for every show in the current + previous
season and writes them back to `anime_info`. This fixes the stale
`actual_score_at_time` problem in `score_prediction_log`: the ingestor
skips existing shows, so without this refresh the prediction log snapshots
the same frozen score every Monday.

Runs as the FIRST step in `.github/workflows/weekly-airing-predictions.yml`,
before `predict_airing_shows.py`. That way `predict_airing_shows.py` reads
week-fresh `mal_score` values when it snapshots `actual_score_at_time`.

CLI:
    python tools/refresh_airing_scores.py
        Default: current + previous season, live Jikan calls, writes to DB.

    python tools/refresh_airing_scores.py --season Fall_2026
        Only refresh shows in one specific season.

    python tools/refresh_airing_scores.py --dry-run
        Fetch Jikan, log diffs, do NOT write to DB.

    python tools/refresh_airing_scores.py --test
        First 5 shows only. Implies --dry-run unless --no-dry-run is passed.

    python tools/refresh_airing_scores.py --delay 200
        Override the inter-call delay in milliseconds (default: 400).

Exit codes:
    0 - all shows processed without Jikan error
    1 - one or more per-show Jikan errors (tolerated; predict step still runs)
    2 - zero airing shows matched the target seasons (between-season gap;
        matches the convention used by predict_airing_shows.py)

Cohort gate: identical to predict_airing_shows.py — season IN (current,
previous) AND consensus_json IS NOT NULL. The two tools must agree on the
cohort definition so a show skipped by the predictor is also skipped here.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(str(ROOT_DIR / "env_variables.env"))

from analysis.prediction_log import (  # noqa: E402
    previous_season_tag,
    season_tag_for_date,
)
from src.jikan_client import JikanClient, ScoreSnapshot  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

DB_PATH = ROOT_DIR / "data" / "anime_intelligence_v2.db"
DEFAULT_DELAY_MS = 400


# ---------------------------------------------------------------------------
# Cohort query
# ---------------------------------------------------------------------------

def _fetch_airing_cohort(conn: sqlite3.Connection, seasons: list[str]) -> list[dict]:
    """Return shows in the target seasons that have consensus_json.

    Gate is identical to predict_airing_shows._fetch_airing_shows so the
    two tools always operate on the same cohort.
    """
    placeholders = ",".join("?" for _ in seasons)
    rows = conn.execute(
        f"""
        SELECT id, english_title, season, mal_score, scored_by
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
            "mal_score": row[3],
            "scored_by": row[4],
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Core refresh logic (importable for unit tests)
# ---------------------------------------------------------------------------

def run_refresh(
    conn: sqlite3.Connection,
    client: JikanClient,
    seasons: list[str],
    *,
    dry_run: bool = False,
    limit: int | None = None,
    delay_ms: int = DEFAULT_DELAY_MS,
) -> int:
    """Refresh mal_score + scored_by for the airing cohort.

    Returns an exit code:
        0 — all shows processed without error
        1 — one or more per-show Jikan errors
        2 — zero shows matched the cohort

    `delay_ms` is applied between Jikan calls by the caller's JikanClient
    `rate_limit_delay`; this parameter lets tests inject 0 to run fast.
    """
    shows = _fetch_airing_cohort(conn, seasons)

    if not shows:
        logger.warning(
            "Zero airing shows matched seasons %s — nothing to refresh.", seasons
        )
        return 2

    if limit is not None:
        shows = shows[:limit]
        logger.info("Limiting to first %d shows.", limit)

    client.rate_limit_delay = delay_ms / 1000.0

    n_updated = 0
    n_unchanged = 0
    n_errors = 0
    total = len(shows)

    for i, show in enumerate(shows, 1):
        mal_id = show["mal_id"]
        title = show["title"]
        old_score = show["mal_score"]
        old_scored_by = show["scored_by"]

        result = client.get_anime_score(mal_id)

        if result.is_error:
            logger.warning(
                "  [%d/%d] %s (MAL %d): Jikan error — %s",
                i, total, title, mal_id, result.summary,
            )
            n_errors += 1
            continue

        snap: ScoreSnapshot = result.value
        new_score = snap.score
        new_scored_by = snap.scored_by

        # Build a human-readable diff for the log line.
        old_str = f"{old_score:.2f}" if old_score is not None else "N/A"
        new_str = f"{new_score:.2f}" if new_score is not None else "N/A"
        votes_str = (
            f"{new_scored_by:,}" if new_scored_by is not None else "N/A"
        )
        diff_tag = ""
        if old_score is not None and new_score is not None:
            delta = new_score - old_score
            if abs(delta) >= 0.005:
                diff_tag = f"  Δ{delta:+.2f}"

        logger.info(
            "  [%d/%d] %s (MAL %d): %s (%s votes) [was %s]%s",
            i, total, title, mal_id, new_str, votes_str, old_str, diff_tag,
        )

        if not dry_run:
            conn.execute(
                "UPDATE anime_info SET mal_score = ?, scored_by = ? WHERE id = ?",
                (new_score, new_scored_by, mal_id),
            )
            conn.commit()

        if old_score == new_score and old_scored_by == new_scored_by:
            n_unchanged += 1
        else:
            n_updated += 1

    action = "WOULD UPDATE" if dry_run else "UPDATED"
    logger.info(
        "\n%s: %d  UNCHANGED: %d  ERRORS: %d  (of %d shows)",
        action, n_updated, n_unchanged, n_errors, total,
    )

    return 1 if n_errors > 0 else 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Refresh mal_score + scored_by for the current airing cohort."
    )
    parser.add_argument(
        "--season",
        help="Explicit season tag (e.g., Fall_2026). Default: current + previous season.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch Jikan, log diffs, do NOT write to DB.",
    )
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Force writes even with --test.",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Limit to first 5 shows. Implies --dry-run unless --no-dry-run.",
    )
    parser.add_argument(
        "--db",
        default=str(DB_PATH),
        help="Path to vault DB.",
    )
    parser.add_argument(
        "--delay",
        type=int,
        default=DEFAULT_DELAY_MS,
        help="Milliseconds between Jikan calls (default: 400).",
    )
    args = parser.parse_args()

    dry_run = args.dry_run or (args.test and not args.no_dry_run)
    limit = 5 if args.test else None

    as_of = datetime.now(timezone.utc)
    if args.season:
        seasons = [args.season]
    else:
        seasons = [season_tag_for_date(as_of), previous_season_tag(as_of)]
    logger.info("Target seasons: %s  dry_run=%s", seasons, dry_run)

    try:
        client = JikanClient()
    except RuntimeError as e:
        logger.error("Cannot create JikanClient: %s", e)
        return 3

    conn = sqlite3.connect(args.db)
    try:
        exit_code = run_refresh(
            conn, client, seasons,
            dry_run=dry_run,
            limit=limit,
            delay_ms=args.delay,
        )
    finally:
        conn.close()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
