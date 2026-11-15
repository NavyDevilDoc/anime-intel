"""Search feedback endpoint — captures search-quality ratings from any visitor."""
from fastapi import APIRouter, HTTPException, status

from analysis import telemetry_logger as telemetry
from api.schemas.requests import SearchFeedbackRequest
from api.supabase_client import supabase_get, supabase_post

router = APIRouter()


@router.post("/api/search-feedback")
def submit_feedback(req: SearchFeedbackRequest):
    """
    Submit a thumbs up/down rating for a search result.

    Anonymous submission — the data point is (search_id, rating); no user
    identity is recorded. The per-IP 30/10 daily search cap naturally limits
    submissions, since each rating ties back to a search_id the client had to
    obtain from a search response.

    One rating per search_id: a duplicate POST returns 409 instead of
    inflating the count.
    """
    existing = supabase_get("search_feedback", {
        "search_id": f"eq.{req.search_id}",
        "select": "id",
        "limit": "1",
    })
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Feedback already submitted for this search.",
        )

    supabase_post("search_feedback", {
        "search_id": req.search_id,
        "rating": req.rating,
    })

    telemetry.log_search_journal({
        "mode": "feedback",
        "search_id": req.search_id,
        "rating": req.rating,
    })

    return {"success": True}
