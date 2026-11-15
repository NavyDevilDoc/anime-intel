"""Compute health + drift metrics from the score_prediction_log table.

Consumed by `tools/report_prediction_health.py` (monthly health report
generator) and `.github/workflows/monthly-hype-retrain.yml` (retrain
pipeline that uses the drift status to decide whether to commit a new
model or just open an issue).

Thresholds:
    healthy  — rolling_mae <= baseline_mae × 1.10
    warning  — 1.10 × baseline < rolling_mae <= 1.20 × baseline
    alert    — rolling_mae > baseline_mae × 1.20

Baseline is the trained model's `test_mae` from the metadata JSON — the
accuracy the model achieved on its held-out test set at train time.
Rolling MAE is the accuracy observed on the LIVE prediction log over
a trailing window (default 30 days). The two measure different things
(baseline = how well the model fit the historical vault; rolling = how
well predictions track reality for currently-airing shows), but when
rolling drifts materially above baseline, something has changed in the
vault composition that the model wasn't trained for.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# Drift thresholds — multiplicative factors on the model's baseline test MAE.
# At baseline 0.44, these put the warning band at [0.484, 0.528] and the alert
# threshold at > 0.528. Tune these if a different cost model emerges from
# accumulated history (e.g., production feedback shows the model is more
# tolerant of upper-band drift than lower-band).
WARNING_RATIO = 1.10
ALERT_RATIO = 1.20


@dataclass
class ModelDriftStatus:
    """One row of the per-model-version health breakdown."""
    model_version: str
    n_predictions: int
    n_with_actual: int           # predictions where actual_score_at_time is not NULL
    date_range_start: str | None
    date_range_end: str | None
    rolling_mae: float | None    # None when n_with_actual == 0
    rolling_rmse: float | None
    drift_status: str            # 'healthy' / 'warning' / 'alert' / 'insufficient_data'


@dataclass
class HealthSummary:
    """Full report payload returned by compute_health_summary."""
    generated_at: str
    baseline_mae: float
    warning_threshold: float
    alert_threshold: float
    window_days: int
    total_predictions: int
    n_model_versions: int
    overall_rolling_mae: float | None
    overall_rolling_rmse: float | None
    overall_drift_status: str
    per_model: list[ModelDriftStatus] = field(default_factory=list)


# -----------------------------------------------------------------------------
# Data loading
# -----------------------------------------------------------------------------


def load_predictions_df(
    conn: sqlite3.Connection,
    since_days: int | None = None,
    as_of: datetime | None = None,
) -> pd.DataFrame:
    """Return the prediction_log rows as a DataFrame.

    `since_days` filters to predictions made within the last N days (useful
    for rolling-window computations). `as_of` lets callers freeze the clock
    for testing — defaults to now.
    """
    if as_of is None:
        as_of = datetime.now(timezone.utc)

    if since_days is not None:
        cutoff = (as_of - timedelta(days=since_days)).strftime("%Y-%m-%d")
        query = (
            "SELECT mal_id, prediction_date, source, model_version, "
            "predicted_score, actual_score_at_time, actual_scored_by_at_time, "
            "prediction_horizon_days "
            "FROM score_prediction_log WHERE prediction_date >= ?"
        )
        return pd.read_sql_query(query, conn, params=(cutoff,))
    return pd.read_sql_query(
        "SELECT mal_id, prediction_date, source, model_version, "
        "predicted_score, actual_score_at_time, actual_scored_by_at_time, "
        "prediction_horizon_days "
        "FROM score_prediction_log",
        conn,
    )


# -----------------------------------------------------------------------------
# Metric computation
# -----------------------------------------------------------------------------


def _mae_and_rmse(df: pd.DataFrame) -> tuple[float | None, float | None]:
    """Compute MAE + RMSE on rows where actual_score_at_time is not NULL."""
    resolved = df.dropna(subset=["actual_score_at_time"])
    if resolved.empty:
        return None, None
    errors = resolved["predicted_score"] - resolved["actual_score_at_time"]
    mae = float(errors.abs().mean())
    rmse = float(np.sqrt((errors ** 2).mean()))
    return mae, rmse


def classify_drift(
    rolling_mae: float | None,
    baseline_mae: float,
    n_with_actual: int,
    min_sample: int = 10,
) -> str:
    """Map a rolling MAE to a drift status label.

    Returns `insufficient_data` when `n_with_actual < min_sample` — rolling
    MAE on fewer than 10 resolved predictions is dominated by variance, not
    signal.
    """
    if rolling_mae is None or n_with_actual < min_sample:
        return "insufficient_data"
    if rolling_mae <= baseline_mae * WARNING_RATIO:
        return "healthy"
    if rolling_mae <= baseline_mae * ALERT_RATIO:
        return "warning"
    return "alert"


def compute_per_model_breakdown(
    df: pd.DataFrame,
    baseline_mae: float,
) -> list[ModelDriftStatus]:
    """One ModelDriftStatus per model_version seen in the log."""
    if df.empty:
        return []
    results: list[ModelDriftStatus] = []
    for version, group in df.groupby("model_version"):
        n_pred = len(group)
        n_with_actual = int(group["actual_score_at_time"].notna().sum())
        mae, rmse = _mae_and_rmse(group)
        status = classify_drift(mae, baseline_mae, n_with_actual)
        results.append(ModelDriftStatus(
            model_version=version,
            n_predictions=n_pred,
            n_with_actual=n_with_actual,
            date_range_start=str(group["prediction_date"].min()) if n_pred else None,
            date_range_end=str(group["prediction_date"].max()) if n_pred else None,
            rolling_mae=round(mae, 4) if mae is not None else None,
            rolling_rmse=round(rmse, 4) if rmse is not None else None,
            drift_status=status,
        ))
    # Sort newest model first (by version string — includes training date)
    results.sort(key=lambda s: s.model_version, reverse=True)
    return results


def compute_health_summary(
    conn: sqlite3.Connection,
    baseline_mae: float,
    window_days: int = 30,
    as_of: datetime | None = None,
) -> HealthSummary:
    """Full health computation. Returns a HealthSummary ready for serialization."""
    if as_of is None:
        as_of = datetime.now(timezone.utc)

    df_window = load_predictions_df(conn, since_days=window_days, as_of=as_of)
    overall_mae, overall_rmse = _mae_and_rmse(df_window)
    overall_n_with_actual = int(df_window["actual_score_at_time"].notna().sum()) if not df_window.empty else 0
    overall_status = classify_drift(overall_mae, baseline_mae, overall_n_with_actual)

    # Per-model breakdown uses the FULL log (not just the window) so historical
    # models' aggregate stats are preserved even after they're superseded.
    df_full = load_predictions_df(conn, since_days=None, as_of=as_of)

    return HealthSummary(
        generated_at=as_of.isoformat(),
        baseline_mae=round(baseline_mae, 4),
        warning_threshold=round(baseline_mae * WARNING_RATIO, 4),
        alert_threshold=round(baseline_mae * ALERT_RATIO, 4),
        window_days=window_days,
        total_predictions=len(df_full),
        n_model_versions=int(df_full["model_version"].nunique()) if not df_full.empty else 0,
        overall_rolling_mae=round(overall_mae, 4) if overall_mae is not None else None,
        overall_rolling_rmse=round(overall_rmse, 4) if overall_rmse is not None else None,
        overall_drift_status=overall_status,
        per_model=compute_per_model_breakdown(df_full, baseline_mae),
    )


def summary_to_dict(summary: HealthSummary) -> dict[str, Any]:
    """Serialize for JSON dumps + templating."""
    d = asdict(summary)
    d["per_model"] = [asdict(m) for m in summary.per_model]
    return d
