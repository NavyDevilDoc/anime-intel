"""Phase 1 hype predictor — vault-internal LinearRegression on staff + metadata features.

Production path for the Phase 1 Part B probe findings. Linear regression
was chosen over marginally-better tree-based models (0.009 MAE
difference, 190× slower training) for interpretability + GH-Action-
friendly retrain cost. See `tools/probe_hype_prediction.py` for the
comparison probe that produced this decision.

Public API:
    HypePredictor.train(db_path) -> HypePredictor
        Full training pipeline from vault DB.
    predictor.predict(show, staff) -> float
        Single-show inference. show + staff dicts.
    predictor.predict_from_db(mal_id, db_path) -> float
        Convenience: fetch show + staff from vault DB, predict.
    predictor.predict_batch(shows, staffs) -> list[float]
        Batched inference for weekly re-prediction cron.
    predictor.save(model_dir) -> dict
        Serialize to disk (pickle + JSON metadata sidecar).
    HypePredictor.load(model_path) -> HypePredictor
    HypePredictor.load_latest(model_dir) -> HypePredictor

Model version tag format:
    {algo_short}-{YYYYMMDD}-{feature_hash}-{vault_hash}
    e.g., lin-20261011-a7b3c2-v6655
    - algo_short:   "lin" for LinearRegression (future: "gbm" / "mlp")
    - YYYYMMDD:     training date (UTC)
    - feature_hash: SHA-256 prefix of sorted feature names, detects
                    "trained before we added ANN sentiment" at the
                    schema level
    - vault_hash:   training sample count prefixed with 'v', detects
                    "trained before the Fall 2026 cohort landed"

The hash scheme means the version tag alone answers "would this
model's predictions be comparable to a prediction from version X?"
without needing to crack open the metadata sidecar.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import train_test_split

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "anime_intelligence_v2.db"
MODELS_DIR = ROOT / "data" / "models"

# Roles the staff cascade (PR #106) populates. Order matters only for
# the feature-name derivation below — keep stable so retrains produce
# identical feature-name hashes when inputs don't change.
STAFF_ROLES: tuple[str, ...] = (
    "Director",
    "Original Creator",
    "Series Composition",
    "Character Design",
    "Music",
)


def _role_feature_name(role: str) -> str:
    """Role → feature-column key. Snake-case, '_hit_rate' suffix."""
    return f"{role.lower().replace(' ', '_')}_hit_rate"


# =============================================================================
# FeatureBuilder — lookup tables + per-row feature projection
# =============================================================================


@dataclass
class FeatureBuilder:
    """Encapsulates feature-extraction state.

    Pickled with the model so inference-time feature extraction uses
    exactly the same lookup tables (and vocabularies) as training.
    Rebuilding the lookups from a modified vault would silently shift
    predictions — the whole point of versioning the FeatureBuilder is
    to make that drift observable, not silent.
    """
    global_mean: float
    studio_hit_rate: dict[str, float]
    person_hit_rate_by_role: dict[str, dict[int, float]]  # role → person_id → mean score
    source_vocab: list[str]
    subtype_vocab: list[str]
    genre_vocab: list[str]

    def feature_names(self) -> list[str]:
        """Canonical ordered list of feature columns the model expects.

        Order matters — sklearn fits by column position. Must match
        between training and inference. Keep deterministic.
        """
        names: list[str] = ["release_year", "studio_hit_rate"]
        names.extend(_role_feature_name(r) for r in STAFF_ROLES)
        names.extend(f"source_{s}" for s in self.source_vocab)
        names.extend(f"subtype_{s}" for s in self.subtype_vocab)
        names.extend(f"genre_{g}" for g in self.genre_vocab)
        return names

    def build_row(self, show: dict[str, Any], staff: dict[str, list[int]]) -> dict[str, float]:
        """Project one show into a feature row.

        show: fields from anime_info — expects at least id, studio,
              release_year, subtype, source, genres (comma-separated).
              Missing keys are tolerated and treated as "unknown"
              (imputed to global_mean for numerics, 0 for one-hots).
        staff: role → list of person_mal_ids for THIS show.
        """
        row: dict[str, float] = {}
        row["release_year"] = (
            float(show["release_year"]) if show.get("release_year") is not None
            else 2010.0  # vault median-ish; fallback for pre-airing shows missing a year
        )
        row["studio_hit_rate"] = float(
            self.studio_hit_rate.get(show.get("studio"), self.global_mean)
        )

        for role in STAFF_ROLES:
            key = _role_feature_name(role)
            people = staff.get(role, [])
            if people:
                lookup = self.person_hit_rate_by_role.get(role, {})
                scores = [lookup.get(pid, self.global_mean) for pid in people]
                row[key] = float(np.mean(scores))
            else:
                row[key] = self.global_mean

        src_val = show.get("source") or ""
        for src in self.source_vocab:
            row[f"source_{src}"] = 1.0 if src_val == src else 0.0

        sub_val = show.get("subtype") or ""
        for sub in self.subtype_vocab:
            row[f"subtype_{sub}"] = 1.0 if sub_val == sub else 0.0

        show_genres = set((show.get("genres") or "").split(", ")) if show.get("genres") else set()
        for g in self.genre_vocab:
            row[f"genre_{g}"] = 1.0 if g in show_genres else 0.0

        return row


# =============================================================================
# Metadata
# =============================================================================


@dataclass
class HypePredictorMetadata:
    """Serializable model metadata — written to the JSON sidecar."""
    model_version: str
    algorithm: str
    created_at: str
    training_sample_size: int
    test_sample_size: int
    feature_names: list[str]
    hyperparameters: dict[str, Any]
    train_mae: float
    test_mae: float
    baseline_mae: float
    train_r2: float
    test_r2: float
    signal_lift_pct: float
    vault_mean_score: float
    feature_coefficients: dict[str, float]
    intercept: float
    source_db_path: str
    random_state: int
    test_size: float

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


# =============================================================================
# Data loading helpers
# =============================================================================


def _training_eligible_rows(conn: sqlite3.Connection) -> pd.DataFrame:
    """Pull vault rows with mal_score + consensus + source populated."""
    return pd.read_sql_query(
        """
        SELECT id, mal_score, studio, release_year, subtype, source, genres
        FROM anime_info
        WHERE mal_score IS NOT NULL
          AND consensus_json IS NOT NULL
          AND source IS NOT NULL
          AND source != ''
        """,
        conn,
    )


def _staff_lookups_from_db(conn: sqlite3.Connection) -> dict[str, dict[int, list[int]]]:
    """Return {role: {anime_id: [person_ids]}} for the five target roles."""
    df = pd.read_sql_query(
        "SELECT anime_id, person_mal_id, role FROM anime_staff "
        "WHERE role IN ({})".format(",".join(f"'{r}'" for r in STAFF_ROLES)),
        conn,
    )
    lookups: dict[str, dict[int, list[int]]] = {}
    for role in STAFF_ROLES:
        role_df = df[df["role"] == role]
        lookups[role] = role_df.groupby("anime_id")["person_mal_id"].apply(list).to_dict()
    return lookups


def _staff_for_show(conn: sqlite3.Connection, mal_id: int) -> dict[str, list[int]]:
    """Return {role: [person_ids]} for one show. Used by predict_from_db."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT role, person_mal_id FROM anime_staff "
        "WHERE anime_id = ? AND role IN ({})".format(
            ",".join(f"'{r}'" for r in STAFF_ROLES)
        ),
        (mal_id,),
    )
    staff: dict[str, list[int]] = {}
    for role, pid in cursor.fetchall():
        staff.setdefault(role, []).append(pid)
    return staff


def _show_from_db(conn: sqlite3.Connection, mal_id: int) -> dict[str, Any] | None:
    """Return anime_info row as a dict, or None if not found."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, studio, release_year, subtype, source, genres "
        "FROM anime_info WHERE id = ?",
        (mal_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "studio": row[1],
        "release_year": row[2],
        "subtype": row[3],
        "source": row[4],
        "genres": row[5],
    }


# =============================================================================
# FeatureBuilder construction (training-only path)
# =============================================================================


def _build_feature_builder(train_df: pd.DataFrame,
                           staff_lookups: dict[str, dict[int, list[int]]],
                           top_n_genres: int = 20) -> FeatureBuilder:
    """Derive lookup tables + vocabs from the training rows only.

    All aggregations use ONLY shows in train_df so test-set evaluation
    doesn't leak target information. The vocabularies (source_vocab,
    subtype_vocab, genre_vocab) are also pinned to training-set
    categories — any unseen category at inference time is just absent
    from all one-hot columns (safe zero-vector).
    """
    global_mean = float(train_df["mal_score"].mean())

    studio_hit_rate = (
        train_df.dropna(subset=["studio"])
        .groupby("studio")["mal_score"].mean().to_dict()
    )

    train_ids_to_score = dict(zip(train_df["id"], train_df["mal_score"]))
    person_hit_rate_by_role: dict[str, dict[int, float]] = {}
    for role in STAFF_ROLES:
        per_role_scores: dict[int, list[float]] = {}
        for anime_id, people_ids in staff_lookups.get(role, {}).items():
            if anime_id not in train_ids_to_score:
                continue
            score = float(train_ids_to_score[anime_id])
            for pid in people_ids:
                per_role_scores.setdefault(pid, []).append(score)
        person_hit_rate_by_role[role] = {
            pid: float(np.mean(scores)) for pid, scores in per_role_scores.items()
        }

    source_vocab = sorted(train_df["source"].dropna().unique().tolist())
    subtype_vocab = sorted(train_df["subtype"].dropna().replace("", "unknown").unique().tolist())

    genre_counter: Counter = Counter()
    for g_str in train_df["genres"].dropna():
        for g in g_str.split(", "):
            if g:
                genre_counter[g] += 1
    genre_vocab = [g for g, _ in genre_counter.most_common(top_n_genres)]

    return FeatureBuilder(
        global_mean=global_mean,
        studio_hit_rate=studio_hit_rate,
        person_hit_rate_by_role=person_hit_rate_by_role,
        source_vocab=source_vocab,
        subtype_vocab=subtype_vocab,
        genre_vocab=genre_vocab,
    )


# =============================================================================
# Version tag
# =============================================================================


def _compute_version_tag(algorithm: str, feature_names: list[str],
                         n_training_rows: int) -> str:
    """{algo}-{YYYYMMDD}-{feature_hash}-{vault_hash}."""
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    feature_hash = hashlib.sha256(
        ",".join(sorted(feature_names)).encode("utf-8")
    ).hexdigest()[:6]
    algo_short = {
        "LinearRegression": "lin",
        "GradientBoostingRegressor": "gbm",
        "HistGradientBoostingRegressor": "hgbm",
        "RandomForestRegressor": "rf",
    }.get(algorithm, algorithm.lower()[:4])
    return f"{algo_short}-{date}-{feature_hash}-v{n_training_rows}"


# =============================================================================
# HypePredictor — the public class
# =============================================================================


class HypePredictor:
    """Trained Linear-Regression hype predictor + its feature builder.

    Immutable after train(): predict() reads self.model + self.feature_builder
    without mutation. Retraining produces a new HypePredictor instance.
    """

    def __init__(
        self,
        model: LinearRegression,
        feature_builder: FeatureBuilder,
        metadata: HypePredictorMetadata,
    ):
        self.model = model
        self.feature_builder = feature_builder
        self.metadata = metadata

    # ---- Training ----

    @classmethod
    def train(cls, db_path: Path = DB_PATH, random_state: int = 42,
              test_size: float = 0.20, top_n_genres: int = 20) -> "HypePredictor":
        """Full training pipeline from the vault DB."""
        logger.info(f"Training hype predictor from {db_path}")
        conn = sqlite3.connect(str(db_path))
        try:
            df = _training_eligible_rows(conn)
            staff_lookups = _staff_lookups_from_db(conn)
        finally:
            conn.close()

        logger.info(f"  {len(df):,} training-eligible shows")

        train_df, test_df = train_test_split(
            df, test_size=test_size, random_state=random_state,
        )

        feature_builder = _build_feature_builder(train_df, staff_lookups, top_n_genres)
        feature_names = feature_builder.feature_names()

        def _rows_to_frame(frame: pd.DataFrame) -> pd.DataFrame:
            # Build staff lookup per show just-in-time
            staff_by_id: dict[int, dict[str, list[int]]] = {}
            for role in STAFF_ROLES:
                for aid, pids in staff_lookups.get(role, {}).items():
                    staff_by_id.setdefault(aid, {})[role] = pids

            rows = []
            for _, show in frame.iterrows():
                staff = staff_by_id.get(show["id"], {})
                show_dict = show.to_dict()
                rows.append(feature_builder.build_row(show_dict, staff))
            return pd.DataFrame(rows)[feature_names]

        X_train = _rows_to_frame(train_df)
        y_train = train_df["mal_score"].to_numpy()
        X_test = _rows_to_frame(test_df)
        y_test = test_df["mal_score"].to_numpy()

        logger.info(f"  Feature matrix: {X_train.shape[1]} features")

        model = LinearRegression()
        model.fit(X_train, y_train)

        y_pred_train = model.predict(X_train)
        y_pred_test = model.predict(X_test)

        train_mae = float(mean_absolute_error(y_train, y_pred_train))
        test_mae = float(mean_absolute_error(y_test, y_pred_test))
        train_r2 = float(r2_score(y_train, y_pred_train))
        test_r2 = float(r2_score(y_test, y_pred_test))
        baseline_mae = float(np.abs(y_test - y_train.mean()).mean())
        signal_lift = (baseline_mae - test_mae) / baseline_mae * 100.0

        logger.info(f"  Train MAE: {train_mae:.4f}  Test MAE: {test_mae:.4f}  "
                    f"Lift vs baseline: {signal_lift:+.1f}%")

        version_tag = _compute_version_tag(
            "LinearRegression", feature_names, len(train_df)
        )
        feature_coefs = {name: float(c) for name, c in zip(feature_names, model.coef_)}

        metadata = HypePredictorMetadata(
            model_version=version_tag,
            algorithm="LinearRegression",
            created_at=datetime.now(timezone.utc).isoformat(),
            training_sample_size=len(train_df),
            test_sample_size=len(test_df),
            feature_names=feature_names,
            hyperparameters={},  # LinearRegression has no hyperparameters
            train_mae=train_mae,
            test_mae=test_mae,
            baseline_mae=baseline_mae,
            train_r2=train_r2,
            test_r2=test_r2,
            signal_lift_pct=round(signal_lift, 2),
            vault_mean_score=round(float(y_train.mean()), 4),
            feature_coefficients=feature_coefs,
            intercept=float(model.intercept_),
            source_db_path=str(db_path),
            random_state=random_state,
            test_size=test_size,
        )

        return cls(model=model, feature_builder=feature_builder, metadata=metadata)

    # ---- Inference ----

    def predict(self, show: dict[str, Any], staff: dict[str, list[int]]) -> float:
        """Predict mal_score for one show's data."""
        row = self.feature_builder.build_row(show, staff)
        X = pd.DataFrame([row])[self.metadata.feature_names]
        return float(self.model.predict(X)[0])

    def predict_batch(self, shows: list[dict[str, Any]],
                      staffs: list[dict[str, list[int]]]) -> list[float]:
        """Batched inference. shows[i] pairs with staffs[i]."""
        if len(shows) != len(staffs):
            raise ValueError(
                f"shows ({len(shows)}) and staffs ({len(staffs)}) must have "
                f"the same length"
            )
        if not shows:
            return []
        rows = [self.feature_builder.build_row(s, st) for s, st in zip(shows, staffs)]
        X = pd.DataFrame(rows)[self.metadata.feature_names]
        return [float(p) for p in self.model.predict(X)]

    def predict_from_db(self, mal_id: int, db_path: Path = DB_PATH) -> float:
        """Convenience: fetch show + staff from vault, predict."""
        conn = sqlite3.connect(str(db_path))
        try:
            show = _show_from_db(conn, mal_id)
            if show is None:
                raise ValueError(f"MAL ID {mal_id} not in vault")
            staff = _staff_for_show(conn, mal_id)
        finally:
            conn.close()
        return self.predict(show, staff)

    # ---- Persistence ----

    def save(self, model_dir: Path = MODELS_DIR) -> dict[str, Path]:
        """Serialize to disk. Returns {'model': ..., 'metadata': ...} paths."""
        model_dir = Path(model_dir)
        model_dir.mkdir(parents=True, exist_ok=True)
        stem = f"hype_predictor_{self.metadata.model_version}"
        model_path = model_dir / f"{stem}.joblib"
        metadata_path = model_dir / f"{stem}.json"

        joblib.dump(
            {"model": self.model, "feature_builder": self.feature_builder},
            model_path,
        )
        metadata_path.write_text(self.metadata.to_json(), encoding="utf-8")

        logger.info(f"Saved model → {model_path}")
        logger.info(f"Saved metadata → {metadata_path}")
        return {"model": model_path, "metadata": metadata_path}

    @classmethod
    def load(cls, model_path: Path) -> "HypePredictor":
        """Load a specific model file + its sidecar metadata."""
        model_path = Path(model_path)
        metadata_path = model_path.with_suffix(".json")
        if not model_path.exists():
            raise FileNotFoundError(f"Model file not found: {model_path}")
        if not metadata_path.exists():
            raise FileNotFoundError(f"Metadata sidecar not found: {metadata_path}")

        bundle = joblib.load(model_path)
        model = bundle["model"]
        feature_builder = bundle["feature_builder"]
        metadata_dict = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata = HypePredictorMetadata(**metadata_dict)

        return cls(model=model, feature_builder=feature_builder, metadata=metadata)

    @classmethod
    def load_latest(cls, model_dir: Path = MODELS_DIR) -> "HypePredictor":
        """Load the most recent model (by filename sort — version tag has date)."""
        model_dir = Path(model_dir)
        candidates = sorted(model_dir.glob("hype_predictor_*.joblib"))
        if not candidates:
            raise FileNotFoundError(f"No hype_predictor_*.joblib files in {model_dir}")
        return cls.load(candidates[-1])
