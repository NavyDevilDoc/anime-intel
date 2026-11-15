"""
PR C / Initiative 2: POST /api/fetch-watchlist endpoint.

Anonymous endpoint that takes a public MAL or AniList username, fetches
their watchlist via the project's existing app-scoped MAL OAuth (for
MAL) or via AniList's anonymous public GraphQL, and returns a
deduplicated list of MAL IDs the user has marked under the requested
statuses. The frontend caches the response in localStorage and threads
the IDs into search/DNA requests as `exclude_mal_ids` (Phase C wiring).

Privacy contract (also documented in Phase F's privacy copy):
  - Backend persists nothing — no username log, no MAL ID log, no
    telemetry write tied to this endpoint.
  - The MAL token used to read the user's public list is the project's
    own app-scoped OAuth token (read-only). It is not exposed in
    responses or logs.
  - Per-IP rate limiting from the existing RateLimitMiddleware
    (30/min anonymous) acts as the abuse ceiling; watchlist fetches
    are once-per-session by nature, so the existing cap is sufficient.

Rollback: setting WATCHLIST_EXCLUSION_ENABLED=false in Railway env
returns 503 from this endpoint without removing the route. Used as
the emergency kill switch if MAL or AniList tightens public access.
"""
from __future__ import annotations

import os
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from analysis.watchlist_fetcher import (
    DEFAULT_STATUSES,
    WatchlistFetchError,
    WatchlistResult,
    fetch_watchlist,
)

router = APIRouter()

CanonicalStatusLiteral = Literal[
    "completed", "dropped", "watching", "plan_to_watch", "on_hold"
]
SourceLiteral = Literal["mal", "anilist"]


class WatchlistFetchRequest(BaseModel):
    source: SourceLiteral
    # MAL usernames are 2-16 chars alphanumeric/underscore; AniList allows
    # up to 20 chars. 50 here keeps the validator permissive without
    # opening the door to absurd payloads.
    username: str = Field(..., min_length=1, max_length=50)
    # Default to completed + dropped per IMPLEMENTATION_PLAN Phase B —
    # watching and plan_to_watch are aspirational/active states where
    # the user may still want comparable recommendations.
    statuses: list[CanonicalStatusLiteral] = Field(default_factory=lambda: list(DEFAULT_STATUSES))


class WatchlistFetchResponse(BaseModel):
    """Mirrors WatchlistResult from analysis.watchlist_fetcher.

    Kept as a separate schema so the wire shape can evolve independently
    of the internal model if needed (e.g., adding pagination metadata
    or per-status counts later).
    """
    source: SourceLiteral
    mal_ids: list[int]
    total_entries: int
    unmappable_entries: int
    errors: list[str]


def _is_enabled() -> bool:
    """Master kill switch for the watchlist exclusion endpoint."""
    return os.environ.get("WATCHLIST_EXCLUSION_ENABLED", "true").lower() == "true"


@router.post("/api/fetch-watchlist", response_model=WatchlistFetchResponse)
def fetch_watchlist_endpoint(req: WatchlistFetchRequest) -> WatchlistFetchResponse:
    """Fetch a user's public watchlist from MAL or AniList.

    Returns 200 with a WatchlistFetchResponse on success (possibly
    partial — see `errors` field).

    Error surfaces:
      - 503: feature disabled via env, OR the project's MAL OAuth token
             is broken and refresh failed
      - 404: user not found or list is private (both sources)
      - 429: upstream rate limit exceeded (currently AniList only;
             MAL's status-per-call cap is well above what this endpoint
             can produce)
      - 502: upstream unreachable / unexpected HTTP error
    """
    if not _is_enabled():
        raise HTTPException(
            status_code=503,
            detail="Watchlist exclusion is temporarily unavailable.",
        )

    try:
        result: WatchlistResult = fetch_watchlist(
            source=req.source,
            username=req.username,
            statuses=req.statuses,
        )
    except WatchlistFetchError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))

    return WatchlistFetchResponse(
        source=result.source,
        mal_ids=result.mal_ids,
        total_entries=result.total_entries,
        unmappable_entries=result.unmappable_entries,
        errors=result.errors,
    )
