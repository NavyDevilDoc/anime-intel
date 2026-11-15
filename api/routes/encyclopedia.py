"""Encyclopedia endpoint — show lookup with fuzzy title resolution."""
import os
import sys

from fastapi import APIRouter
from pydantic import BaseModel, Field

SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from analysis import queries
from analysis.series_clustering import compute_series_context
from analysis.synopsis_utils import strip_mal_attribution
from api.schemas.responses import SeriesContext

router = APIRouter()


class ShowProfile(BaseModel):
    """Full anime profile from the intelligence vault."""
    id: int = 0
    title: str
    studio: str = ""
    quality_score: float = 0.0
    raw_mal_score: float = 0.0
    scored_by: int = 0
    audience_sentiment: float = 0.0
    release_year: int = 0
    synopsis: str = ""
    audience_consensus: str = ""
    pros: list[str] = Field(default_factory=list)
    cons: list[str] = Field(default_factory=list)
    controversy_score: int = 0
    # Gemini-distilled 3-word thematic descriptor from consensus_json
    # (e.g. "Sci-Fi, Neo-noir, Space Western"). Powers the "Explore
    # this vibe" CTA (PR #175 for SEO pages, PR TBD for this widget) —
    # clicks route to /try?q=<URL-encoded triplet> for vibe-based
    # discovery. Empty string when distiller output was missing or
    # malformed (defensive; distiller's Pydantic schema requires it).
    thematic_vibe: str = ""
    mal_rating: str | None = None


class EncyclopediaResponse(BaseModel):
    """Response from the encyclopedia endpoint."""
    success: bool
    matches: list[str] = Field(default_factory=list)
    profile: ShowProfile | None = None
    error: str | None = None
    # Initiative 1 Phase E: position-only series context for the
    # resolved entry. Backend ships the full SeriesContext shape
    # (including trajectory, range, dropped_siblings) but the encyclopedia
    # frontend renders only the position pill ("Entry X of N") — it's a
    # lookup surface, not a recommendation surface, so the rest of the
    # data is irrelevant here.
    series_context: SeriesContext | None = None


@router.get("/api/show/{title}", response_model=EncyclopediaResponse)
def show_lookup(title: str):
    """
    Look up a specific anime by title. Supports typos and partial matches.

    Public endpoint — accessible to anonymous users without quota. Encyclopedia
    is a pure SQLite lookup with no Gemini cost, so unlimited anon access is
    safe under the existing 30 req/min IP rate limit.

    - If exactly one match: returns the full profile.
    - If multiple matches: returns the match list for client-side disambiguation.
    - If no matches: returns error.
    """
    if not title or not title.strip():
        return EncyclopediaResponse(success=False, error="Title is required.")

    if len(title) > 100:
        return EncyclopediaResponse(success=False, error="Title must be 100 characters or fewer.")

    matches = queries.resolve_show_title(title.strip())

    if not matches:
        return EncyclopediaResponse(
            success=False,
            error=f"Could not identify '{title}' in the vault.",
        )

    if len(matches) > 1:
        return EncyclopediaResponse(
            success=True,
            matches=matches,
        )

    # Single match — return full profile
    profiles = queries.fetch_fusion_profiles(matches)
    if not profiles:
        return EncyclopediaResponse(
            success=False,
            error=f"Profile data missing for '{matches[0]}'.",
        )

    profile = profiles[0]

    # Strip MAL-sourced trailing attribution tags (e.g., "[Written by MAL
    # Rewrite]", "(Source: ANN)") from the display-bound synopsis. Scoped
    # to the route projection so reranker inputs via fetch_fusion_profiles
    # (recommendation_engine.py) stay byte-identical — the strip is a
    # display-layer concern, not an engine-layer one. See analysis/
    # synopsis_utils.py for the regex strategy and the probe findings
    # that locked the whitelist.
    profile["synopsis"] = strip_mal_attribution(profile.get("synopsis"))

    # Series context — same compute helper as the search/DNA hydration
    # path. The frontend renders only the position pill here per the
    # encyclopedia design (Initiative 1 Phase E), but backend ships the
    # full shape so the contract matches everywhere series_context
    # appears. Errors swallowed defensively — a broken sibling fetch
    # must not block the encyclopedia profile from loading.
    series_ctx = None
    try:
        sid = profile.get("series_id")
        target_id = profile.get("id")
        if sid is not None and target_id is not None:
            siblings = queries.fetch_series_siblings([sid]).get(str(sid), [])
            series_ctx = compute_series_context(
                siblings=siblings,
                target_mal_id=target_id,
                series_id=str(sid),
            )
    except Exception:  # noqa: BLE001
        series_ctx = None

    return EncyclopediaResponse(
        success=True,
        matches=matches,
        profile=profile,
        series_context=series_ctx,
    )
