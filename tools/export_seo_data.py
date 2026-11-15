"""
TOOL: export_seo_data.py
Reads all anime profiles from SQLite and exports a JSON file for build-time
SEO page generation.

Usage:
    python tools/export_seo_data.py           # Full export
    python tools/export_seo_data.py --stats   # Print profile count without writing

Output:
    frontend/public/data/seo_profiles.json

Wired into vault_enrichment.py as the final step after FAISS sync.
"""

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from analysis.series_clustering import compute_series_context  # noqa: E402
from analysis.synopsis_utils import strip_mal_attribution  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

DB_PATH = os.path.join(ROOT_DIR, "data", "anime_intelligence_v2.db")
OUTPUT_PATH = os.path.join(ROOT_DIR, "frontend", "public", "data", "seo_profiles.json")

# Bayesian scoring constants (mirrors analysis/queries.py)
_BAYESIAN_GLOBAL_MEAN = 7.05
_BAYESIAN_MIN_VOTES = 5000


def _bayesian_score(raw_score, vote_count):
    """Bayesian-adjusted MAL score: (v/(v+m))*R + (m/(v+m))*C"""
    if raw_score is None or vote_count is None:
        return 0.0
    v = vote_count
    m = _BAYESIAN_MIN_VOTES
    C = _BAYESIAN_GLOBAL_MEAN
    return round((v / (v + m)) * raw_score + (m / (v + m)) * C, 1)


def _slugify(mal_id, title):
    """Generate a URL-safe slug: {mal_id}-{slugified-title}"""
    if not title:
        return f"{mal_id}"
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s]+", "-", slug.strip())
    slug = re.sub(r"-+", "-", slug)
    slug = slug.strip("-")
    return f"{mal_id}-{slug}" if slug else f"{mal_id}"


def _parse_consensus(consensus_json_str):
    """Extract structured fields from the consensus_json blob."""
    if not consensus_json_str:
        return {
            "consensus_summary": None,
            "pros": [],
            "cons": [],
            "controversy_score": None,
            "thematic_vibe": None,
        }
    try:
        data = json.loads(consensus_json_str)
        return {
            "consensus_summary": data.get("consensus_summary"),
            "pros": data.get("pros", []),
            "cons": data.get("cons", []),
            "controversy_score": data.get("controversy_score"),
            "thematic_vibe": data.get("thematic_vibe"),
        }
    except (json.JSONDecodeError, TypeError):
        return {
            "consensus_summary": None,
            "pros": [],
            "cons": [],
            "controversy_score": None,
            "thematic_vibe": None,
        }


def _fetch_siblings_by_series(cursor):
    """Return {series_id: [sibling_dict, ...]} for all rows in one query.

    Mirrors analysis.queries.fetch_series_siblings' field set but bypasses
    its per-call IN(...) placeholder limit — for the export we want every
    cluster at once. Ordering matches the runtime helper: (release_year,
    id) ASC. Rows with NULL series_id are skipped (compute_series_context
    can't operate on them anyway).
    """
    cursor.execute(
        "SELECT id, release_year, mal_score, avg_sentiment, subtype, series_id "
        "FROM anime_info "
        "ORDER BY release_year ASC, id ASC"
    )
    siblings_by_series = {}
    for row in cursor.fetchall():
        sid = row["series_id"]
        if sid is None:
            continue
        siblings_by_series.setdefault(sid, []).append({
            "id": row["id"],
            "release_year": row["release_year"],
            "mal_score": row["mal_score"],
            "avg_sentiment": row["avg_sentiment"],
            "subtype": row["subtype"],
        })
    return siblings_by_series


def export_profiles(db_path=DB_PATH):
    """Read all anime from SQLite and return the export data dict."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute(
        "SELECT id, english_title, romaji_title, mal_score, scored_by, "
        "studio, avg_sentiment, consensus_json, mal_synopsis, release_year, "
        "season, genres, narrative_json, mal_rating, series_id, "
        "poster_url_large, poster_url_medium, source "
        "FROM anime_info ORDER BY id"
    )
    rows = cursor.fetchall()

    # Pre-fetch every cluster's siblings in one query so per-profile
    # compute_series_context is pure Python — avoids 6k+ SQL round-trips.
    siblings_by_series = _fetch_siblings_by_series(cursor)
    conn.close()

    profiles = []
    for row in rows:
        consensus = _parse_consensus(row["consensus_json"])
        title = row["english_title"] or row["romaji_title"] or ""
        series_id = row["series_id"]

        # Series context — parity with api/routes/encyclopedia.py's
        # runtime computation. Standalones (series_size == 1) still get
        # a payload; the SeriesContext React component short-circuits on
        # series_size <= 1, so nothing renders. Leaving the payload in
        # keeps the JSON shape consistent for downstream consumers.
        series_context = None
        if series_id is not None:
            siblings = siblings_by_series.get(series_id, [])
            if siblings:
                series_context = compute_series_context(
                    siblings=siblings,
                    target_mal_id=row["id"],
                    series_id=str(series_id),
                )

        profiles.append({
            "id": row["id"],
            "title": title,
            "romaji_title": row["romaji_title"],
            "slug": _slugify(row["id"], title),
            "studio": row["studio"],
            "quality_score": _bayesian_score(row["mal_score"], row["scored_by"]),
            "raw_mal_score": row["mal_score"],
            "scored_by": row["scored_by"],
            "avg_sentiment": row["avg_sentiment"],
            "release_year": row["release_year"],
            "season": row["season"],
            "genres": row["genres"],
            "synopsis": strip_mal_attribution(row["mal_synopsis"]),
            "consensus_summary": consensus["consensus_summary"],
            "pros": consensus["pros"],
            "cons": consensus["cons"],
            "controversy_score": consensus["controversy_score"],
            "thematic_vibe": consensus["thematic_vibe"],
            "has_narrative": row["narrative_json"] is not None,
            "mal_rating": row["mal_rating"],
            "series_context": series_context,
            "poster_url_large": row["poster_url_large"],
            "poster_url_medium": row["poster_url_medium"],
            "source": row["source"],
        })

    return {
        "anime": profiles,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(profiles),
    }


def write_json(data, output_path=OUTPUT_PATH):
    """Write export data to JSON file, creating directories as needed."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
    return file_size_mb


def main():
    parser = argparse.ArgumentParser(
        description="Export anime profiles to JSON for SEO page generation"
    )
    parser.add_argument("--stats", action="store_true", help="Print count only, don't write")
    args = parser.parse_args()

    logger.info(f"Reading profiles from {DB_PATH}")
    data = export_profiles()
    logger.info(f"Exported {data['count']:,} profiles.")

    if args.stats:
        return

    size_mb = write_json(data)
    logger.info(f"Written to {OUTPUT_PATH} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
