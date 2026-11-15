"""
tools/probe_reranker_variance.py — measure reranker output variance across
identical repeated queries.

Purpose: quantify how much Gemini's reranker stochasticity affects
user-visible outputs (pool contents, ordering, top-1 confidence). Used
as the empirical driver for the temperature-control lever from the
reranker-stochasticity mitigation plan.

Approach: pick an eval entry, run it N times with the exact same inputs
(query text, mode, filters), capture per-run pool contents + confidences,
compute variance summary stats. Diff two label-tagged runs (baseline vs
temperature-0) to measure the effect of a config change.

Usage:
    # Baseline capture (current SDK-default temperature)
    python tools/probe_reranker_variance.py --entry slt-steins-psycho --n 5 --label baseline

    # Treatment capture (after adding temperature=0 to reranker config)
    python tools/probe_reranker_variance.py --entry slt-steins-psycho --n 5 --label temp0

    # Diff two captures
    python tools/probe_reranker_variance.py --diff data/rerank_variance/slt-steins-psycho-baseline.json data/rerank_variance/slt-steins-psycho-temp0.json

Cost per capture: ~$0.005-$0.010 per IS run, ~$0.015-$0.020 per SLT run.
15 samples across 3 queries at N=5 each ≈ $0.10-$0.20.

Gemini calls tagged ANIME_INTEL_CALL_ORIGIN=probe by default — override via
env var to attribute to a different origin bucket in the gemini_calls ledger.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent if SCRIPT_DIR.name == "tools" else SCRIPT_DIR
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

os.environ.setdefault("ANIME_INTEL_CALL_ORIGIN", "probe")

from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT_DIR, "env_variables.env"))

from analysis.recommendation_engine import RecommendationEngine  # noqa: E402

EVAL_PATH = ROOT_DIR / "tests" / "data" / "engine_eval_queries.json"
OUT_DIR = ROOT_DIR / "data" / "rerank_variance"

# Banner threshold — matches LowConfidenceBanner in the frontend and the
# threshold documented in the confidence-distribution baseline.
BANNER_THRESHOLD = 75


def _load_entry(entry_id: str) -> dict:
    with open(EVAL_PATH, encoding="utf-8") as f:
        data = json.load(f)
    for q in data["queries"]:
        if q["id"] == entry_id:
            return q
    raise SystemExit(f"ERROR: eval entry {entry_id!r} not found")


def _run_single(engine: RecommendationEngine, entry: dict) -> dict:
    """Execute one query and return a normalized per-run record."""
    mode = entry.get("mode", "find_a_show")
    t0 = time.perf_counter()

    if mode == "find_a_show":
        filters = entry.get("filters") or {}
        lens = filters.get("lens") or "Intelligent Search"
        # Eval-set filters (era + rating_max) don't have kwarg equivalents on
        # fetch_vault_pool at the moment — omit unless needed. slt/vibe/hidden
        # queries in the current probe target set don't use them.
        result = engine.fetch_vault_pool(entry["query"], lens_name=lens)
        pool = result.get("pool", [])
    elif mode == "shows_like_this":
        refs = entry.get("reference_titles") or []
        result = engine.execute_dna_triangulation(refs)
        pool = result.get("data", [])
    else:
        raise SystemExit(f"ERROR: unknown mode {mode!r} on entry {entry['id']}")

    elapsed = time.perf_counter() - t0

    # Normalize per-result shape — profile.title, match_confidence
    normalized = []
    for r in pool:
        profile = r.get("profile") or {}
        title = (
            profile.get("title")
            or profile.get("english_title")
            or profile.get("romaji_title")
            or "?"
        )
        normalized.append({
            "title": title,
            "match_confidence": r.get("match_confidence"),
        })

    top_1_conf = normalized[0]["match_confidence"] if normalized else None
    banner_fired = (top_1_conf is None) or (top_1_conf < BANNER_THRESHOLD)

    return {
        "pool_size": len(normalized),
        "top_1_title": normalized[0]["title"] if normalized else None,
        "top_1_confidence": top_1_conf,
        "top_3_titles": [r["title"] for r in normalized[:3]],
        "top_5_titles": [r["title"] for r in normalized[:5]],
        "top_10_titles": [r["title"] for r in normalized[:10]],
        "banner_fired": banner_fired,
        "low_confidence": result.get("low_confidence", False),
        "fallback_mode": result.get("fallback_mode"),
        "elapsed_s": round(elapsed, 2),
    }


def _pairwise_jaccard(sets: list[set]) -> float | None:
    """Mean pairwise Jaccard across all C(N, 2) unordered set pairs.
    None if fewer than 2 sets."""
    if len(sets) < 2:
        return None
    scores = []
    for a, b in itertools.combinations(sets, 2):
        if not a and not b:
            scores.append(1.0)
            continue
        u = a | b
        i = a & b
        scores.append(len(i) / len(u) if u else 0.0)
    return round(statistics.mean(scores), 3) if scores else None


def _summarize(runs: list[dict]) -> dict:
    """Compute variance summary across the N runs."""
    pool_sizes = [r["pool_size"] for r in runs]
    confs = [r["top_1_confidence"] for r in runs if r["top_1_confidence"] is not None]
    top_3_sets = [set(r["top_3_titles"]) for r in runs]
    top_5_sets = [set(r["top_5_titles"]) for r in runs]

    conf_stats = None
    if confs:
        conf_stats = {
            "min": min(confs),
            "max": max(confs),
            "range": max(confs) - min(confs),
            "mean": round(statistics.mean(confs), 2),
            "stdev": round(statistics.stdev(confs), 2) if len(confs) > 1 else 0.0,
        }

    return {
        "pool_size": {
            "min": min(pool_sizes),
            "max": max(pool_sizes),
            "mean": round(statistics.mean(pool_sizes), 2),
            "stdev": round(statistics.stdev(pool_sizes), 2) if len(pool_sizes) > 1 else 0.0,
        },
        "pool_collapse_count": sum(1 for s in pool_sizes if s < 5),
        "banner_fire_count": sum(1 for r in runs if r["banner_fired"]),
        "top_3_jaccard_mean": _pairwise_jaccard(top_3_sets),
        "top_5_jaccard_mean": _pairwise_jaccard(top_5_sets),
        "top_1_confidence": conf_stats,
        "elapsed_s_total": round(sum(r["elapsed_s"] for r in runs), 2),
        "elapsed_s_mean": round(statistics.mean([r["elapsed_s"] for r in runs]), 2),
    }


def run_capture(entry_id: str, n: int, label: str) -> Path:
    entry = _load_entry(entry_id)
    print("\n=== VARIANCE PROBE ===")
    print(f"  entry:  {entry_id}")
    print(f"  mode:   {entry.get('mode')}")
    if entry.get("mode") == "find_a_show":
        print(f"  query:  {entry.get('query')!r}")
    else:
        print(f"  refs:   {entry.get('reference_titles')}")
    print(f"  n:      {n}")
    print(f"  label:  {label}")
    print()

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise SystemExit("ERROR: GOOGLE_API_KEY not in env")
    engine = RecommendationEngine(api_key=api_key)

    runs = []
    for i in range(1, n + 1):
        print(f"  run {i}/{n}...", end=" ", flush=True)
        try:
            record = _run_single(engine, entry)
            runs.append(record)
            print(
                f"pool={record['pool_size']:>2}  "
                f"top1conf={record['top_1_confidence']}  "
                f"banner={'Y' if record['banner_fired'] else 'n'}  "
                f"{record['elapsed_s']}s"
            )
        except Exception as e:
            print(f"FAILED: {e}")
            runs.append({"error": str(e), "pool_size": 0, "top_1_confidence": None,
                         "top_3_titles": [], "top_5_titles": [], "top_10_titles": [],
                         "banner_fired": True, "low_confidence": True,
                         "fallback_mode": None, "elapsed_s": 0.0, "top_1_title": None})

    summary = _summarize(runs)
    payload = {
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "entry_id": entry_id,
        "mode": entry.get("mode"),
        "query": entry.get("query"),
        "reference_titles": entry.get("reference_titles"),
        "n_runs": n,
        "label": label,
        "runs": runs,
        "summary": summary,
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"{entry_id}-{label}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    print(f"\n=== SUMMARY ({label}) ===")
    print(f"  pool_size:            {summary['pool_size']}")
    print(f"  pool_collapse_count:  {summary['pool_collapse_count']} / {n}  (runs with pool < 5)")
    print(f"  banner_fire_count:    {summary['banner_fire_count']} / {n}  (runs with top-1 conf < {BANNER_THRESHOLD})")
    print(f"  top_3_jaccard_mean:   {summary['top_3_jaccard_mean']}  (1.0 = identical top-3 across all runs)")
    print(f"  top_5_jaccard_mean:   {summary['top_5_jaccard_mean']}")
    if summary["top_1_confidence"]:
        c = summary["top_1_confidence"]
        print(f"  top_1_confidence:     min={c['min']}  max={c['max']}  range={c['range']}  stdev={c['stdev']}")
    print(f"  elapsed:              total={summary['elapsed_s_total']}s  mean={summary['elapsed_s_mean']}s")
    print(f"\n  written: {out_path}")
    return out_path


def run_diff(baseline_path: str, treatment_path: str) -> None:
    with open(baseline_path, encoding="utf-8") as f:
        b = json.load(f)
    with open(treatment_path, encoding="utf-8") as f:
        t = json.load(f)

    if b["entry_id"] != t["entry_id"]:
        print(f"WARNING: entry_id mismatch {b['entry_id']!r} vs {t['entry_id']!r}")

    print("\n=== DIFF ===")
    print(f"  entry:      {b['entry_id']}")
    print(f"  baseline:   {b['label']} (n={b['n_runs']})")
    print(f"  treatment:  {t['label']} (n={t['n_runs']})")
    print()

    bs, ts = b["summary"], t["summary"]

    def _line(label: str, bv, tv):
        if bv is None or tv is None:
            print(f"  {label:<28s} baseline={bv}  treatment={tv}")
            return
        delta = tv - bv
        arrow = "==" if delta == 0 else ("UP" if delta > 0 else "DN")
        print(f"  {label:<28s} baseline={bv:<8}  treatment={tv:<8}  {arrow} {delta:+g}")

    _line("pool_size.mean", bs["pool_size"]["mean"], ts["pool_size"]["mean"])
    _line("pool_size.stdev", bs["pool_size"]["stdev"], ts["pool_size"]["stdev"])
    _line("pool_collapse_count", bs["pool_collapse_count"], ts["pool_collapse_count"])
    _line("banner_fire_count", bs["banner_fire_count"], ts["banner_fire_count"])
    _line("top_3_jaccard_mean", bs["top_3_jaccard_mean"], ts["top_3_jaccard_mean"])
    _line("top_5_jaccard_mean", bs["top_5_jaccard_mean"], ts["top_5_jaccard_mean"])
    if bs["top_1_confidence"] and ts["top_1_confidence"]:
        _line("top_1_conf.range", bs["top_1_confidence"]["range"], ts["top_1_confidence"]["range"])
        _line("top_1_conf.stdev", bs["top_1_confidence"]["stdev"], ts["top_1_confidence"]["stdev"])
        _line("top_1_conf.mean", bs["top_1_confidence"]["mean"], ts["top_1_confidence"]["mean"])


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--entry", help="Eval entry id to probe (e.g. slt-steins-psycho)")
    p.add_argument("--n", type=int, default=5, help="Number of repeated runs (default 5)")
    p.add_argument("--label", default="baseline", help="Label for the output file (e.g. baseline, temp0)")
    p.add_argument("--diff", nargs=2, metavar=("BASELINE_PATH", "TREATMENT_PATH"),
                   help="Instead of capturing, diff two existing captures.")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.diff:
        run_diff(args.diff[0], args.diff[1])
    else:
        if not args.entry:
            raise SystemExit("ERROR: --entry is required unless --diff is used")
        run_capture(args.entry, args.n, args.label)


if __name__ == "__main__":
    sys.exit(main() or 0)
