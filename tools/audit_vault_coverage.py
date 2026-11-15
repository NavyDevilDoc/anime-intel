"""Vault coverage audit — compares MAL's per-season listings against
what's actually in anime_info to surface silently-missing shows.

Phase D of the Vault Coverage Investigation. Answers: which shows
that MAL says exist for a given season are absent from our vault?

The tool is DIAGNOSTIC-ONLY. It does not modify the vault, trigger
ingestion, or clear quarantine rows. Output is a JSON report; gaps
in the report get handed to vault_manager.py's targeted-strike-by-id
mode for backfill.

Ground truth: MAL v2 API's /anime/season/{year}/{season}, same
endpoint the ingestor uses. Any discrepancy between MAL's list and
the vault is therefore a pipeline gap, not a source-of-truth issue.

Cadence: on-demand. Not wired into any CI or workflow — invoked
when a coverage question comes up (post-fall-sweep audit, quarterly
sanity check, or user report of a missing show).

Categories per MAL-returned show:
    VAULTED         — in anime_info with consensus, correct season
    MISCLASSIFIED   — in anime_info, but under a different season
    QUARANTINED     — in intelligence_quarantine (attempted, failed)
    MISSING         — in neither table (the Air Gear class)
    SKIPPED_NON_TV  — media_type outside --media-types whitelist
                      (name kept for backward compat with existing
                      report consumers; broader than "non-TV" when
                      the whitelist is widened for Option-B audits)

Usage:
    # Single-year smoke test (fast, ~1-2 min)
    python tools/audit_vault_coverage.py --year 2006

    # Range
    python tools/audit_vault_coverage.py --start-year 2020 --end-year 2026

    # Full audit 1990-2026 (~10-15 min, produces first baseline report)
    python tools/audit_vault_coverage.py

    # Print summary only, no JSON output
    python tools/audit_vault_coverage.py --year 2006 --summary-only

    # Option-B: popularity audit for Movies + OVAs (non-TV gap closure)
    python tools/audit_vault_coverage.py --mode popularity \\
        --media-types movie ova --top-n 3000 --summary-only
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Optional

# --- PATH ANCHOR ---
SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT_DIR, "env_variables.env"))

from src.mal_api_client import MALClient

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(ROOT_DIR, "data", "anime_intelligence_v2.db")
DEFAULT_START_YEAR = 1979   # vault floor expanded from 1990 → 1979 in the pre-1990 initiative to capture original Mobile Suit Gundam and the 1980s canon
DEFAULT_END_YEAR = datetime.now().year
SEASONS = ("winter", "spring", "summer", "fall")

# Default media-type whitelist for the audit. Matches the seasonal
# ingestor's TV-only policy (seasonal_ingestor_v2.ingest_season L270-276)
# — if nothing else is passed, the audit behaves exactly as it did
# pre-Option-B. Expand via --media-types for coverage audits of Movie /
# OVA / etc. subtypes; the targeted-strike path at
# vault_manager.update_specific_targets_by_id is subtype-agnostic, so a
# wider audit directly drives a wider backfill.
DEFAULT_ALLOWED_MEDIA_TYPES = frozenset({"tv"})


# --- CATEGORIES ---
CAT_VAULTED = "VAULTED"
CAT_MISCLASSIFIED = "MISCLASSIFIED"
CAT_QUARANTINED = "QUARANTINED"
CAT_MISSING = "MISSING"
CAT_SKIPPED_NON_TV = "SKIPPED_NON_TV"

ALL_CATEGORIES = (
    CAT_VAULTED, CAT_MISCLASSIFIED, CAT_QUARANTINED, CAT_MISSING, CAT_SKIPPED_NON_TV,
)


def _iso_date_now() -> str:
    """YYYY-MM-DD in UTC. Report filenames use this for natural ordering."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def snapshot_vault_state(db_path: str) -> tuple[dict[int, str], dict[int, dict]]:
    """Read the vault's current state once, upfront.

    Returns:
        vaulted: {mal_id: season_tag} for rows in anime_info with
                 non-null consensus_json. Season tag preserves the
                 "which season is this show classified under?"
                 answer that MISCLASSIFIED detection needs.
        quarantined: {mal_id: {"error_message": ..., "last_attempt": ...}}
                     for all rows in intelligence_quarantine.

    Single-shot snapshot — the audit is read-only, so we don't need
    to worry about concurrent writes changing state mid-run.
    """
    with sqlite3.connect(db_path, timeout=20) as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, season FROM anime_info "
            "WHERE consensus_json IS NOT NULL"
        )
        vaulted = {row[0]: (row[1] or "") for row in c.fetchall()}

        c.execute(
            "SELECT mal_id, error_message, last_attempt "
            "FROM intelligence_quarantine"
        )
        quarantined = {
            row[0]: {"error_message": row[1], "last_attempt": row[2]}
            for row in c.fetchall()
        }
    return vaulted, quarantined


def categorize_show(
    mal_id: int,
    mal_start_season_tag: str,
    media_type: str,
    vaulted: dict[int, str],
    quarantined: dict[int, dict],
    allowed_media_types: frozenset[str] = DEFAULT_ALLOWED_MEDIA_TYPES,
) -> str:
    """Pure classification function — no I/O.

    `mal_start_season_tag` is MAL's authoritative start_season for
    the show (from the API's start_season field), NOT the season
    currently being audited. This matters for long-running shows:
    Naruto (start_season=Winter_2007) will appear in MAL's Spring_2007,
    Summer_2007, Fall_2007, etc. season listings as it aired across
    them. Comparing vault_season against the QUERIED season would
    flag Naruto as MISCLASSIFIED for every season after its start.
    Comparing against MAL's start_season correctly recognizes it as
    VAULTED regardless of which season we're currently probing.

    `allowed_media_types` is the whitelist — media_types outside it are
    categorized as SKIPPED_NON_TV (name kept for backward compat with
    existing report consumers; the "NON_TV" is a misnomer when the
    whitelist is widened for Option-B style movie / ova audits).

    Order of checks matters:
    1. media_type outside the whitelist is skipped BEFORE anything else —
       the audit mirrors what the backfill target will accept.
    2. Quarantined shows come next — the ingestor tried and failed;
       distinct from "never attempted."
    3. Vaulted + vault_season matches MAL's authoritative start_season
       = VAULTED (regardless of the queried season).
    4. Vaulted + vault_season disagrees with MAL's start_season =
       MISCLASSIFIED (real disagreement about the show's canonical
       season, usually MAL reclassified after original ingest).
    5. Everything else = MISSING (the Air Gear class).
    """
    if media_type not in allowed_media_types:
        return CAT_SKIPPED_NON_TV
    if mal_id in quarantined:
        return CAT_QUARANTINED
    if mal_id in vaulted:
        if vaulted[mal_id] == mal_start_season_tag:
            return CAT_VAULTED
        return CAT_MISCLASSIFIED
    return CAT_MISSING


def _extract_show_fields(node: dict) -> dict:
    """Trim MAL's node payload to the compact fields the audit uses.

    Shared between fetch_mal_season and fetch_mal_popularity — same
    fields, same shape, so downstream categorization + gap-building
    doesn't care where the show came from.
    """
    start = node.get("start_season", {})
    return {
        "mal_id": node.get("id"),
        "title": (
            node.get("alternative_titles", {}).get("en")
            or node.get("title")
            or f"MAL#{node.get('id')}"
        ),
        "media_type": node.get("media_type", "unknown"),
        "scored_by": node.get("num_scoring_users") or 0,
        "mean": node.get("mean"),
        "mal_start_season": start.get("season"),
        "mal_start_year": start.get("year"),
    }


def fetch_mal_season(
    year: int, season: str, mal_client: MALClient, sleep_between_pages: float = 0.5
) -> list[dict]:
    """Paginated fetch of MAL's season listing.

    Returns list of dicts with the fields the audit cares about —
    NOT the full MAL node. Trims to (mal_id, title, media_type,
    scored_by, mean, start_season) so the audit doesn't hold onto
    ~10 KB per show in memory across the 37-year sweep.

    Uses get_with_auto_refresh so the audit is self-healing to a
    stale MAL OAuth token (same PR #143 helper the ingestor uses).
    """
    fields = "media_type,mean,num_scoring_users,alternative_titles,start_season"
    url = f"https://api.myanimelist.net/v2/anime/season/{year}/{season}"
    params = {"limit": 100, "fields": fields}
    shows: list[dict] = []

    while url:
        response = mal_client.get_with_auto_refresh(url, params=params)
        response.raise_for_status()
        payload = response.json()

        for entry in payload.get("data", []):
            shows.append(_extract_show_fields(entry.get("node", {})))

        paging = payload.get("paging", {})
        url = paging.get("next")
        params = None  # subsequent pages carry params in the paging.next URL
        if url:
            time.sleep(sleep_between_pages)

    return shows


def fetch_mal_popularity(
    mal_client: MALClient, top_n: int = 5000, sleep_between_pages: float = 0.5,
) -> list[dict]:
    """Fetch top-N shows from MAL's popularity ranking.

    Uses /anime/ranking?ranking_type=bypopularity — catches shows
    MAL knows are popular regardless of whether they appear in the
    /seasons endpoint. Air Gear (MAL 857, 187K ratings, TV, start
    season Spring 2006) IS missing from /anime/season/2006/spring
    but present here — exactly the coverage-gap class this mode
    exists to catch.

    MAL caps ranking pages at limit=500 (larger than /seasons's 100).
    top_n=5000 = 10 pages, ~5-15 seconds of API time.
    """
    fields = "media_type,mean,num_scoring_users,alternative_titles,start_season"
    url = "https://api.myanimelist.net/v2/anime/ranking"
    params = {
        "ranking_type": "bypopularity",
        "limit": 500,
        "fields": fields,
    }
    shows: list[dict] = []

    while url and len(shows) < top_n:
        response = mal_client.get_with_auto_refresh(url, params=params)
        response.raise_for_status()
        payload = response.json()

        for entry in payload.get("data", []):
            if len(shows) >= top_n:
                break
            shows.append(_extract_show_fields(entry.get("node", {})))

        paging = payload.get("paging", {})
        url = paging.get("next")
        params = None
        if url and len(shows) < top_n:
            time.sleep(sleep_between_pages)

    return shows[:top_n]


def mal_start_season_tag(show: dict, fallback: str = "") -> str:
    """Build 'Season_Year' from MAL's start_season field. Falls back
    to the provided default if the show lacks start_season data
    (rare — most anime have this populated)."""
    season = show.get("mal_start_season")
    year = show.get("mal_start_year")
    if season and year:
        return f"{season.capitalize()}_{year}"
    return fallback


def build_gap_entry(
    show: dict, category: str, quarantine_meta: Optional[dict], vault_season: Optional[str]
) -> dict:
    """Compact record for a single gap show — sufficient for triage
    (title + popularity) plus category-specific context.

    mal_start_season / mal_start_year are included for every gap so
    downstream backfill tooling can filter by year (e.g., respect
    the vault's 1990 floor) without re-fetching MAL per candidate.
    """
    entry = {
        "mal_id": show["mal_id"],
        "title": show["title"],
        "media_type": show["media_type"],
        "scored_by": show["scored_by"],
        "mean": show["mean"],
        "mal_start_season": show.get("mal_start_season"),
        "mal_start_year": show.get("mal_start_year"),
    }
    if category == CAT_QUARANTINED and quarantine_meta:
        entry["quarantine_error"] = quarantine_meta.get("error_message")
        entry["last_attempt"] = quarantine_meta.get("last_attempt")
    if category == CAT_MISCLASSIFIED and vault_season:
        entry["vault_season"] = vault_season
        entry["mal_season"] = mal_start_season_tag(show, fallback="?")
    return entry


def audit_season(
    year: int,
    season: str,
    mal_client: MALClient,
    vaulted: dict[int, str],
    quarantined: dict[int, dict],
    sleep_between_pages: float = 0.5,
    allowed_media_types: frozenset[str] = DEFAULT_ALLOWED_MEDIA_TYPES,
) -> dict:
    """Audit a single (year, season) pair. Returns season report dict."""
    season_tag = f"{season.capitalize()}_{year}"
    try:
        source_shows = fetch_mal_season(year, season, mal_client, sleep_between_pages)
    except Exception as e:  # noqa: BLE001 — a single season's fetch failure shouldn't abort the whole audit
        logger.warning(f"MAL fetch failed for {season_tag}: {type(e).__name__}: {e}")
        return {
            "year": year,
            "season": season,
            "season_tag": season_tag,
            "source_count": 0,
            "fetch_error": f"{type(e).__name__}: {e}",
            "categories": {},
            "gaps": {},
        }

    category_counts: Counter = Counter()
    gaps_by_category: dict[str, list[dict]] = {
        CAT_MISSING: [],
        CAT_QUARANTINED: [],
        CAT_MISCLASSIFIED: [],
    }

    for show in source_shows:
        mal_id = show["mal_id"]
        # Use MAL's authoritative start_season for classification, not the
        # queried season — otherwise long-running shows appearing in
        # multiple seasonal listings get flagged as MISCLASSIFIED against
        # every non-start season they aired in.
        expected_tag = mal_start_season_tag(show, fallback=season_tag)
        cat = categorize_show(
            mal_id, expected_tag, show["media_type"], vaulted, quarantined,
            allowed_media_types=allowed_media_types,
        )
        category_counts[cat] += 1
        if cat in gaps_by_category:
            entry = build_gap_entry(
                show, cat,
                quarantine_meta=quarantined.get(mal_id),
                vault_season=vaulted.get(mal_id),
            )
            gaps_by_category[cat].append(entry)

    # Sort each gap list by popularity descending — operators triage by importance
    for cat, entries in gaps_by_category.items():
        entries.sort(key=lambda x: x["scored_by"] or 0, reverse=True)

    return {
        "year": year,
        "season": season,
        "season_tag": season_tag,
        "source_count": len(source_shows),
        "categories": dict(category_counts),
        "gaps": gaps_by_category,
    }


def audit_popularity(
    mal_client: MALClient,
    vaulted: dict[int, str],
    quarantined: dict[int, dict],
    top_n: int = 5000,
    sleep_between_pages: float = 0.5,
    allowed_media_types: frozenset[str] = DEFAULT_ALLOWED_MEDIA_TYPES,
) -> dict:
    """Audit MAL's top-N most-popular shows against the vault.

    Complementary to the season-based audit — catches shows MAL
    knows are popular but doesn't return via /anime/season/ (the
    Air Gear class). No season concept applies here; a show is
    either VAULTED, QUARANTINED, SKIPPED_NON_TV per policy, or
    MISSING. MISCLASSIFIED doesn't apply cross-season.
    """
    try:
        source_shows = fetch_mal_popularity(mal_client, top_n, sleep_between_pages)
    except Exception as e:  # noqa: BLE001 — a popularity fetch failure preserves any season-audit output that already ran
        logger.warning(f"MAL popularity fetch failed: {type(e).__name__}: {e}")
        return {
            "top_n_target": top_n,
            "total_shows_probed": 0,
            "fetch_error": f"{type(e).__name__}: {e}",
            "categories": {},
            "gaps": {},
        }

    category_counts: Counter = Counter()
    gaps_by_category: dict[str, list[dict]] = {
        CAT_MISSING: [],
        CAT_QUARANTINED: [],
    }

    for show in source_shows:
        mal_id = show["mal_id"]
        # Simpler than season-mode categorization — no season comparison,
        # so MISCLASSIFIED doesn't apply. Present-in-vault always wins.
        if show["media_type"] not in allowed_media_types:
            cat = CAT_SKIPPED_NON_TV
        elif mal_id in quarantined:
            cat = CAT_QUARANTINED
        elif mal_id in vaulted:
            cat = CAT_VAULTED
        else:
            cat = CAT_MISSING

        category_counts[cat] += 1
        if cat in gaps_by_category:
            entry = build_gap_entry(
                show, cat,
                quarantine_meta=quarantined.get(mal_id),
                vault_season=None,
            )
            gaps_by_category[cat].append(entry)

    for entries in gaps_by_category.values():
        entries.sort(key=lambda x: x["scored_by"] or 0, reverse=True)

    return {
        "top_n_target": top_n,
        "total_shows_probed": len(source_shows),
        "categories": dict(category_counts),
        "gaps": gaps_by_category,
    }


def _print_season_block(season_audit: dict) -> None:
    """Season-mode results portion of the summary."""
    yr = season_audit["years_range"]
    filt = season_audit["seasons_filter"]
    print("--- Season audit ---")
    print(f"Years:        {yr[0]}-{yr[1]} ({(yr[1] - yr[0] + 1) * len(filt)} season probes)")
    print(f"Shows probed: {season_audit['total_shows_probed']}")
    total = season_audit["total_shows_probed"] or 1
    for cat in ALL_CATEGORIES:
        count = season_audit["overall_categories"].get(cat, 0)
        pct = (count / total) * 100
        print(f"  {cat:<18} {count:>6}  ({pct:>5.1f}%)")
    print()

    all_missing: list[tuple[dict, str]] = []
    all_misclassified: list[tuple[dict, str]] = []
    for sr in season_audit["seasons"]:
        for gap in sr["gaps"].get(CAT_MISSING, []):
            all_missing.append((gap, sr["season_tag"]))
        for gap in sr["gaps"].get(CAT_MISCLASSIFIED, []):
            all_misclassified.append((gap, sr["season_tag"]))

    all_missing.sort(key=lambda x: x[0]["scored_by"] or 0, reverse=True)
    all_misclassified.sort(key=lambda x: x[0]["scored_by"] or 0, reverse=True)

    if all_missing:
        print(f"Top {min(15, len(all_missing))} season-audit MISSING shows by popularity:")
        for gap, season_tag in all_missing[:15]:
            title = gap["title"][:40]
            print(f"  MAL {gap['mal_id']:>7} | {title:<40} | {season_tag:<15} | scored_by={gap['scored_by']:>7}")
        if len(all_missing) > 15:
            print(f"  ... {len(all_missing) - 15} more in the full JSON report")
        print()

    if all_misclassified:
        print(f"MISCLASSIFIED shows ({len(all_misclassified)} total):")
        for gap, season_tag in all_misclassified[:10]:
            title = gap["title"][:40]
            print(f"  MAL {gap['mal_id']:>7} | {title:<40} | MAL start {gap.get('mal_season', '?'):<15} | vault season {gap.get('vault_season', '?')}")
        print()


def _print_popularity_block(pop_audit: dict) -> None:
    """Popularity-mode results portion of the summary."""
    print("--- Popularity audit ---")
    print(f"Top-N target: {pop_audit['top_n_target']}")
    print(f"Shows probed: {pop_audit['total_shows_probed']}")
    total = pop_audit["total_shows_probed"] or 1
    for cat in ALL_CATEGORIES:
        count = pop_audit["categories"].get(cat, 0)
        pct = (count / total) * 100
        print(f"  {cat:<18} {count:>6}  ({pct:>5.1f}%)")
    print()

    missing = pop_audit["gaps"].get(CAT_MISSING, [])
    if missing:
        print(f"Top {min(15, len(missing))} popularity-audit MISSING shows by ratings count:")
        for gap in missing[:15]:
            title = gap["title"][:45]
            print(f"  MAL {gap['mal_id']:>7} | {title:<45} | scored_by={gap['scored_by']:>7}")
        if len(missing) > 15:
            print(f"  ... {len(missing) - 15} more in the full JSON report")
        print()


def print_summary(report: dict) -> None:
    """Human-readable summary of the aggregate report."""
    print()
    print("=" * 70)
    print("Vault Coverage Audit Report")
    print("=" * 70)
    print(f"Report ID:  {report['report_id']}")
    print(f"Modes run:  {', '.join(report['modes_run'])}")
    print()

    if "season_audit" in report:
        _print_season_block(report["season_audit"])
    if "popularity_audit" in report:
        _print_popularity_block(report["popularity_audit"])

    if report.get("report_path"):
        print(f"Full report: {report['report_path']}")


def build_season_audit_block(
    seasons_data: list[dict],
    years_range: tuple[int, int],
    seasons_filter: tuple[str, ...],
) -> dict:
    """Aggregate per-season results into the season_audit block."""
    overall = Counter()
    for season_report in seasons_data:
        overall.update(season_report.get("categories", {}))
    total_probed = sum(sr.get("source_count", 0) for sr in seasons_data)
    return {
        "years_range": list(years_range),
        "seasons_filter": list(seasons_filter),
        "total_shows_probed": total_probed,
        "overall_categories": dict(overall),
        "seasons": seasons_data,
    }


def build_report(
    modes_run: list[str],
    season_audit: Optional[dict] = None,
    popularity_audit: Optional[dict] = None,
    media_types: Optional[list[str]] = None,
) -> dict:
    """Assemble the final report with per-mode nested blocks.

    Either or both of season_audit / popularity_audit may be present
    depending on --mode. `modes_run` is the ground truth for which
    sections exist so downstream consumers can dispatch cleanly.
    `media_types` records the whitelist filter so a report consumer
    (e.g., backfill_from_coverage_report.py) can cross-check it
    matches what they expected.
    """
    report = {
        "report_id": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ"),
        "modes_run": modes_run,
        "media_types": sorted(media_types) if media_types else sorted(DEFAULT_ALLOWED_MEDIA_TYPES),
    }
    if season_audit is not None:
        report["season_audit"] = season_audit
    if popularity_audit is not None:
        report["popularity_audit"] = popularity_audit
    return report


def write_report(report: dict, output_path: str) -> None:
    """Write report JSON to the specified path."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)


def parse_args():
    p = argparse.ArgumentParser(
        description="Audit vault coverage against MAL's listings.",
    )
    p.add_argument("--mode", choices=["season", "popularity", "combined"],
                   default="combined",
                   help="Which audit mode to run (default: combined — both).")
    p.add_argument("--year", type=int, help="Audit a single year (season mode).")
    p.add_argument("--start-year", type=int, default=DEFAULT_START_YEAR,
                   help=f"Start of year range for season mode (default: {DEFAULT_START_YEAR}).")
    p.add_argument("--end-year", type=int, default=DEFAULT_END_YEAR,
                   help=f"End of year range inclusive (default: current year).")
    p.add_argument("--seasons", nargs="+", default=list(SEASONS), choices=SEASONS,
                   help="Subset of seasons to audit (default: all four).")
    p.add_argument("--top-n", type=int, default=5000,
                   help="Top-N shows for popularity mode (default: 5000).")
    p.add_argument("--output", type=str, default=None,
                   help="Report JSON path (default: data/vault_coverage_report_{DATE}.json).")
    p.add_argument("--sleep", type=float, default=0.5,
                   help="Delay between MAL API pages in seconds (default: 0.5).")
    p.add_argument("--summary-only", action="store_true",
                   help="Print summary to stdout, don't write JSON.")
    p.add_argument("--media-types", nargs="+", default=sorted(DEFAULT_ALLOWED_MEDIA_TYPES),
                   help=("MAL media_type whitelist for the audit. Default: "
                         f"{sorted(DEFAULT_ALLOWED_MEDIA_TYPES)} (preserves "
                         "pre-Option-B TV-only behavior). Pass additional "
                         "subtypes to probe gaps for them — e.g., "
                         "`--media-types tv movie ova`. Shows outside the "
                         "whitelist are categorized as SKIPPED_NON_TV."))
    return p.parse_args()


def run_season_mode(
    mal_client: MALClient,
    vaulted: dict[int, str],
    quarantined: dict[int, dict],
    args,
) -> dict:
    """Run the per-season audit for the given year range + season filter."""
    if args.year:
        years_range = (args.year, args.year)
    else:
        years_range = (args.start_year, args.end_year)
    years = list(range(years_range[0], years_range[1] + 1))
    seasons_filter = tuple(args.seasons)
    allowed_media_types = frozenset(args.media_types)

    seasons_data: list[dict] = []
    total_probes = len(years) * len(seasons_filter)
    probe_num = 0
    for year in years:
        for season in seasons_filter:
            probe_num += 1
            print(f"[{probe_num}/{total_probes}] Auditing {season.capitalize()}_{year}...",
                  end=" ", flush=True)
            sr = audit_season(
                year, season, mal_client, vaulted, quarantined,
                sleep_between_pages=args.sleep,
                allowed_media_types=allowed_media_types,
            )
            cats = sr["categories"]
            print(
                f"source={sr['source_count']} "
                f"MISSING={cats.get(CAT_MISSING, 0)} "
                f"QUAR={cats.get(CAT_QUARANTINED, 0)} "
                f"MISC={cats.get(CAT_MISCLASSIFIED, 0)}"
            )
            seasons_data.append(sr)

    return build_season_audit_block(seasons_data, years_range, seasons_filter)


def run_popularity_mode(
    mal_client: MALClient,
    vaulted: dict[int, str],
    quarantined: dict[int, dict],
    args,
) -> dict:
    """Run the popularity-ranking audit for the top-N shows."""
    print(f"Popularity audit: fetching top-{args.top_n} from MAL "
          f"(media_types={sorted(args.media_types)})...")
    pop_audit = audit_popularity(
        mal_client, vaulted, quarantined, top_n=args.top_n,
        sleep_between_pages=args.sleep,
        allowed_media_types=frozenset(args.media_types),
    )
    cats = pop_audit["categories"]
    print(
        f"  probed={pop_audit['total_shows_probed']} "
        f"MISSING={cats.get(CAT_MISSING, 0)} "
        f"QUAR={cats.get(CAT_QUARANTINED, 0)} "
        f"VAULTED={cats.get(CAT_VAULTED, 0)}"
    )
    return pop_audit


def main():
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    args = parse_args()

    modes_run: list[str] = []
    if args.mode in ("season", "combined"):
        modes_run.append("season")
    if args.mode in ("popularity", "combined"):
        modes_run.append("popularity")

    print(f"Vault coverage audit — mode(s): {', '.join(modes_run)}")
    print(f"Snapshotting vault state from {DB_PATH}...")
    vaulted, quarantined = snapshot_vault_state(DB_PATH)
    print(f"  {len(vaulted)} vaulted shows, {len(quarantined)} quarantined")

    mal_client = MALClient(token_path=os.path.join(ROOT_DIR, "token_data.json"))

    season_audit = None
    popularity_audit = None
    if "season" in modes_run:
        season_audit = run_season_mode(mal_client, vaulted, quarantined, args)
    if "popularity" in modes_run:
        popularity_audit = run_popularity_mode(mal_client, vaulted, quarantined, args)

    report = build_report(
        modes_run,
        season_audit=season_audit,
        popularity_audit=popularity_audit,
        media_types=args.media_types,
    )

    if not args.summary_only:
        output_path = args.output or os.path.join(
            ROOT_DIR, "data", f"vault_coverage_report_{_iso_date_now()}.json"
        )
        write_report(report, output_path)
        report["report_path"] = output_path

    print_summary(report)


if __name__ == "__main__":
    main()
