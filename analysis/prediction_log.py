"""Persistence layer for hype-predictor time-series predictions.

Writes one row per (mal_id, prediction_date) to the `score_prediction_log`
SQLite table, keyed so idempotent re-runs within a day overwrite (not
duplicate) the row. Each row carries the prediction, the actual snapshot
at prediction time, and the model_version tag so later analyses can
restrict to a specific trained model.

The log is append-mostly over time — the weekly cron (PR #178) produces
~50-80 rows per week. Over a year that's ~2.5-4k rows. Lightweight;
no retention policy needed in the near term.

Downstream consumers (PR 3, admin dashboard):
    - 30-day rolling MAE on resolved predictions (where actual_score_at_time
      has stabilized vs the final mal_score)
    - Per-model-version accuracy trajectory charts
    - Drift alerts when rolling MAE > 1.2× the trained-model baseline MAE
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "anime_intelligence_v2.db"

# Prediction sources. Deliberately a small closed set so future consumers
# can select-filter cleanly. New values added here must have a well-defined
# semantic — "when this row is written, what's the state of the show?"
PredictionSource = Literal[
    "preseason_initial",  # First prediction, show has no mal_score yet
    "weekly_update",      # Ongoing weekly re-prediction during airing
    "final",              # Snapshot after show stops accumulating ratings
]
VALID_SOURCES: frozenset[str] = frozenset({"preseason_initial", "weekly_update", "final"})


@dataclass
class PredictionLogEntry:
    """One row of score_prediction_log — the shape of a logged prediction."""
    mal_id: int
    prediction_date: str  # ISO 8601 date (YYYY-MM-DD, UTC)
    source: PredictionSource
    model_version: str
    predicted_score: float
    actual_score_at_time: float | None  # NULL for pre-airing shows
    actual_scored_by_at_time: int | None
    prediction_horizon_days: int | None  # days since the show's season start


def ensure_prediction_log_table(conn: sqlite3.Connection) -> None:
    """Idempotent create of score_prediction_log + indexes."""
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS score_prediction_log (
            mal_id INTEGER NOT NULL,
            prediction_date TEXT NOT NULL,
            source TEXT NOT NULL
                CHECK (source IN ('preseason_initial', 'weekly_update', 'final')),
            model_version TEXT NOT NULL,
            predicted_score REAL NOT NULL,
            actual_score_at_time REAL,
            actual_scored_by_at_time INTEGER,
            prediction_horizon_days INTEGER,
            PRIMARY KEY (mal_id, prediction_date)
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_spl_model_version "
        "ON score_prediction_log(model_version)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_spl_mal_id "
        "ON score_prediction_log(mal_id)"
    )
    conn.commit()


def upsert_prediction(conn: sqlite3.Connection, entry: PredictionLogEntry) -> None:
    """UPSERT one prediction row. Idempotent on (mal_id, prediction_date).

    Re-running on the same day overwrites the row (useful for retries after
    a tool crash). ON CONFLICT update preserves PRIMARY KEY identity.
    """
    if entry.source not in VALID_SOURCES:
        raise ValueError(
            f"Invalid source {entry.source!r}; must be one of {sorted(VALID_SOURCES)}"
        )
    conn.execute(
        """
        INSERT INTO score_prediction_log
            (mal_id, prediction_date, source, model_version,
             predicted_score, actual_score_at_time, actual_scored_by_at_time,
             prediction_horizon_days)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(mal_id, prediction_date) DO UPDATE SET
            source = excluded.source,
            model_version = excluded.model_version,
            predicted_score = excluded.predicted_score,
            actual_score_at_time = excluded.actual_score_at_time,
            actual_scored_by_at_time = excluded.actual_scored_by_at_time,
            prediction_horizon_days = excluded.prediction_horizon_days
        """,
        (
            entry.mal_id,
            entry.prediction_date,
            entry.source,
            entry.model_version,
            entry.predicted_score,
            entry.actual_score_at_time,
            entry.actual_scored_by_at_time,
            entry.prediction_horizon_days,
        ),
    )
    conn.commit()


# -----------------------------------------------------------------------------
# Season-tag helpers (shared with tools/predict_airing_shows.py)
# -----------------------------------------------------------------------------

_MONTH_TO_SEASON = {
    1: "Winter", 2: "Winter", 3: "Winter",
    4: "Spring", 5: "Spring", 6: "Spring",
    7: "Summer", 8: "Summer", 9: "Summer",
    10: "Fall", 11: "Fall", 12: "Fall",
}

_SEASON_START_MONTH = {"Winter": 1, "Spring": 4, "Summer": 7, "Fall": 10}


def season_tag_for_date(dt: datetime | None = None) -> str:
    """Current season tag in anime_info.season format: 'Fall_2026'."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    return f"{_MONTH_TO_SEASON[dt.month]}_{dt.year}"


def previous_season_tag(dt: datetime | None = None) -> str:
    """One season before the given date. Fall_2026 -> Summer_2026, Winter_2026 -> Fall_2025."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    current = _MONTH_TO_SEASON[dt.month]
    order = ("Winter", "Spring", "Summer", "Fall")
    idx = order.index(current)
    if idx == 0:
        # Winter -> previous Fall of prior year
        return f"Fall_{dt.year - 1}"
    return f"{order[idx - 1]}_{dt.year}"


def days_since_season_start(season_tag: str, as_of: datetime | None = None) -> int | None:
    """Days since the (approximate) start of the show's season.

    Uses the 1st of the season-start month as a canonical anchor —
    actual anime typically start airing in the first week, so this
    tends to be within ±7 days of the real airdate. Returns None for
    unparseable season tags.
    """
    if as_of is None:
        as_of = datetime.now(timezone.utc)
    try:
        season, year_str = season_tag.split("_")
        year = int(year_str)
        start = datetime(year, _SEASON_START_MONTH[season], 1, tzinfo=timezone.utc)
    except (ValueError, KeyError):
        return None
    return max(0, (as_of - start).days)


# -----------------------------------------------------------------------------
# airing_sentiment_log — weekly in-season sentiment trail
# (Phase 2, In-Season Performance Tracker)
# -----------------------------------------------------------------------------


def ensure_airing_sentiment_log_table(conn: sqlite3.Connection) -> None:
    """Idempotent create of airing_sentiment_log + index.

    anime_info holds the CURRENT week's avg_sentiment (the live value
    shown in the Encyclopedia %). This table holds the full weekly trail
    so the Season Tracker can compute velocity and render trajectory.

    consensus_json here holds the trajectory-aware summary produced by
    distill_sentiment_rolling — it contains language like "Sentiment
    rising as episode 6 delivered..." and is NOT synced back to
    anime_info.consensus_json (which keeps the neutral ingest-time
    record for the Encyclopedia).
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS airing_sentiment_log (
            anime_id  INTEGER NOT NULL,
            week_date TEXT    NOT NULL,
            avg_sentiment REAL,
            consensus_json TEXT,
            PRIMARY KEY (anime_id, week_date)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_asl_anime_id "
        "ON airing_sentiment_log(anime_id)"
    )
    conn.commit()


def upsert_sentiment_log(
    conn: sqlite3.Connection,
    anime_id: int,
    week_date: str,
    avg_sentiment: float | None,
    consensus_json: dict | None,
) -> None:
    """UPSERT one sentiment snapshot. Idempotent on (anime_id, week_date).

    Re-running on the same Monday overwrites the row — safe to retry
    after a mid-run crash. consensus_json is stored as a JSON string;
    pass a dict and this function serialises it, or None to store NULL.
    """
    consensus_str = json.dumps(consensus_json) if consensus_json is not None else None
    conn.execute(
        """
        INSERT INTO airing_sentiment_log
            (anime_id, week_date, avg_sentiment, consensus_json)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(anime_id, week_date) DO UPDATE SET
            avg_sentiment  = excluded.avg_sentiment,
            consensus_json = excluded.consensus_json
        """,
        (anime_id, week_date, avg_sentiment, consensus_str),
    )
    conn.commit()


def monday_of_week(dt: datetime | None = None) -> str:
    """Return the ISO date string (YYYY-MM-DD) for the Monday of the given week.

    All sentiment snapshots are anchored to Monday so rows from different
    days of the same week share a single key — the cron fires on Monday
    but a manual dispatch on Wednesday produces the same week_date.
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    monday = dt - timedelta(days=dt.weekday())  # weekday() == 0 on Monday
    return monday.strftime("%Y-%m-%d")
