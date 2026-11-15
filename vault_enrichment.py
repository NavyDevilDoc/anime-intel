"""
MODULE: vault_enrichment.py
FUNCTION: Post-ingestion enrichment orchestrator.

After vault_manager.py ingests a season (metadata + reviews + consensus),
this script runs all enrichment steps in sequence:

    1. Genres        (Jikan API — fast, ~0.5s/show)
    2. Narratives    (Gemini API — slow, ~0.5s/show, consumes quota)
    3. FAISS sync    (local — rebuilds vectors for enriched shows)

Each step is idempotent and resumable: it targets only shows with NULL
values in the relevant column, so the script can be re-run safely after
a crash or rate limit interruption.

Usage:
    python vault_enrichment.py                   # Full enrichment
    python vault_enrichment.py --test            # 5 shows per step (dry run)
    python vault_enrichment.py --skip-narratives # Genres only
    python vault_enrichment.py --stats           # Show coverage gaps, no processing
"""

import os
import sys
import sqlite3
import json
import time
import logging
import argparse

# --- BULLETPROOF PATH ANCHORING ---
SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
if os.path.exists(os.path.join(SCRIPT_DIR, "data")):
    ROOT_DIR = SCRIPT_DIR
else:
    ROOT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))

if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

DB_PATH = os.path.join(ROOT_DIR, "data", "anime_intelligence_v2.db")


# =====================================================================
# STEP 1: GENRES (Jikan API)
# =====================================================================

def enrich_genres(conn, test_mode=False):
    """Fetch MAL genre/theme/demographic tags for shows missing them."""
    from src.jikan_client import JikanClient

    cursor = conn.cursor()

    # Ensure column exists
    cursor.execute("PRAGMA table_info(anime_info)")
    columns = {row[1] for row in cursor.fetchall()}
    if "genres" not in columns:
        cursor.execute("ALTER TABLE anime_info ADD COLUMN genres TEXT")
        conn.commit()

    cursor.execute(
        "SELECT id, english_title FROM anime_info WHERE genres IS NULL ORDER BY id"
    )
    rows = cursor.fetchall()

    if not rows:
        logger.info("Genres: All shows already have genre data.")
        return []

    if test_mode:
        rows = rows[:5]

    logger.info(f"Genres: {len(rows)} shows to process.")

    client = JikanClient()
    client.rate_limit_delay = 0.5
    enriched_ids = []

    for i, (mal_id, title) in enumerate(rows, 1):
        result = client.get_anime_genres(mal_id)

        if result.is_error:
            logger.warning(f"  [{i}/{len(rows)}] {title}: FAILED — {result.summary}")
            continue

        tags = result.value
        genres_str = ", ".join(tags) if tags else ""
        cursor.execute(
            "UPDATE anime_info SET genres = ? WHERE id = ?",
            (genres_str, mal_id),
        )
        conn.commit()
        enriched_ids.append(mal_id)
        logger.info(f"  [{i}/{len(rows)}] {title}: {genres_str or '(no tags)'}")

    logger.info(f"Genres: Done. {len(enriched_ids)} enriched.")
    return enriched_ids




# =====================================================================
# STEP 2: MAL RATINGS (Jikan API)
# =====================================================================

def enrich_ratings(conn, test_mode=False):
    """Fetch MAL content-rating string for shows missing one (three-state)."""
    from src.jikan_client import JikanClient

    cursor = conn.cursor()

    # Ensure column exists (idempotent — mirrors backfill_ratings.ensure_rating_column)
    cursor.execute("PRAGMA table_info(anime_info)")
    columns = {row[1] for row in cursor.fetchall()}
    if "mal_rating" not in columns:
        cursor.execute("ALTER TABLE anime_info ADD COLUMN mal_rating TEXT")
        conn.commit()

    cursor.execute(
        "SELECT id, english_title FROM anime_info "
        "WHERE mal_rating IS NULL ORDER BY id"
    )
    rows = cursor.fetchall()

    if not rows:
        logger.info("Ratings: All shows already have rating data.")
        return []

    if test_mode:
        rows = rows[:5]

    logger.info(f"Ratings: {len(rows)} shows to process.")

    client = JikanClient()
    client.rate_limit_delay = 0.5
    enriched_ids = []

    for i, (mal_id, title) in enumerate(rows, 1):
        result = client.get_anime_rating(mal_id)

        if result.is_error:
            logger.warning(f"  [{i}/{len(rows)}] {title}: FAILED — {result.summary}")
            continue

        rating = result.value
        cursor.execute(
            "UPDATE anime_info SET mal_rating = ? WHERE id = ?",
            (rating, mal_id),
        )
        conn.commit()
        enriched_ids.append(mal_id)
        display = rating if rating else "(no rating set on MAL)"
        logger.info(f"  [{i}/{len(rows)}] {title}: {display}")

    logger.info(f"Ratings: Done. {len(enriched_ids)} enriched.")
    return enriched_ids


# =====================================================================
# STEP 3: MAL SUBTYPES (Jikan API)
# =====================================================================

def enrich_subtypes(conn, test_mode=False):
    """Fetch MAL subtype (TV/Movie/OVA/Special/TV Special/ONA/Music/PV/CM)
    for shows missing one (three-state, mirrors enrich_ratings).

    Seeds from data/series_subtype_cache.json before hitting Jikan to
    skip rows whose subtype was already fetched during series clustering.
    """
    from src.jikan_client import JikanClient

    cursor = conn.cursor()

    # Ensure column exists (idempotent — mirrors backfill_subtypes.ensure_subtype_column)
    cursor.execute("PRAGMA table_info(anime_info)")
    columns = {row[1] for row in cursor.fetchall()}
    if "subtype" not in columns:
        cursor.execute("ALTER TABLE anime_info ADD COLUMN subtype TEXT")
        conn.commit()

    # Seed from clustering subtype cache before Jikan. Cache values are
    # the clustering algorithm's view — already vetted during series
    # enrichment — so seeding ensures the new column agrees with the
    # cluster shape AND avoids redundant Jikan calls.
    cache = _load_subtype_cache(SERIES_SUBTYPE_CACHE_PATH)
    if cache:
        cursor.execute("SELECT id FROM anime_info WHERE subtype IS NULL")
        null_ids = {row[0] for row in cursor.fetchall()}
        seeded = null_ids & set(cache.keys())
        for mal_id in seeded:
            cursor.execute(
                "UPDATE anime_info SET subtype = ? WHERE id = ?",
                (cache[mal_id], mal_id),
            )
        conn.commit()
        if seeded:
            logger.info(f"Subtypes: seeded {len(seeded)} from clustering cache.")

    cursor.execute(
        "SELECT id, english_title FROM anime_info "
        "WHERE subtype IS NULL ORDER BY id"
    )
    rows = cursor.fetchall()

    if not rows:
        logger.info("Subtypes: All shows already have subtype data.")
        return []

    if test_mode:
        rows = rows[:5]

    logger.info(f"Subtypes: {len(rows)} shows to process.")

    client = JikanClient()
    client.rate_limit_delay = 0.5
    enriched_ids = []

    for i, (mal_id, title) in enumerate(rows, 1):
        result = client.get_anime_detail(mal_id)

        if result.is_error:
            logger.warning(f"  [{i}/{len(rows)}] {title}: FAILED — {result.summary}")
            continue

        subtype = result.value.media_type
        cursor.execute(
            "UPDATE anime_info SET subtype = ? WHERE id = ?",
            (subtype, mal_id),
        )
        conn.commit()
        enriched_ids.append(mal_id)
        display = subtype if subtype else "(no subtype set on MAL)"
        logger.info(f"  [{i}/{len(rows)}] {title}: {display}")

    logger.info(f"Subtypes: Done. {len(enriched_ids)} enriched.")
    return enriched_ids


# =====================================================================
# STEP 4: SERIES CLUSTERING (Jikan relations + Phase A ruleset)
# =====================================================================

SERIES_RELATIONS_CACHE_PATH = os.path.join(
    ROOT_DIR, "data", "series_relations_cache.jsonl"
)
SERIES_SUBTYPE_CACHE_PATH = os.path.join(
    ROOT_DIR, "data", "series_subtype_cache.json"
)
SERIES_OVERRIDES_PATH = os.path.join(
    ROOT_DIR, "data", "series_cluster_overrides.json"
)

# Phase A's 20 anchor IDs (+ the Tenchi Muyo re-probe). Used as the
# universe for --series-test, which builds clusters from only these
# rows so a quick sanity-check doesn't require the full 60-70min fetch.
SERIES_TEST_ANCHORS = {
    918, 16498, 6336, 31251, 52991,       # long-running franchises
    121, 5114, 136, 11061,                # reboot pairs
    1575, 8888,                           # Code Geass + Akito
    1088, 539,                            # multiverses (Macross + Tenchi)
    9756,                                  # Madoka
    38671, 31240, 31964, 38000, 5680, 14719,  # modern franchises
}


def _load_relations_cache(path):
    """Read JSONL cache. Each line: {"mal_id": int, "relations": [...]}."""
    cache = {}
    if not os.path.exists(path):
        return cache
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                cache[int(rec["mal_id"])] = rec["relations"]
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    return cache


def _append_relations_cache(path, mal_id, relations):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"mal_id": int(mal_id), "relations": relations},
                            ensure_ascii=False))
        fh.write("\n")


def _load_subtype_cache(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}
    return {int(k): v for k, v in raw.items()}


def _save_subtype_cache(path, cache):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({str(k): v for k, v in cache.items()}, fh, indent=2)


def _load_overrides(path):
    if not os.path.exists(path):
        return {"force_same_cluster": [], "force_separate_cluster": []}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def enrich_series(conn, test_mode=False, dry_run=False):
    """Compute series_id for every row in anime_info.

    Three-phase: (1) fetch /relations per row with JSONL cache, (2) fetch
    Alt-Version target subtypes with JSON cache, (3) cluster in memory
    via analysis.series_clustering. dry_run prints the proposed clusters
    without writing to the DB. test_mode restricts to Phase A's anchor
    set (~20 franchises) so the slow fetch is bounded.
    """
    from src.jikan_client import JikanClient
    from analysis.series_clustering import (
        assign_series_ids,
        HYBRID_RELATION,
    )

    cursor = conn.cursor()

    # 1. Ensure column exists (idempotent)
    cursor.execute("PRAGMA table_info(anime_info)")
    columns = {row[1] for row in cursor.fetchall()}
    if "series_id" not in columns:
        cursor.execute("ALTER TABLE anime_info ADD COLUMN series_id TEXT")
        conn.commit()
        logger.info("Series: added series_id column to anime_info.")

    # 2. Load vault metadata
    cursor.execute("SELECT id, english_title, release_year FROM anime_info")
    rows = cursor.fetchall()
    vault_ids = {r[0] for r in rows}
    title_by_id = {r[0]: r[1] for r in rows}
    year_by_id = {r[0]: r[2] for r in rows}

    fetch_universe = SERIES_TEST_ANCHORS & vault_ids if test_mode else vault_ids
    logger.info(
        f"Series: vault has {len(vault_ids):,} rows; "
        f"fetch universe = {len(fetch_universe):,} "
        f"({'TEST MODE' if test_mode else 'FULL'})."
    )

    # 3. Phase 1: fetch relations
    relations_cache = _load_relations_cache(SERIES_RELATIONS_CACHE_PATH)
    client = JikanClient()
    client.rate_limit_delay = 0.5

    def _fetch_missing(targets):
        cached_ids = relations_cache.keys() & targets
        remaining = sorted(targets - cached_ids)
        if not remaining:
            return
        logger.info(
            f"  cache hits: {len(cached_ids):,} / pending: {len(remaining):,}"
        )
        for i, mal_id in enumerate(remaining, 1):
            result = client.get_anime_relations(mal_id)
            if result.is_error:
                logger.warning(
                    f"  [{i}/{len(remaining)}] {title_by_id.get(mal_id, '?')}: "
                    f"FAILED — {result.summary}, treating as no relations"
                )
            # `.value` is [] on error already (JikanFetchResult default),
            # so this covers both success-empty and error paths.
            relations_cache[mal_id] = result.value
            _append_relations_cache(SERIES_RELATIONS_CACHE_PATH, mal_id, result.value)
            if i % 50 == 0 or i == len(remaining):
                logger.info(
                    f"  [{i}/{len(remaining)}] fetched relations "
                    f"(cache size: {len(relations_cache):,})"
                )

    _fetch_missing(fetch_universe)

    # 3b. Test-mode expansion: after the anchor fetch, expand the universe
    # to include every vault entry reachable from an anchor's relations.
    # Without this, anchors' sequels/prequels point outside the test set
    # and the clustering produces all-singletons.
    if test_mode:
        expanded = set(fetch_universe)
        for source_id in fetch_universe:
            for rel in relations_cache.get(source_id, []):
                for entry in rel.get("entry", []):
                    if entry.get("type") != "anime":
                        continue
                    tid = entry.get("mal_id")
                    if tid is not None and tid in vault_ids:
                        expanded.add(int(tid))
        if expanded != fetch_universe:
            logger.info(
                f"Series: test mode expanded universe {len(fetch_universe)} "
                f"→ {len(expanded)} (anchor + reachable vault targets)."
            )
            fetch_universe = expanded
            _fetch_missing(fetch_universe)

    # 4. Phase 2: fetch Alt Version endpoint subtypes.
    # Capture BOTH source and target subtype for every Alt Version edge
    # — the pair-level HYBRID rule consults both. Bidirectional MAL data
    # usually covers both anyway, but asymmetric edges (where only one
    # direction is annotated) need the explicit source-side fetch.
    subtype_cache = _load_subtype_cache(SERIES_SUBTYPE_CACHE_PATH)
    alt_targets = set()
    for source_id, rels in relations_cache.items():
        if source_id not in fetch_universe:
            continue
        for rel in rels:
            if rel.get("relation", "").strip().lower() != HYBRID_RELATION:
                continue
            for entry in rel.get("entry", []):
                if entry.get("type") != "anime":
                    continue
                tid = entry.get("mal_id")
                if tid is not None:
                    alt_targets.add(int(tid))
                    alt_targets.add(int(source_id))

    subtype_remaining = sorted(alt_targets - subtype_cache.keys())
    logger.info(
        f"Series: {len(alt_targets):,} unique Alt Version targets; "
        f"{len(subtype_remaining):,} subtype lookups remaining."
    )

    if subtype_remaining:
        client = JikanClient()
        client.rate_limit_delay = 0.5
        for i, mal_id in enumerate(subtype_remaining, 1):
            result = client.get_anime_detail(mal_id)
            if result.is_error:
                # Failure: leave un-cached so a retry will pick it up.
                # The clustering treats missing subtype as non-TV → SAME,
                # which is the safer default per Phase A findings.
                logger.warning(
                    f"  subtype [{i}/{len(subtype_remaining)}] mal_id={mal_id}: "
                    f"FAILED — {result.summary}"
                )
                continue
            subtype_cache[mal_id] = result.value.media_type
            if i % 25 == 0 or i == len(subtype_remaining):
                _save_subtype_cache(SERIES_SUBTYPE_CACHE_PATH, subtype_cache)
                logger.info(
                    f"  subtype [{i}/{len(subtype_remaining)}] cached "
                    f"(total: {len(subtype_cache):,})"
                )
        _save_subtype_cache(SERIES_SUBTYPE_CACHE_PATH, subtype_cache)

    # 5. Phase 3: cluster
    overrides = _load_overrides(SERIES_OVERRIDES_PATH)
    cluster_universe = fetch_universe
    cluster_relations = {
        mid: rels for mid, rels in relations_cache.items()
        if mid in cluster_universe
    }
    series_id_by_member = assign_series_ids(
        vault_ids=set(cluster_universe),
        relations_by_source=cluster_relations,
        subtype_lookup=subtype_cache,
        year_by_id=year_by_id,
        overrides=overrides,
    )

    # 6. Phase 4: report and (optionally) write
    multi_clusters = {}
    for mal_id, sid in series_id_by_member.items():
        multi_clusters.setdefault(sid, []).append(mal_id)
    multi_clusters = {k: v for k, v in multi_clusters.items() if len(v) > 1}

    total_clusters = len({sid for sid in series_id_by_member.values()})
    standalone = len(series_id_by_member) - sum(len(m) for m in multi_clusters.values())

    print(f"\n{'='*55}")
    print(f"  SERIES CLUSTERING REPORT  ({'DRY RUN' if dry_run else 'WRITE'})")
    print(f"{'='*55}")
    print(f"  Rows clustered:        {len(series_id_by_member):,}")
    print(f"  Distinct series_ids:   {total_clusters:,}")
    print(f"  Multi-entry clusters:  {len(multi_clusters):,}")
    print(f"  Standalone rows:       {standalone:,}")
    if multi_clusters:
        sizes = sorted((len(m) for m in multi_clusters.values()), reverse=True)
        print(f"  Top 5 cluster sizes:   {sizes[:5]}")
        print()
        print("  Largest 10 clusters (canonical -> members):")
        for sid, members in sorted(
            multi_clusters.items(), key=lambda kv: -len(kv[1])
        )[:10]:
            print(f"    series_id={sid} (n={len(members)})")
            for m in sorted(members, key=lambda x: (year_by_id.get(x, 9999), x))[:6]:
                title = title_by_id.get(m, "?")
                year = year_by_id.get(m, "?")
                print(f"      {m:>6}  [{year}]  {title}")
            if len(members) > 6:
                print(f"      ... and {len(members) - 6} more")
    print(f"{'='*55}\n")

    if dry_run:
        logger.info("Series: dry-run complete — no DB writes.")
        return []

    # 7. Write series_id per row
    write_rows = list(series_id_by_member.items())
    cursor.executemany(
        "UPDATE anime_info SET series_id = ? WHERE id = ?",
        [(sid, mal_id) for mal_id, sid in write_rows],
    )
    conn.commit()
    logger.info(f"Series: wrote series_id for {len(write_rows):,} rows.")

    return [mid for mid, _ in write_rows]


# =====================================================================
# STEP 4: NARRATIVE DISTILLATION (Gemini API)
# =====================================================================

def enrich_narratives(conn, test_mode=False):
    """Run Gemini narrative distillation on eligible shows missing narrative_json."""
    from analysis.narrative_distiller import NarrativeDistiller

    MIN_EPISODES_WITH_TEXT = 3

    cursor = conn.cursor()

    # Ensure column exists
    cursor.execute("PRAGMA table_info(anime_info)")
    columns = {row[1] for row in cursor.fetchall()}
    if "narrative_json" not in columns:
        cursor.execute("ALTER TABLE anime_info ADD COLUMN narrative_json TEXT")
        conn.commit()

    # Find eligible candidates
    cursor.execute(
        "SELECT id, english_title, mal_synopsis, episode_synopses "
        "FROM anime_info "
        "WHERE episode_synopses IS NOT NULL "
        "AND episode_synopses != '[]' "
        "AND narrative_json IS NULL "
        "ORDER BY scored_by DESC"
    )
    rows = cursor.fetchall()

    candidates = []
    for row in rows:
        mal_id, title, synopsis, ep_blob = row
        try:
            episodes = json.loads(ep_blob)
            with_text = sum(1 for ep in episodes if ep.get("synopsis", "").strip())
            if with_text >= MIN_EPISODES_WITH_TEXT:
                candidates.append(row)
        except (json.JSONDecodeError, TypeError):
            pass

    if not candidates:
        logger.info("Narratives: All eligible shows already have narrative data.")
        return []

    if test_mode:
        candidates = candidates[:5]

    logger.info(f"Narratives: {len(candidates)} shows to process.")

    # Load API key
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        env_path = os.path.join(ROOT_DIR, "env_variables.env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("GOOGLE_API_KEY="):
                        api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not api_key:
        logger.error("Narratives: GOOGLE_API_KEY not found. Skipping.")
        return []

    distiller = NarrativeDistiller(api_key)
    enriched_ids = []
    failed = 0
    start_time = time.time()

    for i, (mal_id, title, mal_synopsis, ep_blob) in enumerate(candidates, 1):
        episodes = json.loads(ep_blob)

        result = distiller.distill_narrative(
            title=title,
            episode_synopses=episodes,
            mal_synopsis=mal_synopsis,
        )

        if result is not None:
            cursor.execute(
                "UPDATE anime_info SET narrative_json = ? WHERE id = ?",
                (json.dumps(result), mal_id),
            )
            conn.commit()
            enriched_ids.append(mal_id)

            arcs = len(result.get("narrative_arcs", []))
            elapsed = time.time() - start_time
            eta_seconds = (elapsed / i) * (len(candidates) - i) if i > 0 else 0
            eta_m = int(eta_seconds // 60)

            logger.info(
                f"  [{i}/{len(candidates)}] {title}: {arcs} arcs | "
                f"OK:{len(enriched_ids)} FAIL:{failed} | ETA: {eta_m}m"
            )
        else:
            failed += 1
            logger.warning(f"  [{i}/{len(candidates)}] {title}: FAILED")

        time.sleep(0.5)

    elapsed_m = int((time.time() - start_time) // 60)
    logger.info(f"Narratives: Done in {elapsed_m}m. {len(enriched_ids)} enriched, {failed} failed.")
    return enriched_ids


# =====================================================================
# STEP 5: SEO DATA EXPORT
# =====================================================================

def export_seo_data(conn):
    """Export all anime profiles to JSON for build-time SEO page generation."""
    from tools.export_seo_data import export_profiles, write_json

    data = export_profiles(DB_PATH)
    size_mb = write_json(data)
    logger.info(f"SEO export: {data['count']:,} profiles ({size_mb:.1f} MB)")


# =====================================================================
# STATS
# =====================================================================

def print_enrichment_stats(conn):
    """Print coverage gaps across all enrichment dimensions."""
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM anime_info")
    total = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM anime_info WHERE genres IS NULL")
    missing_genres = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM anime_info WHERE narrative_json IS NOT NULL")
    has_narrative = cursor.fetchone()[0]

    cursor.execute(
        "SELECT COUNT(*) FROM anime_info "
        "WHERE episode_synopses IS NOT NULL AND episode_synopses != '[]' "
        "AND narrative_json IS NULL"
    )
    narrative_eligible = cursor.fetchone()[0]

    cursor.execute("PRAGMA table_info(anime_info)")
    schema_cols = {row[1] for row in cursor.fetchall()}
    has_rating_column = "mal_rating" in schema_cols
    if has_rating_column:
        cursor.execute("SELECT COUNT(*) FROM anime_info WHERE mal_rating IS NULL")
        missing_ratings = cursor.fetchone()[0]
    else:
        missing_ratings = None
    has_subtype_column = "subtype" in schema_cols
    if has_subtype_column:
        cursor.execute("SELECT COUNT(*) FROM anime_info WHERE subtype IS NULL")
        missing_subtypes = cursor.fetchone()[0]
    else:
        missing_subtypes = None

    print(f"\n{'='*55}")
    print(f"  Vault Enrichment Coverage ({total:,} total shows)")
    print(f"{'='*55}")
    print(f"  Genres:          {total - missing_genres:>6,} / {total:,}  ({missing_genres:,} missing)")
    print(f"  Narratives:      {has_narrative:>6,} / {total:,}  ({narrative_eligible:,} eligible remaining)")
    if missing_ratings is None:
        print(f"  MAL Ratings:     column not yet present")
    else:
        print(f"  MAL Ratings:     {total - missing_ratings:>6,} / {total:,}  ({missing_ratings:,} missing)")
    if missing_subtypes is None:
        print(f"  MAL Subtypes:    column not yet present")
    else:
        print(f"  MAL Subtypes:    {total - missing_subtypes:>6,} / {total:,}  ({missing_subtypes:,} missing)")
    print(f"{'='*55}\n")


# =====================================================================
# FAISS INDEX SYNC
# =====================================================================

def sync_faiss_index(enriched_ids):
    """Update the FAISS vector index for enriched shows."""
    if not enriched_ids:
        logger.info("FAISS: No shows to sync.")
        return

    from analysis.vector_store import update_index

    # Deduplicate
    unique_ids = list(set(enriched_ids))
    logger.info(f"FAISS: Syncing {len(unique_ids)} enriched shows...")
    try:
        count = update_index(unique_ids)
        logger.info(f"FAISS: Done. {count} vectors updated.")
    except Exception as e:
        logger.error(f"FAISS: Sync failed: {e}")
        logger.error("  The index may be stale. Run build_index() to fully rebuild.")


# =====================================================================
# MAIN ORCHESTRATOR
# =====================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Post-ingestion enrichment: genres, narratives, FAISS sync"
    )
    parser.add_argument("--test", action="store_true", help="Process 5 shows per step")
    parser.add_argument("--skip-narratives", action="store_true", help="Skip narrative distillation")
    parser.add_argument("--stats", action="store_true", help="Show coverage stats only")
    parser.add_argument(
        "--series-only", action="store_true",
        help="Run only the series clustering step (skip genres/ratings/narratives/FAISS/SEO)"
    )
    parser.add_argument(
        "--series-test", action="store_true",
        help="With --series-only: restrict to Phase A anchor set (~20 franchises)"
    )
    parser.add_argument(
        "--series-dry-run", action="store_true",
        help="With --series-only: report clusters without writing series_id to the DB"
    )
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)

    if args.stats:
        print_enrichment_stats(conn)
        conn.close()
        return

    if args.series_only:
        print(f"\n{'='*55}")
        print(f"  SERIES CLUSTERING — Initiative 1 Phase B")
        print(f"  Mode: {'TEST' if args.series_test else 'FULL'}"
              f" / {'DRY-RUN' if args.series_dry_run else 'WRITE'}")
        print(f"{'='*55}\n")
        enrich_series(
            conn,
            test_mode=args.series_test,
            dry_run=args.series_dry_run,
        )
        conn.close()
        return

    print(f"\n{'='*55}")
    print(f"  VAULT ENRICHMENT PIPELINE")
    print(f"  {'TEST MODE' if args.test else 'FULL RUN'}")
    print(f"{'='*55}\n")

    all_enriched_ids = []
    total_steps = "3" if args.skip_narratives else "4"

    # Step 1: Genres
    print(f"\n--- Step 1/{total_steps}: Genres ---")
    ids = enrich_genres(conn, test_mode=args.test)
    all_enriched_ids.extend(ids)

    # Step 2: MAL Ratings
    print(f"\n--- Step 2/{total_steps}: MAL Ratings ---")
    ids = enrich_ratings(conn, test_mode=args.test)
    all_enriched_ids.extend(ids)

    # Step 3: MAL Subtypes
    print(f"\n--- Step 3/{total_steps}: MAL Subtypes ---")
    ids = enrich_subtypes(conn, test_mode=args.test)
    all_enriched_ids.extend(ids)

    # Step 4: Narratives (optional)
    if not args.skip_narratives:
        print(f"\n--- Step 4/4: Narrative Distillation ---")
        ids = enrich_narratives(conn, test_mode=args.test)
        all_enriched_ids.extend(ids)
    else:
        logger.info("Narratives: Skipped (--skip-narratives).")

    # Step 4: FAISS sync
    print(f"\n--- FAISS Index Sync ---")
    sync_faiss_index(all_enriched_ids)

    # Step 5: SEO data export
    print(f"\n--- SEO Data Export ---")
    export_seo_data(conn)

    # Final stats
    print_enrichment_stats(conn)

    conn.close()

    print("ENRICHMENT PIPELINE COMPLETE.\n")

    if all_enriched_ids:
        print(f"{'='*55}")
        print(f"  DEPLOY REMINDER")
        print(f"{'='*55}")
        print(f"  {len(set(all_enriched_ids)):,} shows were enriched. To deploy:")
        print(f"")
        print(f"    git add data/anime_intelligence_v2.db \\")
        print(f"           data/anime_vector_index.faiss \\")
        print(f"           data/anime_vector_metadata.json \\")
        print(f"           frontend/public/data/seo_profiles.json")
        print(f"    git commit -m \"Season enrichment: update DB + FAISS index + SEO data\"")
        print(f"    git push origin main")
        print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
