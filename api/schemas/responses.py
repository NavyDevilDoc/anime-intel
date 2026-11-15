"""Pydantic response models for API output contracts."""
from typing import Literal

from pydantic import BaseModel, Field


class DroppedSibling(BaseModel):
    """A reranked-but-collapsed sibling from the same series cluster.

    Captured by Phase D's same-series collapse when multiple entries
    from one series cleared the rerank floor. The visible result is
    the highest-scoring of the bunch; this carries the per-sibling
    signal the user has already paid for (their reranker pass surfaced
    distinct cons / spoiler flags / confidences) so Phase E can decide
    where/whether to render it.
    """
    id: int
    title: str
    match_confidence: int
    query_relevant_con: str | None = None
    contains_spoiler: bool = False


class SeriesContext(BaseModel):
    """Series-level metadata about the show's cluster.

    Computed post-rerank from the show's series_id and the sibling rows
    in anime_info. Optional on ScoredResult — None when the show is a
    standalone (series_size=1) or when the engine bypassed the
    hydration path (e.g., legacy code paths). Frontend renders the
    position pill, trajectory caption, score range, and dropped-siblings
    drawer (Initiative 1 PR #77 Phase E + PR B subtype bundle).
    """
    series_id: str
    series_size: int  # total cluster size including this entry
    position: int     # 1-indexed chronological position within the cluster
    score_min: float | None = None
    score_max: float | None = None
    # PR B: True when score_min/score_max reflect a TV+TV-Special+Movie
    # filter that *narrowed* the visible range vs the full cluster
    # (e.g., Frieren Mini Anime's 7.4 hidden so range shows 8.8 — 9.3
    # for the main S1+film, not 7.4 — 9.3). False when no filter was
    # applied (OVA-only franchise, fallback to full set) or when the
    # filter didn't change the range. Drives an optional caption in the
    # frontend.
    score_range_filtered: bool = False
    sentiment_min: float | None = None
    sentiment_max: float | None = None
    # Reception trajectory across the series. None for size==1; otherwise:
    #   "stable"  — all transitions within ±0.2 (no meaningful drift)
    #   "decline" — monotonically non-increasing AND cumulative drop ≥0.5
    #   "improve" — monotonically non-decreasing AND cumulative rise ≥0.5
    #   "mixed"   — direction reverses across the series
    trajectory: Literal["stable", "decline", "improve", "mixed"] | None = None
    # PR B: "Season X of Y" label support. tv_position is the 1-indexed
    # rank of this entry among TV-subtype siblings (TV Special excluded
    # — not a numbered season). tv_count is the number of TV-subtype
    # siblings in the cluster. Both None when this entry isn't itself
    # TV-subtype (a movie or OVA doesn't get a TV-relative position).
    # Frontend branches: when both set and tv_count > 1, render
    # "Season X of Y, in a Z-entry franchise"; otherwise fall back to
    # "Entry X of N in the franchise".
    tv_position: int | None = None
    tv_count: int | None = None
    # Phase D: other same-series entries that cleared the rerank floor
    # but were collapsed in favor of the higher-scoring visible result.
    # Empty list when no siblings were collapsed for this entry.
    dropped_siblings: list[DroppedSibling] = Field(default_factory=list)


class ShowProfile(BaseModel):
    """Anime profile data from the intelligence vault."""
    id: int = 0
    title: str
    studio: str = ""
    quality_score: float = 0.0
    scored_by: int = 0
    audience_sentiment: float = 0.0
    release_year: int = 0
    synopsis: str = ""
    audience_consensus: str = ""
    pros: list[str] = Field(default_factory=list)
    cons: list[str] = Field(default_factory=list)
    controversy_score: int = 0
    semantic_similarity: float = 0.0
    # MAL content rating (verbatim Jikan string, e.g. "PG-13 - Teens 13 or older").
    # Empty string when MAL has no rating set for the show; None when not yet
    # backfilled. Frontend hides the pill for both.
    mal_rating: str | None = None


class ScoredResult(BaseModel):
    """A single recommendation with AI reasoning."""
    ai_reasoning: str
    match_confidence: int
    controversy_warning: str | None = None
    contains_spoiler: bool = False
    # One con quoted verbatim from the show's consensus_json.cons list, picked
    # by the reranker as most relevant to the user's specific query. Null when
    # no con genuinely speaks to the query (anti-stretch), when the show has
    # no cons in its consensus data, or when the result came from Objective
    # Rankings which bypasses the LLM reranker entirely. Surfaced on result
    # cards as a "Heads up:" line below the AI Reasoning block, gated behind
    # the existing spoiler wall via the same contains_spoiler boolean.
    query_relevant_con: str | None = None
    profile: ShowProfile
    # Cluster-level context for the show's series (multi-season franchise
    # data). None when the show is a standalone or hydration was skipped.
    # Phase E's frontend renders "Season X of N" + trajectory caption.
    series_context: SeriesContext | None = None


class SearchResponse(BaseModel):
    """Response from the search endpoint."""
    success: bool
    pool: list[ScoredResult] = Field(default_factory=list)
    pool_size: int = 0
    search_id: str | None = None
    error: str | None = None
    # "I Don't Know" mode flag. True when the engine has weak confidence
    # in the result set — either the top pick's match_confidence fell
    # below 75, or the pool is empty entirely (no candidate cleared the
    # drop floor). Frontend renders a yellow advisory banner; the user
    # sees the engine being honest about uncertainty instead of either
    # silent results or a cryptic error.
    low_confidence: bool = False
    # COA B / Option C — "composite" when Gemini's LLM reranker was
    # blocked by non-configurable policy (typically PROHIBITED_CONTENT
    # on adult-adjacent queries) and results fell back to the pre-rerank
    # composite-ordered pool. None on the normal path. Frontend uses
    # this to render a "Semantic match only" indicator and swap the
    # banner copy to a fallback-aware variant.
    fallback_mode: str | None = None
    # Anon quota counters reflecting post-request state. Frontend uses these
    # to keep its remaining-search display in sync with the real server count
    # without polling. Optional because non-anon code paths could call the
    # engine directly in the future; nullable means "no update available."
    search_remaining: int | None = None
    dna_remaining: int | None = None


class DNAResponse(BaseModel):
    """Response from the DNA triangulation endpoint."""
    success: bool
    intersection_summary: str | None = None
    data: list = Field(default_factory=list)
    data_size: int = 0
    error: str | None = None
    # Post-resolution echo of the negative anchors the engine actually used
    # — after same-show-collision filtering and silent-skip of unresolvable
    # titles. Lets the frontend confirm to the user that their avoid-list
    # was processed (and which entries survived). None when no negatives
    # were submitted; empty list when all submitted negatives were filtered.
    negative_anchors_used: list[str] | None = None
    # Human-readable disclosures about title-resolution behaviour: fuzzy
    # substitutions (input not in vault, closest match used), skipped
    # negatives (no match at all), and same-show collisions (negative
    # matched a positive). Surfaced to the user as an advisory banner —
    # the "trusted recommender" position requires explicit catalog-limit
    # honesty, not silent input substitution.
    title_resolution_notes: list[str] | None = None
    # "I Don't Know" mode flag — same semantics as SearchResponse.low_confidence
    # but tuned to DNA's behavior. Triggers more often on this surface (~9.6%
    # of journal-recorded searches vs ~2.4% for Find a Show) because the
    # asymmetric drop floor (40) still produces empty pools when a DNA blend
    # has no good overlaps. Top-result-below-75 in-band branch is mathematically
    # available but historically inert — DNA top results have clustered at
    # 100% above 90 since reranker tuning landed.
    low_confidence: bool = False
    # COA B / Option C — same semantics as SearchResponse.fallback_mode.
    # "composite" when the DNA reranker was blocked and the centroid pool
    # is being returned via composite fallback; None on the normal path.
    fallback_mode: str | None = None
    search_remaining: int | None = None
    dna_remaining: int | None = None


class AnonUsageResponse(BaseModel):
    """
    Read-only snapshot of an anonymous caller's remaining quota for today.

    Returned from GET /api/anon-usage. The frontend calls this on page
    mount to seed its display so a freshly-opened tab shows the truthful
    remaining count instead of the hardcoded daily cap.
    """
    search_remaining: int
    dna_remaining: int
    search_cap: int
    dna_cap: int
    reset_at: str  # ISO timestamp of next UTC midnight
