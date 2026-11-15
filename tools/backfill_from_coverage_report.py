"""Backfill vault gaps identified by a coverage audit report.

Phase E of the Vault Coverage Investigation. Reads a JSON report
produced by tools/audit_vault_coverage.py, filters MISSING entries
by popularity threshold + vault-floor year, and feeds the resulting
MAL IDs to vault_manager's targeted-strike-by-id mode.

Backfill is a live production action:
- Hits MAL API for metadata per show
- Hits Jikan for reviews per show (rate-limited 3s/call)
- Hits Gemini for consensus distillation per successful show (~$0.03/each)
- Writes to anime_info (upsert) + intelligence_quarantine (on failure)
- Re-syncs FAISS index at the end

Default behavior is DRY RUN — prints what would be ingested but takes
no action. Pass --execute (or answer 'yes' to the interactive prompt)
to trigger the real ingest.

Usage:
    # Preview (dry-run) — popularity MISSING with scored_by >= 100000, post-1990
    python tools/backfill_from_coverage_report.py

    # Execute the backfill
    python tools/backfill_from_coverage_report.py --execute

    # Lower the popularity threshold — 50K+ instead of 100K+
    python tools/backfill_from_coverage_report.py --min-scored-by 50000 --execute

    # Include shows without start_year data (default: skip if missing)
    python tools/backfill_from_coverage_report.py --allow-unknown-year --execute

    # Point at a specific report (default: newest matching)
    python tools/backfill_from_coverage_report.py \\
        --report data/vault_coverage_report_2026-09-26.json --execute

    # Season-mode gaps instead of popularity
    python tools/backfill_from_coverage_report.py --source season --execute
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import logging
import os
import sys
from typing import Optional

# --- PATH ANCHOR ---
SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

logger = logging.getLogger(__name__)

DEFAULT_MIN_SCORED_BY = 100_000
DEFAULT_MIN_YEAR = 1990  # vault floor per project design


def find_latest_report(reports_dir: str) -> Optional[str]:
    """Return the path of the most-recent coverage report JSON, or None."""
    pattern = os.path.join(reports_dir, "vault_coverage_report_*.json")
    matches = sorted(glob.glob(pattern))
    return matches[-1] if matches else None


def load_report(report_path: str) -> dict:
    """Load a coverage report from disk."""
    with open(report_path, encoding="utf-8") as f:
        return json.load(f)


def extract_missing_from_source(report: dict, source: str) -> list[dict]:
    """Return the MISSING gap entries from either the popularity_audit
    block or the season_audit block (all seasons combined).

    source: 'popularity' or 'season'.
    """
    if source == "popularity":
        pop = report.get("popularity_audit")
        if not pop:
            return []
        return pop.get("gaps", {}).get("MISSING", [])
    if source == "season":
        season = report.get("season_audit")
        if not season:
            return []
        # Season audit's MISSING lives per-season; flatten across all seasons,
        # deduplicating by mal_id (long-running shows appear in multiple).
        seen: set[int] = set()
        combined: list[dict] = []
        for sr in season.get("seasons", []):
            for gap in sr.get("gaps", {}).get("MISSING", []):
                if gap["mal_id"] not in seen:
                    seen.add(gap["mal_id"])
                    combined.append(gap)
        return combined
    raise ValueError(f"Unknown source: {source!r} (expected 'popularity' or 'season')")


def filter_candidates(
    entries: list[dict],
    min_scored_by: int,
    min_year: int,
    allow_unknown_year: bool,
) -> tuple[list[dict], list[dict]]:
    """Split entries into (kept, excluded) by the popularity and year filters.

    Kept entries meet both thresholds. Excluded entries are held for
    visibility — we don't drop them silently, we report them so the
    operator knows what's out of scope.
    """
    kept: list[dict] = []
    excluded: list[dict] = []
    for entry in entries:
        scored_by = entry.get("scored_by") or 0
        year = entry.get("mal_start_year")
        if scored_by < min_scored_by:
            excluded.append({**entry, "_exclusion": f"scored_by {scored_by} < {min_scored_by}"})
            continue
        if year is None:
            if allow_unknown_year:
                kept.append(entry)
            else:
                excluded.append({**entry, "_exclusion": "mal_start_year unknown"})
            continue
        if year < min_year:
            excluded.append({**entry, "_exclusion": f"start_year {year} < vault floor {min_year}"})
            continue
        kept.append(entry)
    return kept, excluded


def print_preview(kept: list[dict], excluded: list[dict], min_scored_by: int, min_year: int) -> None:
    """Human-readable preview of what will be ingested + what's out of scope."""
    print()
    print("=" * 70)
    print("Backfill preview")
    print("=" * 70)
    print(f"Filters: scored_by >= {min_scored_by:,}, mal_start_year >= {min_year}")
    print(f"Candidates to ingest: {len(kept)}")
    print(f"Excluded (out of scope): {len(excluded)}")
    print()

    if kept:
        print(f"Top 25 candidates by popularity:")
        for entry in kept[:25]:
            year = entry.get("mal_start_year") or "?"
            title = (entry.get("title") or "")[:45]
            print(f"  MAL {entry['mal_id']:>7} | {title:<45} | year={year} | scored_by={entry['scored_by']:>7}")
        if len(kept) > 25:
            print(f"  ... and {len(kept) - 25} more")
        print()

    excluded_pre_floor = [e for e in excluded if "vault floor" in e.get("_exclusion", "")]
    if excluded_pre_floor:
        print(f"Pre-{min_year} shows excluded ({len(excluded_pre_floor)}) — usually intentional per vault design:")
        for entry in excluded_pre_floor[:5]:
            year = entry.get("mal_start_year")
            title = (entry.get("title") or "")[:45]
            print(f"  MAL {entry['mal_id']:>7} | {title:<45} | year={year} | scored_by={entry['scored_by']:>7}")
        if len(excluded_pre_floor) > 5:
            print(f"  ... and {len(excluded_pre_floor) - 5} more pre-{min_year}")
        print()


def parse_args():
    p = argparse.ArgumentParser(
        description="Backfill vault gaps from a coverage audit report."
    )
    p.add_argument("--report", type=str, default=None,
                   help="Path to report JSON (default: newest in data/).")
    p.add_argument("--source", choices=["popularity", "season"], default="popularity",
                   help="Which audit's MISSING to backfill (default: popularity).")
    p.add_argument("--min-scored-by", type=int, default=DEFAULT_MIN_SCORED_BY,
                   help=f"Popularity threshold (default: {DEFAULT_MIN_SCORED_BY:,}).")
    p.add_argument("--min-year", type=int, default=DEFAULT_MIN_YEAR,
                   help=f"Vault floor year (default: {DEFAULT_MIN_YEAR}).")
    p.add_argument("--allow-unknown-year", action="store_true",
                   help="Include shows lacking mal_start_year (default: skip).")
    p.add_argument("--execute", action="store_true",
                   help="Actually run the backfill (default: dry-run preview only).")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap the number of shows ingested (default: no cap).")
    return p.parse_args()


def main():
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    args = parse_args()

    reports_dir = os.path.join(ROOT_DIR, "data")
    report_path = args.report or find_latest_report(reports_dir)
    if not report_path or not os.path.exists(report_path):
        print(f"ERROR: no coverage report found. Run tools/audit_vault_coverage.py first.")
        sys.exit(1)

    print(f"Loading report: {report_path}")
    report = load_report(report_path)
    print(f"Report ID: {report.get('report_id')}")
    print(f"Modes:     {', '.join(report.get('modes_run', []))}")

    entries = extract_missing_from_source(report, args.source)
    print(f"Source '{args.source}' MISSING entries: {len(entries)}")

    kept, excluded = filter_candidates(
        entries,
        min_scored_by=args.min_scored_by,
        min_year=args.min_year,
        allow_unknown_year=args.allow_unknown_year,
    )

    if args.limit is not None:
        kept = kept[: args.limit]
        print(f"Limit applied: capped to top {args.limit} by popularity.")

    print_preview(kept, excluded, args.min_scored_by, args.min_year)

    if not kept:
        print("No candidates to ingest. Adjust --min-scored-by / --min-year or check the report.")
        sys.exit(0)

    if not args.execute:
        print("DRY RUN — pass --execute to trigger the backfill.")
        sys.exit(0)

    # Live ingest — hand the MAL IDs to vault_manager's targeted-strike mode.
    mal_ids = [entry["mal_id"] for entry in kept]
    print(f"\nEXECUTING backfill for {len(mal_ids)} shows...")
    print(f"Estimated time: ~{len(mal_ids) * 8}s (~{len(mal_ids) * 8 // 60} min) at 8s/show")
    print("=" * 70)

    from vault_manager import update_specific_targets_by_id  # noqa: E402
    asyncio.run(update_specific_targets_by_id(mal_ids))

    print("=" * 70)
    print(f"Backfill complete. Re-run tools/audit_vault_coverage.py to verify")
    print(f"the MISSING count decreased.")


if __name__ == "__main__":
    main()
