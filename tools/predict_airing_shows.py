"""Weekly hype-prediction logging tool.

Runs via the `weekly-airing-predictions.yml` GH Action every Monday
(and ad-hoc via workflow_dispatch). For each show in the "currently
relevant" cohort (current season + previous season by default), loads
the latest trained HypePredictor, runs inference, snapshots the
show's current actual mal_score + scored_by, and UPSERTs a row into
`score_prediction_log`.

The composite PK (mal_id, prediction_date) means re-running on the
same day overwrites rather than duplicates — safe to retry after a
crash or dispatch manually mid-week.

CLI:
    python tools/predict_airing_shows.py
        Default: current + previous season, source=weekly_update,
        latest model.

    python tools/predict_airing_shows.py --season Fall_2026
        Only predict for one specific season (overrides the two-season
        rolling window).

    python tools/predict_airing_shows.py --dry-run
        Compute everything, print the summary, DO NOT write.

    python tools/predict_airing_shows.py --test
        Limit to first 5 matched shows, implies --dry-run unless
        --no-dry-run also passed.

    python tools/predict_airing_shows.py --source preseason_initial
        Override the default source enum. Normal weekly runs leave
        this as 'weekly_update'; use for backfilling or special runs.

Exit codes:
    0 - all shows predicted + logged successfully
    1 - predictor loading failed (no model under data/models/)
    2 - vault query returned zero airing shows (not necessarily an
        error — could mean the week between a late-fall + early-winter
        transition — but worth surfacing as nonzero exit for CI logs)

Rolling-window rationale: new seasons premiere ~week 1; shows from
the PREVIOUS season are still actively accumulating ratings for 2-3
weeks after their finale. Including the previous season tag means
we capture the "post-finale rating settle" period that's valuable
training ground for retrospective MAE analysis.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from analysis.hype_predictor import DB_PATH, MODELS_DIR, HypePredictor  # noqa: E402
from analysis.prediction_log import (  # noqa: E402
    PredictionLogEntry,
    days_since_season_start,
    ensure_prediction_log_table,
    previous_season_tag,
    season_tag_for_date,
    upsert_prediction,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def _resolve_target_seasons(explicit_season: str | None,
                            as_of: datetime) -> list[str]:
    """Build the list of season tags to query for.

    --season X overrides. Default is current + previous season.
    """
    if explicit_season:
        return [explicit_season]
    return [season_tag_for_date(as_of), previous_season_tag(as_of)]


def _fetch_airing_shows(conn: sqlite3.Connection, seasons: list[str]) -> list[dict]:
    """Query vault for shows whose season tag is in the target list.

    Only returns shows that are predictor-eligible — consensus_json +
    source both populated. Shows without consensus_json cannot be
    predicted (the model's feature set assumes staff cascade is
    available, which lands only after full ingest completes).
    """
    placeholders = ",".join("?" for _ in seasons)
    cursor = conn.cursor()
    cursor.execute(
        f"""
        SELECT id, english_title, season, mal_score, scored_by
        FROM anime_info
        WHERE season IN ({placeholders})
          AND consensus_json IS NOT NULL
        ORDER BY id
        """,
        seasons,
    )
    return [
        {
            "mal_id": row[0],
            "title": row[1],
            "season": row[2],
            "mal_score": row[3],
            "scored_by": row[4],
        }
        for row in cursor.fetchall()
    ]


def main():
    parser = argparse.ArgumentParser(
        description="Weekly hype-prediction logging for currently-airing shows."
    )
    parser.add_argument("--season", help="Explicit season tag (e.g., Fall_2026). "
                                          "Default: current + previous season.")
    parser.add_argument("--source", default="weekly_update",
                        choices=["preseason_initial", "weekly_update", "final"],
                        help="Prediction source enum (default: weekly_update)")
    parser.add_argument("--test", action="store_true",
                        help="Limit to first 5 shows. Implies --dry-run unless --no-dry-run.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute predictions but don't write to DB.")
    parser.add_argument("--no-dry-run", action="store_true",
                        help="Force writes even with --test.")
    parser.add_argument("--db", default=str(DB_PATH), help="Vault DB path.")
    parser.add_argument("--models-dir", default=str(MODELS_DIR),
                        help="Trained-model directory.")
    args = parser.parse_args()

    dry_run = args.dry_run or (args.test and not args.no_dry_run)

    # Load predictor
    try:
        predictor = HypePredictor.load_latest(model_dir=Path(args.models_dir))
    except FileNotFoundError as e:
        logger.error(f"No trained model found: {e}")
        return 1

    model_version = predictor.metadata.model_version
    logger.info(f"Loaded model: {model_version}")

    # Resolve seasons + fetch airing shows
    as_of = datetime.now(timezone.utc)
    seasons = _resolve_target_seasons(args.season, as_of)
    logger.info(f"Target seasons: {seasons}")

    conn = sqlite3.connect(args.db)
    ensure_prediction_log_table(conn)
    shows = _fetch_airing_shows(conn, seasons)
    logger.info(f"Found {len(shows)} currently-relevant shows")

    if not shows:
        conn.close()
        logger.warning("Zero airing shows matched the target seasons — "
                       "nothing to log.")
        return 2

    if args.test:
        shows = shows[:5]
        logger.info(f"TEST MODE: limiting to {len(shows)} shows "
                    f"(dry_run={dry_run})")

    # Prediction loop
    today_iso = as_of.strftime("%Y-%m-%d")
    n_logged = 0
    n_errors = 0
    for show in shows:
        try:
            predicted = predictor.predict_from_db(show["mal_id"], db_path=Path(args.db))
        except Exception as e:  # noqa: BLE001 — one bad row shouldn't abort the sweep
            logger.warning(f"  predict failed MAL {show['mal_id']} "
                           f"({show['title']!r}): {type(e).__name__}: {e}")
            n_errors += 1
            continue

        horizon = days_since_season_start(show["season"], as_of)
        entry = PredictionLogEntry(
            mal_id=show["mal_id"],
            prediction_date=today_iso,
            source=args.source,
            model_version=model_version,
            predicted_score=round(float(predicted), 4),
            actual_score_at_time=show["mal_score"],
            actual_scored_by_at_time=show["scored_by"],
            prediction_horizon_days=horizon,
        )

        if not dry_run:
            upsert_prediction(conn, entry)
        n_logged += 1

        delta = ""
        if show["mal_score"] is not None:
            d = predicted - show["mal_score"]
            delta = f"  (actual {show['mal_score']:.2f}, delta {d:+.2f})"
        logger.info(f"  MAL {show['mal_id']:6d} {show['title'][:40]:40s}: "
                    f"predicted {predicted:.2f}{delta}")

    conn.close()

    # Summary
    action = "WOULD LOG" if dry_run else "LOGGED"
    logger.info(f"\n{action}: {n_logged} predictions, {n_errors} errors")
    logger.info(f"Model version: {model_version}")
    logger.info(f"Prediction date: {today_iso}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
