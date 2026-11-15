"""COA C Phase A — Popularity-bias root-cause probe.

Runs a small slice of the eval set through the pipeline and captures the
scored_by distribution at three checkpoints:

    Stage 1 — Raw FAISS retrieval (top-300 pre-composite-rerank)
    Stage 2 — Composite-reranked top-15 (what Phase 2 sees)
    Stage 3 — Phase-2 reranker output top-K (what the user sees)

The percentile-band bucketing matches tools/journal_diagnostic.py so the
numbers are directly comparable to the vault-baseline reference.

Intended as a one-off diagnostic for COA C Phase A. Delete after the
mitigation ships and the finding is captured in ENGINE_WORK_PROGRESS.md.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(str(ROOT / "env_variables.env"))
os.environ.setdefault("ANIME_INTEL_CALL_ORIGIN", "probe")

# Silence verbose engine logging so the probe output stays readable.
logging.basicConfig(level=logging.WARNING)

from analysis import vector_store
from analysis.recommendation_engine import RecommendationEngine

DB_PATH = ROOT / "data" / "anime_intelligence_v2.db"
EVAL_PATH = ROOT / "tests" / "data" / "engine_eval_queries.json"


def load_vault_baselines() -> dict:
    """Mirror of tools/journal_diagnostic._load_vault_baselines percentile
    band construction so distributions line up 1:1 with the journal
    diagnostic report."""
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    cur.execute("SELECT scored_by FROM anime_info WHERE scored_by IS NOT NULL")
    scored = sorted(row[0] for row in cur.fetchall())
    conn.close()

    n = len(scored)

    def pct(p: float) -> int:
        return scored[min(n - 1, int(n * p))]

    baselines = {
        "scored_p25": pct(0.25),
        "scored_p50": pct(0.50),
        "scored_p75": pct(0.75),
        "scored_p90": pct(0.90),
    }

    # Vault baseline distribution (percentile bands are equal-count by
    # construction, but rendering them alongside the observed pipeline
    # distributions makes over/under-representation immediately readable).
    vault_dist = Counter()
    for v in scored:
        vault_dist[_bucket(v, baselines)] += 1
    baselines["vault_dist"] = {k: round(v / n * 100, 1) for k, v in vault_dist.items()}
    baselines["vault_n"] = n
    return baselines


def _bucket(v: int | None, b: dict) -> str:
    if v is None:
        return "unrated"
    if v < b["scored_p25"]:
        return "obscure (<p25)"
    if v < b["scored_p50"]:
        return "niche (p25-p50)"
    if v < b["scored_p75"]:
        return "known (p50-p75)"
    if v < b["scored_p90"]:
        return "popular (p75-p90)"
    return "mainstream (>p90)"


_BUCKET_ORDER = [
    "obscure (<p25)", "niche (p25-p50)", "known (p50-p75)",
    "popular (p75-p90)", "mainstream (>p90)", "unrated",
]


def fetch_scored_by(titles: list[str]) -> dict[str, int | None]:
    """Batch lookup {title -> scored_by}. Uses english_title first, then
    romaji_title. Chunked to respect SQLite's 999-parameter cap."""
    if not titles:
        return {}
    out: dict[str, int | None] = {}
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    for i in range(0, len(titles), 400):
        chunk = titles[i:i + 400]
        placeholders = ",".join(["?"] * len(chunk))
        cur.execute(
            f"SELECT english_title, romaji_title, scored_by "
            f"FROM anime_info "
            f"WHERE english_title IN ({placeholders}) OR romaji_title IN ({placeholders})",
            chunk + chunk,
        )
        for en, rj, sc in cur.fetchall():
            if en in chunk:
                out[en] = sc
            if rj in chunk and rj not in out:
                out[rj] = sc
    conn.close()
    return out


def distribution(titles: list[str], baselines: dict) -> dict:
    """Return {bucket: (count, pct)} for a list of titles."""
    meta = fetch_scored_by(titles)
    dist = Counter()
    for t in titles:
        dist[_bucket(meta.get(t), baselines)] += 1
    n = len(titles)
    return {b: (dist[b], round(dist[b] / n * 100, 1) if n else 0.0)
            for b in _BUCKET_ORDER}


def load_eval_slice() -> list[dict]:
    """Load the 4 hidden-gems + 4 vibe-anchor entries used as the Phase A
    probe slice. Vibe-anchor acts as the control — any bias present in
    both categories is baseline pipeline behavior, not hidden-gems
    specific."""
    with open(EVAL_PATH) as f:
        data = json.load(f)
    hidden = [e for e in data["queries"] if e.get("category") == "hidden-gems"]
    vibe = [e for e in data["queries"] if e.get("category") == "vibe-anchor"][:4]
    return hidden + vibe


def probe_query(engine: RecommendationEngine, entry: dict) -> dict:
    """Run one query through all three stages and return per-stage title
    lists."""
    query = entry["query"]
    filters = entry.get("filters", {}) or {}
    era = filters.get("era")
    era_to_years = {
        "1990s": (1990, 1999), "2000s": (2000, 2009),
        "2010s": (2010, 2019), "2020s": (2020, 2029),
    }
    year_min, year_max = era_to_years.get(era, (None, None))
    lens = filters.get("lens") or "Intelligent Search"

    # --- Stage 1: raw FAISS (300 candidates, no composite rerank) ---
    q_emb = vector_store._embed([query])
    raw_faiss = vector_store._search_faiss_raw(q_emb, 300)
    stage_1_titles = [r["title"] for r in raw_faiss[:50]]  # top-50 for parity

    # --- Stage 2: composite-reranked top-15 (what Phase 2 sees) ---
    search_result = vector_store.search(
        query, top_k=15, year_min=year_min, year_max=year_max,
    )
    stage_2_titles = [r["title"] for r in search_result["results"]]

    # --- Stage 3: Phase-2 reranker output (what the user sees) ---
    fetch_result = engine.fetch_vault_pool(
        query, lens_name=lens, year_min=year_min, year_max=year_max,
        era_label=None if era in (None, "All") else era,
        rating_max=filters.get("rating_max"),
    )
    stage_3_titles: list[str] = []
    for cand in fetch_result.get("pool", []) or []:
        profile = cand.get("profile") or {}
        title = profile.get("title") or profile.get("english_title")
        if title:
            stage_3_titles.append(title)

    return {
        "id": entry["id"],
        "query": query,
        "category": entry["category"],
        "stage_1_top_50": stage_1_titles,
        "stage_2_top_15": stage_2_titles,
        "stage_3_top_K": stage_3_titles,
        "stage_3_top_3": stage_3_titles[:3],
        "fallback_mode": fetch_result.get("fallback_mode"),
        "low_confidence": fetch_result.get("low_confidence"),
        "intent": search_result["meta"].get("intent"),
    }


def render_bucket_line(label: str, dist: dict, n: int) -> str:
    parts = [f"{label:<25} n={n:<3}"]
    for b in _BUCKET_ORDER:
        c, pct = dist[b]
        parts.append(f"{b.split(' (')[0]}={pct:>4.1f}%")
    return "  ".join(parts)


def aggregate_stage(records: list[dict], stage_key: str, baselines: dict) -> dict:
    """Aggregate title lists across records for a given stage."""
    all_titles = []
    for r in records:
        all_titles.extend(r[stage_key])
    return distribution(all_titles, baselines), len(all_titles)


def main():
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print("ERROR: GOOGLE_API_KEY missing.", file=sys.stderr)
        return 2

    baselines = load_vault_baselines()
    print(f"Vault baseline (n={baselines['vault_n']:,}):")
    for b in _BUCKET_ORDER:
        v = baselines["vault_dist"].get(b, 0.0)
        print(f"  {b:<25} {v:>5.1f}%")
    print(f"\nPercentile bands: p25={baselines['scored_p25']:,}  "
          f"p50={baselines['scored_p50']:,}  p75={baselines['scored_p75']:,}  "
          f"p90={baselines['scored_p90']:,}")

    slice_entries = load_eval_slice()
    print(f"\nProbing {len(slice_entries)} eval entries "
          f"({sum(1 for e in slice_entries if e['category']=='hidden-gems')} hidden-gems, "
          f"{sum(1 for e in slice_entries if e['category']=='vibe-anchor')} vibe-anchor control)...\n")

    engine = RecommendationEngine(api_key=api_key)

    per_query = []
    for entry in slice_entries:
        try:
            rec = probe_query(engine, entry)
            per_query.append(rec)
            print(f"  ✓ {rec['category']:<12} {rec['id']:<40} "
                  f"intent={rec['intent']}  s1={len(rec['stage_1_top_50'])}  "
                  f"s2={len(rec['stage_2_top_15'])}  s3={len(rec['stage_3_top_K'])}  "
                  f"fb={rec['fallback_mode']}")
        except Exception as e:
            print(f"  ✗ {entry['id']}: {type(e).__name__}: {e}", file=sys.stderr)

    # Aggregate by category
    print("\n" + "=" * 100)
    print("HIDDEN-GEMS CATEGORY (4 entries)")
    print("=" * 100)
    hg = [r for r in per_query if r["category"] == "hidden-gems"]
    for stage_key, label in [
        ("stage_1_top_50", "Stage 1 — raw FAISS top-50"),
        ("stage_2_top_15", "Stage 2 — composite top-15"),
        ("stage_3_top_K", "Stage 3 — Phase-2 top-K"),
        ("stage_3_top_3", "Stage 3 — Phase-2 top-3"),
    ]:
        dist, n = aggregate_stage(hg, stage_key, baselines)
        print(render_bucket_line(label, dist, n))

    print("\n" + "=" * 100)
    print("VIBE-ANCHOR CONTROL (4 entries)")
    print("=" * 100)
    va = [r for r in per_query if r["category"] == "vibe-anchor"]
    for stage_key, label in [
        ("stage_1_top_50", "Stage 1 — raw FAISS top-50"),
        ("stage_2_top_15", "Stage 2 — composite top-15"),
        ("stage_3_top_K", "Stage 3 — Phase-2 top-K"),
        ("stage_3_top_3", "Stage 3 — Phase-2 top-3"),
    ]:
        dist, n = aggregate_stage(va, stage_key, baselines)
        print(render_bucket_line(label, dist, n))

    print("\n" + "=" * 100)
    print("VAULT BASELINE (reference)")
    print("=" * 100)
    print(render_bucket_line("Vault distribution", {
        b: (int(baselines["vault_dist"].get(b, 0.0) * baselines["vault_n"] / 100),
            baselines["vault_dist"].get(b, 0.0)) for b in _BUCKET_ORDER
    }, baselines["vault_n"]))

    # Persist for later analysis
    out_path = ROOT / "data" / "probe_popularity_bias_results.json"
    with open(out_path, "w") as f:
        json.dump({"baselines": baselines, "per_query": per_query}, f, indent=2)
    print(f"\nFull results written to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
