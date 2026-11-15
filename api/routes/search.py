"""Search endpoint — wraps the recommendation engine's fetch_vault_pool."""
from fastapi import APIRouter, Depends, Request

from api.dependencies import get_engine
from api.middleware.anon_quota import check_anon_quota
from api.schemas.requests import SearchRequest
from api.schemas.responses import SearchResponse

router = APIRouter()


@router.post("/api/search", response_model=SearchResponse)
def search(req: SearchRequest, request: Request,
           engine=Depends(get_engine)):  # noqa: B008 — FastAPI DI pattern
    """
    Execute an Intelligent Search or Objective Rankings query.

    Open-access: every request is rate-limited via the per-IP daily cap in
    anon_quota (default 30/day). No per-user accounting.
    """
    remaining = check_anon_quota(request, "search")

    result = engine.fetch_vault_pool(
        req.query,
        req.lens,
        year_min=req.year_min,
        year_max=req.year_max,
        era_label=req.era_label,
        rating_max=req.rating_max,
        exclude_mal_ids=req.exclude_mal_ids,
        staff_filter=req.staff_filter,
    )

    if not result.get("success"):
        return SearchResponse(
            success=False,
            error=result.get("error", "Search failed."),
            low_confidence=result.get("low_confidence", False),
            fallback_mode=result.get("fallback_mode"),
            search_remaining=remaining.search_remaining,
            dna_remaining=remaining.dna_remaining,
        )

    return SearchResponse(
        success=True,
        pool=result["pool"],
        pool_size=len(result["pool"]),
        search_id=result.get("search_id"),
        low_confidence=result.get("low_confidence", False),
        fallback_mode=result.get("fallback_mode"),
        search_remaining=remaining.search_remaining,
        dna_remaining=remaining.dna_remaining,
    )
