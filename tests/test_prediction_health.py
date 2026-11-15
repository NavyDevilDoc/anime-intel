"""Tests for analysis/prediction_health.py.

Covers drift classification, per-model breakdowns, and the health
summary payload shape consumed by the report generator.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from analysis.prediction_health import (
    ALERT_RATIO,
    WARNING_RATIO,
    classify_drift,
    compute_health_summary,
    compute_per_model_breakdown,
    load_predictions_df,
    summary_to_dict,
)
from analysis.prediction_log import PredictionLogEntry, ensure_prediction_log_table, upsert_prediction


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def ready_conn(tmp_path: Path):
    """SQLite with prediction_log table ready."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    ensure_prediction_log_table(conn)
    yield conn
    conn.close()


def _entry(mal_id: int, pred: float, actual: float | None = None,
           date: str = "2026-10-11", version: str = "lin-20261011-abc-v100",
           source: str = "weekly_update") -> PredictionLogEntry:
    return PredictionLogEntry(
        mal_id=mal_id,
        prediction_date=date,
        source=source,
        model_version=version,
        predicted_score=pred,
        actual_score_at_time=actual,
        actual_scored_by_at_time=10000 if actual is not None else None,
        prediction_horizon_days=14,
    )


def _seed(conn: sqlite3.Connection, entries: list[PredictionLogEntry]) -> None:
    for e in entries:
        upsert_prediction(conn, e)


# -----------------------------------------------------------------------------
# Drift classification
# -----------------------------------------------------------------------------


class TestClassifyDrift:
    def test_insufficient_data_when_mae_is_none(self):
        assert classify_drift(None, baseline_mae=0.44, n_with_actual=100) == "insufficient_data"

    def test_insufficient_data_when_sample_too_small(self):
        # Below min_sample (default 10) → insufficient regardless of mae value
        assert classify_drift(0.4, baseline_mae=0.44, n_with_actual=5) == "insufficient_data"

    def test_healthy_at_baseline(self):
        assert classify_drift(0.44, baseline_mae=0.44, n_with_actual=100) == "healthy"

    def test_healthy_below_baseline(self):
        # Model performing BETTER than baseline is healthy
        assert classify_drift(0.33, baseline_mae=0.44, n_with_actual=100) == "healthy"

    def test_warning_just_above_warning_ratio(self):
        baseline = 0.44
        # Just above warning threshold = warning
        just_warning = baseline * WARNING_RATIO + 0.001
        assert classify_drift(just_warning, baseline, n_with_actual=100) == "warning"

    def test_warning_at_boundary_rounds_healthy(self):
        baseline = 0.44
        # Exactly at warning_ratio × baseline is still healthy (<=  check)
        at_warning = baseline * WARNING_RATIO
        assert classify_drift(at_warning, baseline, n_with_actual=100) == "healthy"

    def test_alert_when_above_alert_ratio(self):
        baseline = 0.44
        above_alert = baseline * ALERT_RATIO + 0.001
        assert classify_drift(above_alert, baseline, n_with_actual=100) == "alert"

    def test_alert_at_boundary_still_warning(self):
        baseline = 0.44
        at_alert = baseline * ALERT_RATIO
        assert classify_drift(at_alert, baseline, n_with_actual=100) == "warning"


# -----------------------------------------------------------------------------
# Per-model breakdown
# -----------------------------------------------------------------------------


class TestPerModelBreakdown:
    def test_empty_df_returns_empty_list(self, ready_conn):
        df = load_predictions_df(ready_conn)
        assert compute_per_model_breakdown(df, baseline_mae=0.44) == []

    def test_single_model_single_prediction_insufficient(self, ready_conn):
        _seed(ready_conn, [_entry(mal_id=1, pred=7.0, actual=7.1)])
        df = load_predictions_df(ready_conn)
        rows = compute_per_model_breakdown(df, baseline_mae=0.44)
        assert len(rows) == 1
        assert rows[0].n_predictions == 1
        assert rows[0].drift_status == "insufficient_data"

    def test_single_model_well_calibrated(self, ready_conn):
        # 20 predictions clustered around the actual with ~0.3 error — healthy
        for i in range(20):
            _seed(ready_conn, [_entry(mal_id=i, pred=7.0, actual=7.2)])
        df = load_predictions_df(ready_conn)
        rows = compute_per_model_breakdown(df, baseline_mae=0.44)
        assert rows[0].drift_status == "healthy"

    def test_multiple_models_sorted_newest_first(self, ready_conn):
        _seed(ready_conn, [
            _entry(mal_id=1, pred=7.0, actual=7.1, version="lin-20260101-aaa-v100"),
            _entry(mal_id=2, pred=7.0, actual=7.1, version="lin-20260601-bbb-v200"),
            _entry(mal_id=3, pred=7.0, actual=7.1, version="lin-20260301-ccc-v150"),
        ])
        df = load_predictions_df(ready_conn)
        rows = compute_per_model_breakdown(df, baseline_mae=0.44)
        # Version string sort descending → June first, then March, then January
        assert rows[0].model_version == "lin-20260601-bbb-v200"
        assert rows[1].model_version == "lin-20260301-ccc-v150"
        assert rows[2].model_version == "lin-20260101-aaa-v100"

    def test_null_actuals_excluded_from_mae(self, ready_conn):
        # 15 resolved + 5 preseason entries. Only 15 count for MAE.
        for i in range(15):
            _seed(ready_conn, [_entry(mal_id=i, pred=7.0, actual=7.3)])
        for i in range(15, 20):
            _seed(ready_conn, [_entry(mal_id=i, pred=7.0, actual=None, source="preseason_initial")])
        df = load_predictions_df(ready_conn)
        rows = compute_per_model_breakdown(df, baseline_mae=0.44)
        assert rows[0].n_predictions == 20
        assert rows[0].n_with_actual == 15


# -----------------------------------------------------------------------------
# Window filter
# -----------------------------------------------------------------------------


class TestWindowFilter:
    def test_since_days_filters_old_entries(self, ready_conn):
        as_of = datetime(2026, 10, 11, tzinfo=timezone.utc)
        old_date = (as_of - timedelta(days=60)).strftime("%Y-%m-%d")
        new_date = (as_of - timedelta(days=5)).strftime("%Y-%m-%d")
        _seed(ready_conn, [
            _entry(mal_id=1, pred=7.0, actual=7.1, date=old_date),
            _entry(mal_id=2, pred=7.0, actual=7.1, date=new_date),
        ])
        df_window = load_predictions_df(ready_conn, since_days=30, as_of=as_of)
        assert len(df_window) == 1
        assert df_window.iloc[0]["mal_id"] == 2


# -----------------------------------------------------------------------------
# Full health summary
# -----------------------------------------------------------------------------


class TestHealthSummary:
    def test_empty_log_returns_insufficient_data(self, ready_conn):
        summary = compute_health_summary(ready_conn, baseline_mae=0.44)
        assert summary.total_predictions == 0
        assert summary.n_model_versions == 0
        assert summary.overall_rolling_mae is None
        assert summary.overall_drift_status == "insufficient_data"
        assert summary.per_model == []

    def test_populated_log_fills_summary(self, ready_conn):
        for i in range(15):
            _seed(ready_conn, [_entry(mal_id=i, pred=7.0, actual=7.2)])
        summary = compute_health_summary(ready_conn, baseline_mae=0.44)
        assert summary.total_predictions == 15
        assert summary.n_model_versions == 1
        assert summary.overall_rolling_mae is not None
        assert summary.overall_drift_status == "healthy"
        assert summary.warning_threshold == pytest.approx(0.44 * WARNING_RATIO, abs=1e-6)
        assert summary.alert_threshold == pytest.approx(0.44 * ALERT_RATIO, abs=1e-6)

    def test_summary_to_dict_is_json_round_trippable(self, ready_conn):
        import json
        _seed(ready_conn, [_entry(mal_id=i, pred=7.0, actual=7.2) for i in range(12)])
        summary = compute_health_summary(ready_conn, baseline_mae=0.44)
        d = summary_to_dict(summary)
        # Must serialize to valid JSON and back
        s = json.dumps(d)
        back = json.loads(s)
        assert back["total_predictions"] == 12
        assert back["overall_drift_status"] == summary.overall_drift_status

    def test_window_days_narrows_rolling_but_not_per_model(self, ready_conn):
        as_of = datetime(2026, 10, 11, tzinfo=timezone.utc)
        old_date = (as_of - timedelta(days=60)).strftime("%Y-%m-%d")
        new_date = (as_of - timedelta(days=5)).strftime("%Y-%m-%d")
        # 11 old rows + 11 new rows, same model
        for i in range(11):
            _seed(ready_conn, [_entry(mal_id=i, pred=7.0, actual=7.1, date=old_date)])
        for i in range(11, 22):
            _seed(ready_conn, [_entry(mal_id=i, pred=7.0, actual=7.3, date=new_date)])
        summary = compute_health_summary(ready_conn, baseline_mae=0.44,
                                         window_days=30, as_of=as_of)
        # per_model (full log) sees 22 predictions; window sees 11
        assert summary.per_model[0].n_predictions == 22
        # overall rolling (window) counts 11
        # Rolling MAE should match the new-batch error (0.3)
        assert summary.overall_rolling_mae == pytest.approx(0.3, abs=1e-6)
