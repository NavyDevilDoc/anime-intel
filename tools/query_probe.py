"""
tools/query_probe.py — direct-import query diagnostic for engine iteration.

Bypasses the FastAPI/HTTP layer and calls the recommendation engine directly,
so the dev loop is:  edit engine code → re-run probe → read output.  No
uvicorn restart, no frontend spin-up, no middleware / rate-limit / quota
plumbing to work around.

Usage (from repo root):

    # Search mode (default) — auto-classified intent + Fusion/Constraint/
    # Narrative rerank based on what the query looks like:
    python tools/query_probe.py "sincere romcom without fanservice"

    # Search mode with a specific lens:
    python tools/query_probe.py "top sports anime" --lens "Objective Rankings"

    # DNA / Shows-Like-This mode — 1-3 reference titles:
    python tools/query_probe.py --dna "Steins;Gate" "Psycho-Pass"

Gemini calls made by this tool are tagged `ANIME_INTEL_CALL_ORIGIN=probe`
so their spend doesn't pollute the production attribution in the
`gemini_calls` ledger. Override via env var if you need to attribute a probe
run to a different origin bucket.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

# --- SYSTEM PATH ANCHORING (mirror the pattern used across tools/) ---
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent if SCRIPT_DIR.name == "tools" else SCRIPT_DIR
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Tag Gemini calls before importing gemini_client so the module picks up
# the origin at first access. setdefault so operators can override on the
# command line (e.g., `ANIME_INTEL_CALL_ORIGIN=dev python tools/...`).
os.environ.setdefault("ANIME_INTEL_CALL_ORIGIN", "probe")

from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT_DIR, "env_variables.env"))

from analysis.recommendation_engine import RecommendationEngine  # noqa: E402


def _fmt_pool_entry(idx, entry):
    """Pretty-print one reranked candidate. Field names mirror the engine's
    scored_pool shape: `profile.title` for the show, `ai_reasoning` for the
    reranker's explanation."""
    profile = entry.get("profile") or {}
    title = profile.get("title") or profile.get("english_title") or "<no title>"
    confidence = entry.get("match_confidence", "?")
    reasoning = (entry.get("ai_reasoning") or "").strip()
    query_con = (entry.get("query_relevant_con") or "").strip()
    spoiler = entry.get("contains_spoiler", False)

    lines = [f"  [{idx:>2}] {title}   ({confidence}% match)"]
    if reasoning:
        lines.append(f"        why: {reasoning}")
    if query_con:
        lines.append(f"        con: {query_con}")
    if spoiler:
        lines.append("        !! contains spoiler")
    return "\n".join(lines)


def run_search(engine, query, lens):
    print(f"\n>>> SEARCH  lens={lens!r}  query={query!r}")
    t0 = time.perf_counter()
    result = engine.fetch_vault_pool(query, lens_name=lens)
    elapsed = time.perf_counter() - t0

    if not result.get("success"):
        print(f"    failed in {elapsed:.1f}s: {result.get('error', '<no error>')}")
        if result.get("low_confidence"):
            print("    (low_confidence flag set — engine is uncertain)")
        return

    pool = result.get("pool", [])
    print(
        f"    {len(pool)} result(s) in {elapsed:.1f}s"
        + ("   [low_confidence]" if result.get("low_confidence") else "")
    )
    for i, entry in enumerate(pool, start=1):
        print(_fmt_pool_entry(i, entry))


def run_dna(engine, titles):
    print(f"\n>>> DNA     titles={titles!r}")
    t0 = time.perf_counter()
    result = engine.execute_dna_triangulation(titles)
    elapsed = time.perf_counter() - t0

    if not result.get("success"):
        print(f"    failed in {elapsed:.1f}s: {result.get('error', '<no error>')}")
        for note in result.get("title_resolution_notes") or []:
            print(f"    note: {note}")
        if result.get("low_confidence"):
            print("    (low_confidence flag set — engine is uncertain)")
        return

    summary = result.get("intersection_summary")
    if summary:
        print(f"    summary: {summary}")
    for note in result.get("title_resolution_notes") or []:
        print(f"    note: {note}")

    data = result.get("data", [])
    print(
        f"    {len(data)} match(es) in {elapsed:.1f}s"
        + ("   [low_confidence]" if result.get("low_confidence") else "")
    )
    for i, entry in enumerate(data, start=1):
        print(_fmt_pool_entry(i, entry))


def build_parser():
    p = argparse.ArgumentParser(
        description="Direct-import query diagnostic for the recommendation engine.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "query",
        nargs="+",
        help="Search query (search mode), or 1-3 reference titles (--dna mode).",
    )
    p.add_argument(
        "--dna",
        action="store_true",
        help="Run in Shows-Like-This mode. Positional args are treated as reference titles.",
    )
    p.add_argument(
        "--lens",
        default="Intelligent Search",
        help='Search lens (search mode only). Default: "Intelligent Search". '
        'The only other supported value is "Objective Rankings", which '
        'bypasses the LLM reranker and sorts by Bayesian quality_score. '
        '(Discovery intent — hidden_gems, crowd_pleasers, polarizing, etc. — '
        'is auto-detected from query text inside Intelligent Search; no '
        'lens argument needed.)',
    )
    p.add_argument(
        "--raw",
        action="store_true",
        help="Also dump the full engine response as JSON at the end (for debugging).",
    )
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print("ERROR: GOOGLE_API_KEY not found in env_variables.env", file=sys.stderr)
        return 2

    origin = os.environ.get("ANIME_INTEL_CALL_ORIGIN", "?")
    print(f"[query_probe] engine origin tag: {origin!r}")

    engine = RecommendationEngine(api_key=api_key)

    if args.dna:
        titles = args.query[:3]
        if len(titles) < 1:
            print("ERROR: --dna requires at least 1 reference title.", file=sys.stderr)
            return 2
        run_dna(engine, titles)
    else:
        # In search mode, join the positional tokens back into one query string
        # so callers don't have to quote (`python tools/query_probe.py sincere romcom`
        # works the same as `... "sincere romcom"`).
        query = " ".join(args.query).strip()
        if not query:
            print("ERROR: empty query.", file=sys.stderr)
            return 2
        run_search(engine, query, args.lens)

    if args.raw:
        # Re-run to capture the raw dict for JSON dump — the engine mutates
        # some internal state, so a second call is honest about "what does
        # the engine return" rather than trying to recover state from
        # the printout above.
        if args.dna:
            raw = engine.execute_dna_triangulation(args.query[:3])
        else:
            raw = engine.fetch_vault_pool(" ".join(args.query).strip(), lens_name=args.lens)
        print("\n--- raw response ---")
        print(json.dumps(raw, indent=2, default=str))

    return 0


if __name__ == "__main__":
    sys.exit(main())
