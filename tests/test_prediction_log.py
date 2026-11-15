"""Tests for analysis/prediction_log.py.

Covers the schema setup, upsert semantics (including the ON CONFLICT
behavior that makes multiple same-day runs idempotent), the source
enum CHECK constraint, and the season-tag helpers that drive the
weekly cron's "currently airing" cohort selection.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from analysis.prediction_log import (
    PredictionLogEntry,
    VALID_SOURCES,
    days_since_season_start,
    ensure_prediction_log_table,
    previous_season_tag,
    season_tag_for_date,
    upsert_prediction,
)


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    yield conn
    conn.close()


@pytest.fixture
def ready_conn(conn):
    """Connection with the prediction_log table already created."""
    ensure_prediction_log_table(conn)
    return conn


def _make_entry(**overrides) -> PredictionLogEntry:
    """Build a plausible entry; overrides change specific fields."""
    defaults = dict(
        mal_id=59193,
        prediction_date="2026-10-11",
        source="weekly_update",
        model_version="lin-20261011-f7568c-v5324",
        predicted_score=8.35,
        actual_score_at_time=8.69,
        actual_scored_by_at_time=45000,
        prediction_horizon_days=10,
    )
    defaults.update(overrides)
    return PredictionLogEntry(**defaults)


# -----------------------------------------------------------------------------
# Schema setup
# -----------------------------------------------------------------------------


class TestEnsureTable:
    def test_creates_table_on_fresh_db(self, conn):
        ensure_prediction_log_table(conn)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='score_prediction_log'"
        ).fetchall()
        assert len(rows) == 1

    def test_is_idempotent(self, conn):
        ensure_prediction_log_table(conn)
        # Should not raise on second call.
        ensure_prediction_log_table(conn)
        tables = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name='score_prediction_log'"
        ).fetchone()[0]
        assert tables == 1

    def test_creates_indexes(self, conn):
        ensure_prediction_log_table(conn)
        indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='score_prediction_log' AND name LIKE 'idx_%'"
        ).fetchall()
        index_names = {r[0] for r in indexes}
        assert "idx_spl_model_version" in index_names
        assert "idx_spl_mal_id" in index_names


# -----------------------------------------------------------------------------
# Upsert semantics
# -----------------------------------------------------------------------------


class TestUpsert:
    def test_insert_new_row(self, ready_conn):
        upsert_prediction(ready_conn, _make_entry())
        count = ready_conn.execute(
            "SELECT COUNT(*) FROM score_prediction_log"
        ).fetchone()[0]
        assert count == 1

    def test_same_day_rerun_overwrites_not_duplicates(self, ready_conn):
        # First prediction: delta +0.5
        upsert_prediction(ready_conn, _make_entry(predicted_score=7.5))
        # Rerun same day: delta different (model drifted or new data arrived)
        upsert_prediction(ready_conn, _make_entry(predicted_score=7.8))
        rows = ready_conn.execute(
            "SELECT predicted_score FROM score_prediction_log "
            "WHERE mal_id=? AND prediction_date=?",
            (59193, "2026-10-11"),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 7.8

    def test_different_days_coexist(self, ready_conn):
        upsert_prediction(ready_conn, _make_entry(prediction_date="2026-10-11"))
        upsert_prediction(ready_conn, _make_entry(prediction_date="2026-10-18"))
        count = ready_conn.execute(
            "SELECT COUNT(*) FROM score_prediction_log WHERE mal_id=?",
            (59193,),
        ).fetchone()[0]
        assert count == 2

    def test_different_mal_ids_coexist(self, ready_conn):
        upsert_prediction(ready_conn, _make_entry(mal_id=59193))
        upsert_prediction(ready_conn, _make_entry(mal_id=52991))
        count = ready_conn.execute(
            "SELECT COUNT(*) FROM score_prediction_log WHERE prediction_date=?",
            ("2026-10-11",),
        ).fetchone()[0]
        assert count == 2

    def test_invalid_source_raises(self, ready_conn):
        with pytest.raises(ValueError, match="Invalid source"):
            upsert_prediction(ready_conn, _make_entry(source="not_a_real_source"))

    def test_schema_check_constraint_also_rejects_invalid_source(self, ready_conn):
        """Belt-and-suspenders: the Python validation raises first, but if a
        caller bypasses it with raw SQL, the CHECK constraint catches it."""
        with pytest.raises(sqlite3.IntegrityError):
            ready_conn.execute(
                "INSERT INTO score_prediction_log "
                "(mal_id, prediction_date, source, model_version, predicted_score) "
                "VALUES (?, ?, ?, ?, ?)",
                (123, "2026-10-11", "bogus_source", "v1", 7.0),
            )

    def test_actual_fields_can_be_null_for_pre_airing_shows(self, ready_conn):
        """Preseason-initial entries lack mal_score + scored_by in the vault."""
        upsert_prediction(ready_conn, _make_entry(
            source="preseason_initial",
            actual_score_at_time=None,
            actual_scored_by_at_time=None,
            prediction_horizon_days=None,
        ))
        row = ready_conn.execute(
            "SELECT actual_score_at_time, actual_scored_by_at_time "
            "FROM score_prediction_log WHERE mal_id=?",
            (59193,),
        ).fetchone()
        assert row == (None, None)


# -----------------------------------------------------------------------------
# Source enum
# -----------------------------------------------------------------------------


class TestSourceEnum:
    def test_valid_sources_set(self):
        assert "preseason_initial" in VALID_SOURCES
        assert "weekly_update" in VALID_SOURCES
        assert "final" in VALID_SOURCES
        assert len(VALID_SOURCES) == 3

    def test_all_valid_sources_pass_upsert(self, ready_conn):
        for i, src in enumerate(sorted(VALID_SOURCES)):
            upsert_prediction(ready_conn, _make_entry(
                mal_id=1000 + i, source=src,
            ))
        count = ready_conn.execute(
            "SELECT COUNT(DISTINCT source) FROM score_prediction_log"
        ).fetchone()[0]
        assert count == 3


# -----------------------------------------------------------------------------
# Season-tag helpers
# -----------------------------------------------------------------------------


class TestSeasonTagForDate:
    @pytest.mark.parametrize("month,expected_season", [
        (1, "Winter"), (2, "Winter"), (3, "Winter"),
        (4, "Spring"), (5, "Spring"), (6, "Spring"),
        (7, "Summer"), (8, "Summer"), (9, "Summer"),
        (10, "Fall"), (11, "Fall"), (12, "Fall"),
    ])
    def test_each_month_maps_to_correct_season(self, month, expected_season):
        dt = datetime(2026, month, 15, tzinfo=timezone.utc)
        assert season_tag_for_date(dt) == f"{expected_season}_2026"

    def test_default_to_current_utc(self):
        tag = season_tag_for_date()
        season, year = tag.split("_")
        assert season in {"Winter", "Spring", "Summer", "Fall"}
        assert int(year) >= 2026


class TestPreviousSeasonTag:
    @pytest.mark.parametrize("current_month,expected", [
        (10, "Summer_2026"),  # Fall -> Summer
        (7, "Spring_2026"),   # Summer -> Spring
        (4, "Winter_2026"),   # Spring -> Winter
        (1, "Fall_2025"),     # Winter -> Fall of prior year (crosses year boundary)
    ])
    def test_rolls_back_one_season_crossing_year_at_winter(
        self, current_month, expected,
    ):
        dt = datetime(2026, current_month, 15, tzinfo=timezone.utc)
        assert previous_season_tag(dt) == expected


class TestDaysSinceSeasonStart:
    def test_fall_show_counted_from_october_1(self):
        as_of = datetime(2026, 10, 11, tzinfo=timezone.utc)
        assert days_since_season_start("Fall_2026", as_of) == 10

    def test_shows_from_previous_season_count_beyond_90_days(self):
        as_of = datetime(2026, 10, 11, tzinfo=timezone.utc)
        # Summer 2026 started July 1; by Oct 11 that's 102 days
        assert days_since_season_start("Summer_2026", as_of) == 102

    def test_pre_season_shows_floor_at_zero_not_negative(self):
        # If someone runs the tool before the season officially starts
        as_of = datetime(2026, 9, 15, tzinfo=timezone.utc)
        assert days_since_season_start("Fall_2026", as_of) == 0

    def test_unparseable_season_returns_none(self):
        assert days_since_season_start("NotASeason") is None
        assert days_since_season_start("Winter") is None
        assert days_since_season_start("") is None
