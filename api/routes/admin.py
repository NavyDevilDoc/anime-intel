"""
Admin dashboard endpoint — aggregate metrics and operational controls.

All queries use the service key (bypasses RLS) since they aggregate across all users.
The only write operation is the lockdown toggle, which is audit-logged.
"""
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api.middleware.anon_quota import _get_cap
from api.middleware.basic_auth import verify_basic_auth
from api.middleware.lockdown import reset_lockdown_cache
from api.supabase_client import (
    supabase_count,
    supabase_get,
    supabase_patch,
    supabase_rpc,
)

logger = logging.getLogger(__name__)
router = APIRouter()


class FeedbackSummary(BaseModel):
    thumbs_up: int = 0
    thumbs_down: int = 0


class AnonTraffic(BaseModel):
    anon_searches_today: int = 0
    anon_dna_today: int = 0
    anon_unique_ips_today: int = 0
    anon_cap_hits_today: int = 0
    anon_unique_ips_7d: int = 0
    anon_unique_ips_30d: int = 0


class ApiCosts(BaseModel):
    """
    Three-lens cost reporting:

    - `gemini_mtd_usd` is REAL cumulative Gemini spend for the current
      UTC month, summed from the `gemini_calls` ledger (PR #85 wrapper).
      Headline value on the dashboard; resets at the UTC month boundary
      when the query's `gte.{first_of_month}` filter shifts forward.
    - `production_projection_monthly_usd` is a heuristic projection of
      today's anon-traffic spend × 30. Honest about its scope: it only
      sees production HTTP requests via anon_usage, missing dev /
      regression / probe / ingestion burn. Kept for footer nuance.
    - `last_7d_total_usd` + `last_7d_by_origin` read REAL spend from the
      `gemini_calls` ledger broken down by origin so the dashboard
      surfaces the full picture instead of just one slice.
    """
    total_searches_today: int = 0
    total_dna_today: int = 0
    gemini_cost_today_usd: float = 0.0
    gemini_mtd_usd: float = 0.0
    gemini_ceiling_usd: float = 35.0
    production_projection_monthly_usd: float = 0.0
    railway_monthly_usd: float = 15.0
    total_projected_monthly_usd: float = 15.0
    budget_ceiling_usd: float = 50.0
    last_7d_total_usd: float = 0.0
    last_7d_by_origin: dict[str, float] = Field(default_factory=dict)


# Gemini 2.5 Flash per-user-action cost heuristic drives the headline
# `production_projection_monthly_usd` dashboard tile. Both constants
# were back-calculated 2026-10-02 from the real gemini_calls Supabase
# ledger (1,020 production rows written between 2026-06-13 and
# 2026-10-02, origin='production').
#
# Methodology: each user search issues one query embedding (_embed) +
# one reranker call (RecommendationEngine._rerank_candidates and
# variants). DNA issues one centroid embedding + one DNA synthesis
# call (execute_dna_triangulation) + one DNA reranker
# (_rerank_dna_candidates). Caller counts in the ledger match this
# pattern 1:1 within sampling noise, so per-action cost = sum of
# mean cost across the per-caller cohorts:
#
#   SEARCH per action:
#     _embed:                           0.000024 avg (n=427)
#     _rerank_candidates (+ variants):  0.006229 avg (n=354)
#     total:                            0.006253  →  0.0063
#
#   DNA per action:
#     _embed:                           0.000024 avg (n=427)
#     execute_dna_triangulation:        0.002158 avg (n=44)
#     _rerank_dna_candidates:           0.009089 avg (n=44)
#     total:                            0.011271  →  0.0113
#
# The pre-PR-#87 constants (both 0.028) over-estimated by 4.48x on
# SEARCH and 2.48x on DNA — the dashboard projection read ~4x too
# high, making $5/mo real spend look like $21/mo. Recalibration drops
# the headline tile into honest-reporting territory.
#
# Refresh cadence: re-run the ledger math any time a Gemini model
# version, thinking budget, or prompt structure changes materially
# (parked as a periodic hygiene task). The wrapper writes REAL costs
# to gemini_calls; these constants only drive the heuristic projection.
COST_PER_SEARCH_USD = 0.0063  # was 0.028 pre-recalibration
COST_PER_DNA_USD = 0.0113     # was 0.028 pre-recalibration
RAILWAY_MONTHLY_USD = 15.0
BUDGET_CEILING_USD = 50.0
# Gemini-only ceiling for the MTD tile: $50 total ceiling minus the
# $15 Railway fixed line item. Color coding on the headline tile
# (yellow ≥ 60%, red ≥ 90%) compares cumulative MTD spend against this.
GEMINI_CEILING_USD = 35.0
ATTRIBUTION_WINDOW_DAYS = 7


class StagePercentiles(BaseModel):
    p50_ms: float | None = None
    p95_ms: float | None = None
    p99_ms: float | None = None


class TimingProfile(BaseModel):
    rerank_type: str
    sample_count: int = 0
    retrieval: StagePercentiles = StagePercentiles()
    hydration: StagePercentiles = StagePercentiles()
    rerank: StagePercentiles = StagePercentiles()
    total: StagePercentiles = StagePercentiles()


class PipelineTimings(BaseModel):
    """Latency percentiles per rerank_type over the last `window_hours`."""
    window_hours: int = 24
    profiles: list[TimingProfile] = Field(default_factory=list)
    has_data: bool = False
    error: str | None = None


class AdminDashboardResponse(BaseModel):
    feedback_summary: FeedbackSummary = FeedbackSummary()
    contact_messages: int = 0
    anon_traffic: AnonTraffic = AnonTraffic()
    lockdown_active: bool = False
    api_costs: ApiCosts = ApiCosts()
    pipeline_timings: PipelineTimings = PipelineTimings()


class LockdownRequest(BaseModel):
    enabled: bool


class LockdownResponse(BaseModel):
    lockdown_active: bool
    updated_by: str
    updated_at: str


TIMING_WINDOW_HOURS = 24


def _round_or_none(val):
    if val is None:
        return None
    return round(float(val), 1)


def _fetch_pipeline_timings() -> PipelineTimings:
    """
    Call the Supabase get_pipeline_timings() RPC and shape the response.

    Fail-soft: any failure (RPC missing, network error, table missing)
    returns an empty profile with an explanatory error string. The dashboard
    renders a "no data" or error state — never a 500.
    """
    try:
        rows = supabase_rpc(
            "get_pipeline_timings",
            {"p_window_hours": TIMING_WINDOW_HOURS},
        )
    except Exception as e:  # noqa: BLE001 — dashboard is fail-soft; a broken telemetry RPC must not 500 the whole admin view
        logger.warning(f"Pipeline timing RPC failed: {type(e).__name__}: {e}")
        return PipelineTimings(
            window_hours=TIMING_WINDOW_HOURS,
            error=f"Telemetry query failed ({type(e).__name__})",
        )

    profiles = [
        TimingProfile(
            rerank_type=r["rerank_type"],
            sample_count=int(r["sample_count"]),
            retrieval=StagePercentiles(
                p50_ms=_round_or_none(r.get("retrieval_p50")),
                p95_ms=_round_or_none(r.get("retrieval_p95")),
                p99_ms=_round_or_none(r.get("retrieval_p99")),
            ),
            hydration=StagePercentiles(
                p50_ms=_round_or_none(r.get("hydration_p50")),
                p95_ms=_round_or_none(r.get("hydration_p95")),
                p99_ms=_round_or_none(r.get("hydration_p99")),
            ),
            rerank=StagePercentiles(
                p50_ms=_round_or_none(r.get("rerank_p50")),
                p95_ms=_round_or_none(r.get("rerank_p95")),
                p99_ms=_round_or_none(r.get("rerank_p99")),
            ),
            total=StagePercentiles(
                p50_ms=_round_or_none(r.get("total_p50")),
                p95_ms=_round_or_none(r.get("total_p95")),
                p99_ms=_round_or_none(r.get("total_p99")),
            ),
        )
        for r in rows
    ]
    return PipelineTimings(
        window_hours=TIMING_WINDOW_HOURS,
        profiles=profiles,
        has_data=bool(profiles),
    )


@router.get("/api/admin/dashboard", response_model=AdminDashboardResponse)
def admin_dashboard(user: str = Depends(verify_basic_auth)):
    """
    Returns aggregate system metrics. Admin-only, read-only.
    All data sourced from Supabase via service key (no RLS).
    """
    # --- Feedback ---
    thumbs_up = supabase_count("search_feedback", {"rating": "eq.up"})
    thumbs_down = supabase_count("search_feedback", {"rating": "eq.down"})

    # --- Contact messages ---
    contact_messages = supabase_count("contact_messages")

    # --- Anonymous traffic (from anon_usage table) ---
    # Today's rows feed the existing counters; the 7d / 30d distinct-IP
    # series are computed from the historical rows preserved by PR #84's
    # composite-PK schema. Pre-PR-#84, prior-day rows were destroyed on
    # rollover and these aggregates were impossible.
    anon_traffic = AnonTraffic()
    try:
        today = datetime.now(timezone.utc).date()
        today_utc = today.isoformat()
        seven_days_ago = (today - timedelta(days=6)).isoformat()
        thirty_days_ago = (today - timedelta(days=29)).isoformat()

        anon_rows = supabase_get("anon_usage", {
            "usage_date": f"eq.{today_utc}",
            "select": "search_count,dna_count",
        })
        # PostgREST default page size caps individual responses around
        # 1000 rows; at current ~5 IPs/day production traffic, 30 days
        # is ~150 rows — well under the cap. Bumping limit to 10000 as
        # a defensive headroom; if we ever exceed that, swap to an RPC
        # that does COUNT(DISTINCT) in Postgres.
        anon_rows_7d = supabase_get("anon_usage", {
            "usage_date": f"gte.{seven_days_ago}",
            "select": "ip_hash",
            "limit": "10000",
        })
        anon_rows_30d = supabase_get("anon_usage", {
            "usage_date": f"gte.{thirty_days_ago}",
            "select": "ip_hash",
            "limit": "10000",
        })

        # Reuse the same cap resolution as the enforcement path — otherwise
        # a missing env var would produce different fallback values here vs
        # in anon_quota, and the `anon_cap_hits_today` count would compare
        # against the wrong ceiling.
        search_cap = _get_cap("search")
        dna_cap = _get_cap("dna")
        anon_traffic = AnonTraffic(
            anon_searches_today=sum(r.get("search_count", 0) for r in anon_rows),
            anon_dna_today=sum(r.get("dna_count", 0) for r in anon_rows),
            anon_unique_ips_today=len(anon_rows),
            anon_cap_hits_today=sum(
                1 for r in anon_rows
                if r.get("search_count", 0) >= search_cap
                or r.get("dna_count", 0) >= dna_cap
            ),
            anon_unique_ips_7d=len({r["ip_hash"] for r in anon_rows_7d}),
            anon_unique_ips_30d=len({r["ip_hash"] for r in anon_rows_30d}),
        )
    except Exception:  # noqa: BLE001, S110 — dashboard tile: fail-soft to zeroed traffic counters rather than 500 the whole view
        pass

    # --- Lockdown status ---
    lockdown_active = False
    try:
        flag_rows = supabase_get("system_flags", {
            "flag_name": "eq.global_lockdown",
            "select": "flag_value",
        })
        lockdown_active = bool(flag_rows and flag_rows[0].get("flag_value", False))
    except Exception:  # noqa: BLE001, S110 — dashboard tile: fail-soft to lockdown_active=False rather than 500
        pass

    # --- API cost estimates ---
    # Production-traffic-only projection: anon searches/DNA today × heuristic.
    # Misses dev / regression / probe / ingestion burn — that's the
    # last_7d_by_origin breakdown below, which reads REAL spend from the
    # gemini_calls ledger written by analysis/gemini_client.py (PR #85).
    all_searches = anon_traffic.anon_searches_today
    all_dna = anon_traffic.anon_dna_today
    gemini_cost_today = (all_searches * COST_PER_SEARCH_USD) + (all_dna * COST_PER_DNA_USD)
    gemini_projected = gemini_cost_today * 30

    # --- Cost attribution from gemini_calls (real spend, last 7 days) ---
    last_7d_total = 0.0
    last_7d_by_origin: dict[str, float] = {}
    try:
        window_start = (
            datetime.now(timezone.utc) - timedelta(days=ATTRIBUTION_WINDOW_DAYS)
        ).isoformat()
        # PostgREST doesn't expose GROUP BY via query params; fetch the
        # raw rows for the window and aggregate in Python. At current
        # scale (a few hundred calls/week peak) this is well under the
        # 10000-row limit; revisit via Postgres RPC if call volume scales
        # past that ceiling.
        call_rows = supabase_get("gemini_calls", {
            "event_at": f"gte.{window_start}",
            "select": "origin,cost_usd_estimate",
            "limit": "10000",
        })
        for row in call_rows:
            origin = row.get("origin", "unknown") or "unknown"
            cost = float(row.get("cost_usd_estimate", 0) or 0)
            last_7d_by_origin[origin] = last_7d_by_origin.get(origin, 0.0) + cost
            last_7d_total += cost
        # Round the aggregates so the dashboard reads cleanly without
        # losing sub-cent fidelity (per-call costs go to 6 decimal places).
        last_7d_by_origin = {k: round(v, 4) for k, v in last_7d_by_origin.items()}
    except Exception as e:  # noqa: BLE001 — dashboard tile: fail-soft to zeroed cost aggregates rather than 500
        logger.warning(f"Cost attribution read failed: {type(e).__name__}: {e}")

    # --- Real Gemini month-to-date spend from gemini_calls ledger ---
    # Replaces the daily-extrapolation projection as the dashboard's
    # headline cost reading. Resets naturally at the UTC month boundary
    # since the filter shifts to the new first-of-month timestamp.
    gemini_mtd = 0.0
    try:
        month_start = (
            datetime.now(timezone.utc)
            .replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            .isoformat()
        )
        mtd_rows = supabase_get("gemini_calls", {
            "event_at": f"gte.{month_start}",
            "select": "cost_usd_estimate",
            "limit": "10000",
        })
        gemini_mtd = sum(float(r.get("cost_usd_estimate", 0) or 0) for r in mtd_rows)
    except Exception as e:  # noqa: BLE001 — dashboard tile: fail-soft to gemini_mtd=0 rather than 500
        logger.warning(f"Gemini MTD read failed: {type(e).__name__}: {e}")

    api_costs = ApiCosts(
        total_searches_today=all_searches,
        total_dna_today=all_dna,
        gemini_cost_today_usd=round(gemini_cost_today, 4),
        gemini_mtd_usd=round(gemini_mtd, 4),
        gemini_ceiling_usd=GEMINI_CEILING_USD,
        production_projection_monthly_usd=round(gemini_projected, 2),
        railway_monthly_usd=RAILWAY_MONTHLY_USD,
        total_projected_monthly_usd=round(RAILWAY_MONTHLY_USD + gemini_projected, 2),
        budget_ceiling_usd=BUDGET_CEILING_USD,
        last_7d_total_usd=round(last_7d_total, 4),
        last_7d_by_origin=last_7d_by_origin,
    )

    # --- Pipeline timings (Supabase RPC) ---
    pipeline_timings = _fetch_pipeline_timings()

    return AdminDashboardResponse(
        feedback_summary=FeedbackSummary(thumbs_up=thumbs_up, thumbs_down=thumbs_down),
        contact_messages=contact_messages,
        anon_traffic=anon_traffic,
        lockdown_active=lockdown_active,
        api_costs=api_costs,
        pipeline_timings=pipeline_timings,
    )


# =====================================================================
# LOCKDOWN TOGGLE
# =====================================================================

@router.get("/api/admin/lockdown")
def get_lockdown_status(user: str = Depends(verify_basic_auth)):
    """Returns the current lockdown state. Admin-only."""
    try:
        rows = supabase_get("system_flags", {
            "flag_name": "eq.global_lockdown",
            "select": "flag_value,updated_at,updated_by",
        })
        if rows:
            return {
                "lockdown_active": bool(rows[0].get("flag_value", False)),
                "updated_by": rows[0].get("updated_by"),
                "updated_at": rows[0].get("updated_at"),
            }
    except Exception:  # noqa: BLE001, S110 — admin status endpoint: fail-soft to lockdown_active=False rather than 500
        pass
    return {"lockdown_active": False, "updated_by": None, "updated_at": None}


@router.post("/api/admin/lockdown", response_model=LockdownResponse)
def toggle_lockdown(body: LockdownRequest, user: str = Depends(verify_basic_auth)):
    """
    Toggle the global lockdown flag. Admin-only.
    When enabled, all non-admin requests return 503.
    Audit-logged: records who toggled and when.
    """
    now = datetime.now(timezone.utc).isoformat()
    admin_id = user  # Basic Auth dep returns the authenticated username

    supabase_patch(
        "system_flags",
        {"flag_value": body.enabled, "updated_at": now, "updated_by": admin_id},
        {"flag_name": "eq.global_lockdown"},
    )

    # Invalidate the middleware cache so lockdown takes effect immediately
    reset_lockdown_cache()

    return LockdownResponse(
        lockdown_active=body.enabled,
        updated_by=admin_id,
        updated_at=now,
    )
