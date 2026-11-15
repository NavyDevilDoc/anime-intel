"""
PR C / Initiative 2 Phase E — pool-degradation empirical probe.

Runs 13 stratified queries through the live engine under three exclusion
conditions and writes a findings memo to data/phase_e_findings.md
(gitignored — scratch artifact for the PR description).

Conditions
----------
A. Baseline           — no exclusion (current engine behavior)
B. User's MAL list    — fetched live via fetch_mal() from AnimeMathNerd.
                        Represents a small real-world exclusion (~25
                        MAL IDs → cluster-expanded to ~50-100).
C. Synthetic heavy    — top 1,000 most-scored TV shows from the vault.
                        Represents a heavy MAL user who has watched
                        most of the popular catalog.

Each query × condition captures:
  - result_count (0 = empty pool)
  - top_match_confidence (None if empty)
  - banner_fired (top_confidence < 75 OR pool empty)
  - top_5_titles (for Jaccard comparison vs baseline)

Pre-merge gates from IMPLEMENTATION_PLAN.md Phase E:
  - Empty-pool rate < 5% under user-list condition
  - Banner-fire rate under exclusion within 2-3x the baseline rate

Usage:
    python tools/probe_watchlist_pool_degradation.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

ROOT_DIR = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# Disable journal writes so the probe doesn't pollute analytics.
os.environ["TESTING"] = "1"

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT_DIR, "env_variables.env"))

# Bypass the per-IP daily anon quota so the probe can make 39 engine
# calls per run without burning through the 30-search + 10-DNA budget.
# Matches the test_api.py pattern. Without this, every call after the
# quota cap returns HTTP 429 fast-fail and the engine never runs —
# results look like uniformly empty pools and zero-second timings.
from unittest.mock import patch  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from analysis.queries import expand_to_cluster_ids  # noqa: E402
from analysis.watchlist_fetcher import fetch_mal  # noqa: E402
from api.main import app  # noqa: E402
from api.middleware.anon_quota import AnonRemaining  # noqa: E402

_BYPASS_REMAINING = AnonRemaining(30, 10)
for _mod in ("api.routes.search", "api.routes.dna"):
    patch(f"{_mod}.check_anon_quota", return_value=_BYPASS_REMAINING).start()

DB_PATH = os.path.join(ROOT_DIR, "data", "anime_intelligence_v2.db")
FINDINGS_PATH = os.path.join(ROOT_DIR, "data", "phase_e_findings.md")
RAW_PATH = os.path.join(ROOT_DIR, "data", "phase_e_raw.json")
FIXTURES_PATH = os.path.join(ROOT_DIR, "tests", "data", "watchlist_test_fixtures.json")

# Stratified query set across FaS intent types and SLT shapes.
FAS_QUERIES = [
    {"query": "dark psychological thriller with mind games", "intent": "vibe"},
    {"query": "wholesome slice of life with comedy", "intent": "vibe"},
    {"query": "underrated sci-fi anime that nobody talks about", "intent": "hidden_gems"},
    {"query": "seinen military anime about the cost of war", "intent": "genre_combo"},
    {"query": "most popular shounen of all time", "intent": "crowd_pleasers"},
    {"query": "controversial isekai that fans love or hate", "intent": "polarizing"},
    {"query": "anime where the MC is betrayed by a friend", "intent": "narrative"},
    {"query": "cozy slice of life", "intent": "vibe"},
    {"query": "romance anime", "intent": "vibe"},
    {"query": "mecha anime", "intent": "genre_combo"},
]

SLT_QUERIES = [
    {"titles": ["Attack on Titan"], "shape": "single_popular"},
    {"titles": ["Steins;Gate", "Ga-Rei-Zero", "Erased"], "shape": "triple_complex"},
    {"titles": ["Cowboy Bebop", "Samurai Champloo", "Carole & Tuesday"], "shape": "triple_distinctive"},
]

BANNER_THRESHOLD = 75  # per PR #72


def synthetic_heavy_exclusion(n: int = 1000) -> list[int]:
    """Top-N most-scored TV shows — heavy-user proxy."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM anime_info "
        "WHERE subtype = 'TV' AND scored_by IS NOT NULL "
        "ORDER BY scored_by DESC LIMIT ?",
        (n,),
    )
    rows = [r[0] for r in cur.fetchall()]
    conn.close()
    return rows


def call_search(client: TestClient, query: str, exclude_mal_ids: list[int] | None) -> dict:
    body = {"query": query}
    if exclude_mal_ids:
        body["exclude_mal_ids"] = exclude_mal_ids
    t0 = time.time()
    resp = client.post("/api/search", json=body)
    elapsed = time.time() - t0
    payload = resp.json() if resp.status_code == 200 else {}
    pool = payload.get("pool") or []
    top_conf = pool[0]["match_confidence"] if pool else None
    return {
        "elapsed_s": round(elapsed, 1),
        "result_count": len(pool),
        "top_match_confidence": top_conf,
        "low_confidence": payload.get("low_confidence", False),
        "banner_fired": (not pool) or (top_conf is not None and top_conf < BANNER_THRESHOLD),
        "top_5_titles": [p["profile"]["title"] for p in pool[:5]],
    }


def call_dna(client: TestClient, titles: list[str], exclude_mal_ids: list[int] | None) -> dict:
    body = {"titles": titles}
    if exclude_mal_ids:
        body["exclude_mal_ids"] = exclude_mal_ids
    t0 = time.time()
    resp = client.post("/api/dna", json=body)
    elapsed = time.time() - t0
    payload = resp.json() if resp.status_code == 200 else {}
    data = payload.get("data") or []
    top_conf = data[0]["match_confidence"] if data else None
    return {
        "elapsed_s": round(elapsed, 1),
        "result_count": len(data),
        "top_match_confidence": top_conf,
        "low_confidence": payload.get("low_confidence", False),
        "banner_fired": (not data) or (top_conf is not None and top_conf < BANNER_THRESHOLD),
        "top_5_titles": [
            r["profile"]["title"]
            for r in data[:5]
        ],
    }


def jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    if not (sa | sb):
        return 1.0
    return len(sa & sb) / len(sa | sb)


def run():
    print(f"Phase E pool-degradation probe — started {datetime.now(timezone.utc).isoformat()}")
    print(f"  DB: {DB_PATH}")

    # Load exclusion sets
    print("\nFetching user MAL list (AnimeMathNerd)...")
    with open(FIXTURES_PATH) as f:
        username = json.load(f)["mal_username"]
    user_result = fetch_mal(username, statuses=["completed", "dropped", "watching"])
    user_raw = user_result.mal_ids
    user_expanded = sorted(expand_to_cluster_ids(user_raw))
    print(f"  Raw user IDs: {len(user_raw)}")
    print(f"  Cluster-expanded: {len(user_expanded)}")

    synthetic_raw = synthetic_heavy_exclusion(1000)
    synthetic_expanded = sorted(expand_to_cluster_ids(synthetic_raw))
    print(f"  Synthetic raw (top-1k TV by scored_by): {len(synthetic_raw)}")
    print(f"  Synthetic cluster-expanded: {len(synthetic_expanded)}")

    client = TestClient(app)
    results: list[dict] = []

    total_queries = len(FAS_QUERIES) + len(SLT_QUERIES)
    qi = 0

    for q in FAS_QUERIES:
        qi += 1
        print(f"\n[{qi}/{total_queries}] FaS — {q['query']!r}")
        baseline = call_search(client, q["query"], None)
        print(f"  baseline    n={baseline['result_count']:<3} top={baseline['top_match_confidence']} banner={baseline['banner_fired']} ({baseline['elapsed_s']}s)")
        user = call_search(client, q["query"], user_expanded)
        print(f"  user-list   n={user['result_count']:<3} top={user['top_match_confidence']} banner={user['banner_fired']} ({user['elapsed_s']}s)")
        synth = call_search(client, q["query"], synthetic_expanded)
        print(f"  synth-heavy n={synth['result_count']:<3} top={synth['top_match_confidence']} banner={synth['banner_fired']} ({synth['elapsed_s']}s)")
        results.append({
            "mode": "fas",
            "query": q["query"],
            "intent": q["intent"],
            "baseline": baseline,
            "user_list": user,
            "synthetic_heavy": synth,
            "jaccard_user_vs_baseline": round(jaccard(user["top_5_titles"], baseline["top_5_titles"]), 2),
            "jaccard_synth_vs_baseline": round(jaccard(synth["top_5_titles"], baseline["top_5_titles"]), 2),
        })

    for q in SLT_QUERIES:
        qi += 1
        print(f"\n[{qi}/{total_queries}] SLT — {' + '.join(q['titles'])}")
        baseline = call_dna(client, q["titles"], None)
        print(f"  baseline    n={baseline['result_count']:<3} top={baseline['top_match_confidence']} banner={baseline['banner_fired']} ({baseline['elapsed_s']}s)")
        user = call_dna(client, q["titles"], user_expanded)
        print(f"  user-list   n={user['result_count']:<3} top={user['top_match_confidence']} banner={user['banner_fired']} ({user['elapsed_s']}s)")
        synth = call_dna(client, q["titles"], synthetic_expanded)
        print(f"  synth-heavy n={synth['result_count']:<3} top={synth['top_match_confidence']} banner={synth['banner_fired']} ({synth['elapsed_s']}s)")
        results.append({
            "mode": "slt",
            "query": " + ".join(q["titles"]),
            "shape": q["shape"],
            "baseline": baseline,
            "user_list": user,
            "synthetic_heavy": synth,
            "jaccard_user_vs_baseline": round(jaccard(user["top_5_titles"], baseline["top_5_titles"]), 2),
            "jaccard_synth_vs_baseline": round(jaccard(synth["top_5_titles"], baseline["top_5_titles"]), 2),
        })

    # Aggregate metrics
    n_queries = len(results)

    def empty_rate(condition: str) -> float:
        return sum(1 for r in results if r[condition]["result_count"] == 0) / n_queries

    def banner_rate(condition: str) -> float:
        return sum(1 for r in results if r[condition]["banner_fired"]) / n_queries

    def mean_top_conf(condition: str) -> float | None:
        confs = [r[condition]["top_match_confidence"] for r in results
                 if r[condition]["top_match_confidence"] is not None]
        return round(sum(confs) / len(confs), 1) if confs else None

    def mean_jaccard(key: str) -> float:
        return round(sum(r[key] for r in results) / n_queries, 2)

    summary = {
        "n_queries": n_queries,
        "user_raw_count": len(user_raw),
        "user_expanded_count": len(user_expanded),
        "synthetic_raw_count": len(synthetic_raw),
        "synthetic_expanded_count": len(synthetic_expanded),
        "empty_rate_baseline_pct": round(empty_rate("baseline") * 100, 1),
        "empty_rate_user_list_pct": round(empty_rate("user_list") * 100, 1),
        "empty_rate_synth_pct": round(empty_rate("synthetic_heavy") * 100, 1),
        "banner_rate_baseline_pct": round(banner_rate("baseline") * 100, 1),
        "banner_rate_user_list_pct": round(banner_rate("user_list") * 100, 1),
        "banner_rate_synth_pct": round(banner_rate("synthetic_heavy") * 100, 1),
        "mean_top_conf_baseline": mean_top_conf("baseline"),
        "mean_top_conf_user_list": mean_top_conf("user_list"),
        "mean_top_conf_synth": mean_top_conf("synthetic_heavy"),
        "mean_jaccard_user_vs_baseline": mean_jaccard("jaccard_user_vs_baseline"),
        "mean_jaccard_synth_vs_baseline": mean_jaccard("jaccard_synth_vs_baseline"),
    }

    print("\n" + "=" * 60)
    print("AGGREGATE METRICS")
    print("=" * 60)
    print(f"  Queries run: {summary['n_queries']}")
    print(f"  User exclusion size: raw={summary['user_raw_count']} expanded={summary['user_expanded_count']}")
    print(f"  Synthetic exclusion size: raw={summary['synthetic_raw_count']} expanded={summary['synthetic_expanded_count']}")
    print()
    print(f"  Empty-pool rate:  baseline={summary['empty_rate_baseline_pct']}%  user={summary['empty_rate_user_list_pct']}%  synth={summary['empty_rate_synth_pct']}%")
    print(f"  Banner-fire rate: baseline={summary['banner_rate_baseline_pct']}%  user={summary['banner_rate_user_list_pct']}%  synth={summary['banner_rate_synth_pct']}%")
    print(f"  Mean top conf:    baseline={summary['mean_top_conf_baseline']}  user={summary['mean_top_conf_user_list']}  synth={summary['mean_top_conf_synth']}")
    print(f"  Mean Jaccard (top-5 vs baseline): user={summary['mean_jaccard_user_vs_baseline']}  synth={summary['mean_jaccard_synth_vs_baseline']}")

    # Write raw + memo
    with open(RAW_PATH, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "per_query": results}, f, indent=2, ensure_ascii=False)

    with open(FINDINGS_PATH, "w", encoding="utf-8") as f:
        f.write("# Phase E findings — watchlist exclusion pool degradation\n\n")
        f.write(f"Probe run: {datetime.now(timezone.utc).isoformat()}\n\n")
        f.write("## Exclusion-set sizes\n\n")
        f.write(f"- **User's MAL list** ({username}): {summary['user_raw_count']} raw IDs → {summary['user_expanded_count']} after cluster expansion\n")
        f.write(f"- **Synthetic heavy** (top-1k TV by scored_by): {summary['synthetic_raw_count']} raw IDs → {summary['synthetic_expanded_count']} after cluster expansion\n\n")
        f.write(f"## Aggregate metrics ({n_queries} queries: 10 FaS + 3 SLT)\n\n")
        f.write("| Metric | Baseline | User list | Synthetic heavy |\n")
        f.write("|---|---|---|---|\n")
        f.write(f"| Empty-pool rate | {summary['empty_rate_baseline_pct']}% | {summary['empty_rate_user_list_pct']}% | {summary['empty_rate_synth_pct']}% |\n")
        f.write(f"| Banner-fire rate | {summary['banner_rate_baseline_pct']}% | {summary['banner_rate_user_list_pct']}% | {summary['banner_rate_synth_pct']}% |\n")
        f.write(f"| Mean top confidence | {summary['mean_top_conf_baseline']} | {summary['mean_top_conf_user_list']} | {summary['mean_top_conf_synth']} |\n")
        f.write(f"| Mean Jaccard top-5 vs baseline | 1.0 | {summary['mean_jaccard_user_vs_baseline']} | {summary['mean_jaccard_synth_vs_baseline']} |\n\n")
        f.write("## Pre-merge success criteria (from IMPLEMENTATION_PLAN.md Phase E)\n\n")
        baseline_banner = summary['banner_rate_baseline_pct']
        ok_user_banner = summary['banner_rate_user_list_pct'] <= max(baseline_banner * 3, baseline_banner + 5)
        ok_user_empty = summary['empty_rate_user_list_pct'] < 5
        f.write(f"- Empty-pool rate < 5% under user-list condition: **{summary['empty_rate_user_list_pct']}%** — {'PASS' if ok_user_empty else 'FAIL'}\n")
        f.write(f"- Banner rate under user-list within 2-3x baseline ({baseline_banner}%): **{summary['banner_rate_user_list_pct']}%** — {'PASS' if ok_user_banner else 'FAIL'}\n")
        f.write("- Synthetic-heavy is informational only (worst case > average user)\n\n")
        f.write("## Per-query detail\n\n")
        f.write("Raw data in `data/phase_e_raw.json` (also gitignored).\n\n")
        f.write("| Mode | Query | Baseline (n / top%) | User (n / top%) | Synth (n / top%) | Jac-user | Jac-synth |\n")
        f.write("|---|---|---|---|---|---|---|\n")
        for r in results:
            q = r["query"][:50]
            f.write(
                f"| {r['mode'].upper()} | {q} | "
                f"{r['baseline']['result_count']} / {r['baseline']['top_match_confidence']} | "
                f"{r['user_list']['result_count']} / {r['user_list']['top_match_confidence']} | "
                f"{r['synthetic_heavy']['result_count']} / {r['synthetic_heavy']['top_match_confidence']} | "
                f"{r['jaccard_user_vs_baseline']} | "
                f"{r['jaccard_synth_vs_baseline']} |\n"
            )

    print(f"\nMemo: {FINDINGS_PATH}")
    print(f"Raw:  {RAW_PATH}")


if __name__ == "__main__":
    run()
