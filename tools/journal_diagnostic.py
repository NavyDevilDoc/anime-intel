"""
tools/journal_diagnostic.py

One-shot failure-mode / bias diagnostic against production journal data.
Wraps the existing analysis.journal_reader (Supabase-backed) with the
analyses that don't yet exist as standalone tools:

  1. Volume + mode breakdown for the sampled window
  2. Failure segments (empty pool, low-confidence, thumbs-down join)
  3. Recency-bias check (return-year distribution vs vault baseline)
  4. Popularity-bias check (scored_by distribution vs vault baseline)
  5. Query archetype heuristic clustering
  6. Raw failure samples for eyeballing

Usage (from repo root):
    python tools/journal_diagnostic.py                         # July-August window, all modes
    python tools/journal_diagnostic.py --since 2026-07-01
    python tools/journal_diagnostic.py --failures-sample 50    # more raw samples

Sources data from Supabase (JOURNAL_SOURCE=supabase implicit) so it
reflects production traffic, not local dev. Requires SUPABASE_URL +
SUPABASE_SERVICE_KEY in env_variables.env (already set for telemetry
on this machine).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

# --- BULLETPROOF PATH ANCHORING ---
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent if SCRIPT_DIR.name == "tools" else SCRIPT_DIR
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Force UTF-8 stdout for CJK-in-title safety.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

from dotenv import load_dotenv

load_dotenv(ROOT_DIR / "env_variables.env")

from analysis.journal_reader import read_entries  # noqa: E402
from api.supabase_client import supabase_get  # noqa: E402

DB_PATH = ROOT_DIR / "data" / "anime_intelligence_v2.db"
LOW_CONFIDENCE_THRESHOLD = 75

# Queries I ran via tools/query_probe.py during PR #97 development. Not
# real user traffic — flagged so they don't pollute the report.
PROBE_QUERY_CONTENT = {
    "cozy slice of life",  # my probe test query
    "test query",  # generic probe
}

# Word buckets for the archetype heuristic. Case-insensitive substring
# match against the query. Not mutually exclusive — a query can hit
# multiple buckets, so archetypes report as a multi-tag histogram.
ARCHETYPE_PATTERNS = {
    "mood/vibe": [
        "cozy", "gritty", "wholesome", "dark", "chill", "vibe", "atmosphere",
        "melancholy", "uplifting", "feel-good", "heartwarming", "bittersweet",
    ],
    "genre-combo": [
        "with", "and", "but", "meets", "crossed",
    ],
    "quality-anchor": [
        "top ", "best ", "highly rated", "underrated", "hidden gem",
        "critically acclaimed", "must-watch",
    ],
    "era-anchor": [
        "90s", "1990s", "2000s", "2010s", "2020s", "old", "classic",
        "recent", "new", "modern", "from 20",
    ],
    "character-driven": [
        "protagonist", "mc ", "main character", "female lead", "male lead",
        "cast", "characters", "character development",
    ],
    "plot-driven": [
        "plot", "story", "narrative", "arc", "twist", "mystery", "reveal",
        "war", "revenge", "betrayed", "conspiracy",
    ],
    "franchise-root": [
        # detected downstream by short exact-title query; placeholder here
    ],
    "meta/comparison": [
        "like ", "similar to", "reminds me of", "in the style of",
    ],
    "studio-anchor": [
        "mappa", "wit ", "kyoani", "kyoto animation", "trigger", "bones",
        "ufotable", "madhouse", "ghibli", "pierrot", "sunrise", "toei",
        "shaft", "a-1",
    ],
}


def _is_probe_query(query: str) -> bool:
    if not query:
        return False
    q = query.strip().lower()
    return q in PROBE_QUERY_CONTENT


def _classify_archetypes(query: str) -> list[str]:
    q = (query or "").lower()
    tags: list[str] = []
    for tag, patterns in ARCHETYPE_PATTERNS.items():
        for p in patterns:
            if p in q:
                tags.append(tag)
                break
    if not tags:
        tags.append("uncategorized")
    return tags


def _load_vault_baselines() -> dict:
    """Vault-side reference distributions for bias checks."""
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    cursor.execute("SELECT release_year, scored_by FROM anime_info")
    years = []
    scored = []
    for y, s in cursor.fetchall():
        if y:
            years.append(y)
        if s:
            scored.append(s)
    conn.close()

    # Decade bucketing
    decades: Counter[str] = Counter()
    for y in years:
        d = (y // 10) * 10
        decades[f"{d}s"] += 1

    # scored_by percentile bands — 5 buckets tuned to catch obscure vs mainstream
    scored_sorted = sorted(scored)
    n = len(scored_sorted)
    def pct(p: float) -> int:
        return scored_sorted[min(n - 1, int(n * p))]
    return {
        "total_with_year": len(years),
        "decade_counts": dict(decades),
        "decade_pct": {k: round(v / len(years) * 100, 1) for k, v in decades.items()},
        "scored_p25": pct(0.25),
        "scored_p50": pct(0.50),
        "scored_p75": pct(0.75),
        "scored_p90": pct(0.90),
    }


def _fetch_show_meta_by_titles(titles: list[str]) -> dict[str, dict]:
    """Batch-lookup (release_year, scored_by) for a set of titles."""
    if not titles:
        return {}
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    # IN(...) has SQLite's 999-parameter cap; chunk if needed
    out: dict[str, dict] = {}
    for i in range(0, len(titles), 500):
        chunk = titles[i:i + 500]
        placeholders = ",".join(["?"] * len(chunk))
        cursor.execute(
            f"SELECT english_title, romaji_title, release_year, scored_by "
            f"FROM anime_info "
            f"WHERE english_title IN ({placeholders}) OR romaji_title IN ({placeholders})",
            chunk + chunk,
        )
        for en, rj, yr, sc in cursor.fetchall():
            if en:
                out[en] = {"release_year": yr, "scored_by": sc}
            if rj and rj not in out:
                out[rj] = {"release_year": yr, "scored_by": sc}
    conn.close()
    return out


def _fetch_feedback_index() -> dict[str, str]:
    """Return {search_id: 'up'|'down'} for all search_feedback rows.

    Supabase pagination — request up to 1000 at a time until exhausted."""
    idx: dict[str, str] = {}
    offset = 0
    page = 1000
    while True:
        rows = supabase_get(
            "search_feedback",
            {"select": "search_id,rating", "order": "created_at.asc",
             "limit": str(page), "offset": str(offset)},
        )
        if not rows:
            break
        for r in rows:
            sid = r.get("search_id")
            rating = r.get("rating")
            if sid and rating in ("up", "down"):
                idx[sid] = rating
        if len(rows) < page:
            break
        offset += page
    return idx


def _bucket_scored_by(v: int | None, baselines: dict) -> str:
    if v is None or v == 0:
        return "unrated"
    if v < baselines["scored_p25"]:
        return "obscure (<p25)"
    if v < baselines["scored_p50"]:
        return "niche (p25-p50)"
    if v < baselines["scored_p75"]:
        return "known (p50-p75)"
    if v < baselines["scored_p90"]:
        return "popular (p75-p90)"
    return "mainstream (>p90)"


def _decade_of(year: int | None) -> str:
    if not year:
        return "unknown"
    return f"{(year // 10) * 10}s"


def _print_hr():
    print("\n" + "=" * 78)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default="2026-07-01",
                        help="ISO date; default 2026-07-01 for the July-August window.")
    parser.add_argument("--failures-sample", type=int, default=25,
                        help="Number of raw failure entries to include in the report.")
    args = parser.parse_args()

    since_dt = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)

    # ---------- Pull data ----------
    print(f"Loading journal from Supabase since {args.since}...", file=sys.stderr)
    rows = read_entries(source="supabase", since=since_dt)
    print(f"  Retrieved {len(rows):,} entries.", file=sys.stderr)

    # Drop probe-flagged queries and non-search entries (feedback events)
    entries = [
        r for r in rows
        if r.get("mode") in ("find_a_show", "shows_like_this")
        and not _is_probe_query(r.get("query") or "")
    ]
    dropped_probe = len(rows) - len(entries) - sum(1 for r in rows if r.get("mode") not in ("find_a_show", "shows_like_this"))
    non_search_dropped = sum(1 for r in rows if r.get("mode") not in ("find_a_show", "shows_like_this"))
    print(f"  {len(entries):,} search entries after filtering "
          f"({dropped_probe} probe-flagged, {non_search_dropped} non-search).", file=sys.stderr)

    print("Loading vault baselines from SQLite...", file=sys.stderr)
    baselines = _load_vault_baselines()

    print("Loading search_feedback index from Supabase...", file=sys.stderr)
    feedback_idx = _fetch_feedback_index()
    print(f"  {len(feedback_idx):,} feedback rows.", file=sys.stderr)

    # Prefetch (release_year, scored_by) for every returned title so bias
    # analysis doesn't hit SQLite per-entry.
    all_titles: set[str] = set()
    for e in entries:
        for r in e.get("results") or []:
            t = r.get("title")
            if t:
                all_titles.add(t)
    print(f"Fetching metadata for {len(all_titles):,} distinct result titles...", file=sys.stderr)
    title_meta = _fetch_show_meta_by_titles(sorted(all_titles))

    # ---------- Section 1: Volume ----------
    print("\n" + "#" * 78)
    print(f"# Journal Diagnostic — since {args.since} through today")
    print("#" * 78)

    _print_hr()
    print("## 1. Volume + mode breakdown\n")
    modes = Counter(e.get("mode") for e in entries)
    print(f"Total search events: {len(entries):,}")
    for m, c in modes.most_common():
        print(f"  {m}: {c:,} ({c / len(entries) * 100:.1f}%)")
    print(f"Distinct queries: {len(set(e.get('query') or '' for e in entries)):,}")

    # ---------- Section 2: Failure segments ----------
    _print_hr()
    print("## 2. Failure segments\n")
    empty_pool = [e for e in entries if not (e.get("results") or [])]
    low_conf = [e for e in entries if (e.get("results") or []) and
                (e.get("top_match_confidence") or 0) < LOW_CONFIDENCE_THRESHOLD]
    with_feedback = [e for e in entries if e.get("search_id") in feedback_idx]
    thumbs_down = [e for e in with_feedback if feedback_idx.get(e.get("search_id")) == "down"]
    thumbs_up = [e for e in with_feedback if feedback_idx.get(e.get("search_id")) == "up"]

    print(f"Empty pool (no results returned): {len(empty_pool):,} "
          f"({len(empty_pool) / len(entries) * 100:.1f}%)")
    print(f"Low-confidence (top < {LOW_CONFIDENCE_THRESHOLD}, non-empty): {len(low_conf):,} "
          f"({len(low_conf) / len(entries) * 100:.1f}%)")
    print(f"Total banner-fire segment: {len(empty_pool) + len(low_conf):,} "
          f"({(len(empty_pool) + len(low_conf)) / len(entries) * 100:.1f}%)")
    print()
    print(f"Search events with feedback attached: {len(with_feedback):,} "
          f"({len(with_feedback) / len(entries) * 100:.1f}%)")
    print(f"  Thumbs up: {len(thumbs_up):,}")
    print(f"  Thumbs down: {len(thumbs_down):,}")

    # Failure by mode
    for mode in modes:
        me = [e for e in entries if e.get("mode") == mode]
        me_empty = sum(1 for e in me if not (e.get("results") or []))
        me_low = sum(1 for e in me if (e.get("results") or []) and
                     (e.get("top_match_confidence") or 0) < LOW_CONFIDENCE_THRESHOLD)
        print(f"\n  {mode}: empty {me_empty}/{len(me)} ({me_empty/len(me)*100:.1f}%), "
              f"low-conf {me_low}/{len(me)} ({me_low/len(me)*100:.1f}%)")

    # ---------- Section 3: Recency bias ----------
    _print_hr()
    print("## 3. Recency-bias check\n")
    print("Vault baseline (decade distribution across the 6,342-show catalog):")
    for decade in sorted(baselines["decade_counts"].keys()):
        print(f"  {decade}: {baselines['decade_pct'][decade]:>5.1f}%  "
              f"(n={baselines['decade_counts'][decade]:,})")

    # Compare against top-3 results returned when NO era filter was applied.
    # Era-filtered searches would obviously skew their era's decade.
    # "no era filter" means era is None, empty string, or the literal "All"
    # sentinel — the frontend renders "All" as the default radio button.
    no_era = [e for e in entries if e.get("mode") == "find_a_show"
              and (e.get("era") in (None, "", "All"))]
    return_decades: Counter[str] = Counter()
    for e in no_era:
        for r in (e.get("results") or [])[:3]:
            meta = title_meta.get(r.get("title") or "")
            if meta:
                return_decades[_decade_of(meta.get("release_year"))] += 1
    total_returns = sum(return_decades.values())

    print(f"\nFind-a-Show (no era filter) top-3 return distribution "
          f"(n={total_returns} title-picks over {len(no_era)} queries):")
    for decade in sorted(return_decades.keys()):
        pct_return = return_decades[decade] / total_returns * 100 if total_returns else 0
        pct_vault = baselines["decade_pct"].get(decade, 0)
        skew = pct_return - pct_vault
        skew_str = f"({'+' if skew >= 0 else ''}{skew:.1f} vs vault)"
        print(f"  {decade}: {pct_return:>5.1f}%  {skew_str}")

    # ---------- Section 4: Popularity bias ----------
    _print_hr()
    print("## 4. Popularity-bias check\n")
    print("Vault scored_by percentile bands used for bucketing:")
    print(f"  p25={baselines['scored_p25']:,}  p50={baselines['scored_p50']:,}  "
          f"p75={baselines['scored_p75']:,}  p90={baselines['scored_p90']:,}")

    pop_buckets_top: Counter[str] = Counter()
    for e in entries:
        for r in (e.get("results") or [])[:3]:
            meta = title_meta.get(r.get("title") or "")
            if meta:
                pop_buckets_top[_bucket_scored_by(meta.get("scored_by"), baselines)] += 1
    total_pop = sum(pop_buckets_top.values())
    print(f"\nTop-3 result popularity distribution (n={total_pop} title-picks, "
          f"all modes, all filters):")
    for bucket in ["obscure (<p25)", "niche (p25-p50)", "known (p50-p75)",
                   "popular (p75-p90)", "mainstream (>p90)", "unrated"]:
        cnt = pop_buckets_top.get(bucket, 0)
        pct = cnt / total_pop * 100 if total_pop else 0
        print(f"  {bucket:>22s}: {pct:>5.1f}%  (n={cnt:,})")

    # ---------- Section 5: Archetypes ----------
    _print_hr()
    print("## 5. Query archetype clustering (heuristic, non-exclusive)\n")
    archetype_counts: Counter[str] = Counter()
    archetype_query_samples: dict[str, list[str]] = defaultdict(list)
    fas_entries = [e for e in entries if e.get("mode") == "find_a_show"]
    for e in fas_entries:
        q = e.get("query") or ""
        tags = _classify_archetypes(q)
        for t in tags:
            archetype_counts[t] += 1
            if len(archetype_query_samples[t]) < 3:
                archetype_query_samples[t].append(q)
    print(f"Find-a-Show query archetypes (n={len(fas_entries)}, tags non-exclusive):")
    for tag, cnt in archetype_counts.most_common():
        print(f"  {tag:>20s}: {cnt:>4d}  ({cnt / len(fas_entries) * 100:>5.1f}% of FaS)")
        for s in archetype_query_samples[tag][:2]:
            print(f"      e.g., \"{s[:70]}\"")

    # ---------- Section 6: Raw failure samples ----------
    _print_hr()
    print(f"## 6. Sample raw failures (up to {args.failures_sample} for eyeballing)\n")
    # Prioritize thumbs-down first, then low-conf, then empty
    failures = []
    seen_search_ids: set[str] = set()
    for e in thumbs_down:
        sid = e.get("search_id")
        if sid and sid not in seen_search_ids:
            failures.append(("THUMBS_DOWN", e))
            seen_search_ids.add(sid)
    for e in low_conf:
        sid = e.get("search_id")
        if sid and sid in seen_search_ids:
            continue
        failures.append(("LOW_CONF", e))
        if sid:
            seen_search_ids.add(sid)
        if len(failures) >= args.failures_sample:
            break
    for e in empty_pool:
        if len(failures) >= args.failures_sample:
            break
        sid = e.get("search_id")
        if sid and sid in seen_search_ids:
            continue
        failures.append(("EMPTY", e))
        if sid:
            seen_search_ids.add(sid)

    for tag, e in failures[:args.failures_sample]:
        q = e.get("query") or "(no query)"
        mode = e.get("mode")
        ref = e.get("reference_titles") or []
        neg = e.get("negative_anchors") or []
        top_titles = [r.get("title") for r in (e.get("results") or [])[:3]]
        top_confs = [r.get("confidence") for r in (e.get("results") or [])[:3]]
        top_str = ", ".join(f"{t} ({c}%)" for t, c in zip(top_titles, top_confs)) if top_titles else "(empty)"
        input_str = f'"{q}"' if mode == "find_a_show" else f"refs={ref} negs={neg}"
        print(f"[{tag}] {mode}  {input_str}")
        print(f"    top3: {top_str}")
        print(f"    lens={e.get('lens')} era={e.get('era')} rating_max={e.get('mal_rating_max')} "
              f"intent={e.get('intent')} rerank={e.get('rerank_type')}")
        print()

    _print_hr()
    print("End of report.")


if __name__ == "__main__":
    main()
