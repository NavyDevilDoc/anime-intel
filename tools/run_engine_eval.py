"""
tools/run_engine_eval.py — capture engine quality baseline against curated eval set.

Reads tests/data/engine_eval_queries.json, dispatches each entry through the
recommendation engine (Find-a-Show / Objective Rankings via lens_name;
Shows-Like-This via execute_dna_triangulation), and scores results against
hand-annotated ground truth.

STRICT matching by MAL ID — engine-returned titles and eval-set titles are
both resolved to mal_id via the vault, so no fuzzy substitution risk. Requires
verify_eval_titles.py to be green before running (all eval titles must
strict-resolve, otherwise scoring silently under-counts hits).

Metrics per query:
  precision@3       len(returned_top_3 ∩ ideal) / min(3, len(ideal))
                    Denominator is min(3, len(ideal)) so an engine that
                    surfaces every ideal in top-3 gets 1.0 regardless of
                    len(ideal). None when ideal is empty.
  coverage@10       len(returned_top_10 ∩ (ideal ∪ acceptable)) / len(union)
                    None when both ideal and acceptable are empty.
  violations        count of must_not_appear titles that surfaced anywhere in
                    the returned pool.
  correctly_empty   1.0 if the engine correctly returned zero results for an
                    entry that expected emptiness (ideal AND acceptable both
                    empty — e.g. `empty-vague-dark`). 0.0 if it returned
                    anything. None for normal (non-empty-eval) entries.

Aggregates (overall + by category + by confidence tier):
  mean(precision@3), mean(coverage@10), sum(violations), correctly_empty ratio.

Gemini calls tagged ANIME_INTEL_CALL_ORIGIN=probe by default so eval spend
doesn't pollute the production attribution ledger. Override via env var.

Usage (from repo root):

    # Full baseline run — writes tests/data/eval_baseline_YYYY-MM-DD.json
    python tools/run_engine_eval.py

    # Category filter (smoke-test a slice before full run)
    python tools/run_engine_eval.py --categories vibe-anchor,known-failure

    # Run only the first N entries (fast sanity check)
    python tools/run_engine_eval.py --limit 3

    # Custom output path
    python tools/run_engine_eval.py --out tests/data/eval_experiment.json
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# --- SYSTEM PATH ANCHORING (mirror the pattern used across tools/) ---
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent if SCRIPT_DIR.name == "tools" else SCRIPT_DIR
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Tag Gemini calls before importing gemini_client so the module picks up the
# origin at first access. setdefault so operators can override on the command
# line (e.g., `ANIME_INTEL_CALL_ORIGIN=dev python tools/run_engine_eval.py`).
os.environ.setdefault("ANIME_INTEL_CALL_ORIGIN", "probe")

# Force UTF-8 stdout for CJK / accented titles in the per-query console lines.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT_DIR, "env_variables.env"))

from analysis.recommendation_engine import RecommendationEngine  # noqa: E402

DB_PATH = ROOT_DIR / "data" / "anime_intelligence_v2.db"
EVAL_PATH = ROOT_DIR / "tests" / "data" / "engine_eval_queries.json"

# Mirror of ERA_OPTIONS in frontend/src/components/FindAShow.tsx so the
# scoring tool passes the exact (year_min, year_max) tuple the frontend
# would. Keep in sync if the frontend eras ever change.
ERA_TO_YEARS: dict[str | None, tuple[int | None, int | None]] = {
    "1990s": (1990, 1999),
    "2000s": (2000, 2009),
    "2010s": (2010, 2019),
    "2020s": (2020, 2029),
    "All": (None, None),
    None: (None, None),
}


def load_vault_lookup(db_path: Path) -> tuple[dict[str, int], dict[int, str]]:
    """Return (title→mal_id, mal_id→english_title) built from the vault.

    Both english_title and romaji_title map into the forward index so an
    eval-set entry can canonicalize on either form. First-write-wins on
    collision (english_title indexed first)."""
    title_to_id: dict[str, int] = {}
    id_to_english: dict[int, str] = {}
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("SELECT id, english_title, romaji_title FROM anime_info")
    for mal_id, en, ro in cur.fetchall():
        if en:
            title_to_id[en] = mal_id
            id_to_english[mal_id] = en
        if ro and ro not in title_to_id:
            title_to_id[ro] = mal_id
    conn.close()
    return title_to_id, id_to_english


def titles_to_ids(titles: list[str], title_to_id: dict[str, int]) -> set[int]:
    """Convert eval-set title strings to a set of mal_ids. Silently drops
    unresolvable titles — verify_eval_titles.py must be green before this
    tool runs; any drop here would indicate the vault or eval set changed
    since verification."""
    return {title_to_id[t] for t in titles if t in title_to_id}


def extract_returned_ids(pool_entries: list[dict], title_to_id: dict[str, int]) -> list[int]:
    """Ordered list of mal_ids returned by the engine. Preserves order —
    top-K precision needs it. Falls back on title lookup when the profile
    doesn't expose an id field (defensive; Objective Rankings and DNA
    paths sometimes surface leaner profiles)."""
    ids: list[int] = []
    for entry in pool_entries:
        profile = entry.get("profile") or {}
        mal_id = profile.get("id") or profile.get("mal_id")
        if not mal_id:
            title = profile.get("title") or profile.get("english_title") or entry.get("title")
            mal_id = title_to_id.get(title) if title else None
        if mal_id:
            ids.append(mal_id)
    return ids


def score_entry(entry: dict, returned_ids: list[int], title_to_id: dict[str, int]) -> dict:
    """Compute per-query metrics. See module docstring for definitions."""
    ideal = titles_to_ids(entry.get("ideal_top_3", []), title_to_id)
    acceptable = titles_to_ids(entry.get("acceptable_top_10", []), title_to_id)
    must_not = titles_to_ids(entry.get("must_not_appear", []), title_to_id)
    combined = ideal | acceptable

    top_3 = set(returned_ids[:3])
    top_10 = set(returned_ids[:10])
    all_returned = set(returned_ids)

    # Correctly-empty entries have both ideal AND acceptable empty (see
    # `empty-vague-dark` — the notes make the intent explicit). Score 1.0
    # if the engine returned nothing, 0.0 if it returned anything.
    is_empty_eval = not ideal and not acceptable
    correctly_empty: float | None = None
    if is_empty_eval:
        correctly_empty = 1.0 if not returned_ids else 0.0

    precision_at_3: float | None = None
    if ideal:
        precision_at_3 = len(top_3 & ideal) / min(3, len(ideal))

    coverage_at_10: float | None = None
    if combined:
        coverage_at_10 = len(top_10 & combined) / len(combined)

    return {
        "returned_count": len(returned_ids),
        "returned_top_3_hits": sorted(top_3 & ideal),
        "returned_top_10_hits": sorted(top_10 & combined),
        "must_not_appear_violations": sorted(all_returned & must_not),
        "precision_at_3": precision_at_3,
        "coverage_at_10": coverage_at_10,
        "correctly_empty": correctly_empty,
    }


def dispatch(engine: RecommendationEngine, entry: dict) -> tuple[list[dict], float, dict]:
    """Call the engine for one eval entry. Return (pool_entries, elapsed_sec,
    engine_meta). Mode dispatch: find_a_show → fetch_vault_pool (with lens
    handling OR routing internally); shows_like_this → execute_dna_triangulation."""
    mode = entry["mode"]
    filters = entry.get("filters", {}) or {}

    t0 = time.perf_counter()
    if mode == "find_a_show":
        lens = filters.get("lens") or "Intelligent Search"
        era = filters.get("era")
        year_min, year_max = ERA_TO_YEARS.get(era, (None, None))
        result = engine.fetch_vault_pool(
            entry["query"],
            lens_name=lens,
            year_min=year_min,
            year_max=year_max,
            era_label=None if era in (None, "All") else era,
            rating_max=filters.get("rating_max"),
        )
        pool = result.get("pool", []) or []
    elif mode == "shows_like_this":
        result = engine.execute_dna_triangulation(
            entry["reference_titles"],
            negative_anchors=entry.get("negative_anchors") or None,
        )
        pool = result.get("data", []) or []
    else:
        raise ValueError(f"Unsupported eval mode: {mode!r}")

    elapsed = time.perf_counter() - t0
    engine_meta = {
        "success": result.get("success"),
        "low_confidence": bool(result.get("low_confidence")),
        "error": result.get("error"),
    }
    return pool, elapsed, engine_meta


def aggregate(per_query: list[dict], key_func=None) -> dict[str, dict]:
    """Group per_query records and compute mean(p@3), mean(c@10),
    sum(violations), correctly_empty stats. key_func=None yields one
    bucket keyed 'overall'."""
    buckets: dict[str, list[dict]] = {}
    for q in per_query:
        key = "overall" if key_func is None else key_func(q)
        buckets.setdefault(key, []).append(q)

    out: dict[str, dict] = {}
    for key, qs in buckets.items():
        p3_values = [q["precision_at_3"] for q in qs if q["precision_at_3"] is not None]
        c10_values = [q["coverage_at_10"] for q in qs if q["coverage_at_10"] is not None]
        empty_qs = [q for q in qs if q["correctly_empty"] is not None]
        empty_hits = sum(1 for q in empty_qs if q["correctly_empty"] == 1.0)
        violations = sum(len(q["must_not_appear_violations"]) for q in qs)
        out[key] = {
            "n": len(qs),
            "precision_at_3_mean": statistics.mean(p3_values) if p3_values else None,
            "coverage_at_10_mean": statistics.mean(c10_values) if c10_values else None,
            "must_not_appear_violations": violations,
            "correctly_empty_hit": empty_hits,
            "correctly_empty_n": len(empty_qs),
        }
    return out


def format_console_line(entry: dict, scored: dict, elapsed: float, meta: dict) -> str:
    """One-line per-query summary for stdout."""
    category = entry.get("category", "?")
    entry_id = entry.get("id", "?")

    if scored["correctly_empty"] is not None:
        status = "CORRECTLY EMPTY ✓" if scored["correctly_empty"] == 1.0 else "expected empty, returned results ✗"
        return f"[{category:<16}] {entry_id:<44} {status:<40}  {elapsed:>5.1f}s"

    p3 = scored["precision_at_3"]
    c10 = scored["coverage_at_10"]
    p3s = f"{p3:.2f}" if p3 is not None else " n/a"
    c10s = f"{c10:.2f}" if c10 is not None else " n/a"
    viol = len(scored["must_not_appear_violations"])
    viol_tag = f"✗ {viol}viol" if viol else "     ✓"
    tail_flags = []
    if meta.get("low_confidence"):
        tail_flags.append("low_conf")
    if scored["returned_count"] == 0:
        tail_flags.append("empty pool")
    if meta.get("error"):
        tail_flags.append("error")
    flag_str = f"  [{', '.join(tail_flags)}]" if tail_flags else ""
    return (f"[{category:<16}] {entry_id:<44} p@3={p3s}  c@10={c10s}  "
            f"{viol_tag}  {elapsed:>5.1f}s{flag_str}")


def _fmt_num(v) -> str:
    return f"{v:.2f}" if isinstance(v, float) else "n/a"


def capture_gemini_cost(run_start_utc: datetime, drain_seconds: float = 4.0) -> dict | None:
    """Query the Supabase `gemini_calls` ledger (PR #85 wrapper) for all
    calls made since run_start_utc and aggregate real cost.

    The wrapper writes each call via a daemon thread, so the last few
    per-query writes may still be in flight when this is invoked. The
    drain sleep lets those settle before the read — 4s is conservative
    at PR #85's per-call latency (~200-500ms per Supabase POST).

    Returns dict with total_usd, call_count, by_model, by_origin, and
    the query window. Returns None on any error so the caller can still
    surface the scoring data — cost capture is best-effort.
    """
    print(f"\n[eval] draining daemon writes ({drain_seconds:.0f}s)...", file=sys.stderr)
    time.sleep(drain_seconds)

    try:
        from api.supabase_client import supabase_get
        rows = supabase_get("gemini_calls", {
            "event_at": f"gte.{run_start_utc.isoformat()}",
            "select": "origin,model,cost_usd_estimate,input_tokens,output_tokens",
            "limit": "10000",
        })
    except Exception as e:
        print(f"[eval] cost capture failed: {type(e).__name__}: {e}", file=sys.stderr)
        return None

    total_usd = 0.0
    by_model: dict[str, dict] = {}
    by_origin: dict[str, dict] = {}
    total_input = 0
    total_output = 0
    for r in rows:
        cost = float(r.get("cost_usd_estimate", 0) or 0)
        input_tok = int(r.get("input_tokens", 0) or 0)
        output_tok = int(r.get("output_tokens", 0) or 0)
        model = r.get("model") or "unknown"
        origin = r.get("origin") or "unknown"

        total_usd += cost
        total_input += input_tok
        total_output += output_tok

        m = by_model.setdefault(model, {"calls": 0, "usd": 0.0, "input_tokens": 0, "output_tokens": 0})
        m["calls"] += 1
        m["usd"] += cost
        m["input_tokens"] += input_tok
        m["output_tokens"] += output_tok

        o = by_origin.setdefault(origin, {"calls": 0, "usd": 0.0})
        o["calls"] += 1
        o["usd"] += cost

    for m in by_model.values():
        m["usd"] = round(m["usd"], 6)
    for o in by_origin.values():
        o["usd"] = round(o["usd"], 6)

    return {
        "total_usd": round(total_usd, 6),
        "call_count": len(rows),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "by_model": by_model,
        "by_origin": by_origin,
        "window_start_utc": run_start_utc.isoformat(),
    }


def print_cost_section(cost: dict | None) -> None:
    print("\n=== ACTUAL COST (from gemini_calls ledger) ===")
    if cost is None:
        print("  cost capture unavailable — see stderr for reason")
        return
    if cost["call_count"] == 0:
        print("  no calls landed in window — daemon writes may still be draining, "
              "or SUPABASE_URL/SUPABASE_SERVICE_KEY not set")
        return
    print(f"Total       ${cost['total_usd']:.4f}   {cost['call_count']} calls   "
          f"in={cost['total_input_tokens']:,} tok   out={cost['total_output_tokens']:,} tok")
    if cost["by_model"]:
        print("\nby model:")
        for model in sorted(cost["by_model"]):
            m = cost["by_model"][model]
            print(f"  {model:<32} {m['calls']:>3} calls   ${m['usd']:.4f}")
    if len(cost["by_origin"]) > 1 or (cost["by_origin"] and "probe" not in cost["by_origin"]):
        print("\nby origin (unexpected if not all 'probe'):")
        for origin in sorted(cost["by_origin"]):
            o = cost["by_origin"][origin]
            print(f"  {origin:<32} {o['calls']:>3} calls   ${o['usd']:.4f}")


def print_summary(overall: dict, by_category: dict, by_confidence: dict) -> None:
    print("\n=== SUMMARY ===")
    o = overall
    print(f"Overall     n={o['n']:>3}   "
          f"p@3_mean={_fmt_num(o['precision_at_3_mean'])}   "
          f"c@10_mean={_fmt_num(o['coverage_at_10_mean'])}   "
          f"violations={o['must_not_appear_violations']}   "
          f"correctly_empty={o['correctly_empty_hit']}/{o['correctly_empty_n']}")

    print("\nby category:")
    for cat in sorted(by_category):
        v = by_category[cat]
        print(f"  {cat:<20} n={v['n']:>3}   "
              f"p@3={_fmt_num(v['precision_at_3_mean'])}   "
              f"c@10={_fmt_num(v['coverage_at_10_mean'])}   "
              f"viol={v['must_not_appear_violations']}")

    print("\nby confidence:")
    for tier in ("high", "medium", "low"):
        if tier in by_confidence:
            v = by_confidence[tier]
            print(f"  {tier:<20} n={v['n']:>3}   "
                  f"p@3={_fmt_num(v['precision_at_3_mean'])}   "
                  f"c@10={_fmt_num(v['coverage_at_10_mean'])}   "
                  f"viol={v['must_not_appear_violations']}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Capture engine quality baseline against curated eval set.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--out", type=Path, default=None,
                   help="Output JSON path. Default: tests/data/eval_baseline_YYYY-MM-DD.json")
    p.add_argument("--categories", default=None,
                   help="Comma-separated category filter (e.g., 'vibe-anchor,known-failure').")
    p.add_argument("--limit", type=int, default=None,
                   help="Only run the first N entries (after category filter). Smoke-test aid.")
    p.add_argument("--quiet", action="store_true", help="Suppress per-query console output.")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print("ERROR: GOOGLE_API_KEY not found in env_variables.env", file=sys.stderr)
        return 2

    print(f"[eval] origin tag: {os.environ.get('ANIME_INTEL_CALL_ORIGIN', '?')!r}", file=sys.stderr)

    title_to_id, id_to_english = load_vault_lookup(DB_PATH)
    n_shows = len(set(title_to_id.values()))
    print(f"[eval] vault: {n_shows:,} distinct mal_ids "
          f"({len(title_to_id):,} title strings across english + romaji)", file=sys.stderr)

    with EVAL_PATH.open(encoding="utf-8") as f:
        eval_data = json.load(f)

    queries = eval_data.get("queries", [])
    if args.categories:
        allowed = {c.strip() for c in args.categories.split(",")}
        queries = [q for q in queries if q.get("category") in allowed]
    if args.limit is not None:
        queries = queries[: args.limit]

    print(f"[eval] running {len(queries)} entries\n", file=sys.stderr)

    # Capture BEFORE engine init so no Gemini calls sneak in ahead of the window.
    run_start_utc = datetime.now(timezone.utc)

    engine = RecommendationEngine(api_key=api_key)

    per_query: list[dict] = []
    for entry in queries:
        try:
            pool, elapsed, meta = dispatch(engine, entry)
        except Exception as e:
            print(f"[eval] EXCEPTION on {entry.get('id')}: {type(e).__name__}: {e}",
                  file=sys.stderr)
            pool, elapsed, meta = [], 0.0, {"success": False, "error": str(e),
                                            "low_confidence": False}

        returned_ids = extract_returned_ids(pool, title_to_id)
        scored = score_entry(entry, returned_ids, title_to_id)

        record = {
            "id": entry.get("id"),
            "category": entry.get("category"),
            "confidence": entry.get("confidence"),
            "mode": entry.get("mode"),
            "returned_ids": returned_ids,
            "returned_titles": [id_to_english.get(i, f"<id {i}>") for i in returned_ids],
            "elapsed_sec": round(elapsed, 2),
            "engine_meta": meta,
            **scored,
        }
        per_query.append(record)

        if not args.quiet:
            print(format_console_line(entry, scored, elapsed, meta))

    overall = aggregate(per_query, key_func=None)["overall"]
    by_category = aggregate(per_query, key_func=lambda q: q["category"])
    by_confidence = aggregate(per_query, key_func=lambda q: q["confidence"])

    gemini_cost = capture_gemini_cost(run_start_utc)

    output = {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "eval_schema_version": eval_data.get("schema_version"),
        "vault_row_count": n_shows,
        "n_queries_run": len(per_query),
        "overall": overall,
        "by_category": by_category,
        "by_confidence": by_confidence,
        "gemini_cost_actual": gemini_cost,
        "per_query": per_query,
    }

    if args.out is None:
        date_str = datetime.now().strftime("%Y-%m-%d")
        args.out = ROOT_DIR / "tests" / "data" / f"eval_baseline_{date_str}.json"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print_summary(overall, by_category, by_confidence)
    print_cost_section(gemini_cost)
    print(f"\n[eval] wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
