"""Generate the hype-predictor health report (markdown + PNG plots).

Writes to `data/hype_predictor_health/` by default:
    README.md                      — human-readable report (GitHub renders)
    summary.json                   — machine-readable payload
    1_residual_distribution.png    — error distribution histogram (if data)
    2_predicted_vs_actual.png      — scatter with y=x reference (if data)

Called monthly by `.github/workflows/monthly-hype-retrain.yml`. Can also
be run on-demand to inspect current state. Fully deterministic — same
inputs produce byte-identical plots so git diffs stay reviewable.

Scope note: the first few monthly runs after PR 3 ships will land in
"data accumulation" phase (prediction log has <100 resolved rows). The
report handles this gracefully with a banner at the top; plots are
suppressed when there aren't enough data points to be meaningful.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from analysis.hype_predictor import DB_PATH, MODELS_DIR, HypePredictor  # noqa: E402
from analysis.prediction_health import (  # noqa: E402
    compute_health_summary,
    load_predictions_df,
    summary_to_dict,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

HEALTH_DIR = ROOT_DIR / "data" / "hype_predictor_health"

# Minimum resolved predictions before we draw plots. Below this the plots
# are too noisy to carry signal.
PLOT_MIN_SAMPLE = 20

STATUS_EMOJI = {
    "healthy": "[OK]",
    "warning": "[WARN]",
    "alert": "[ALERT]",
    "insufficient_data": "[PENDING]",
}


def _render_markdown(summary_dict: dict, model_metadata: dict,
                     have_plots: bool) -> str:
    """Compose the README.md body."""
    status = summary_dict["overall_drift_status"]
    status_label = STATUS_EMOJI.get(status, status)

    lines: list[str] = []
    lines.append(f"# Hype Predictor Health Report")
    lines.append("")
    lines.append(f"**Status: {status_label} {status}**  ")
    lines.append(f"**Generated:** `{summary_dict['generated_at']}`  ")
    lines.append(f"**Rolling window:** last {summary_dict['window_days']} days")
    lines.append("")
    lines.append("## Current committed model")
    lines.append("")
    lines.append(f"- **Version:** `{model_metadata['model_version']}`")
    lines.append(f"- **Algorithm:** {model_metadata['algorithm']}")
    lines.append(f"- **Trained:** {model_metadata['created_at'][:10]}")
    lines.append(f"- **Training sample:** {model_metadata['training_sample_size']:,} shows")
    lines.append(f"- **Test MAE at train time (baseline):** {model_metadata['test_mae']:.4f}")
    lines.append(f"- **Signal lift vs predict-mean:** {model_metadata['signal_lift_pct']:.1f}%")
    lines.append("")
    lines.append("## Drift thresholds")
    lines.append("")
    lines.append(f"| Threshold | Rolling MAE value | Meaning |")
    lines.append(f"|-----------|-------------------|---------|")
    lines.append(f"| Warning (baseline x 1.10) | {summary_dict['warning_threshold']:.4f} | Monitor closely |")
    lines.append(f"| Alert (baseline x 1.20) | {summary_dict['alert_threshold']:.4f} | Open issue, consider retrain |")
    lines.append("")
    lines.append("## Rolling-window health")
    lines.append("")
    if summary_dict["overall_rolling_mae"] is None:
        lines.append(f"> **Data accumulation phase.** No resolved predictions yet in the last "
                     f"{summary_dict['window_days']} days. Rolling MAE will appear once the "
                     f"weekly cron (PR #178) has accumulated enough logged predictions whose "
                     f"`actual_score_at_time` is populated.")
    else:
        lines.append(f"- **Rolling MAE:** {summary_dict['overall_rolling_mae']:.4f}")
        lines.append(f"- **Rolling RMSE:** {summary_dict['overall_rolling_rmse']:.4f}")
        drift_delta_pct = ((summary_dict['overall_rolling_mae']
                            - summary_dict['baseline_mae'])
                           / summary_dict['baseline_mae'] * 100.0)
        lines.append(f"- **Drift from baseline:** {drift_delta_pct:+.1f}%")
    lines.append("")
    lines.append("## Prediction log stats")
    lines.append("")
    lines.append(f"- **Total rows:** {summary_dict['total_predictions']:,}")
    lines.append(f"- **Distinct model versions:** {summary_dict['n_model_versions']}")
    lines.append("")
    lines.append("## Per-model-version breakdown")
    lines.append("")
    if not summary_dict["per_model"]:
        lines.append("_No predictions logged yet._")
    else:
        lines.append("| Version | Predictions | Resolved | Rolling MAE | Status |")
        lines.append("|---------|-------------|----------|-------------|--------|")
        for m in summary_dict["per_model"]:
            mae_display = (f"{m['rolling_mae']:.4f}" if m['rolling_mae'] is not None
                           else "(insufficient data)")
            status_label = STATUS_EMOJI.get(m['drift_status'], m['drift_status'])
            lines.append(f"| `{m['model_version']}` | {m['n_predictions']} | "
                         f"{m['n_with_actual']} | {mae_display} | {status_label} |")
    lines.append("")
    lines.append("## Visualizations")
    lines.append("")
    if have_plots:
        lines.append("![Residual distribution](1_residual_distribution.png)")
        lines.append("")
        lines.append("![Predicted vs actual](2_predicted_vs_actual.png)")
    else:
        lines.append(f"_Plots will appear here once at least {PLOT_MIN_SAMPLE} resolved "
                     f"predictions have accumulated. Currently insufficient data._")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("This report is regenerated monthly by "
                 "`.github/workflows/monthly-hype-retrain.yml`. Source: "
                 "`tools/report_prediction_health.py`.")
    return "\n".join(lines)


def _render_plots(df: pd.DataFrame, out_dir: Path) -> bool:
    """Emit the two diagnostic PNGs. Returns True if plots were drawn."""
    resolved = df.dropna(subset=["actual_score_at_time"])
    if len(resolved) < PLOT_MIN_SAMPLE:
        return False

    residuals = resolved["predicted_score"] - resolved["actual_score_at_time"]
    mae = float(residuals.abs().mean())

    # 1 — Residual distribution histogram
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.hist(residuals, bins=30, color="steelblue", edgecolor="black", alpha=0.75)
    ax.axvline(0, color="black", linestyle="--", lw=1, label="Zero error")
    mean_res = float(residuals.mean())
    ax.axvline(mean_res, color="crimson", lw=2,
               label=f"Mean residual = {mean_res:+.3f}")
    ax.set_xlabel("Residual (predicted - actual)", fontsize=11)
    ax.set_ylabel("Count", fontsize=11)
    ax.set_title(f"Logged-prediction residual distribution\n"
                 f"n={len(resolved)}  MAE={mae:.4f}  "
                 f"σ={float(residuals.std()):.3f}", fontsize=12)
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(alpha=0.3, axis="y")
    plt.tight_layout()
    plt.savefig(out_dir / "1_residual_distribution.png", dpi=120)
    plt.close(fig)

    # 2 — Predicted vs actual scatter
    fig, ax = plt.subplots(figsize=(7, 7))
    ax.scatter(resolved["actual_score_at_time"], resolved["predicted_score"],
               alpha=0.4, s=20, color="steelblue", edgecolors="none")
    xmin = float(min(resolved["actual_score_at_time"].min(),
                     resolved["predicted_score"].min()))
    xmax = float(max(resolved["actual_score_at_time"].max(),
                     resolved["predicted_score"].max()))
    ax.plot([xmin, xmax], [xmin, xmax], "k--", lw=1, label="Perfect (y=x)")
    ax.set_xlabel("Actual mal_score at prediction time", fontsize=11)
    ax.set_ylabel("Predicted mal_score", fontsize=11)
    ax.set_title(f"Logged predictions: predicted vs actual\n"
                 f"n={len(resolved)}  MAE={mae:.4f}", fontsize=12)
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(out_dir / "2_predicted_vs_actual.png", dpi=120)
    plt.close(fig)

    return True


def generate_report(db_path: Path = DB_PATH,
                    models_dir: Path = MODELS_DIR,
                    out_dir: Path = HEALTH_DIR,
                    window_days: int = 30) -> dict:
    """Produce the markdown + PNGs. Returns the summary dict for callers."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    predictor = HypePredictor.load_latest(model_dir=Path(models_dir))
    model_metadata = {
        "model_version": predictor.metadata.model_version,
        "algorithm": predictor.metadata.algorithm,
        "created_at": predictor.metadata.created_at,
        "training_sample_size": predictor.metadata.training_sample_size,
        "test_mae": predictor.metadata.test_mae,
        "signal_lift_pct": predictor.metadata.signal_lift_pct,
    }

    conn = sqlite3.connect(str(db_path))
    try:
        summary = compute_health_summary(
            conn, baseline_mae=predictor.metadata.test_mae,
            window_days=window_days,
        )
        df_window = load_predictions_df(conn, since_days=window_days)
    finally:
        conn.close()

    have_plots = _render_plots(df_window, out_dir)

    md = _render_markdown(summary_to_dict(summary), model_metadata, have_plots)
    (out_dir / "README.md").write_text(md, encoding="utf-8")
    (out_dir / "summary.json").write_text(
        json.dumps(summary_to_dict(summary), indent=2),
        encoding="utf-8",
    )

    logger.info(f"Wrote {out_dir / 'README.md'}")
    logger.info(f"Wrote {out_dir / 'summary.json'}")
    if have_plots:
        logger.info(f"Wrote 2 PNG diagnostics")
    else:
        logger.info(f"Skipped plots (fewer than {PLOT_MIN_SAMPLE} resolved predictions)")

    return summary_to_dict(summary)


def main():
    parser = argparse.ArgumentParser(
        description="Generate the hype-predictor monthly health report."
    )
    parser.add_argument("--db", default=str(DB_PATH), help="Vault DB path")
    parser.add_argument("--models-dir", default=str(MODELS_DIR),
                        help="Trained-model directory")
    parser.add_argument("--out-dir", default=str(HEALTH_DIR),
                        help="Output directory")
    parser.add_argument("--window-days", type=int, default=30,
                        help="Rolling-window length in days (default: 30)")
    args = parser.parse_args()

    summary = generate_report(
        db_path=Path(args.db),
        models_dir=Path(args.models_dir),
        out_dir=Path(args.out_dir),
        window_days=args.window_days,
    )
    print(f"\nStatus: {summary['overall_drift_status']}")
    if summary["overall_rolling_mae"] is not None:
        drift_pct = ((summary['overall_rolling_mae'] - summary['baseline_mae'])
                     / summary['baseline_mae'] * 100.0)
        print(f"Rolling MAE: {summary['overall_rolling_mae']:.4f} "
              f"(baseline {summary['baseline_mae']:.4f}, drift {drift_pct:+.1f}%)")


if __name__ == "__main__":
    main()
