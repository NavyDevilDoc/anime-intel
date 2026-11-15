"""Controversy Landscape data endpoint — feeds the signature interactive
scatter plot on the landing + /how-it-works pages.

Landing Redesign PR #4. Returns compact rows for every vaulted show so
the frontend can render a 2D scatter: X = Bayesian smoothed quality,
Y = controversy_score, dot color encodes popularity (log scored_by).

Payload sizing (verified against production DB 2026-09-30, 6,427 shows
with consensus_json):
  Raw JSON:    ~991 KB
  Gzipped:     ~218 KB   (<< plan's 500 KB estimate)

Cache strategy: Cache-Control: public, max-age=86400 (1 day). The
underlying data only changes on vault ingest (seasonal cadence),
so caching aggressively is honest. Vercel's edge network will serve
the response without hitting Railway for the overwhelming majority of
requests.

Field selection minimizes payload while giving the frontend what it
needs for the scatter + filter controls + hover tooltip:
  mal_id            — click-through to encyclopedia
  title             — hover tooltip + encyclopedia navigation
  quality           — X-axis (Bayesian smoothed, computed here)
  controversy_score — Y-axis (0-10, extracted from consensus_json)
  scored_by         — dot color (log scale on frontend)
  avg_sentiment     — reserved for future encoding option
  genres            — filter control (list of strings)
  release_year      — decade filter + metadata
  subtype           — subtype filter (TV / Movie / etc.)
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# Reuse the canonical Bayesian smoothing function from queries.py so the
# landscape's X-axis uses the same quality formula as the recommendation
# engine itself. _bayesian_score is private-by-convention (leading
# underscore) but importing beats duplicating the formula + constants
# in two places — if the parked COA C recalibration ever ships, only
# queries.py needs updating.
from analysis.queries import _bayesian_score  # noqa: PLC2701

DB_PATH = os.path.join(ROOT_DIR, "data", "anime_intelligence_v2.db")

router = APIRouter()

CACHE_CONTROL_HEADER = "public, max-age=86400"


class LandscapeShow(BaseModel):
    """Compact per-show payload for the scatter plot."""
    mal_id: int
    title: str
    quality: float            # Bayesian smoothed mal_score
    controversy_score: int    # 0-10, from consensus_json
    scored_by: int
    avg_sentiment: float
    genres: list[str] = Field(default_factory=list)
    release_year: int
    subtype: str


class LandscapeResponse(BaseModel):
    shows: list[LandscapeShow]
    generated_at: str          # ISO8601 UTC, informational only


def _connect_readonly() -> sqlite3.Connection:
    """Read-only URI connection — matches queries.py / spotlight.py."""
    uri_path = f"file:{DB_PATH}?mode=ro"
    return sqlite3.connect(uri_path, uri=True)


def _parse_genres(raw: str | None) -> list[str]:
    """`anime_info.genres` is a comma-separated string. Split + strip +
    drop empties. Returns an empty list for NULL / empty-string inputs."""
    if not raw:
        return []
    return [g.strip() for g in raw.split(",") if g.strip()]


def _build_rows(conn: sqlite3.Connection) -> list[LandscapeShow]:
    """One SQL pass + Python assembly. json_extract pulls controversy_score
    directly in SQL (avoids per-row JSON parse); Bayesian smoothing and
    genre split happen in the Python loop."""
    cursor = conn.execute("""
        SELECT
            id,
            COALESCE(english_title, romaji_title) AS title,
            COALESCE(mal_score, 0.0) AS mal_score,
            COALESCE(scored_by, 0) AS scored_by,
            COALESCE(avg_sentiment, 0.0) AS avg_sentiment,
            genres,
            COALESCE(release_year, 0) AS release_year,
            COALESCE(subtype, '') AS subtype,
            CAST(json_extract(consensus_json, '$.controversy_score') AS INTEGER)
                AS controversy_score
        FROM anime_info
        WHERE consensus_json IS NOT NULL
          AND mal_score IS NOT NULL
          AND scored_by IS NOT NULL
        ORDER BY id
    """)
    shows: list[LandscapeShow] = []
    for row in cursor.fetchall():
        (mal_id, title, mal_score, scored_by, avg_sentiment,
         genres_raw, release_year, subtype, controversy_score) = row
        if title is None:
            continue  # Pathological row; skip rather than ship a null title
        shows.append(LandscapeShow(
            mal_id=mal_id,
            title=title,
            quality=round(_bayesian_score(mal_score, scored_by), 2),
            controversy_score=controversy_score or 0,
            scored_by=scored_by,
            avg_sentiment=round(avg_sentiment, 3),
            genres=_parse_genres(genres_raw),
            release_year=release_year,
            subtype=subtype,
        ))
    return shows


@router.get("/api/controversy-landscape-data", response_model=LandscapeResponse)
def get_landscape_data(response: Response) -> LandscapeResponse:
    """Returns every vaulted show with the fields needed to render the
    Controversy Landscape scatter plot. Deterministic — depends only on
    vault state. Cached at the edge for 24 hours."""
    conn = _connect_readonly()
    try:
        shows = _build_rows(conn)
    finally:
        conn.close()

    response.headers["Cache-Control"] = CACHE_CONTROL_HEADER
    return LandscapeResponse(
        shows=shows,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
