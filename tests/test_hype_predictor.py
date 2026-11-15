"""Tests for analysis/hype_predictor.py.

Covers the training pipeline, feature extraction edge cases, round-trip
persistence, and the inference API. Uses a small synthetic SQLite DB
to avoid dependence on the production vault.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pytest

from analysis.hype_predictor import (
    FeatureBuilder,
    HypePredictor,
    HypePredictorMetadata,
    STAFF_ROLES,
    _compute_version_tag,
    _role_feature_name,
)


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def mini_vault_db(tmp_path: Path) -> Path:
    """Build a tiny SQLite vault with ~30 synthetic shows + staff.

    Must be large enough that train/test split + at-least-two-shows-per
    person-ID gives training signal — otherwise LinearRegression fits
    nothing and MAE is dominated by variance alone.
    """
    db_path = tmp_path / "mini_vault.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE anime_info (
            id INTEGER PRIMARY KEY,
            mal_score REAL,
            studio TEXT,
            release_year INTEGER,
            subtype TEXT,
            source TEXT,
            genres TEXT,
            consensus_json TEXT
        );
        CREATE TABLE anime_staff (
            anime_id INTEGER,
            person_mal_id INTEGER,
            role TEXT
        );
        """
    )

    # 30 shows across 3 studios and 2 directors — enough signal for the
    # model to learn studio_hit_rate + director_hit_rate associations.
    import random
    rng = random.Random(1234)
    shows = []
    for i in range(1, 31):
        studio = ["GoodStudio", "MehStudio", "BadStudio"][i % 3]
        director = 1001 if i % 2 == 0 else 1002
        creator = 2001 + (i % 5)  # 5 distinct creators
        source = ["Manga", "Original", "Light novel"][i % 3]
        subtype = "TV"
        genres = "Action, Fantasy" if i % 2 else "Comedy, Slice of Life"
        # Target: good studio + director 1001 → high score
        base = {"GoodStudio": 8.0, "MehStudio": 7.0, "BadStudio": 6.0}[studio]
        dir_bonus = 0.3 if director == 1001 else -0.1
        noise = rng.gauss(0, 0.3)
        score = round(base + dir_bonus + noise, 2)
        shows.append((i, score, studio, 2020 + (i % 5), subtype, source, genres, "{}"))

    conn.executemany(
        "INSERT INTO anime_info VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        shows,
    )
    # Staff: each show gets 1 director + 1 original creator
    staff_rows = []
    for i in range(1, 31):
        director = 1001 if i % 2 == 0 else 1002
        creator = 2001 + (i % 5)
        staff_rows.append((i, director, "Director"))
        staff_rows.append((i, creator, "Original Creator"))
    conn.executemany(
        "INSERT INTO anime_staff VALUES (?, ?, ?)",
        staff_rows,
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def trained_predictor(mini_vault_db: Path) -> HypePredictor:
    return HypePredictor.train(mini_vault_db, random_state=42, test_size=0.3)


# -----------------------------------------------------------------------------
# Version tag
# -----------------------------------------------------------------------------


class TestVersionTag:
    def test_tag_shape_includes_date_and_sample_size(self):
        tag = _compute_version_tag("LinearRegression", ["f1", "f2", "f3"], 100)
        parts = tag.split("-")
        assert len(parts) == 4
        assert parts[0] == "lin"
        assert parts[1].isdigit() and len(parts[1]) == 8
        assert len(parts[2]) == 6
        assert parts[3] == "v100"

    def test_feature_hash_changes_when_features_change(self):
        tag1 = _compute_version_tag("LinearRegression", ["a", "b"], 100)
        tag2 = _compute_version_tag("LinearRegression", ["a", "b", "c"], 100)
        # Different features → different hashes (parts[2])
        assert tag1.split("-")[2] != tag2.split("-")[2]

    def test_vault_hash_changes_with_sample_size(self):
        tag1 = _compute_version_tag("LinearRegression", ["f"], 100)
        tag2 = _compute_version_tag("LinearRegression", ["f"], 200)
        assert tag1.split("-")[3] != tag2.split("-")[3]

    def test_algorithm_short_names(self):
        assert _compute_version_tag("LinearRegression", ["f"], 1).startswith("lin-")
        assert _compute_version_tag("GradientBoostingRegressor", ["f"], 1).startswith("gbm-")


# -----------------------------------------------------------------------------
# FeatureBuilder
# -----------------------------------------------------------------------------


class TestFeatureBuilder:
    def test_feature_names_ordering_is_stable(self):
        fb = FeatureBuilder(
            global_mean=7.0, studio_hit_rate={}, person_hit_rate_by_role={},
            source_vocab=["Manga", "Original"],
            subtype_vocab=["TV"],
            genre_vocab=["Action"],
        )
        names = fb.feature_names()
        # First come numerics, then source/subtype/genre
        assert names[:2] == ["release_year", "studio_hit_rate"]
        for role in STAFF_ROLES:
            assert _role_feature_name(role) in names
        assert "source_Manga" in names
        assert "source_Original" in names
        assert "subtype_TV" in names
        assert "genre_Action" in names

    def test_build_row_missing_fields_impute_to_global_mean(self):
        fb = FeatureBuilder(
            global_mean=7.5, studio_hit_rate={}, person_hit_rate_by_role={},
            source_vocab=["Manga"], subtype_vocab=["TV"], genre_vocab=["Action"],
        )
        row = fb.build_row(show={}, staff={})
        assert row["studio_hit_rate"] == 7.5
        for role in STAFF_ROLES:
            assert row[_role_feature_name(role)] == 7.5
        assert row["source_Manga"] == 0.0
        assert row["subtype_TV"] == 0.0

    def test_build_row_known_studio_uses_lookup(self):
        fb = FeatureBuilder(
            global_mean=7.0, studio_hit_rate={"MAPPA": 8.1},
            person_hit_rate_by_role={}, source_vocab=[], subtype_vocab=[], genre_vocab=[],
        )
        row = fb.build_row(show={"studio": "MAPPA"}, staff={})
        assert row["studio_hit_rate"] == 8.1

    def test_build_row_person_hit_rate_averages_multiple_people(self):
        fb = FeatureBuilder(
            global_mean=7.0, studio_hit_rate={},
            person_hit_rate_by_role={"Director": {100: 8.0, 200: 6.0}},
            source_vocab=[], subtype_vocab=[], genre_vocab=[],
        )
        row = fb.build_row(show={}, staff={"Director": [100, 200]})
        assert row["director_hit_rate"] == 7.0  # mean(8, 6)

    def test_build_row_genre_presence_one_hot(self):
        fb = FeatureBuilder(
            global_mean=7.0, studio_hit_rate={}, person_hit_rate_by_role={},
            source_vocab=[], subtype_vocab=[],
            genre_vocab=["Action", "Comedy", "Drama"],
        )
        row = fb.build_row(show={"genres": "Action, Drama"}, staff={})
        assert row["genre_Action"] == 1.0
        assert row["genre_Drama"] == 1.0
        assert row["genre_Comedy"] == 0.0

    def test_build_row_unknown_source_leaves_all_source_zero(self):
        fb = FeatureBuilder(
            global_mean=7.0, studio_hit_rate={}, person_hit_rate_by_role={},
            source_vocab=["Manga", "Original"], subtype_vocab=[], genre_vocab=[],
        )
        row = fb.build_row(show={"source": "SomeNewFormat"}, staff={})
        assert row["source_Manga"] == 0.0
        assert row["source_Original"] == 0.0


# -----------------------------------------------------------------------------
# Training
# -----------------------------------------------------------------------------


class TestTrain:
    def test_train_returns_fully_populated_metadata(self, trained_predictor: HypePredictor):
        meta = trained_predictor.metadata
        assert meta.algorithm == "LinearRegression"
        assert meta.training_sample_size > 0
        assert meta.test_sample_size > 0
        assert len(meta.feature_names) > 0
        assert len(meta.feature_coefficients) == len(meta.feature_names)
        assert isinstance(meta.test_mae, float)
        assert meta.vault_mean_score > 0

    def test_train_produces_version_tag(self, trained_predictor: HypePredictor):
        assert trained_predictor.metadata.model_version.startswith("lin-")

    def test_train_feature_builder_vocabs_populated(self, trained_predictor: HypePredictor):
        fb = trained_predictor.feature_builder
        assert len(fb.source_vocab) > 0
        assert "TV" in fb.subtype_vocab or fb.subtype_vocab  # TV present in fixture

    def test_training_lifts_model_over_baseline_on_signal_fixture(
        self, trained_predictor: HypePredictor,
    ):
        # Fixture is constructed with real signal (studio + director both
        # drive score deterministically); model should beat predict-mean.
        meta = trained_predictor.metadata
        assert meta.test_mae < meta.baseline_mae, (
            f"Model MAE {meta.test_mae} should beat baseline {meta.baseline_mae} "
            f"on a signal-rich fixture — regression or bad fixture."
        )


# -----------------------------------------------------------------------------
# Inference
# -----------------------------------------------------------------------------


class TestPredict:
    def test_predict_returns_float(self, trained_predictor: HypePredictor):
        pred = trained_predictor.predict(
            show={"studio": "GoodStudio", "release_year": 2024,
                  "subtype": "TV", "source": "Manga",
                  "genres": "Action, Fantasy"},
            staff={"Director": [1001]},
        )
        assert isinstance(pred, float)

    def test_predict_batch_matches_predict_single(self, trained_predictor: HypePredictor):
        shows = [{"studio": "GoodStudio", "subtype": "TV", "source": "Manga"},
                 {"studio": "BadStudio", "subtype": "TV", "source": "Original"}]
        staffs = [{"Director": [1001]}, {"Director": [1002]}]
        batch = trained_predictor.predict_batch(shows, staffs)
        assert len(batch) == 2
        assert batch[0] == pytest.approx(trained_predictor.predict(shows[0], staffs[0]))
        assert batch[1] == pytest.approx(trained_predictor.predict(shows[1], staffs[1]))

    def test_predict_batch_rejects_mismatched_lengths(self, trained_predictor: HypePredictor):
        with pytest.raises(ValueError, match="same length"):
            trained_predictor.predict_batch([{}], [{}, {}])

    def test_predict_batch_empty_returns_empty(self, trained_predictor: HypePredictor):
        assert trained_predictor.predict_batch([], []) == []

    def test_predict_from_db_fetches_show_and_staff(
        self, mini_vault_db: Path, trained_predictor: HypePredictor,
    ):
        pred = trained_predictor.predict_from_db(1, db_path=mini_vault_db)
        assert isinstance(pred, float)

    def test_predict_from_db_unknown_id_raises(
        self, mini_vault_db: Path, trained_predictor: HypePredictor,
    ):
        with pytest.raises(ValueError, match="not in vault"):
            trained_predictor.predict_from_db(999999, db_path=mini_vault_db)


# -----------------------------------------------------------------------------
# Round-trip persistence
# -----------------------------------------------------------------------------


class TestPersistence:
    def test_save_creates_both_files(self, trained_predictor: HypePredictor, tmp_path: Path):
        paths = trained_predictor.save(model_dir=tmp_path)
        assert paths["model"].exists()
        assert paths["metadata"].exists()
        assert paths["model"].suffix == ".joblib"
        assert paths["metadata"].suffix == ".json"

    def test_round_trip_preserves_predictions(
        self, trained_predictor: HypePredictor, tmp_path: Path,
    ):
        paths = trained_predictor.save(model_dir=tmp_path)
        reloaded = HypePredictor.load(paths["model"])
        show = {"studio": "GoodStudio", "release_year": 2024,
                "subtype": "TV", "source": "Manga", "genres": "Action, Fantasy"}
        staff = {"Director": [1001]}
        orig = trained_predictor.predict(show, staff)
        back = reloaded.predict(show, staff)
        assert orig == pytest.approx(back, abs=1e-9)

    def test_round_trip_preserves_metadata(
        self, trained_predictor: HypePredictor, tmp_path: Path,
    ):
        paths = trained_predictor.save(model_dir=tmp_path)
        reloaded = HypePredictor.load(paths["model"])
        assert reloaded.metadata.model_version == trained_predictor.metadata.model_version
        assert reloaded.metadata.feature_names == trained_predictor.metadata.feature_names

    def test_load_latest_picks_most_recent(
        self, trained_predictor: HypePredictor, tmp_path: Path,
    ):
        trained_predictor.save(model_dir=tmp_path)
        latest = HypePredictor.load_latest(model_dir=tmp_path)
        assert latest.metadata.model_version == trained_predictor.metadata.model_version

    def test_load_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            HypePredictor.load(tmp_path / "nonexistent.joblib")

    def test_load_latest_empty_dir_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            HypePredictor.load_latest(model_dir=tmp_path)
