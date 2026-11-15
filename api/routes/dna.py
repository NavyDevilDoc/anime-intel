"""DNA Triangulation endpoint — wraps execute_dna_triangulation."""
from fastapi import APIRouter, Depends, Request

from api.dependencies import get_engine
from api.middleware.anon_quota import check_anon_quota
from api.schemas.requests import DNARequest
from api.schemas.responses import DNAResponse

router = APIRouter()


@router.post("/api/dna", response_model=DNAResponse)
def dna_triangulation(req: DNARequest, request: Request,
                      engine=Depends(get_engine)):  # noqa: B008 — FastAPI DI pattern
    """
    Execute DNA Triangulation for 1-3 reference shows.

    Open-access: every request is rate-limited via the per-IP daily cap in
    anon_quota (default 10/day). No per-user accounting.
    """
    remaining = check_anon_quota(request, "dna")

    result = engine.execute_dna_triangulation(
        req.titles,
        negative_anchors=req.negative_anchors,
        exclude_mal_ids=req.exclude_mal_ids,
    )

    if not result.get("success"):
        return DNAResponse(
            success=False,
            error=result.get("error", "DNA synthesis failed."),
            title_resolution_notes=result.get("title_resolution_notes") or None,
            low_confidence=result.get("low_confidence", False),
            fallback_mode=result.get("fallback_mode"),
            search_remaining=remaining.search_remaining,
            dna_remaining=remaining.dna_remaining,
        )

    return DNAResponse(
        success=True,
        intersection_summary=result.get("intersection_summary"),
        data=result.get("data", []),
        data_size=len(result.get("data", [])),
        negative_anchors_used=result.get("negative_anchors_used") or None,
        title_resolution_notes=result.get("title_resolution_notes") or None,
        low_confidence=result.get("low_confidence", False),
        fallback_mode=result.get("fallback_mode"),
        search_remaining=remaining.search_remaining,
        dna_remaining=remaining.dna_remaining,
    )
