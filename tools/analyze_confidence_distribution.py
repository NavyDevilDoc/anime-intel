"""
tools/analyze_confidence_distribution.py

Reads data/search_journal.jsonl and prints the per-mode confidence
distribution that informs the "I Don't Know" mode threshold. The script
is the reproducible baseline behind the locked thresholds in
IMPLEMENTATION_PLAN.md and the recurring drift check documented in
CLAUDE.md.

Usage:
    python tools/analyze_confidence_distribution.py
    python tools/analyze_confidence_distribution.py --json

The default output is a human-readable summary suitable for periodic
review; --json emits a single object with the same numbers for
programmatic consumption (e.g., a future CI guard or scheduled report).

Run cadence:
    - Quarterly (calendar reminder)
    - Before any reranker prompt change
    - Before any embedding-model change
    - Whenever a user reports that "recommendations feel different"

What to watch for:
    The baseline distribution (frozen 2026-05-27, 1,206 entries) is
    heavily concentrated at 95-100 for both modes. The "I Don't Know"
    banner fires when the top result is below 75 OR the pool is empty
    — combined ~3% of all searches today. A drift would show up as:
      - Top-result mean confidence dropping by >2 points
      - Banner-band population (< 75) growing past ~5% of any mode
      - Empty-result rate growing past ~12% for Shows Like This
    Any of those would warrant looking at recent prompt/model changes.
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

# Source-agnostic journal reader added in Initiative 2 Phase D. Importing
# at module level so the script fails loudly if the reader module is
# missing or broken — same import-time fail-fast as supabase_client.
SCRIPT_DIR_FOR_IMPORT = Path(__file__).resolve().parent
ROOT_FOR_IMPORT = SCRIPT_DIR_FOR_IMPORT.parent if SCRIPT_DIR_FOR_IMPORT.name == "tools" else SCRIPT_DIR_FOR_IMPORT
if str(ROOT_FOR_IMPORT) not in sys.path:
    sys.path.insert(0, str(ROOT_FOR_IMPORT))
from analysis.journal_reader import read_entries  # noqa: E402

# Force UTF-8 stdout — titles in spot-checks include CJK characters that
# cp1252 (Windows console default for Python ≤ 3.14) cannot encode.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

# --- BULLETPROOF PATH ANCHORING ---
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent if SCRIPT_DIR.name == "tools" else SCRIPT_DIR
JOURNAL_PATH = ROOT_DIR / "data" / "search_journal.jsonl"

# The "I Don't Know" banner threshold used by both FaS and SLT. Kept in
# sync with recommendation_engine.py — the production drop floors sit
# below this (FaS 65, DNA 40) so pool contents drop before banner logic
# sees them.
LOW_CONFIDENCE_THRESHOLD = 75

FAS_BANDS = [
    (0, 49, "<50"),
    (50, 54, "50-54"),
    (55, 64, "55-64"),
    (65, 69, "65-69"),
    (70, 74, "70-74"),
    (75, 79, "75-79"),
    (80, 84, "80-84"),
    (85, 89, "85-89"),
    (90, 94, "90-94"),
    (95, 100, "95-100"),
]

DNA_BANDS = [
    (0, 39, "<40"),
    (40, 49, "40-49"),
    (50, 59, "50-59"),
    (60, 69, "60-69"),
    (70, 79, "70-79"),
    (80, 89, "80-89"),
    (90, 100, "90-100"),
]


def _load_journal(source: str | None, file_path: Path) -> list[dict]:
    """
    Read the journal via the source-agnostic reader. Source defaults to
    JOURNAL_SOURCE env var if `source` is None, else 'file'.

    Returns dicts in JSONL shape regardless of underlying source — Supabase
    rows get translated so downstream `_summarize` doesn't know or care.
    """
    rows = read_entries(source=source, file_path=file_path)
    if not rows:
        location = source or os.environ.get("JOURNAL_SOURCE") or "file"
        if location == "file":
            location = f"file ({file_path})"
        print(f"ERROR: no journal entries returned from {location}", file=sys.stderr)
        sys.exit(1)
    return rows


def _bucket(confidences: list[int], bands: list[tuple[int, int, str]]) -> list[dict]:
    total = len(confidences)
    out = []
    for lo, hi, label in bands:
        n = sum(1 for c in confidences if lo <= c <= hi)
        pct = (n / total * 100) if total else 0.0
        out.append({"band": label, "lo": lo, "hi": hi, "count": n, "pct": round(pct, 2)})
    return out


def _summarize(rows: list[dict]) -> dict:
    """Compute the full picture in one pass. Pure function — no IO."""
    fas = [r for r in rows if r.get("mode") == "find_a_show"]
    slt = [r for r in rows if r.get("mode") == "shows_like_this"]

    fas_with_results = [r for r in fas if r.get("results")]
    slt_with_results = [r for r in slt if r.get("results")]

    fas_empty = sum(1 for r in fas if not r.get("results"))
    slt_empty = sum(1 for r in slt if not r.get("results"))

    fas_top = [r["results"][0]["confidence"] for r in fas_with_results]
    slt_top = [r["results"][0]["confidence"] for r in slt_with_results]

    # Per-intent breakdown for Find a Show — rerank_type carries the
    # routing decision (fusion / constrained / narrative / objective)
    # and intent carries the upstream intent classifier output (vibe,
    # genre_combo, hidden_gems, etc.). The intent classifier output is
    # more informative for tuning, so prefer it when present.
    fas_per_intent: dict[str, list[int]] = defaultdict(list)
    for r in fas_with_results:
        intent = r.get("intent") or (r.get("timings") or {}).get("rerank_type") or "unknown"
        fas_per_intent[intent].append(r["results"][0]["confidence"])

    # Banner fire counts — top result below 75 OR empty pool
    fas_banner_inband = sum(1 for c in fas_top if c < LOW_CONFIDENCE_THRESHOLD)
    slt_banner_inband = sum(1 for c in slt_top if c < LOW_CONFIDENCE_THRESHOLD)
    fas_banner_total = fas_banner_inband + fas_empty
    slt_banner_total = slt_banner_inband + slt_empty

    return {
        "total_entries": len(rows),
        "low_confidence_threshold": LOW_CONFIDENCE_THRESHOLD,
        "modes": {
            "find_a_show": {
                "total_searches": len(fas),
                "with_results": len(fas_with_results),
                "empty_results": fas_empty,
                "empty_pct": round(fas_empty / len(fas) * 100, 2) if fas else 0,
                "top_distribution": _bucket(fas_top, FAS_BANDS),
                "banner_inband_count": fas_banner_inband,
                "banner_inband_pct": round(fas_banner_inband / len(fas) * 100, 2) if fas else 0,
                "banner_total_fires": fas_banner_total,
                "banner_total_pct": round(fas_banner_total / len(fas) * 100, 2) if fas else 0,
                "top_mean": round(sum(fas_top) / len(fas_top), 2) if fas_top else 0,
                "per_intent": {
                    intent: {
                        "count": len(confs),
                        "mean": round(sum(confs) / len(confs), 2),
                        "p50": sorted(confs)[len(confs) // 2],
                        "below_threshold": sum(1 for c in confs if c < LOW_CONFIDENCE_THRESHOLD),
                        "below_threshold_pct": round(
                            sum(1 for c in confs if c < LOW_CONFIDENCE_THRESHOLD) / len(confs) * 100, 2
                        ),
                    }
                    for intent, confs in sorted(fas_per_intent.items())
                    if confs
                },
            },
            "shows_like_this": {
                "total_searches": len(slt),
                "with_results": len(slt_with_results),
                "empty_results": slt_empty,
                "empty_pct": round(slt_empty / len(slt) * 100, 2) if slt else 0,
                "top_distribution": _bucket(slt_top, DNA_BANDS),
                "banner_inband_count": slt_banner_inband,
                "banner_inband_pct": round(slt_banner_inband / len(slt) * 100, 2) if slt else 0,
                "banner_total_fires": slt_banner_total,
                "banner_total_pct": round(slt_banner_total / len(slt) * 100, 2) if slt else 0,
                "top_mean": round(sum(slt_top) / len(slt_top), 2) if slt_top else 0,
            },
        },
    }


def _print_summary(summary: dict) -> None:
    threshold = summary["low_confidence_threshold"]
    source = summary.get("source", "file")
    location = summary.get("journal_path") if source == "file" else "supabase://search_journal"
    print(f"=== Journal: {location} (source={source}) ===")
    print(f"Total entries: {summary['total_entries']:,}")
    print(f"Banner threshold: top result < {threshold} OR empty pool")
    print()

    for mode_key, label in (("find_a_show", "Find a Show"), ("shows_like_this", "Shows Like This")):
        m = summary["modes"][mode_key]
        print(f"--- {label} ---")
        print(f"  Searches:           {m['total_searches']:,}")
        print(f"  With results:       {m['with_results']:,}")
        print(f"  Empty results:      {m['empty_results']:,}  ({m['empty_pct']}%)")
        print(f"  Top-result mean:    {m['top_mean']}")
        print(
            f"  Banner fires:       in-band={m['banner_inband_count']} ({m['banner_inband_pct']}%)  "
            f"empty={m['empty_results']} ({m['empty_pct']}%)  "
            f"total={m['banner_total_fires']} ({m['banner_total_pct']}%)"
        )
        print("  Top-result distribution:")
        for row in m["top_distribution"]:
            bar = "#" * int(row["pct"] / 2)
            marker = "  <-- banner" if row["hi"] < threshold else ""
            print(f"    {row['band']:>7s}: {row['count']:>5d}  {row['pct']:5.1f}%  {bar}{marker}")
        if "per_intent" in m:
            print("  Per-intent (top-result confidence):")
            for intent, stats in m["per_intent"].items():
                below = f"<{threshold}: {stats['below_threshold']} ({stats['below_threshold_pct']}%)"
                print(
                    f"    {intent:>14s}: n={stats['count']:>4d}  "
                    f"mean={stats['mean']:5.1f}  p50={stats['p50']:>3d}  {below}"
                )
        print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Confidence-distribution analysis for the search journal.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a single JSON object to stdout instead of human-readable text.",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=JOURNAL_PATH,
        help=f"Path to search_journal.jsonl when --source=file "
             f"(default: {JOURNAL_PATH}).",
    )
    parser.add_argument(
        "--source",
        choices=["file", "supabase"],
        default=None,
        help="Journal source. Defaults to JOURNAL_SOURCE env var, "
             "then 'file'. Use 'supabase' for cross-deploy aggregates "
             "from the persistent table.",
    )
    args = parser.parse_args()

    rows = _load_journal(source=args.source, file_path=args.path)
    summary = _summarize(rows)
    # Stamp the source on the output so a downstream diff can tell which
    # corpus the numbers came from.
    summary["source"] = args.source or os.environ.get("JOURNAL_SOURCE") or "file"
    if summary["source"] == "file":
        summary["journal_path"] = str(args.path)

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        _print_summary(summary)


if __name__ == "__main__":
    main()
