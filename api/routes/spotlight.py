"""Today's Spotlight — algorithmic day-of-week rotation of noteworthy shows.

Landing Redesign PR #2. Feeds the <TodaysSpotlight> component on the
new marketing hero. Deterministic per date: same day always returns the
same show, so users who reload the landing don't see a shuffle.

Category rotation by weekday (Monday=0 through Sunday=6):
  Mon  hidden_gem            Well-rated but low-vote (5k-50k)
  Tue  controversial_classic Well-rated + high controversy_score
  Wed  deep_cut              Even more obscure high-quality (1k-15k votes)
  Thu  franchise_spotlight   Multi-entry series (>=5 clustered), top show
                             plus sibling list
  Fri  weekend_watch         One-sitting: Movies + TV Specials, quality>=7.5
  Sat  behind_the_scenes     Prolific person (>=3 attributed shows) with
                             top-rated body of work
  Sun  vibe_of_the_week      A thematic vibe from consensus_json, plus
                             three exemplar shows

Determinism: hashlib.md5(date_iso) seeds Python's random.Random. hash()
would be process-random (PYTHONHASHSEED), so cross-restart determinism
requires a stable hash.

Cache-Control: public, max-age=3600. Response only varies by date (UTC).
Sub-day cache is fine for cross-timezone users near the day boundary
without letting stale content persist across days.

Deviations from the plan (IMPLEMENTATION_PLAN.md PR #2):
  - "num_episodes <= 13" substituted with subtype IN ('Movie', 'TV Special')
    because num_episodes is not a column in anime_info. "One-sitting"
    matches the weekend-watch spirit better than "short TV series" anyway.
  - Percentile bands substituted with concrete mal_score + scored_by
    thresholds. SQLite has NTILE but the numeric thresholds are more
    predictable across vault-size changes and easier to hand-verify.
"""
import hashlib
import json
import os
import random
import sqlite3
import sys
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field

# Rollover happens at midnight US Eastern — matches the vault's
# primary audience day boundary rather than UTC (which was 8pm ET
# in daylight saving, 7pm ET in standard). Non-US users get ET-based
# rollover regardless; acceptable since the audience skews US.
_ROLLOVER_TZ = ZoneInfo("America/New_York")

SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

DB_PATH = os.path.join(ROOT_DIR, "data", "anime_intelligence_v2.db")

router = APIRouter()

CACHE_CONTROL_HEADER = "public, max-age=3600"

# Category keys map 1:1 to Python weekday() ordinals (Monday=0 through
# Sunday=6). Order matters — index by day-of-week.
CATEGORY_ROTATION: tuple[str, ...] = (
    "hidden_gem",              # Mon
    "controversial_classic",   # Tue
    "deep_cut",                # Wed
    "franchise_spotlight",     # Thu
    "weekend_watch",           # Fri
    "behind_the_scenes",       # Sat
    "vibe_of_the_week",        # Sun
)

CATEGORY_LABELS: dict[str, str] = {
    "hidden_gem": "Today: Hidden Gem",
    "controversial_classic": "Today: Controversial Classic",
    "deep_cut": "Today: Deep Cut",
    "franchise_spotlight": "Today: Franchise Spotlight",
    "weekend_watch": "Today: Weekend Watch",
    "behind_the_scenes": "Today: Behind the Scenes",
    "vibe_of_the_week": "Today: Vibe of the Week",
}


# ------------------------------------------------------------------------
# Response schemas
# ------------------------------------------------------------------------

class SpotlightShow(BaseModel):
    """Compact show shape used in both primary + supplementary slots.
    Sized for the landing card, not for full profile display."""
    id: int
    title: str
    release_year: int = 0
    mal_score: float = 0.0
    thematic_vibe: str = ""
    consensus_summary: str = ""
    pros: list[str] = Field(default_factory=list)
    controversy_score: int = 0
    poster_url_large: str | None = None
    poster_url_medium: str | None = None


class SpotlightResponse(BaseModel):
    category: str            # e.g. "hidden_gem" — matches CATEGORY_ROTATION
    category_label: str      # e.g. "Today: Hidden Gem"
    date: str                # ISO date, e.g. "2026-09-30"
    primary_show: SpotlightShow
    supplementary_shows: list[SpotlightShow] = Field(default_factory=list)
    supplementary_context: dict[str, Any] | None = None
    # ^ Free-form per-category payload for the frontend. Franchise spotlight
    # supplies {"series_id": ..., "series_size": ...}; behind_the_scenes
    # supplies {"person_name": ..., "role": ...}; vibe_of_the_week supplies
    # {"vibe": ...}. Categories without extra context set this to None.


# ------------------------------------------------------------------------
# DB access (read-only, matching queries.py convention)
# ------------------------------------------------------------------------

def _connect_readonly() -> sqlite3.Connection:
    """Open the vault DB in read-only URI mode. Same guarantee as
    queries.py — no route-generated SQL can mutate data even by mistake."""
    uri_path = f"file:{DB_PATH}?mode=ro"
    return sqlite3.connect(uri_path, uri=True)


def _fetch_show(conn: sqlite3.Connection, anime_id: int) -> SpotlightShow | None:
    """Hydrate one show into the SpotlightShow shape from a single row."""
    row = conn.execute("""
        SELECT id, COALESCE(english_title, romaji_title) AS title,
               COALESCE(release_year, 0),
               COALESCE(mal_score, 0.0),
               consensus_json,
               poster_url_large,
               poster_url_medium
        FROM anime_info WHERE id = ?
    """, (anime_id,)).fetchone()
    if row is None:
        return None

    id_, title, release_year, mal_score, consensus_json, poster_large, poster_medium = row
    thematic_vibe = ""
    consensus_summary = ""
    pros: list[str] = []
    controversy_score = 0
    if consensus_json:
        try:
            payload = json.loads(consensus_json)
        except json.JSONDecodeError:
            payload = {}
        thematic_vibe = str(payload.get("thematic_vibe") or "")
        consensus_summary = str(payload.get("consensus_summary") or "")
        pros_raw = payload.get("pros")
        if isinstance(pros_raw, list):
            pros = [str(p) for p in pros_raw[:3]]  # Landing card only shows top 3
        try:
            controversy_score = int(payload.get("controversy_score") or 0)
        except (TypeError, ValueError):
            controversy_score = 0

    return SpotlightShow(
        id=id_,
        title=title or "",
        release_year=release_year,
        mal_score=mal_score,
        thematic_vibe=thematic_vibe,
        consensus_summary=consensus_summary,
        pros=pros,
        controversy_score=controversy_score,
        poster_url_large=poster_large,
        poster_url_medium=poster_medium,
    )


# ------------------------------------------------------------------------
# Category pool builders — each returns a deterministically-sorted list
# of MAL IDs that satisfy the category's criteria.
# ------------------------------------------------------------------------

def _pool_hidden_gem(conn: sqlite3.Connection) -> list[int]:
    """Well-rated (mal_score >= 7.5) but low-vote (5k-50k) — quality
    without the popularity signal."""
    return [r[0] for r in conn.execute("""
        SELECT id FROM anime_info
        WHERE mal_score >= 7.5
          AND scored_by BETWEEN 5000 AND 50000
          AND consensus_json IS NOT NULL
          AND poster_url_large IS NOT NULL
        ORDER BY id
    """).fetchall()]


def _pool_controversial_classic(conn: sqlite3.Connection) -> list[int]:
    """Well-rated (mal_score >= 7.5) with meaningfully-divisive reviews
    (controversy_score >= 7 from consensus_json)."""
    return [r[0] for r in conn.execute("""
        SELECT id FROM anime_info
        WHERE mal_score >= 7.5
          AND scored_by >= 20000
          AND consensus_json IS NOT NULL
          AND poster_url_large IS NOT NULL
          AND CAST(json_extract(consensus_json, '$.controversy_score') AS INTEGER) >= 7
        ORDER BY id
    """).fetchall()]


def _pool_deep_cut(conn: sqlite3.Connection) -> list[int]:
    """Very obscure (1k-15k votes) but highly rated (mal_score >= 8.0)."""
    return [r[0] for r in conn.execute("""
        SELECT id FROM anime_info
        WHERE mal_score >= 8.0
          AND scored_by BETWEEN 1000 AND 15000
          AND consensus_json IS NOT NULL
          AND poster_url_large IS NOT NULL
        ORDER BY id
    """).fetchall()]


def _pool_franchise_series_ids(conn: sqlite3.Connection) -> list[str]:
    """series_id values with >= 5 vaulted entries, sorted deterministically
    for seeded picking."""
    return [r[0] for r in conn.execute("""
        SELECT series_id
        FROM anime_info
        WHERE series_id IS NOT NULL
          AND consensus_json IS NOT NULL
          AND poster_url_large IS NOT NULL
        GROUP BY series_id
        HAVING COUNT(*) >= 5
        ORDER BY series_id
    """).fetchall()]


def _pool_weekend_watch(conn: sqlite3.Connection) -> list[int]:
    """One-sitting quality: Movies + TV Specials with mal_score >= 7.5.
    Substitutes for the plan's num_episodes<=13 criterion since num_episodes
    isn't a column in anime_info."""
    return [r[0] for r in conn.execute("""
        SELECT id FROM anime_info
        WHERE subtype IN ('Movie', 'TV Special')
          AND mal_score >= 7.5
          AND scored_by >= 3000
          AND consensus_json IS NOT NULL
          AND poster_url_large IS NOT NULL
        ORDER BY id
    """).fetchall()]


def _pool_behind_the_scenes(conn: sqlite3.Connection) -> list[tuple[int, str, str]]:
    """People (person_mal_id, name_display, role) with >= 3 attributed shows
    whose average mal_score >= 8.0. Sorted deterministically."""
    return [(r[0], r[1], r[2]) for r in conn.execute("""
        SELECT p.person_mal_id, p.name_display, ast.role
        FROM people p
        JOIN anime_staff ast ON ast.person_mal_id = p.person_mal_id
        JOIN anime_info ai ON ai.id = ast.anime_id
        WHERE ai.consensus_json IS NOT NULL
          AND ai.poster_url_large IS NOT NULL
        GROUP BY p.person_mal_id, ast.role
        HAVING COUNT(DISTINCT ai.id) >= 3
           AND AVG(ai.mal_score) >= 8.0
        ORDER BY p.person_mal_id, ast.role
    """).fetchall()]


def _pool_vibes(conn: sqlite3.Connection) -> list[str]:
    """Distinct thematic_vibe strings that appear on >= 3 shows. Vibes
    live inside consensus_json.thematic_vibe; extracted + deduped in
    Python since SQL grouping on a JSON field is awkward.

    Marketing filter: only counts shows whose mal_rating is safer than
    R+ (Mild Nudity) / Rx (Hentai). Without this filter, thematic vibes
    like "Ecchi, Harem, Supernatural" surface on the Sunday landing
    spotlight because they satisfy the >=3 exemplar threshold — technically
    correct, editorially wrong for a marketing first-impression surface.
    NULL mal_rating is excluded conservatively (unknown = not featured).
    Other categories (hidden_gem, deep_cut, weekend_watch) filter by
    quality or subtype already; only vibe_of_the_week groups purely by
    thematic tag, so only this pool needs the extra guard."""
    rows = conn.execute("""
        SELECT json_extract(consensus_json, '$.thematic_vibe') AS vibe
        FROM anime_info
        WHERE consensus_json IS NOT NULL
          AND poster_url_large IS NOT NULL
          AND mal_rating IS NOT NULL
          AND mal_rating NOT LIKE 'R+%'
          AND mal_rating NOT LIKE 'Rx%'
    """).fetchall()
    counts: dict[str, int] = {}
    for (vibe,) in rows:
        if vibe:
            counts[vibe] = counts.get(vibe, 0) + 1
    return sorted(v for v, n in counts.items() if n >= 3)


def _shows_by_series_id(conn: sqlite3.Connection, series_id: str) -> list[int]:
    """All vaulted MAL IDs in the given series_id, ordered for stable
    ranking. Franchise Spotlight uses the top-score entry as primary and
    the rest as supplementary."""
    return [r[0] for r in conn.execute("""
        SELECT id FROM anime_info
        WHERE series_id = ?
          AND consensus_json IS NOT NULL
          AND poster_url_large IS NOT NULL
        ORDER BY mal_score DESC, id ASC
    """, (series_id,)).fetchall()]


def _shows_by_person_role(conn: sqlite3.Connection, person_mal_id: int,
                          role: str, limit: int = 4) -> list[int]:
    """Top-N shows attributed to a person in a given role, by mal_score."""
    return [r[0] for r in conn.execute("""
        SELECT ai.id FROM anime_info ai
        JOIN anime_staff ast ON ast.anime_id = ai.id
        WHERE ast.person_mal_id = ?
          AND ast.role = ?
          AND ai.consensus_json IS NOT NULL
          AND ai.poster_url_large IS NOT NULL
        ORDER BY ai.mal_score DESC, ai.id ASC
        LIMIT ?
    """, (person_mal_id, role, limit)).fetchall()]


def _shows_by_vibe(conn: sqlite3.Connection, vibe: str, limit: int = 4) -> list[int]:
    """Top-N shows whose thematic_vibe matches, by mal_score. Applies
    the same rating filter as _pool_vibes so exemplars align with the
    marketability guard on the pool."""
    return [r[0] for r in conn.execute("""
        SELECT id FROM anime_info
        WHERE consensus_json IS NOT NULL
          AND poster_url_large IS NOT NULL
          AND mal_rating IS NOT NULL
          AND mal_rating NOT LIKE 'R+%'
          AND mal_rating NOT LIKE 'Rx%'
          AND json_extract(consensus_json, '$.thematic_vibe') = ?
        ORDER BY mal_score DESC, id ASC
        LIMIT ?
    """, (vibe, limit)).fetchall()]


# ------------------------------------------------------------------------
# Determinism seed
# ------------------------------------------------------------------------

def _seed_for(date_iso: str, category: str) -> int:
    """Stable seed from (date, category) pair. Python's hash() is
    process-randomized (PYTHONHASHSEED) — hashlib gives cross-restart
    stability, which the daily-shuffle contract depends on."""
    return int(hashlib.md5(f"{date_iso}|{category}".encode()).hexdigest()[:15], 16)


# ------------------------------------------------------------------------
# Category resolvers — build a SpotlightResponse for each category
# ------------------------------------------------------------------------

def _resolve_single_show_category(conn: sqlite3.Connection, category: str,
                                  date_iso: str, pool: list[int]) -> SpotlightResponse | None:
    """Shared shape for the four "one show" categories."""
    if not pool:
        return None
    rng = random.Random(_seed_for(date_iso, category))
    picked = rng.choice(pool)
    primary = _fetch_show(conn, picked)
    if primary is None:
        return None
    return SpotlightResponse(
        category=category,
        category_label=CATEGORY_LABELS[category],
        date=date_iso,
        primary_show=primary,
    )


def _resolve_franchise_spotlight(conn: sqlite3.Connection,
                                 date_iso: str) -> SpotlightResponse | None:
    series_pool = _pool_franchise_series_ids(conn)
    if not series_pool:
        return None
    rng = random.Random(_seed_for(date_iso, "franchise_spotlight"))
    picked_series = rng.choice(series_pool)
    show_ids = _shows_by_series_id(conn, picked_series)
    if not show_ids:
        return None
    primary = _fetch_show(conn, show_ids[0])
    if primary is None:
        return None
    supplementary = [
        s for s in (_fetch_show(conn, sid) for sid in show_ids[1:5])
        if s is not None
    ]
    return SpotlightResponse(
        category="franchise_spotlight",
        category_label=CATEGORY_LABELS["franchise_spotlight"],
        date=date_iso,
        primary_show=primary,
        supplementary_shows=supplementary,
        supplementary_context={
            "series_id": picked_series,
            "series_size": len(show_ids),
        },
    )


def _resolve_behind_the_scenes(conn: sqlite3.Connection,
                               date_iso: str) -> SpotlightResponse | None:
    people_pool = _pool_behind_the_scenes(conn)
    if not people_pool:
        return None
    rng = random.Random(_seed_for(date_iso, "behind_the_scenes"))
    person_mal_id, name_display, role = rng.choice(people_pool)
    show_ids = _shows_by_person_role(conn, person_mal_id, role, limit=4)
    if not show_ids:
        return None
    primary = _fetch_show(conn, show_ids[0])
    if primary is None:
        return None
    supplementary = [
        s for s in (_fetch_show(conn, sid) for sid in show_ids[1:])
        if s is not None
    ]
    return SpotlightResponse(
        category="behind_the_scenes",
        category_label=CATEGORY_LABELS["behind_the_scenes"],
        date=date_iso,
        primary_show=primary,
        supplementary_shows=supplementary,
        supplementary_context={
            "person_name": name_display,
            "role": role,
        },
    )


def _resolve_vibe_of_the_week(conn: sqlite3.Connection,
                              date_iso: str) -> SpotlightResponse | None:
    vibes = _pool_vibes(conn)
    if not vibes:
        return None
    rng = random.Random(_seed_for(date_iso, "vibe_of_the_week"))
    picked_vibe = rng.choice(vibes)
    show_ids = _shows_by_vibe(conn, picked_vibe, limit=4)
    if not show_ids:
        return None
    primary = _fetch_show(conn, show_ids[0])
    if primary is None:
        return None
    supplementary = [
        s for s in (_fetch_show(conn, sid) for sid in show_ids[1:])
        if s is not None
    ]
    return SpotlightResponse(
        category="vibe_of_the_week",
        category_label=CATEGORY_LABELS["vibe_of_the_week"],
        date=date_iso,
        primary_show=primary,
        supplementary_shows=supplementary,
        supplementary_context={"vibe": picked_vibe},
    )


# ------------------------------------------------------------------------
# Dispatcher — pick category by weekday, build response
# ------------------------------------------------------------------------

def _resolve_spotlight(target_date: date) -> SpotlightResponse:
    """Build the spotlight for a given date. Raises HTTPException(503) if
    no category can populate — signals the vault is in a state the
    endpoint can't handle (empty, corrupted, etc.)."""
    date_iso = target_date.isoformat()
    category = CATEGORY_ROTATION[target_date.weekday()]

    conn = _connect_readonly()
    try:
        response: SpotlightResponse | None = None
        if category == "hidden_gem":
            response = _resolve_single_show_category(
                conn, category, date_iso, _pool_hidden_gem(conn))
        elif category == "controversial_classic":
            response = _resolve_single_show_category(
                conn, category, date_iso, _pool_controversial_classic(conn))
        elif category == "deep_cut":
            response = _resolve_single_show_category(
                conn, category, date_iso, _pool_deep_cut(conn))
        elif category == "weekend_watch":
            response = _resolve_single_show_category(
                conn, category, date_iso, _pool_weekend_watch(conn))
        elif category == "franchise_spotlight":
            response = _resolve_franchise_spotlight(conn, date_iso)
        elif category == "behind_the_scenes":
            response = _resolve_behind_the_scenes(conn, date_iso)
        elif category == "vibe_of_the_week":
            response = _resolve_vibe_of_the_week(conn, date_iso)
    finally:
        conn.close()

    if response is None:
        raise HTTPException(
            status_code=503,
            detail=f"Spotlight category '{category}' has no eligible shows in the current vault.",
        )
    return response


# ------------------------------------------------------------------------
# Route
# ------------------------------------------------------------------------

@router.get("/api/spotlight", response_model=SpotlightResponse)
def get_spotlight(
    response: Response,
    date_override: str | None = Query(
        default=None, alias="date",
        description="Optional ISO date (YYYY-MM-DD). Defaults to today (UTC).",
    ),
) -> SpotlightResponse:
    """Today's algorithmic spotlight — a rotating category of vaulted show(s)
    picked deterministically per date."""
    if date_override is not None:
        try:
            target = date.fromisoformat(date_override)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid date '{date_override}'. Expected ISO format YYYY-MM-DD.",
            )
    else:
        target = datetime.now(_ROLLOVER_TZ).date()

    result = _resolve_spotlight(target)
    response.headers["Cache-Control"] = CACHE_CONTROL_HEADER
    return result
