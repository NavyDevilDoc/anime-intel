"""Phase 1 Part B probe — vault-internal predictive signal for mal_score.

Compares multiple sklearn regressors on the same features / split / seed
to pick a model before productionization. All non-trivial sklearn
regressors available without new dependencies are included:
  - Baseline (predict mean) — control
  - LinearRegression (unregularized) — minimum-complexity reference
  - Ridge (L2) — regularized linear
  - Lasso (L1) — sparsity-inducing regularized linear
  - RandomForestRegressor — bagging
  - GradientBoostingRegressor — boosting (original candidate)
  - HistGradientBoostingRegressor — sklearn's faster GBM

Output:
  - Markdown comparison table to stdout
  - Named-case predictions per model (so we can see where each model
    gets the Exiled Heavy Knight / Mushoku Tensei S3 / Frieren /
    Delicious in Dungeon prediction right or wrong)
  - JSON dump to data/hype_prediction_probe_results.json with per-model
    metrics + top feature importance for the tree-based models

NOT committed per the agreed PR plan — stays local while we iterate.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import warnings
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # headless — don't try to open a window
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.ensemble import (
    GradientBoostingRegressor,
    HistGradientBoostingRegressor,
    RandomForestRegressor,
)
from sklearn.linear_model import Lasso, LinearRegression, Ridge
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import train_test_split
from sklearn.neural_network import MLPRegressor
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "anime_intelligence_v2.db"
OUTPUT_PATH = ROOT / "data" / "hype_prediction_probe_results.json"
VIZ_DIR = ROOT / "data" / "hype_prediction_viz"

NAMED_CASES = {
    59741: "Exiled Heavy Knight (flop; Web manga, GoHands)",
    59193: "Mushoku Tensei S3 (hit; Light novel, Studio Bind)",
    52991: "Frieren S1 (hit; Manga, Madhouse)",
    52701: "Delicious in Dungeon (hit; Manga, Trigger)",
}

RANDOM_STATE = 42
TEST_SIZE = 0.20


@dataclass
class ModelResult:
    name: str
    mae: float
    r2: float
    train_seconds: float
    named_predictions: list
    top_features: dict = field(default_factory=dict)


def load_vault() -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        """
        SELECT id, english_title, mal_score, scored_by, studio, release_year,
               subtype, source, genres
        FROM anime_info
        WHERE mal_score IS NOT NULL
          AND consensus_json IS NOT NULL
          AND source IS NOT NULL
          AND source != ''
        """,
        conn,
    )
    conn.close()
    return df


def load_staff_lookups() -> dict:
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT anime_id, person_mal_id, role FROM anime_staff",
        conn,
    )
    conn.close()
    lookups = {}
    for role in ("Director", "Original Creator", "Series Composition",
                 "Character Design", "Music"):
        role_df = df[df["role"] == role]
        lookups[role] = role_df.groupby("anime_id")["person_mal_id"].apply(list).to_dict()
    return lookups


def compute_hit_rates(df: pd.DataFrame, train_mask: np.ndarray) -> dict:
    train = df[train_mask]
    global_mean = train["mal_score"].mean()
    lookups = {"global_mean": global_mean}
    lookups["studio"] = train.groupby("studio")["mal_score"].mean().to_dict()
    return lookups


def compute_person_hit_rates(df: pd.DataFrame, train_mask: np.ndarray,
                              staff_lookups: dict) -> dict:
    train = df[train_mask]
    train_ids_to_score = dict(zip(train["id"], train["mal_score"]))
    person_rates = {}
    for role, anime_to_people in staff_lookups.items():
        person_scores = {}
        for anime_id, people_ids in anime_to_people.items():
            if anime_id not in train_ids_to_score:
                continue
            score = train_ids_to_score[anime_id]
            for pid in people_ids:
                person_scores.setdefault(pid, []).append(score)
        person_rates[role] = {
            pid: float(np.mean(scores))
            for pid, scores in person_scores.items()
        }
    return person_rates


def build_features(df: pd.DataFrame, hit_rates: dict, person_rates: dict,
                   staff_lookups: dict, source_values: list,
                   subtype_values: list, genre_values: list) -> pd.DataFrame:
    global_mean = hit_rates["global_mean"]
    rows = []
    for _, show in df.iterrows():
        row = {"id": show["id"]}
        row["release_year"] = show["release_year"] if pd.notna(show["release_year"]) else 2010
        row["studio_hit_rate"] = hit_rates["studio"].get(show["studio"], global_mean)
        for role in ("Director", "Original Creator", "Series Composition",
                     "Character Design", "Music"):
            people = staff_lookups.get(role, {}).get(show["id"], [])
            if people:
                scores = [person_rates[role].get(pid, global_mean) for pid in people]
                row[f"{role.lower().replace(' ', '_')}_hit_rate"] = float(np.mean(scores))
            else:
                row[f"{role.lower().replace(' ', '_')}_hit_rate"] = global_mean
        for src in source_values:
            row[f"source_{src}"] = 1 if show["source"] == src else 0
        subtype = show["subtype"] if pd.notna(show["subtype"]) and show["subtype"] else "unknown"
        for sub in subtype_values:
            row[f"subtype_{sub}"] = 1 if subtype == sub else 0
        show_genres = set((show["genres"] or "").split(", ")) if show["genres"] else set()
        for g in genre_values:
            row[f"genre_{g}"] = 1 if g in show_genres else 0
        rows.append(row)
    return pd.DataFrame(rows)


def evaluate_model(name: str, model, X_train, X_test, y_train, y_test,
                   named_ids: dict, feature_names: list) -> ModelResult:
    t0 = time.perf_counter()
    model.fit(X_train, y_train)
    train_seconds = time.perf_counter() - t0
    y_pred = model.predict(X_test)
    mae = float(mean_absolute_error(y_test, y_pred))
    r2 = float(r2_score(y_test, y_pred))

    named_predictions = []
    for mal_id, label in NAMED_CASES.items():
        if mal_id not in X_test.index:
            continue
        actual = float(y_test.loc[mal_id])
        pred = float(model.predict(X_test.loc[[mal_id]])[0])
        named_predictions.append({
            "mal_id": mal_id,
            "label": label,
            "actual": round(actual, 2),
            "predicted": round(pred, 2),
            "error": round(pred - actual, 2),
        })

    top_features = {}
    if hasattr(model, "feature_importances_"):
        pairs = sorted(zip(feature_names, model.feature_importances_),
                       key=lambda x: -x[1])
        top_features = {n: round(float(s), 4) for n, s in pairs[:10]}
    elif hasattr(model, "coef_"):
        pairs = sorted(zip(feature_names, np.abs(model.coef_)),
                       key=lambda x: -x[1])
        top_features = {n: round(float(s), 4) for n, s in pairs[:10]}

    return ModelResult(
        name=name, mae=round(mae, 4), r2=round(r2, 4),
        train_seconds=round(train_seconds, 3),
        named_predictions=named_predictions, top_features=top_features,
    )


def main():
    print("Loading vault + staff cascade...", file=sys.stderr)
    df = load_vault()
    staff_lookups = load_staff_lookups()
    print(f"  {len(df):,} training-eligible shows loaded.", file=sys.stderr)

    named_ids = set(NAMED_CASES.keys())
    named_mask = df["id"].isin(named_ids)
    df_named = df[named_mask].copy()
    df_pool = df[~named_mask].copy()

    train_pool, test_pool = train_test_split(
        df_pool, test_size=TEST_SIZE, random_state=RANDOM_STATE,
    )
    df_train = train_pool
    df_test = pd.concat([test_pool, df_named], ignore_index=True)
    print(f"  train={len(df_train):,}  test={len(df_test):,}  "
          f"(named forced into test: {len(df_named)})", file=sys.stderr)

    full_df = pd.concat([df_train, df_test], ignore_index=True)
    train_mask = full_df["id"].isin(set(df_train["id"]))
    hit_rates = compute_hit_rates(full_df, train_mask.to_numpy())
    person_rates = compute_person_hit_rates(full_df, train_mask.to_numpy(), staff_lookups)

    source_values = sorted(df_train["source"].dropna().unique().tolist())
    subtype_values = sorted(
        df_train["subtype"].dropna().replace("", "unknown").unique().tolist()
    )
    genre_counter = Counter()
    for g_str in df_train["genres"].dropna():
        for g in g_str.split(", "):
            if g:
                genre_counter[g] += 1
    genre_values = [g for g, _ in genre_counter.most_common(20)]

    X_full = build_features(
        full_df, hit_rates, person_rates, staff_lookups,
        source_values, subtype_values, genre_values,
    )
    y_full = full_df.set_index("id")["mal_score"]
    X_train = X_full[X_full["id"].isin(df_train["id"])].set_index("id")
    X_test = X_full[X_full["id"].isin(df_test["id"])].set_index("id")
    y_train = y_full.reindex(X_train.index)
    y_test = y_full.reindex(X_test.index)
    feature_names = list(X_train.columns)
    print(f"  Feature matrix: {X_train.shape[1]} features", file=sys.stderr)

    # Baseline
    baseline_pred = np.full(len(y_test), y_train.mean())
    baseline_mae = float(mean_absolute_error(y_test, baseline_pred))
    baseline_r2 = float(r2_score(y_test, baseline_pred))

    # MLPs need standardized features; wrap in a Pipeline with StandardScaler.
    # Tree-based models are scale-invariant so they stay unscaled for parity
    # with the earlier results.
    def mlp_pipeline(hidden_layer_sizes, label):
        return Pipeline([
            ("scaler", StandardScaler()),
            ("mlp", MLPRegressor(
                hidden_layer_sizes=hidden_layer_sizes,
                activation="relu",
                solver="adam",
                alpha=1e-4,
                learning_rate="adaptive",
                learning_rate_init=1e-3,
                max_iter=500,
                early_stopping=True,
                validation_fraction=0.15,
                n_iter_no_change=20,
                random_state=RANDOM_STATE,
            )),
        ])

    models = [
        ("LinearRegression",
         LinearRegression()),
        ("Ridge (alpha=1.0)",
         Ridge(alpha=1.0, random_state=RANDOM_STATE)),
        ("Lasso (alpha=0.01)",
         Lasso(alpha=0.01, random_state=RANDOM_STATE, max_iter=10000)),
        ("RandomForest (n=200)",
         RandomForestRegressor(n_estimators=200, random_state=RANDOM_STATE,
                               n_jobs=-1)),
        ("GradientBoosting (n=200)",
         GradientBoostingRegressor(n_estimators=200, max_depth=4,
                                   learning_rate=0.05, random_state=RANDOM_STATE)),
        ("HistGradientBoosting (iter=200)",
         HistGradientBoostingRegressor(max_iter=200, learning_rate=0.05,
                                       max_depth=4, random_state=RANDOM_STATE)),
        ("MLP (64)",
         mlp_pipeline((64,), "shallow-1L")),
        ("MLP (128, 64)",
         mlp_pipeline((128, 64), "funnel-2L")),
        ("MLP (64, 32, 16)",
         mlp_pipeline((64, 32, 16), "narrow-3L")),
    ]

    results = []
    for name, model in models:
        print(f"Training {name}...", file=sys.stderr)
        r = evaluate_model(name, model, X_train, X_test, y_train, y_test,
                           NAMED_CASES, feature_names)
        results.append(r)
        print(f"  MAE={r.mae}  R2={r.r2}  ({r.train_seconds}s)", file=sys.stderr)

    # Markdown report
    print("# Phase 1 Hype-Prediction Probe — Multi-Model Comparison\n")
    print(f"**Captured:** {datetime.now(timezone.utc).isoformat()}  ")
    print(f"**Split:** {len(X_train):,} train / {len(X_test):,} test  ")
    print(f"**Features:** {X_train.shape[1]}  ")
    print(f"**Mean mal_score (training):** {y_train.mean():.3f}  ")
    print(f"**Baseline MAE (predict mean):** {baseline_mae:.4f}\n")

    print("## Model comparison (lower MAE is better)\n")
    print("| Model | MAE | R² | Lift vs baseline | Train time |")
    print("|-------|-----|-----|------------------|------------|")
    print(f"| Baseline (predict mean) | {baseline_mae:.4f} | {baseline_r2:+.4f} | — | 0.000s |")
    for r in sorted(results, key=lambda x: x.mae):
        lift = (baseline_mae - r.mae) / baseline_mae * 100
        print(f"| {r.name} | **{r.mae:.4f}** | {r.r2:.4f} | **{lift:+.1f}%** | {r.train_seconds}s |")
    print()

    print("## Named-case predictions (actual → predicted per model)\n")
    header = "| Show (actual) |" + "".join(f" {r.name} |" for r in results)
    sep = "|---------------|" + "|".join("------" for _ in results) + "|"
    print(header)
    print(sep)
    for mal_id, label in NAMED_CASES.items():
        actuals = [p["actual"] for r in results for p in r.named_predictions if p["mal_id"] == mal_id]
        actual = actuals[0] if actuals else None
        row = f"| {label.split('(')[0].strip()} ({actual}) |"
        for r in results:
            match = next((p for p in r.named_predictions if p["mal_id"] == mal_id), None)
            if match:
                row += f" {match['predicted']:.2f} ({match['error']:+.2f}) |"
            else:
                row += " — |"
        print(row)
    print()

    # Winner's feature importance
    winner = min(results, key=lambda r: r.mae)
    print(f"## Winner: {winner.name} — top 10 features\n")
    print("| Feature | Importance |")
    print("|---------|------------|")
    for name, score in winner.top_features.items():
        print(f"| {name} | {score} |")
    print()

    # --- Diagnostic plots for the chosen production model (LinearRegression) ---
    # Four standard regression diagnostics saved as PNGs under data/hype_prediction_viz/.
    # LinearRegression (not the technical MAE winner) because it's the model we're
    # productionizing and we want its diagnostics specifically.
    VIZ_DIR.mkdir(parents=True, exist_ok=True)
    lin = LinearRegression()
    lin.fit(X_train, y_train)
    lin_pred = lin.predict(X_test)
    residuals = y_test.values - lin_pred

    # 1. Predicted vs Actual scatter — the classic regression viz. Points on the
    #    y=x diagonal are perfect predictions; distance from diagonal = error.
    fig, ax = plt.subplots(figsize=(7, 7))
    ax.scatter(y_test, lin_pred, alpha=0.3, s=15, color="steelblue",
               edgecolors="none", label=f"Test shows (n={len(y_test):,})")
    diag_min, diag_max = min(y_test.min(), lin_pred.min()), max(y_test.max(), lin_pred.max())
    ax.plot([diag_min, diag_max], [diag_min, diag_max], "k--", lw=1,
            label="Perfect prediction (y=x)")
    # Highlight named cases
    for mal_id, label in NAMED_CASES.items():
        if mal_id not in X_test.index:
            continue
        actual = float(y_test.loc[mal_id])
        pred = float(lin.predict(X_test.loc[[mal_id]])[0])
        short = label.split("(")[0].strip()
        ax.scatter([actual], [pred], s=120, color="crimson", zorder=5,
                   edgecolors="black", linewidths=1)
        ax.annotate(short, (actual, pred), xytext=(8, 4), textcoords="offset points",
                    fontsize=9, fontweight="bold")
    ax.set_xlabel("Actual mal_score", fontsize=11)
    ax.set_ylabel("Predicted mal_score (LinearRegression)", fontsize=11)
    ax.set_title(f"Predicted vs Actual — LinearRegression\n"
                 f"MAE={mean_absolute_error(y_test, lin_pred):.4f}  "
                 f"R²={r2_score(y_test, lin_pred):.4f}", fontsize=12)
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(VIZ_DIR / "1_predicted_vs_actual.png", dpi=120)
    plt.close(fig)

    # 2. Residual plot — residuals (actual - predicted) vs predicted. Reveals
    #    heteroscedasticity, systematic bias. A good model's residuals are
    #    randomly scattered around zero with roughly constant variance.
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.scatter(lin_pred, residuals, alpha=0.3, s=15, color="steelblue",
               edgecolors="none")
    ax.axhline(0, color="black", linestyle="--", lw=1)
    # Rolling-mean residual to visualize bias trend
    sort_idx = np.argsort(lin_pred)
    pred_sorted = lin_pred[sort_idx]
    resid_sorted = residuals[sort_idx]
    window = 100
    if len(pred_sorted) >= window:
        rolling_mean = pd.Series(resid_sorted).rolling(window, center=True).mean()
        ax.plot(pred_sorted, rolling_mean, color="crimson", lw=2,
                label=f"Rolling mean residual (n={window})")
        ax.legend(loc="upper right", fontsize=9)
    ax.set_xlabel("Predicted mal_score", fontsize=11)
    ax.set_ylabel("Residual (actual - predicted)", fontsize=11)
    ax.set_title("Residual plot — LinearRegression\n"
                 "A healthy model has residuals centered on 0 with constant spread",
                 fontsize=12)
    ax.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(VIZ_DIR / "2_residual_plot.png", dpi=120)
    plt.close(fig)

    # 3. Residual distribution histogram — are errors normally distributed?
    #    Is the distribution centered on zero (no bias)?
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.hist(residuals, bins=40, color="steelblue", edgecolor="black", alpha=0.7)
    ax.axvline(0, color="black", linestyle="--", lw=1, label="Zero error")
    ax.axvline(residuals.mean(), color="crimson", linestyle="-", lw=2,
               label=f"Mean residual = {residuals.mean():+.3f}")
    ax.set_xlabel("Residual (actual - predicted)", fontsize=11)
    ax.set_ylabel("Count", fontsize=11)
    ax.set_title(f"Residual distribution — LinearRegression\n"
                 f"σ={residuals.std():.3f}  MAE={np.abs(residuals).mean():.4f}",
                 fontsize=12)
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(alpha=0.3, axis="y")
    plt.tight_layout()
    plt.savefig(VIZ_DIR / "3_residual_distribution.png", dpi=120)
    plt.close(fig)

    # 4. Top-20 coefficient bar chart (signed, so direction is visible).
    coef_pairs = sorted(
        zip(feature_names, lin.coef_),
        key=lambda x: -abs(x[1]),
    )[:20]
    names_sorted = [n for n, _ in coef_pairs][::-1]  # reversed so largest at top
    coefs_sorted = [c for _, c in coef_pairs][::-1]
    colors = ["crimson" if c < 0 else "steelblue" for c in coefs_sorted]

    fig, ax = plt.subplots(figsize=(10, 8))
    ax.barh(names_sorted, coefs_sorted, color=colors, edgecolor="black")
    ax.axvline(0, color="black", lw=0.8)
    ax.set_xlabel("Coefficient (signed — blue = positive, red = negative)",
                  fontsize=11)
    ax.set_title("Top-20 features by |coefficient| — LinearRegression\n"
                 "Positive = feature pushes predicted score up; negative = down",
                 fontsize=12)
    ax.grid(alpha=0.3, axis="x")
    plt.tight_layout()
    plt.savefig(VIZ_DIR / "4_feature_coefficients.png", dpi=120)
    plt.close(fig)

    print(f"\nDiagnostic plots written to {VIZ_DIR.relative_to(ROOT)}/", file=sys.stderr)
    for name in ("1_predicted_vs_actual.png", "2_residual_plot.png",
                 "3_residual_distribution.png", "4_feature_coefficients.png"):
        print(f"  - {name}", file=sys.stderr)

    # JSON dump
    payload = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "n_train": len(X_train),
        "n_test": len(X_test),
        "n_features": X_train.shape[1],
        "mean_mal_score": round(float(y_train.mean()), 4),
        "baseline": {"mae": baseline_mae, "r2": baseline_r2},
        "models": [asdict(r) for r in results],
        "winner": winner.name,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"\nFull results written to {OUTPUT_PATH.relative_to(ROOT)}", file=sys.stderr)


if __name__ == "__main__":
    main()
