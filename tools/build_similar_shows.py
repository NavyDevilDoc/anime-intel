"""Item 3 Phase C — Similar-shows generator CLI.

Runs the ranking pipeline in `analysis/similar_shows.py` over every show
in the vault and emits one lazy-loadable JSON file per anime under
`frontend/public/data/similar/{mal_id}.json`. The frontend Encyclopedia
component fetches these on demand — one small file per Encyclopedia
lookup, zero backend involvement.

Usage:
    python tools/build_similar_shows.py                    # Full run
    python tools/build_similar_shows.py --limit 20         # Smoke test
    python tools/build_similar_shows.py --only-missing     # Skip already-emitted files
    python tools/build_similar_shows.py --top-k 8          # Override recs per show
    python tools/build_similar_shows.py --out DIR          # Custom output dir

Zero external calls (pure FAISS + SQL). Wallclock ~5-10 min for the
full ~6,300-show vault.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Force UTF-8 stdout for titles like "∀ Gundam" that break Windows cp1252
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from analysis import similar_shows, vector_store

DEFAULT_DB = ROOT / "data" / "anime_intelligence_v2.db"
DEFAULT_OUT_DIR = ROOT / "frontend" / "public" / "data" / "similar"


def slugify(mal_id: int, title: str) -> str:
    """Match the pattern used by tools/export_seo_data.py._slugify and
    the frontend's Encyclopedia.tsx slugify() so URLs are consistent
    across surfaces."""
    if not title:
        return f"{mal_id}"
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s]+", "-", slug.strip())
    slug = re.sub(r"-+", "-", slug)
    slug = slug.strip("-")
    return f"{mal_id}-{slug}" if slug else f"{mal_id}"


def build_output_for_show(
    ctx: similar_shows.EngineContext,
    reference_id: int,
    top_k: int,
    generated_at: str,
) -> dict | None:
    """Build the JSON payload for a single show. Returns None if the
    show can't be processed (missing from FAISS metadata / no lookup)."""
    ref_title = ctx.id_to_title.get(reference_id)
    if not ref_title:
        return None

    recs = similar_shows.build_recommendations(
        ctx, reference_id, top_k=top_k,
        faiss_search_fn=vector_store._search_faiss_raw,
    )
    if not recs:
        return None

    # Trim internal scoring fields — keep them out of the frontend
    # payload to reduce noise. If debugging is ever needed, uncomment
    # the raw fields or add a --verbose flag.
    #
    # personnel_hook: string like "Also directed by X" when the reference
    #   and candidate share a Director / Music composer / Original Creator;
    #   None otherwise. Frontend renders it as the primary provenance line.
    # thematic_hint: raw comma-separated theme descriptor (bare, no label);
    #   the frontend labels it "Theme:" at render time. Populated when
    #   consensus_json.thematic_vibe is present; None otherwise. When
    #   personnel_hook is set AND thematic_hint is set, the card shows
    #   both lines — the personnel connection AND the show's own theme,
    #   so the "why you'd like this" story is complete without asserting
    #   thematic similarity we can't always defend.
    payload_recs = []
    for rec in recs:
        payload_recs.append({
            "anime_id": rec["anime_id"],
            "title": rec["title"],
            "slug": slugify(rec["anime_id"], rec["title"]),
            "release_year": rec["release_year"],
            "mal_score": rec["mal_score"],
            "personnel_hook": rec["personnel_hook"],
            "thematic_hint": rec["thematic_hint"],
        })

    return {
        "reference_id": reference_id,
        "reference_title": ref_title,
        "generated_at": generated_at,
        "recommendations": payload_recs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help=f"SQLite path. Default: {DEFAULT_DB}")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR,
                        help=f"Output directory. Default: {DEFAULT_OUT_DIR}")
    parser.add_argument("--top-k", type=int, default=6,
                        help="Recommendations per show. Default: 6")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only process the first N shows. For smoke testing.")
    parser.add_argument("--only-missing", action="store_true",
                        help="Skip shows whose output file already exists.")
    args = parser.parse_args()

    if not args.db.exists():
        print(f"ERROR: DB not found at {args.db}", file=sys.stderr)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    print(f"[build] Loading engine context from {args.db}...")
    t_load = time.perf_counter()
    ctx = similar_shows.load_engine_context(
        str(args.db), faiss_loader=vector_store._load_index
    )
    print(f"[build] Context loaded in {time.perf_counter() - t_load:.1f}s.  "
          f"{len(ctx.title_lookup):,} shows in vault.  "
          f"{sum(len(v) for v in ctx.staff_by_anime.values()):,} staff attributions.")

    all_ids = sorted(ctx.id_to_title.keys())
    if args.limit is not None:
        all_ids = all_ids[: args.limit]

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n_processed = 0
    n_skipped = 0
    n_written = 0
    n_no_recs = 0
    t_start = time.perf_counter()

    for i, anime_id in enumerate(all_ids, 1):
        out_path = args.out / f"{anime_id}.json"
        if args.only_missing and out_path.exists():
            n_skipped += 1
            continue

        payload = build_output_for_show(ctx, anime_id, args.top_k, generated_at)
        if payload is None:
            n_no_recs += 1
        else:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            n_written += 1

        n_processed += 1
        if i % 500 == 0 or i == len(all_ids):
            elapsed = time.perf_counter() - t_start
            rate = i / elapsed if elapsed > 0 else 0
            eta = (len(all_ids) - i) / rate if rate > 0 else 0
            print(f"[build] {i}/{len(all_ids)}  written={n_written} "
                  f"no_recs={n_no_recs} skipped={n_skipped}  "
                  f"rate={rate:.0f}/s  eta={eta:.0f}s")

    print()
    print("=" * 70)
    total_elapsed = time.perf_counter() - t_start
    print(f"COMPLETE. Processed {n_processed} shows in {total_elapsed:.1f}s.")
    print(f"  Written:        {n_written}")
    print(f"  No recommendations: {n_no_recs}")
    print(f"  Skipped (--only-missing): {n_skipped}")
    print(f"  Output dir: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
