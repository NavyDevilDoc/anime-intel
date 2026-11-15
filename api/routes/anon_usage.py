"""
GET /api/anon-usage — read-only snapshot of an anon caller's remaining quota.

The frontend calls this on page mount so a freshly-opened tab can display
the truthful remaining counts instead of the hardcoded daily cap. Without
it, each tab starts at {30, 10} locally and decrements its own in-memory
counter, which lies whenever a sibling tab or browser has consumed quota
from the same IP.

This endpoint is a pure read — no DB writes, no quota consumed. A 401 is
returned if ANON_ACCESS_ENABLED is "false" (matches the rest of the
anon-quota module's failure mode for kill-switch consistency).
"""
from fastapi import APIRouter, Request

from api.middleware.anon_quota import (
    _get_cap,
    _next_midnight_utc,
    get_anon_remaining,
)
from api.schemas.responses import AnonUsageResponse

router = APIRouter()


@router.get("/api/anon-usage", response_model=AnonUsageResponse)
def anon_usage(request: Request) -> AnonUsageResponse:
    """Return the caller's remaining anon quota plus caps and reset hint."""
    remaining = get_anon_remaining(request)
    return AnonUsageResponse(
        search_remaining=remaining.search_remaining,
        dna_remaining=remaining.dna_remaining,
        search_cap=_get_cap("search"),
        dna_cap=_get_cap("dna"),
        reset_at=_next_midnight_utc(),
    )
