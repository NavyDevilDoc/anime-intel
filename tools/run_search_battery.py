"""
TOOL: tools/run_search_battery.py
Automated search battery for engine tuning. Runs a set of queries across
all era filters and both discovery modes, logging every result to the
search journal (data/search_journal.jsonl).

Usage:
    python tools/run_search_battery.py

Phase 1 (retrieval + composite reranking) is 100% deterministic.
Phase 2 (Gemini reranking) is stochastic but consistent within ±5-10 pts.
Objective Rankings bypass Phase 2 entirely (100% deterministic).

Results are appended to data/search_journal.jsonl for offline analysis.
A summary CSV is also written to data/battery_results.csv for quick review.
"""

import csv
import os
import sys
import time
from datetime import datetime

# --- BULLETPROOF PATH ANCHORING ---
SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT_DIR, "env_variables.env"))

from analysis.recommendation_engine import RecommendationEngine

# --- TEST BATTERY ---

INTELLIGENT_QUERIES = [
    "dark psychological thriller with mind games",
    "wholesome slice of life with comedy",
    "underrated sci-fi anime that nobody talks about",
    "most popular romance anime everyone loves",
    "controversial anime that divided the fanbase",
    "mecha anime with romance aspects",
    "anime that explores grief and loss",
    "intense sports competition with strong rivalries",
    "magical fantasy adventure with a journey",
    "seinen military anime about the cost of war",
]

OBJECTIVE_QUERIES = [
    "top sports anime",
    "top romance anime from 2023",
    "best mecha anime",
    "top anime from Winter 2024",
    "best sci-fi anime after 2020",
    "top horror anime from 2015 to 2020",
    "top shounen anime",
    "best anime from Fall 2019",
    "top comedy anime before 2010",
    "best fantasy adventure anime from 2022",
]

ERA_OPTIONS = {
    "All":   (None, None),
    "1990s": (1990, 1999),
    "2000s": (2000, 2009),
    "2010s": (2010, 2019),
    "2020s": (2020, None),
}


def run_battery():
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print("ERROR: GOOGLE_API_KEY not found. Set it in env_variables.env.")
        sys.exit(1)

    engine = RecommendationEngine(api_key=api_key)

    csv_path = os.path.join(ROOT_DIR, "data", "battery_results.csv")
    csv_file = open(csv_path, "w", newline="", encoding="utf-8")
    writer = csv.writer(csv_file)
    writer.writerow([
        "timestamp", "query", "mode", "era", "success",
        "result_count", "top_1", "top_1_confidence", "top_1_similarity",
        "top_3_titles", "avg_confidence", "elapsed_sec",
    ])

    # Pair each mode with its purpose-built query set
    mode_queries = [
        ("Intelligent Search", INTELLIGENT_QUERIES),
        ("Objective Rankings", OBJECTIVE_QUERIES),
    ]

    total = sum(len(qs) * len(ERA_OPTIONS) for _, qs in mode_queries)
    completed = 0

    for mode, queries in mode_queries:
        for query in queries:
            for era_label, (year_min, year_max) in ERA_OPTIONS.items():
                completed += 1
                tag = f"[{completed}/{total}]"
                print(f"{tag} {mode} | {era_label} | \"{query}\"")

                start = time.time()
                try:
                    result = engine.fetch_vault_pool(
                        query, mode,
                        year_min=year_min, year_max=year_max,
                        era_label=era_label,
                    )
                    elapsed = time.time() - start
                    success = result.get("success", False)
                    pool = result.get("pool", [])

                    if success and pool:
                        top = pool[0]
                        top_title = top["profile"]["title"]
                        top_conf = top["match_confidence"]
                        top_sim = top["profile"].get("semantic_similarity", 0.0)
                        top_3 = " | ".join(
                            item["profile"]["title"] for item in pool[:3]
                        )
                        avg_conf = sum(
                            item["match_confidence"] for item in pool
                        ) / len(pool)
                    else:
                        top_title = "N/A"
                        top_conf = 0
                        top_sim = 0.0
                        top_3 = result.get("error", "no results")
                        avg_conf = 0

                    writer.writerow([
                        datetime.now().isoformat(), query, mode, era_label,
                        success, len(pool), top_title, top_conf,
                        f"{top_sim:.4f}", top_3, f"{avg_conf:.1f}",
                        f"{elapsed:.1f}",
                    ])
                    csv_file.flush()

                    status = f"  -> {len(pool)} results, top: {top_title} ({top_conf}%)"
                    print(f"  -> {status} [{elapsed:.1f}s]")

                except Exception as e:
                    elapsed = time.time() - start
                    print(f"  -> ERROR: {e} [{elapsed:.1f}s]")
                    writer.writerow([
                        datetime.now().isoformat(), query, mode, era_label,
                        False, 0, "ERROR", 0, "0.0", str(e), "0.0",
                        f"{elapsed:.1f}",
                    ])
                    csv_file.flush()

    csv_file.close()
    print(f"\nBattery complete. {completed} searches logged.")
    print("Journal: data/search_journal.jsonl")
    print(f"Summary: {csv_path}")


if __name__ == "__main__":
    run_battery()
