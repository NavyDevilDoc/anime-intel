"""
tools/analyze_timings.py

Reads data/search_journal.jsonl and reports per-stage latency distributions
(P50/P95/P99, min, max) grouped by mode and rerank_type.

Stages are populated by `recommendation_engine.fetch_vault_pool` and
`execute_dna_triangulation` — see the `timings` dict written to each journal
entry. Entries without timings (older than the instrumentation change) are
counted separately and skipped from the percentile math.

Usage:
    python tools/analyze_timings.py
    python tools/analyze_timings.py --mode find_a_show
    python tools/analyze_timings.py --rerank-type fusion
    python tools/analyze_timings.py --since 2026-05-15
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
JOURNAL_PATH = ROOT_DIR / "data" / "search_journal.jsonl"


def percentile(values, p):
    """Linear-interpolation percentile (0-100) over a list of numeric values."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def parse_args():
    ap = argparse.ArgumentParser(description="Search journal timing analysis")
    ap.add_argument("--mode", help="Filter by mode (find_a_show, shows_like_this)")
    ap.add_argument("--rerank-type", help="Filter by rerank type (fusion, narrative, constrained, objective, dna)")
    ap.add_argument("--since", help="Only entries on or after this ISO date (YYYY-MM-DD)")
    ap.add_argument("--journal", default=str(JOURNAL_PATH), help="Path to journal JSONL file")
    return ap.parse_args()


def main():
    args = parse_args()
    journal_path = Path(args.journal)

    if not journal_path.exists():
        print(f"Journal not found: {journal_path}")
        sys.exit(1)

    since_dt = None
    if args.since:
        try:
            since_dt = datetime.fromisoformat(args.since).date()
        except ValueError:
            print(f"--since must be ISO date (YYYY-MM-DD), got: {args.since}")
            sys.exit(1)

    # group_key -> stage_name -> [values]
    grouped = defaultdict(lambda: defaultdict(list))
    total_entries = 0
    with_timings = 0
    skipped_filter = 0

    with open(journal_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            total_entries += 1

            mode = entry.get("mode", "unknown")
            timings = entry.get("timings")
            if not timings:
                continue

            if args.mode and mode != args.mode:
                skipped_filter += 1
                continue

            rerank_type = timings.get("rerank_type", "n/a")
            if args.rerank_type and rerank_type != args.rerank_type:
                skipped_filter += 1
                continue

            if since_dt:
                ts = entry.get("timestamp")
                if ts:
                    try:
                        entry_date = datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
                        if entry_date < since_dt:
                            skipped_filter += 1
                            continue
                    except ValueError:
                        pass

            with_timings += 1
            group_key = f"{mode} / {rerank_type}"
            for stage, value in timings.items():
                if isinstance(value, (int, float)):
                    grouped[group_key][stage].append(value)

    print(f"Journal: {journal_path}")
    print(f"Entries scanned: {total_entries}  (with timings: {with_timings}, skipped by filter: {skipped_filter})")
    if args.mode or args.rerank_type or args.since:
        filters = []
        if args.mode:
            filters.append(f"mode={args.mode}")
        if args.rerank_type:
            filters.append(f"rerank_type={args.rerank_type}")
        if args.since:
            filters.append(f"since={args.since}")
        print(f"Filters: {', '.join(filters)}")
    print()

    if not grouped:
        print("No entries with timing data matched. Run a few searches and try again.")
        return

    for group_key in sorted(grouped.keys()):
        stages = grouped[group_key]
        print(f"=== {group_key} ===")
        rows = []
        for stage, values in stages.items():
            if not values:
                continue
            rows.append({
                "stage": stage,
                "n": len(values),
                "p50": percentile(values, 50),
                "p95": percentile(values, 95),
                "p99": percentile(values, 99),
                "min": min(values),
                "max": max(values),
            })
        # Sort: total_ms last; everything else by P50 desc to surface slow stages first
        rows.sort(key=lambda r: (r["stage"] == "total_ms", -r["p50"]))

        print(f"{'Stage':<26} {'n':>4} {'P50 ms':>9} {'P95 ms':>9} {'P99 ms':>9} {'Min ms':>9} {'Max ms':>9}")
        print("-" * 86)
        for r in rows:
            print(f"{r['stage']:<26} {r['n']:>4} {r['p50']:>9.1f} {r['p95']:>9.1f} {r['p99']:>9.1f} {r['min']:>9.1f} {r['max']:>9.1f}")
        print()


if __name__ == "__main__":
    main()
