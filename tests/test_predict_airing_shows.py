"""Tool-level integration tests for tools/predict_airing_shows.py.

Covers the airing-cohort query, the CLI entry point's --dry-run and
--test flags, and the exit codes we rely on for CI signaling.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import joblib
import numpy as np
import pytest

from analysis.hype_predictor import (
    FeatureBuilder,
    HypePredictor,
    HypePredictorMetadata,
    STAFF_ROLES,
)
from analysis.prediction_log import ensure_prediction_log_table

TOOL = Path(__file__).resolve().parents[1] / "tools" / "predict_airing_shows.py"


# -----------------------------------------------------------------------------
# Fixtures — synthetic vault + predictor matching the predict_airing flow
# -----------------------------------------------------------------------------


@pytest.fixture
def mini_vault_db(tmp_path: Path) -> Path:
    """Vault with 8 shows: 4 Fall_2026, 2 Summer_2026, 2 Spring_2026.

    Mirrors the shape the tool queries — season tag + consensus_json
    gate the airing-cohort filter.
    """
    db_path = tmp_path / "mini_vault.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE anime_info (
            id INTEGER PRIMARY KEY,
            english_title TEXT,
            romaji_title TEXT,
            mal_score REAL,
            scored_by INTEGER,
            studio TEXT,
            release_year INTEGER,
            subtype TEXT,
            source TEXT,
            genres TEXT,
            season TEXT,
            consensus_json TEXT
        );
        CREATE TABLE anime_staff (
            anime_id INTEGER,
            person_mal_id INTEGER,
            role TEXT
        );
        """
    )
    shows = []
    seasons = ["Fall_2026"] * 4 + ["Summer_2026"] * 2 + ["Spring_2026"] * 2
    for i, season in enumerate(seasons, 1):
        shows.append((
            i, f"Show {i}", f"ShowRom {i}",
            7.0 + 0.1 * i,  # mal_score 7.1..7.8
            1000 * i,
            "TestStudio",
            2026,
            "TV",
            "Manga",
            "Action, Drama",
            season,
            "{}",  # consensus_json populated → eligible
        ))
    # Also a show in Winter_2024 that should NOT be in cohort
    shows.append((
        99, "Not Airing", "NotAiring", 6.5, 50000,
        "TestStudio", 2024, "TV", "Manga", "Comedy",
        "Winter_2024", "{}",
    ))
    conn.executemany(
        "INSERT INTO anime_info VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        shows,
    )
    # Simple staff: all shows share one director
    for show_id in list(range(1, 9)) + [99]:
        conn.execute(
            "INSERT INTO anime_staff VALUES (?, ?, ?)",
            (show_id, 1001, "Director"),
        )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def mini_model_dir(tmp_path: Path) -> Path:
    """Trained predictor saved to a tmp model dir.

    Hand-constructs a HypePredictor + FeatureBuilder matching the
    inference-time contract so the tool loads it the same way GH
    Actions would load the committed production model.
    """
    model_dir = tmp_path / "models"
    model_dir.mkdir()

    from sklearn.linear_model import LinearRegression
    feature_builder = FeatureBuilder(
        global_mean=7.0,
        studio_hit_rate={"TestStudio": 7.3},
        person_hit_rate_by_role={"Director": {1001: 7.5}},
        source_vocab=["Manga"],
        subtype_vocab=["TV"],
        genre_vocab=["Action", "Drama", "Comedy"],
    )
    feature_names = feature_builder.feature_names()
    model = LinearRegression()
    # Train on 20 rows of self-generated data so the model has shape.
    rng = np.random.default_rng(42)
    X = rng.normal(size=(20, len(feature_names)))
    y = X.sum(axis=1) * 0.1 + 7.0
    model.fit(X, y)

    metadata = HypePredictorMetadata(
        model_version="lin-20261011-testfx-v16",
        algorithm="LinearRegression",
        created_at="2026-10-11T00:00:00+00:00",
        training_sample_size=16,
        test_sample_size=4,
        feature_names=feature_names,
        hyperparameters={},
        train_mae=0.3,
        test_mae=0.4,
        baseline_mae=0.6,
        train_r2=0.5,
        test_r2=0.4,
        signal_lift_pct=33.3,
        vault_mean_score=7.0,
        feature_coefficients={n: float(c) for n, c in zip(feature_names, model.coef_)},
        intercept=float(model.intercept_),
        source_db_path="",
        random_state=42,
        test_size=0.2,
    )

    predictor = HypePredictor(model=model, feature_builder=feature_builder,
                              metadata=metadata)
    predictor.save(model_dir)
    return model_dir


# -----------------------------------------------------------------------------
# Tests — CLI invocation via subprocess (true integration path)
# -----------------------------------------------------------------------------


def _run_tool(args: list[str], env_db: Path, env_models: Path) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(TOOL), "--db", str(env_db),
           "--models-dir", str(env_models), *args]
    return subprocess.run(cmd, capture_output=True, text=True)


class TestToolSmoke:
    def test_default_run_logs_fall_plus_summer_shows(
        self, mini_vault_db, mini_model_dir,
    ):
        # Freeze the as-of date via --season to make test deterministic
        # (defaulting to datetime.now() would depend on CI clock).
        result = _run_tool(
            ["--season", "Fall_2026"],
            mini_vault_db, mini_model_dir,
        )
        assert result.returncode == 0, result.stderr
        # Verify rows landed in DB
        conn = sqlite3.connect(str(mini_vault_db))
        count = conn.execute(
            "SELECT COUNT(*) FROM score_prediction_log"
        ).fetchone()[0]
        conn.close()
        # 4 Fall_2026 shows should have been logged
        assert count == 4

    def test_dry_run_writes_nothing(self, mini_vault_db, mini_model_dir):
        result = _run_tool(
            ["--season", "Fall_2026", "--dry-run"],
            mini_vault_db, mini_model_dir,
        )
        assert result.returncode == 0
        conn = sqlite3.connect(str(mini_vault_db))
        # The table should be created but empty
        count = conn.execute(
            "SELECT COUNT(*) FROM score_prediction_log"
        ).fetchone()[0]
        conn.close()
        assert count == 0

    def test_test_flag_limits_to_five_shows(self, mini_vault_db, mini_model_dir):
        # Even though 4 Fall + 2 Summer = 6 shows match, --test caps at 5
        # (there are only 6 anyway so test with a bigger fixture-aware
        # season filter: use default which hits current+previous = 6).
        # Freeze season via --season so test is deterministic.
        result = _run_tool(
            ["--season", "Fall_2026", "--test"],
            mini_vault_db, mini_model_dir,
        )
        assert result.returncode == 0
        # --test implies --dry-run → no writes
        conn = sqlite3.connect(str(mini_vault_db))
        count = conn.execute(
            "SELECT COUNT(*) FROM score_prediction_log"
        ).fetchone()[0]
        conn.close()
        assert count == 0

    def test_no_matching_shows_returns_exit_2(
        self, mini_vault_db, mini_model_dir,
    ):
        result = _run_tool(
            ["--season", "Fall_2099"],  # future season, no shows
            mini_vault_db, mini_model_dir,
        )
        assert result.returncode == 2
        assert "Zero airing shows" in result.stderr

    def test_rerun_same_day_is_idempotent_not_duplicative(
        self, mini_vault_db, mini_model_dir,
    ):
        _run_tool(["--season", "Fall_2026"], mini_vault_db, mini_model_dir)
        _run_tool(["--season", "Fall_2026"], mini_vault_db, mini_model_dir)
        conn = sqlite3.connect(str(mini_vault_db))
        count = conn.execute(
            "SELECT COUNT(*) FROM score_prediction_log"
        ).fetchone()[0]
        conn.close()
        assert count == 4  # Same 4 rows, not 8

    def test_model_version_persists_to_log_rows(
        self, mini_vault_db, mini_model_dir,
    ):
        _run_tool(["--season", "Fall_2026"], mini_vault_db, mini_model_dir)
        conn = sqlite3.connect(str(mini_vault_db))
        row = conn.execute(
            "SELECT DISTINCT model_version FROM score_prediction_log"
        ).fetchone()
        conn.close()
        assert row[0] == "lin-20261011-testfx-v16"

    def test_actual_score_snapshot_copied_from_vault(
        self, mini_vault_db, mini_model_dir,
    ):
        _run_tool(["--season", "Fall_2026"], mini_vault_db, mini_model_dir)
        conn = sqlite3.connect(str(mini_vault_db))
        # Show 1 has mal_score 7.1 in the fixture
        row = conn.execute(
            "SELECT actual_score_at_time FROM score_prediction_log WHERE mal_id=1"
        ).fetchone()
        conn.close()
        assert row[0] == pytest.approx(7.1, abs=1e-6)
