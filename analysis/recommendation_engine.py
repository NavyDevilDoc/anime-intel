"""Central orchestrator for the recommendation pipeline.

Routes a user query through FAISS retrieval (vector_store), Gemini reranking
(via one of four intent-specific prompt paths), same-series collapse, and
series-context hydration. Also owns the DNA Triangulation pipeline used by
Shows Like This.
"""

import functools
import logging
import os
import re
import sys
import time
import uuid

from pydantic import BaseModel, Field

# --- BULLETPROOF PATH ANCHORING ---
SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
if os.path.basename(SCRIPT_DIR) in ['tools', 'analysis', 'src']:
    ROOT_DIR = os.path.dirname(SCRIPT_DIR)
else:
    ROOT_DIR = SCRIPT_DIR

if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from analysis import gemini_client, prompts, queries, vector_store
from analysis import telemetry_logger as telemetry
from analysis.series_clustering import compute_series_context

logger = logging.getLogger(__name__)

# "I Don't Know" mode — banner fires when the top result is below this
# threshold OR the pool is empty entirely. Drop floor (_FIND_A_SHOW_FLOOR /
# _DNA_FLOOR) is applied independently before this check, so the in-band
# case only fires for results that already cleared the floor. Threshold
# chosen empirically from search_journal.jsonl analysis (see
# tools/analyze_confidence_distribution.py for the reproducible baseline)
# — top results cluster at 95+ overwhelmingly, so the value matters less
# than the architecture.
LOW_CONFIDENCE_THRESHOLD = 75

# Per-path confidence floors applied to reranker output before assembling
# the visible pool. Empirically calibrated:
#   - Find a Show (65): tuned via the eval suite in PR #101 / COA D.
#   - DNA (40): tuned via AoT SLT probe — raised to 40 kept the pool
#     non-empty across borderline safety-block cases.
_FIND_A_SHOW_FLOOR = 65
_DNA_FLOOR = 40

# User-facing message returned when the Gemini reranker hits Google's
# quota / rate-limit surface. Identical copy across all four rerank
# paths.
_RATE_LIMIT_ERROR_MSG = (
    "System cooling down. API rate limits are currently at maximum "
    "capacity. Please try again in 60 seconds."
)

# Per-intent thinking_budget caps for the gemini-2.5-flash reranker
# (PR #87 Phase D). Calibrated from Phase A's empirical probe:
#  - SDK default produces 3,800-5,000 thoughts per call (mostly
#    unbounded reasoning, dominates per-call cost).
#  - Mechanical intents (FUSION, CONSTRAINT) tolerate budget=0 with
#    no structural quality loss — they rank within an already-filtered
#    pool, not from scratch.
#  - Reasoning-heavy intents (NARRATIVE, DNA) keep modest headroom
#    against the saturation observed at low caps (DNA used 125/128
#    thoughts at budget=128). DNA is unified across the rerank and
#    triangulation paths; raise the unified constant if Phase E
#    surfaces SLT-pool composition drift.
# Phase E regression suite is the semantic-quality gate; these are
# starting values, not load-bearing assertions about correctness.
FUSION_THINKING_BUDGET = 0
CONSTRAINT_THINKING_BUDGET = 0
NARRATIVE_THINKING_BUDGET = 128
DNA_THINKING_BUDGET = 1024  # was 256; raised after AoT SLT probe showed 1/3 empty at 512, 0/3 at 1024+

# --- PYDANTIC SCHEMAS FOR STRUCTURED UI OUTPUT ---

class RerankedShow(BaseModel):
    title: str = Field(description="The exact english_title of the selected show.")
    match_confidence: int = Field(description="A score from 0 to 100 indicating how well the show matches the requested vibe or DNA.")
    reasoning: str = Field(description="A 1-sentence explanation of why the objective plot and subjective consensus match the user's intent.")
    controversy_driver: str | None = Field(default=None, description="Include ONLY if Controversy Score >= 6. Explain the division.")
    contains_spoiler: bool = Field(default=False, description="True if the reasoning OR query_relevant_con reveals a major plot twist, character death, betrayal, identity reveal, or ending.")
    query_relevant_con: str | None = Field(default=None, description="One con from this show's Cons list, quoted verbatim, that most directly addresses friction with the user's query. Null when no con genuinely speaks to the query (anti-stretch) or when the candidate has no cons.")

class TriangulationPlan(BaseModel):
    intersection_summary: str = Field(description="1-2 sentence analysis of the shared DNA.")


# --- LLM RERANKER BLOCK HANDLING (COA B / Option C) ---
#
# Gemini's non-configurable PROHIBITED_CONTENT policy silently blocks certain
# query classes (fan-service / ecchi surfaced first, but the mechanism is
# universal). When blocked, the SDK returns a response with `.parsed = None`
# and `.prompt_feedback.block_reason` populated. Pre-2026-09, all four rerank
# helpers converted this to a silent `return []`, which downstream became a
# `success: True + pool: []` result — indistinguishable from "engine
# correctly determined no matches." Investigation trail lives in the COA B
# probes (tools/probe_rerank_safety.py, probe_rerank_relaxed.py,
# probe_composite_fallback.py) captured 2026-08-31 through 2026-09-02.
#
# Fix pattern: detect the block via prompt_feedback, signal it to callers
# via a sentinel list, and let the caller fall back to the pre-rerank
# composite-ordered pool with a clear "semantic match only" label.

# Sentinel values returned by the four rerank helpers in place of a
# parsed Pydantic list. Callers use the `is_rerank_blocked` and
# `is_rate_limited` predicates below rather than string comparisons.
_RERANK_BLOCKED_SENTINEL = ["RERANK_BLOCKED"]
_RATE_LIMIT_SENTINEL = ["RATE_LIMIT_ERROR"]


def is_rerank_blocked(result) -> bool:
    """True when the reranker was blocked by Gemini's PROHIBITED_CONTENT
    policy — caller should fall back to composite-ordered ranking."""
    return result == _RERANK_BLOCKED_SENTINEL


def is_rate_limited(result) -> bool:
    """True when the Gemini call surfaced a 429 / quota-exhausted error —
    caller should short-circuit with a user-facing cooling-down message."""
    return result == _RATE_LIMIT_SENTINEL


# Confidence assigned to composite-fallback entries. Above both drop
# floors so the fallback surfaces at all, but below LOW_CONFIDENCE_THRESHOLD
# (75) so the banner still fires — the user needs to know reranker analysis
# wasn't available. Tune conservatively as we gather feedback on fallback UX.
_FALLBACK_CONFIDENCE = 70

_FALLBACK_REASONING = (
    "Semantic match only — detailed analysis unavailable for this content class."
)


# ---------------------------------------------------------------------
# Embedding-layer rate-limit handling
# ---------------------------------------------------------------------
# vector_store._embed raises EmbeddingRateLimited when its internal
# retry loop (5 attempts with exponential backoff) has been exhausted
# on sustained 429s. Without a catch somewhere in the engine stack,
# the exception propagates to FastAPI's unhandled-exception middleware
# and renders as a scary "Internal Server Error" red block in the UI.
#
# The decorator below wraps the two engine entry points
# (fetch_vault_pool and execute_dna_triangulation) and converts the
# typed exception into the standard success:False response dict that
# the search/dna routes already know how to render — no route-layer
# changes needed.
#
# Parallel to the Phase 2 reranker's _RATE_LIMIT_SENTINEL pattern
# above, but adapted for Phase 1: the embedding error is a raised
# exception from deep code, not a sentinel return threaded up through
# conditionals, so a decorator catch is the clean intercept.

_RATE_LIMITED_SEARCH_RESPONSE = {
    "success": False,
    "error": "Search temporarily rate-limited — try again in a minute.",
    "low_confidence": False,
    "fallback_mode": None,
}

_RATE_LIMITED_DNA_RESPONSE = {
    "success": False,
    "error": "DNA Triangulation temporarily rate-limited — try again in a minute.",
    "low_confidence": False,
    "fallback_mode": None,
    "title_resolution_notes": None,
}


def _rate_limit_safe(response_template):
    """Decorator factory: returns a decorator that catches
    `vector_store.EmbeddingRateLimited` and returns a shallow copy of
    `response_template` instead of propagating. Preserves the decorated
    method's signature via functools.wraps so FastAPI dependency wiring
    and existing call sites are unaffected."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            try:
                return fn(self, *args, **kwargs)
            except vector_store.EmbeddingRateLimited as e:
                logger.warning(
                    "Gemini embedding rate-limit surfaced to engine for %s; "
                    "returning user-friendly response. Detail: %s",
                    fn.__name__, e,
                )
                return dict(response_template)
        return wrapper
    return decorator


def _handle_rerank_error(exc: Exception, label: str):
    """Interpret an exception raised by a rerank `generate_content` call
    and translate to one of the sentinel returns. Blind Exception catch
    is intentional — the Gemini SDK exposes rate-limit errors as
    generic exceptions whose message text is the only reliable
    discriminator ("429" / "quota" / "exhausted"). `label` names the
    rerank path for the log line.
    """
    error_msg = str(exc).lower()
    if "429" in error_msg or "quota" in error_msg or "exhausted" in error_msg:
        logger.error(f"API Rate Limit Exceeded during {label} Reranking.")
        return list(_RATE_LIMIT_SENTINEL)
    logger.error(f"LLM {label} Reranking Error: {exc}")
    return []


def _extract_reranker_result(response):
    """Interpret a Gemini reranker response envelope.

    Returns one of:
      - `response.parsed` (list of RerankedShow) — normal success path
      - `_RERANK_BLOCKED_SENTINEL` — Gemini blocked the prompt (safety /
        PROHIBITED_CONTENT / any other block_reason). Caller falls back
        to composite scoring via `is_rerank_blocked()`.
      - `[]` — empty response with no block reason. Rare edge case, logged
        for future diagnosis but treated as "genuinely nothing to rank."
    """
    if response.parsed:
        return response.parsed

    pf = getattr(response, "prompt_feedback", None)
    block_reason = getattr(pf, "block_reason", None) if pf is not None else None
    if block_reason is not None:
        safety_ratings = getattr(pf, "safety_ratings", None)
        logger.warning(
            f"Reranker blocked by Gemini policy: block_reason={block_reason}, "
            f"safety_ratings={safety_ratings}"
        )
        return list(_RERANK_BLOCKED_SENTINEL)

    logger.warning(
        "Reranker returned empty response with no block_reason. "
        f"candidates_present={bool(getattr(response, 'candidates', None))}"
    )
    return []


def _build_composite_fallback(pool):
    """Synthesize scored_pool entries from a composite-ordered candidate pool
    when the LLM reranker was blocked. `pool` is the exact list of hydrated
    profiles that would have been sent to the reranker — already ranked by
    the composite (semantic + quality + popularity) score inside
    vector_store.search / search_by_centroid.

    Every entry gets the same fixed match_confidence and honest reasoning
    string. No per-show LLM analysis means no query_relevant_con and no
    spoiler flag — those fields stay None/False. Downstream series-collapse
    and series-context hydration still apply, keeping variety and context.
    """
    return [
        {
            "ai_reasoning": _FALLBACK_REASONING,
            "match_confidence": _FALLBACK_CONFIDENCE,
            "controversy_warning": None,
            "contains_spoiler": False,
            "query_relevant_con": None,
            "profile": p,
        }
        for p in pool
    ]


def _build_candidates_block(pool, include_quality: bool = False) -> str:
    """Format a candidate pool as the plain-text block interpolated into
    reranker prompts (Fusion / Constraint / DNA). Constraint uses
    `include_quality=True` to surface Quality Score + Audience Mood so the
    prompt can reason about numeric constraints; Fusion and DNA omit both.

    Kept out of `_rerank_*` methods so the exact wire text is
    inspectable in one place — prompt shape is load-bearing for
    ranking behavior. Narrative uses a different structure and builds
    its own block inline.
    """
    lines = []
    for p in pool:
        lines.append(f"Title: {p['title']}")
        lines.append(f"Semantic Match: {p.get('semantic_similarity', 0.0):.2f}")
        if include_quality:
            lines.append(f"Quality Score: {p.get('quality_score', 0.0)}")
            lines.append(f"Audience Mood: {p.get('audience_sentiment', 0.0):.2f}")
        lines.append(f"Scored By: {p.get('scored_by', 0):,} users")
        lines.append(f"Synopsis: {p['synopsis']}")
        lines.append(f"Audience Consensus: {p['audience_consensus']}")
        cons_list = p.get('cons') or []
        if cons_list:
            lines.append("Cons:")
            lines.extend(f"  - {c}" for c in cons_list[:8])
        lines.append(f"Controversy Score: {p['controversy_score']}/10")
        lines.append("-" * 40)
    return "\n".join(lines) + "\n"


def _finalize_top_picks(top_picks, pool, floor: int):
    """Sort reranker output by match_confidence, drop below-floor picks,
    and rebuild scored_pool entries by joining back to the hydrated pool
    profile. Runs on the parsed-Pydantic path only — callers must handle
    the sentinel returns (is_rerank_blocked / is_rate_limited) before
    calling.

    O(pool_size) via a title→profile lookup dict rather than the linear
    `next(...)` scan the pre-refactor code used per pick.
    """
    top_picks = sorted(top_picks, key=lambda x: x.match_confidence, reverse=True)
    pool_by_title = {p['title']: p for p in pool}
    scored_pool = []
    for pick in top_picks:
        if pick.match_confidence < floor:
            continue
        profile_data = pool_by_title.get(pick.title)
        if profile_data is None:
            continue
        scored_pool.append({
            "ai_reasoning": pick.reasoning,
            "match_confidence": pick.match_confidence,
            "controversy_warning": pick.controversy_driver,
            "contains_spoiler": pick.contains_spoiler,
            "query_relevant_con": pick.query_relevant_con,
            "profile": profile_data,
        })
    return scored_pool


# --- SERIES-CONTEXT HYDRATION (Phase C) + SAME-SERIES COLLAPSE (Phase D) ---

def _compute_sibling_exclude_titles(reference_profiles, resolved_titles):
    """Return the set of titles that share a series cluster with any of
    the resolved reference titles. Used by execute_dna_triangulation
    to widen search_by_centroid's exclusion set beyond the prefix-based
    find_franchise_titles heuristic.

    Reference titles themselves are kept OUT of the returned set —
    search_by_centroid already excludes them via the `titles` argument.
    Profiles missing series_id (legacy/malformed data) contribute
    nothing; degrades gracefully to the prefix-based exclusion alone.
    """
    ref_series_ids = []
    for prof in reference_profiles:
        sid = prof.get("series_id")
        if sid is not None:
            ref_series_ids.append(str(sid))
    if not ref_series_ids:
        return set()
    sibling_map = queries.fetch_series_siblings(ref_series_ids)
    titles = set()
    for siblings in sibling_map.values():
        for s in siblings:
            t = s.get("title")
            if t and t not in resolved_titles:
                titles.add(t)
    return titles


def _series_collapse_enabled():
    """Read the SERIES_COLLAPSE_ENABLED env var. Default true; only the
    explicit falsy values disable. Re-read on each call so Railway can
    toggle without a redeploy (engine instance is process-scoped but
    env_var reads are cheap)."""
    val = os.environ.get("SERIES_COLLAPSE_ENABLED", "true").strip().lower()
    return val not in ("false", "0", "no", "off")


def _collapse_same_series(scored_pool):
    """Group scored_pool entries by their profile.series_id; keep the
    highest-confidence entry per series. Caller must pass a pool already
    sorted by match_confidence DESC — the first occurrence per series_id
    wins.

    Dropped siblings are stashed on the kept entry's `_dropped_siblings`
    list (private key, consumed by _hydrate_series_context and removed
    before the response payload is built). Each sibling carries the
    minimal user-facing data the reranker already produced: id, title,
    match_confidence, query_relevant_con, contains_spoiler.

    Entries whose profile lacks series_id (legacy or malformed) pass
    through individually — we won't dedup on missing data.

    Disabled when SERIES_COLLAPSE_ENABLED env var is set to a falsy
    value (default: enabled). Returns the input list unchanged when
    disabled or empty.
    """
    if not scored_pool or not _series_collapse_enabled():
        return scored_pool

    seen_sids = {}  # series_id -> kept entry reference
    collapsed = []
    for entry in scored_pool:
        profile = entry.get("profile") or {}
        sid = profile.get("series_id")
        if sid is None:
            collapsed.append(entry)
            continue
        sid_key = str(sid)
        if sid_key not in seen_sids:
            seen_sids[sid_key] = entry
            collapsed.append(entry)
        else:
            kept = seen_sids[sid_key]
            sibling_profile = entry.get("profile") or {}
            kept.setdefault("_dropped_siblings", []).append({
                "id": sibling_profile.get("id"),
                "title": sibling_profile.get("title"),
                "match_confidence": entry.get("match_confidence"),
                "query_relevant_con": entry.get("query_relevant_con"),
                "contains_spoiler": entry.get("contains_spoiler", False),
            })
    return collapsed


def _hydrate_series_context(scored_pool):
    """Attach a SeriesContext payload to each entry in scored_pool in-place.

    Reads the profile's `series_id`, batches a single SQLite query for
    every distinct series_id in the pool, and computes the per-entry
    context via the pure helper in analysis.series_clustering. Also
    merges any `_dropped_siblings` left behind by _collapse_same_series
    into the resulting SeriesContext.dropped_siblings list (then strips
    the private key from the entry).

    Entries whose profile lacks `series_id` get series_context = None.
    Errors in the hydration path are swallowed and logged so a broken
    sibling fetch never blocks search results from reaching the user.
    """
    if not scored_pool:
        return

    try:
        unique_sids = []
        seen = set()
        for entry in scored_pool:
            sid = (entry.get("profile") or {}).get("series_id")
            if sid is not None and sid not in seen:
                seen.add(sid)
                unique_sids.append(sid)

        siblings_by_sid = queries.fetch_series_siblings(unique_sids) if unique_sids else {}

        for entry in scored_pool:
            profile = entry.get("profile") or {}
            sid = profile.get("series_id")
            target_id = profile.get("id")
            dropped = entry.pop("_dropped_siblings", None) or []
            if sid is None or target_id is None:
                entry["series_context"] = None
                continue
            ctx = compute_series_context(
                siblings=siblings_by_sid.get(str(sid), []),
                target_mal_id=target_id,
                series_id=str(sid),
            )
            if ctx is not None and dropped:
                ctx["dropped_siblings"] = dropped
            entry["series_context"] = ctx
    except Exception as exc:  # noqa: BLE001 — series-context is a nice-to-have; a broken sibling fetch must never block search results from reaching the user
        logger.warning(f"Series-context hydration failed: {exc}")
        for entry in scored_pool:
            entry.pop("_dropped_siblings", None)
            entry.setdefault("series_context", None)

# --- THE ORCHESTRATOR ---

class RecommendationEngine:
    def __init__(self, api_key):
        self.client = gemini_client.make_client(api_key)
        self.model_id = "gemini-2.5-flash"

    def _rerank_candidates(self, user_prompt, current_batch, retrieval_meta=None):
        """Phase 2: Evaluates a specific chunk of fusion profiles."""
        if not current_batch:
            return []

        candidates_block = _build_candidates_block(current_batch)

        prompt = prompts.FUSION_RERANKER_PROMPT.format(
            user_prompt=user_prompt,
            candidates_block=candidates_block,
        )

        # When structured filters (studio, decade, genre) are active, tell the
        # reranker that all candidates already satisfy those constraints so it
        # doesn't second-guess them or penalize candidates it thinks don't fit.
        if retrieval_meta:
            filters = retrieval_meta.get("filters_applied") or {}
            preamble_parts = []
            if filters.get("studio"):
                preamble_parts.append(f"Studio: {filters['studio']}")
            if filters.get("year_min") and filters.get("year_max"):
                preamble_parts.append(f"Release years: {filters['year_min']}-{filters['year_max']}")
            elif filters.get("year_min"):
                preamble_parts.append(f"Released after {filters['year_min']}")
            elif filters.get("year_max"):
                preamble_parts.append(f"Released before {filters['year_max']}")
            if filters.get("genre_db_terms"):
                preamble_parts.append(f"Genre: {', '.join(filters['genre_db_terms'])}")
            if filters.get("mal_rating_max"):
                preamble_parts.append(f"MAL rating at or below {filters['mal_rating_max']}")
            if preamble_parts:
                constraint_note = "; ".join(preamble_parts)
                prompt += f"\n\nIMPORTANT: All candidates have already been filtered to satisfy: {constraint_note}. Do NOT penalize or question whether a candidate meets these constraints — they are guaranteed. Rank purely by thematic and tonal fit to the user's request."

            # PR C — watchlist exclusion context. When the user opted to
            # exclude their watched/dropped list, surface the count to the
            # reranker so it understands the candidate pool was narrowed
            # by user intent rather than by data scarcity. Helps the model
            # avoid over-stretching to fill slots when the post-exclusion
            # pool is thin. Reads `exclude_mal_ids_count` (set → int
            # collapsed at the telemetry boundary; see
            # vector_store._filters_for_telemetry).
            excluded_count = filters.get("exclude_mal_ids_count") or 0
            if excluded_count > 0:
                prompt += (
                    f"\n\nCONTEXT: The user has already watched or dropped "
                    f"{excluded_count} shows that have been filtered out of "
                    f"this candidate pool. Rank the remaining candidates honestly "
                    f"— if no candidate is a strong match for the user's request, "
                    f"score them conservatively rather than stretching for a fit."
                )

        # Force strict compliance on chunk size
        prompt += f"\n\nCRITICAL: You MUST evaluate and return exactly {len(current_batch)} shows from the list above. Do not drop any shows. Assign a 'match_confidence' score (0-100) to each and return them in ranked order."

        try:
            response = gemini_client.generate_content(
                self.client,
                model=self.model_id,
                contents=prompt,
                thinking_budget=FUSION_THINKING_BUDGET,
                config={
                    # Reranker-stochasticity lever 1 (2026-09-20 probe). Baseline
                    # captures at SDK-default temperature (1.0) showed top-3
                    # Jaccard 0.65 across identical repeated queries on IS
                    # entries — the reranker's stochastic sampler drives real
                    # user-visible churn in result sets. temperature=0 shrinks
                    # the sampler's variance to its API-exposed minimum. Doesn't
                    # eliminate variance (Gemini has no seed parameter) but
                    # removes the largest tunable knob.
                    "temperature": 0,
                    "response_mime_type": "application/json",
                    "response_schema": list[RerankedShow]
                }
            )
            return _extract_reranker_result(response)
        except Exception as e:  # noqa: BLE001 — see _handle_rerank_error
            return _handle_rerank_error(e, "Fusion")

    def _rerank_constrained_candidates(self, user_prompt, constraint_summary, current_batch):
        """Phase 2 for constraint-aware queries: includes quality_score and audience_sentiment
        in the candidates block and uses the CONSTRAINT_RERANKER_PROMPT."""
        if not current_batch:
            return []

        candidates_block = _build_candidates_block(current_batch, include_quality=True)

        prompt = prompts.CONSTRAINT_RERANKER_PROMPT.format(
            user_prompt=user_prompt,
            constraint_summary=constraint_summary,
            candidates_block=candidates_block,
        )
        prompt += f"\n\nCRITICAL: You MUST evaluate and return exactly {len(current_batch)} shows from the list above. Do not drop any shows. Assign a 'match_confidence' score (0-100) to each and return them in ranked order."

        try:
            response = gemini_client.generate_content(
                self.client,
                model=self.model_id,
                contents=prompt,
                thinking_budget=CONSTRAINT_THINKING_BUDGET,
                config={
                    "temperature": 0,  # See FUSION rerank for rationale
                    "response_mime_type": "application/json",
                    "response_schema": list[RerankedShow]
                }
            )
            return _extract_reranker_result(response)
        except Exception as e:  # noqa: BLE001 — see _handle_rerank_error
            return _handle_rerank_error(e, "Constrained")

    def _rerank_narrative_candidates(self, user_prompt, current_batch):
        """Phase 2 for narrative queries: includes plot arcs, character dynamics,
        and character archetypes in the candidates block."""
        if not current_batch:
            return []

        candidates_block = ""
        for p in current_batch:
            candidates_block += f"Title: {p['title']}\n"
            candidates_block += f"Semantic Match: {p.get('semantic_similarity', 0.0):.2f}\n"
            candidates_block += f"Synopsis: {p['synopsis']}\n"

            # Include narrative data if available
            narrative = p.get("narrative_data", {})
            if narrative:
                arcs = narrative.get("narrative_arcs", [])
                if arcs:
                    candidates_block += f"Narrative Arcs: {'; '.join(arcs)}\n"
                dynamics = narrative.get("character_dynamics", [])
                if dynamics:
                    candidates_block += f"Character Dynamics: {'; '.join(dynamics)}\n"
                archetypes = narrative.get("character_archetypes", [])
                if archetypes:
                    candidates_block += f"Character Archetypes: {'; '.join(archetypes)}\n"
                scenarios = narrative.get("key_scenarios", [])
                if scenarios:
                    candidates_block += f"Key Scenarios: {'; '.join(scenarios)}\n"
                trajectory = narrative.get("emotional_trajectory", "")
                if trajectory:
                    candidates_block += f"Emotional Arc: {trajectory}\n"
                tone = narrative.get("tone_shifts", "")
                if tone and tone.lower() != "none":
                    candidates_block += f"Tone Shifts: {tone}\n"
            else:
                candidates_block += "Narrative Data: Not available\n"

            candidates_block += f"Audience Consensus: {p['audience_consensus']}\n"
            cons_list = p.get('cons') or []
            if cons_list:
                candidates_block += "Cons:\n" + "\n".join(f"  - {c}" for c in cons_list[:8]) + "\n"
            candidates_block += f"Controversy Score: {p['controversy_score']}/10\n"
            candidates_block += "-"*40 + "\n"

        prompt = prompts.NARRATIVE_RERANKER_PROMPT.format(
            user_prompt=user_prompt,
            candidates_block=candidates_block,
        )
        prompt += f"\n\nCRITICAL: You MUST evaluate and return exactly {len(current_batch)} shows from the list above. Do not drop any shows. Assign a 'match_confidence' score (0-100) to each and return them in ranked order."

        try:
            response = gemini_client.generate_content(
                self.client,
                model=self.model_id,
                contents=prompt,
                thinking_budget=NARRATIVE_THINKING_BUDGET,
                config={
                    "temperature": 0,  # See FUSION rerank for rationale
                    "response_mime_type": "application/json",
                    "response_schema": list[RerankedShow]
                }
            )
            return _extract_reranker_result(response)
        except Exception as e:  # noqa: BLE001 — see _handle_rerank_error
            return _handle_rerank_error(e, "Narrative")

    def _rerank_dna_candidates(self, intersection_summary, reference_titles, current_batch, negative_anchors=None, watchlist_exclusion_count=0):
        """Phase 2 for DNA Triangulation: scores candidates against thematic DNA, not a freeform query.

        When `negative_anchors` is non-empty, the prompt includes an explicit
        AVOID-RESEMBLING section so the LLM can lower scores for candidates
        that share unwanted qualities with the negative anchors and mention
        the conflict in the reasoning surfaced to the user.

        When `watchlist_exclusion_count` > 0 (PR C), the prompt surfaces the
        count so the reranker knows the candidate pool was narrowed by user
        intent and can score conservatively rather than stretching for a fit.
        """
        if not current_batch:
            return []

        candidates_block = _build_candidates_block(current_batch)

        if negative_anchors:
            negative_anchors_block = (
                f"\nAVOID-RESEMBLING SHOWS: {', '.join(negative_anchors)}\n"
                f"The user explicitly indicated these shows do NOT match what they want. "
                f"For each candidate, check whether it shares core qualities (tone, pacing, "
                f"themes, character archetypes) with the avoid-shows that conflict with the "
                f"DNA Profile above. If a candidate strongly resembles an avoid-show in those "
                f"ways, LOWER its match_confidence and explicitly name the conflict in the "
                f"reasoning so the user can see the system understood what they wanted to "
                f"avoid. Surface-genre overlap alone is NOT a reason to lower the score — "
                f"judge on what specifically makes the avoid-shows unwanted in this context.\n"
            )
        else:
            negative_anchors_block = ""

        prompt = prompts.DNA_RERANKER_PROMPT.format(
            reference_titles=", ".join(reference_titles),
            intersection_summary=intersection_summary,
            negative_anchors_block=negative_anchors_block,
            candidates_block=candidates_block,
        )
        prompt += f"\n\nEvaluate all {len(current_batch)} shows above. Assign a 'match_confidence' score (0-100) to each and return them in ranked order. If a show has no meaningful thematic connection, assign it a score below 30 — it will be filtered out downstream."

        # PR C — watchlist exclusion context (mirrors the FUSION-path note
        # in fetch_vault_pool's preamble).
        if watchlist_exclusion_count > 0:
            prompt += (
                f"\n\nCONTEXT: The user has already watched or dropped "
                f"{watchlist_exclusion_count} shows that have been filtered out "
                f"of this candidate pool. Score honestly — if no candidate is "
                f"a strong DNA match, lower scores are correct, not a failure mode."
            )

        try:
            response = gemini_client.generate_content(
                self.client,
                model=self.model_id,
                contents=prompt,
                thinking_budget=DNA_THINKING_BUDGET,
                config={
                    "temperature": 0,  # See FUSION rerank for rationale
                    "response_mime_type": "application/json",
                    "response_schema": list[RerankedShow]
                }
            )
            return _extract_reranker_result(response)
        except Exception as e:  # noqa: BLE001 — see _handle_rerank_error
            return _handle_rerank_error(e, "DNA")

    # =====================================================================
    # FIND A SHOW PIPELINE (Ahead-of-Time global scoring)
    # =====================================================================

    @_rate_limit_safe(_RATE_LIMITED_SEARCH_RESPONSE)
    def fetch_vault_pool(self, user_prompt, lens_name="Intelligent Search", year_min=None, year_max=None, era_label=None, rating_max=None, exclude_mal_ids=None, staff_filter=None):
        """STEP 1: Retrieves candidates via FAISS vector search, Objective Rankings, or Staff/Studio SQL, then PRE-SCORES."""

        t_start = time.perf_counter()
        timings = {}

        retrieval_meta = None
        # COA B / Option C — set to "composite" when the LLM reranker is
        # blocked by Gemini policy and we fall back to composite-ranked
        # results. None otherwise. Surfaces on the response envelope +
        # search_journal so callers and analytics can distinguish reranked
        # from fallback pools.
        fallback_mode = None
        t0 = time.perf_counter()
        if lens_name == "Staff/Studio":
            # Item 4a — deterministic chip-filtered SQL. Bypasses FAISS
            # retrieval + Gemini reranker entirely (same architectural
            # slot as Objective Rankings). Rating filter and watchlist
            # exclusion intentionally deferred to v2 — Staff/Studio is a
            # "shows by X" surface where narrowing further via those
            # controls isn't yet demonstrated to help. Revisit if usage
            # signal warrants.
            search_results = vector_store.staff_studio_search(staff_filter, top_k=15)
        elif lens_name == "Objective Rankings":
            # Objective Rankings deliberately bypasses the rating filter for v1:
            # it operates on a different code path (direct SQL sort), and the
            # mode is already a power-user surface. Add later only if signal warrants.
            # Watchlist exclusion (PR C) follows the same v1-bypass rationale —
            # Objective Rankings is the "show me the top of the catalog" mode,
            # and watchlist exclusion isn't a natural fit. Revisit if usage signal warrants.
            search_results = vector_store.objective_rankings_search(user_prompt, top_k=50)
        else:
            search_response = vector_store.search(
                user_prompt,
                top_k=50,
                year_min=year_min,
                year_max=year_max,
                rating_max=rating_max,
                exclude_mal_ids=exclude_mal_ids,
            )
            search_results = search_response["results"]
            retrieval_meta = search_response["meta"]
        timings["retrieval_ms"] = round((time.perf_counter() - t0) * 1000, 2)

        if not search_results:
            timings["total_ms"] = round((time.perf_counter() - t_start) * 1000, 2)
            telemetry.log_search_journal({
                "mode": "find_a_show",
                "query": user_prompt,
                "lens": lens_name,
                "era": era_label,
                "success": False,
                "error": "no_results",
                "timings": timings,
            })
            # "I Don't Know" mode: empty pool is the strongest expression
            # of the trust positioning — the engine being explicit that
            # it has nothing strong, rather than surfacing a cryptic
            # error. Frontend prefers low_confidence over the error field.
            return {
                "success": False,
                "error": "No classified intelligence matched that specific combination of traits.",
                "low_confidence": True,
            }

        # Build similarity lookup from search results
        sim_lookup = {r["title"]: r["similarity"] for r in search_results}
        candidate_titles = [r["title"] for r in search_results]

        t0 = time.perf_counter()
        fusion_profiles = queries.fetch_fusion_profiles(candidate_titles)
        timings["hydration_ms"] = round((time.perf_counter() - t0) * 1000, 2)

        # Inject similarity scores and re-sort to match search result order.
        # fetch_fusion_profiles() returns profiles in SQLite insertion order,
        # not the search ranking order. Without re-sorting, the top-15 slice
        # for Phase 2 would be random DB order, not the best candidates.
        title_order = {t: i for i, t in enumerate(candidate_titles)}
        for p in fusion_profiles:
            p["semantic_similarity"] = sim_lookup.get(p["title"], 0.0)
        fusion_profiles.sort(key=lambda x: title_order.get(x["title"], 999))

        if lens_name == "Objective Rankings":
            # Preserve the SQL ordering (already sorted by mal_score DESC)
            pool = fusion_profiles
            scored_pool = []
            for idx, p in enumerate(pool[:30]):
                scored_pool.append({
                    "ai_reasoning": f"Objective Result #{idx + 1} from direct Vault query.",
                    "match_confidence": 100,
                    "controversy_warning": None,
                    "contains_spoiler": False,
                    "query_relevant_con": None,
                    "profile": p
                })
            timings["rerank_type"] = "objective"
            timings["rerank_ms"] = 0.0
        elif lens_name == "Staff/Studio":
            # Item 4a — SQL-ordered chip filter results, no reranker.
            # Match_confidence pinned at 100 (deterministic filter, not a
            # scored match). Reasoning kept minimal and mode-scoped so
            # the result card renders without pretending an AI ranking
            # took place. Frontend can suppress the "AI Reasoning" line
            # entirely for this mode if desired (Phase 5 polish).
            pool = fusion_profiles
            scored_pool = []
            for idx, p in enumerate(pool[:15]):
                scored_pool.append({
                    "ai_reasoning": f"Staff/Studio match #{idx + 1} from chip-filtered Vault query.",
                    "match_confidence": 100,
                    "controversy_warning": None,
                    "contains_spoiler": False,
                    "query_relevant_con": None,
                    "profile": p
                })
            timings["rerank_type"] = "staff_studio"
            timings["rerank_ms"] = 0.0
        else:
            # LLM-reranked branches share the same envelope: run one of the
            # three intent-specific rerankers, then either short-circuit on
            # rate limit, fall back to composite scoring on Gemini block,
            # or finalize the picks by sorting + flooring against the pool.
            pool = fusion_profiles[:15]
            t0 = time.perf_counter()
            if retrieval_meta and retrieval_meta.get("intent") == "narrative":
                top_picks = self._rerank_narrative_candidates(user_prompt, pool)
                timings["rerank_type"] = "narrative"
            elif retrieval_meta and retrieval_meta.get("has_numeric_constraints"):
                constraint_summary = retrieval_meta.get("constraint_summary", "")
                top_picks = self._rerank_constrained_candidates(user_prompt, constraint_summary, pool)
                timings["rerank_type"] = "constrained"
            else:
                top_picks = self._rerank_candidates(user_prompt, pool, retrieval_meta=retrieval_meta)
                timings["rerank_type"] = "fusion"
            timings["rerank_ms"] = round((time.perf_counter() - t0) * 1000, 2)

            if is_rate_limited(top_picks):
                return {"success": False, "error": _RATE_LIMIT_ERROR_MSG}
            if is_rerank_blocked(top_picks):
                scored_pool = _build_composite_fallback(pool)
                fallback_mode = "composite"
            else:
                scored_pool = _finalize_top_picks(top_picks, pool, _FIND_A_SHOW_FLOOR)

        # --- Same-series collapse (Phase D) ---
        # Groups same-series candidates that all cleared the floor and
        # keeps only the highest-scoring per series. Dropped siblings
        # ride along on a private key consumed by the hydration step.
        # Toggleable via SERIES_COLLAPSE_ENABLED env var.
        #
        # Item 4a — Skip for Staff/Studio. The whole point of chip-filtering
        # by a director or studio is to see their full catalog, including
        # multiple seasons of the same franchise. Collapsing 4 AoT seasons +
        # 5 JJK seasons under a "MAPPA" filter dropped 15 candidates to 7
        # (surfaced live 2026-09-17). IS/OR modes still collapse — they're
        # about surfacing distinct experiences, not attribution catalogs.
        if lens_name != "Staff/Studio":
            scored_pool = _collapse_same_series(scored_pool)

        # --- Series-context hydration (Phase C) ---
        # Batched single-query fetch of all siblings for every distinct
        # series_id in scored_pool, then in-memory compute of position,
        # ranges, and trajectory. One SQLite read per request regardless
        # of pool size; pure-logic computation per result. Also merges
        # any dropped-sibling data the collapse step left behind into
        # SeriesContext.dropped_siblings.
        _hydrate_series_context(scored_pool)

        # --- Cloud telemetry (Supabase telemetry_logs): unified call across all branches ---
        # Runs once with the final timings dict (including total_ms) so every
        # rerank_type — including Objective Rankings — appears on the admin
        # Pipeline Timings panel. supabase_post is fire-and-forget on a daemon
        # thread inside log_engine_execution.
        search_id = str(uuid.uuid4())
        timings["total_ms"] = round((time.perf_counter() - t_start) * 1000, 2)

        if scored_pool:
            sql_used_map = {
                "objective": "RAG:OBJECTIVE",
                "staff_studio": "RAG:STAFF_STUDIO",
                "narrative": "RAG:FAISS:NARRATIVE",
                "constrained": "RAG:FAISS:CONSTRAINED",
                "fusion": "RAG:FAISS",
            }
            rerank_type = timings.get("rerank_type", "fusion")
            sql_used = sql_used_map.get(rerank_type, "RAG:FAISS")
            rec_titles = ", ".join([item['profile']['title'] for item in scored_pool[:5]])
            telemetry.log_engine_execution(
                prompt=user_prompt,
                lens=lens_name,
                sql=sql_used,
                candidate_count=len(pool),
                success=True,
                error_msg="",
                recommendations=rec_titles,
                mode="find_a_show",
                timings=timings,
            )

        # --- Search Journal: capture full context for engine tuning ---
        # Fallback labels when retrieval_meta is None (non-vibe modes):
        # Objective Rankings → "objective_rankings" / "objective"
        # Staff/Studio      → "staff_studio" / "staff_studio"
        if retrieval_meta:
            journal_retrieval_path = retrieval_meta["retrieval_path"]
            journal_intent = retrieval_meta.get("intent")
        elif lens_name == "Staff/Studio":
            journal_retrieval_path = "staff_studio"
            journal_intent = "staff_studio"
        else:
            journal_retrieval_path = "objective_rankings"
            journal_intent = "objective"
        telemetry.log_search_journal({
            "search_id": search_id,
            "mode": "find_a_show",
            "query": user_prompt,
            "lens": lens_name,
            "era": era_label,
            "retrieval_path": journal_retrieval_path,
            "intent": journal_intent,
            "concepts_detected": retrieval_meta["concepts_detected"] if retrieval_meta else None,
            "candidates_retrieved": retrieval_meta["candidates_retrieved"] if retrieval_meta else len(search_results),
            "filters_applied": retrieval_meta["filters_applied"] if retrieval_meta else None,
            "candidates_after_rerank": len(scored_pool),
            # COA B / Option C — captures whether the LLM reranker was
            # blocked and the pool was built from composite scoring
            # instead. Landed in local JSONL for now; Supabase drops it
            # silently until a schema migration adds the column.
            "fallback_mode": fallback_mode,
            "success": True,
            "timings": timings,
            "results": [
                {
                    "title": item["profile"]["title"],
                    "confidence": item["match_confidence"],
                    "similarity": item["profile"].get("semantic_similarity", 0.0),
                    "quality_score": item["profile"].get("quality_score", 0.0),
                }
                for item in scored_pool
            ],
        })

        # "I Don't Know" mode flag — fires when either the post-rerank pool
        # is empty (no candidate cleared the 65 drop floor) OR the top
        # candidate is below the LOW_CONFIDENCE_THRESHOLD. Either case
        # warrants the yellow advisory banner instead of presenting
        # marginal picks as if they were confident recommendations.
        # Composite-fallback pools always score at _FALLBACK_CONFIDENCE
        # (70) which is below the 75 threshold, so the banner fires
        # naturally — no special-case needed.
        low_confidence = (
            not scored_pool
            or scored_pool[0]["match_confidence"] < LOW_CONFIDENCE_THRESHOLD
        )

        return {
            "success": True,
            "pool": scored_pool,
            "sql_used": "RAG:FAISS",
            "search_id": search_id,
            "low_confidence": low_confidence,
            "fallback_mode": fallback_mode,
        }

    # =====================================================================
    # DNA TRIANGULATION
    # =====================================================================

    @_rate_limit_safe(_RATE_LIMITED_DNA_RESPONSE)
    def execute_dna_triangulation(self, titles_list, negative_anchors=None, exclude_mal_ids=None):
        """The endpoint for the DNA Triangulation Matrix. Accepts up to 3 positive
        reference shows and (optionally) up to 2 negative anchors the user wants
        to avoid resembling. Unresolvable negatives are silently dropped — they
        are an optional refinement, not a required input."""
        if not titles_list:
            return {"success": False, "error": "DNA Triangulation requires at least 1 target show."}

        t_start = time.perf_counter()
        timings = {}
        # COA B / Option C — mirrors fetch_vault_pool. Set to "composite"
        # when the DNA reranker gets blocked by Gemini policy and we fall
        # back to composite-ordered centroid results.
        fallback_mode = None

        # Title-resolution notes surfaced to the user. Three classes of event
        # are worth disclosing — all three are otherwise invisible and would
        # otherwise erode trust ("the AI changed my input for mysterious
        # reasons"). The shared frame is *transparent catalog limitation*.
        #   1. Fuzzy substitution — input matched a different vault title.
        #   2. Skipped negative — input matched nothing, dropped silently.
        #   3. Same-show collision — negative resolved to a positive title.
        # Positive failures (no match) still hard-error with "Target MIA";
        # they don't reach this list.
        resolution_notes: list[str] = []

        def _is_fuzzy_substitution(input_title: str, resolved_title: str) -> bool:
            """True when the resolver substituted a *meaningfully* different
            title than the user typed. Cosmetic differences (case,
            punctuation, hyphens-as-spaces, collapsed whitespace) are NOT
            meaningful — surfacing them as a substitution note reads as
            the system being pedantic about formatting the user can't see.
            Token-level differences (different words, missing/extra words)
            ARE meaningful and produce a substitution note."""
            def _normalize(s: str) -> str:
                s = s.strip().lower()
                s = s.replace('-', ' ')      # hyphens treated as word separators
                s = re.sub(r'[^\w\s]', '', s) # strip remaining punctuation
                s = re.sub(r'\s+', ' ', s)    # collapse repeated whitespace
                return s
            return _normalize(input_title) != _normalize(resolved_title)

        t0 = time.perf_counter()
        resolved_titles = []
        for t in titles_list:
            matches = queries.resolve_show_title(t)
            if matches:
                resolved = matches[0]
                resolved_titles.append(resolved)
                if _is_fuzzy_substitution(t, resolved):
                    resolution_notes.append(
                        f"\"{t}\" isn't in the vault. Used the closest match — \"{resolved}\" — instead."
                    )
            else:
                return {"success": False, "error": f"Target MIA: Could not identify '{t}' in the vault."}

        # Resolve negative anchors. Unresolvable entries are skipped; same-show
        # collisions are dropped. Both surface as resolution notes now instead
        # of being silently logged-and-discarded.
        resolved_negatives = []
        if negative_anchors:
            for t in negative_anchors:
                matches = queries.resolve_show_title(t)
                if not matches:
                    resolution_notes.append(
                        f"\"{t}\" isn't in the vault and didn't match any title. Skipped from avoid list."
                    )
                    continue
                resolved = matches[0]
                if resolved in resolved_titles:
                    resolution_notes.append(
                        f"\"{t}\" was in both your reference and avoid lists. Removed from avoid list."
                    )
                    continue
                if resolved in resolved_negatives:
                    continue  # de-dupe, silent
                resolved_negatives.append(resolved)
                if _is_fuzzy_substitution(t, resolved):
                    resolution_notes.append(
                        f"\"{t}\" isn't in the vault. Used the closest match — \"{resolved}\" — instead."
                    )
        timings["title_resolution_ms"] = round((time.perf_counter() - t0) * 1000, 2)

        t0 = time.perf_counter()
        reference_profiles = queries.fetch_fusion_profiles(resolved_titles)
        timings["reference_hydration_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        
        trinity_data = ""
        for p in reference_profiles:
            trinity_data += f"SHOW: {p['title']}\n"
            trinity_data += f"SYNOPSIS: {p['synopsis']}\n"
            trinity_data += f"VIBE & CONSENSUS: {p['audience_consensus']}\n\n"

        # Adapt prompt instructions based on the number of targets
        target_count = len(resolved_titles)
        task_directive = f"Analyze the shared thematic DNA between these {target_count} shows." if target_count > 1 else "Analyze the core thematic DNA of this show."

        # When the user supplied negative anchors, the triangulation summary
        # must also articulate the contrast — otherwise the user has no
        # visible confirmation that their avoid-list was even considered.
        if resolved_negatives:
            triangulation_negatives_block = (
                f"\nAVOID-RESEMBLING SHOWS: {', '.join(resolved_negatives)}\n"
                f"The user explicitly wants results that do NOT resemble these shows. "
                f"Identify the specific qualities of these avoid-shows that contrast with "
                f"the reference shows above (e.g., tone, pacing, character archetypes, "
                f"thematic depth) so the summary can name them directly.\n"
            )
        else:
            triangulation_negatives_block = ""

        prompt = prompts.TRIANGULATION_PROMPT.format(
            trinity_data=trinity_data,
            task_directive=task_directive,
            negative_anchors_block=triangulation_negatives_block,
        )

        try:
            t0 = time.perf_counter()
            response = gemini_client.generate_content(
                self.client,
                model=self.model_id,
                contents=prompt,
                thinking_budget=DNA_THINKING_BUDGET,
                config={
                    "temperature": 0,  # See FUSION rerank for rationale
                    "response_mime_type": "application/json",
                    "response_schema": TriangulationPlan
                }
            )
            timings["triangulation_ms"] = round((time.perf_counter() - t0) * 1000, 2)
            plan = response.parsed
            if not plan:
                raise ValueError("Failed to parse JSON")
        except Exception as e:  # noqa: BLE001 — Gemini SDK errors surface as generic Exceptions
            return {"success": False, "error": f"DNA Synthesis Failed: {e}"}

        # Vector centroid retrieval: use the reference shows' own FAISS
        # vectors instead of embedding a lossy LLM-generated text query.
        # Franchise exclusion is handled inside search_by_centroid().
        #
        # Phase D — additionally exclude every sibling-cluster title of
        # the reference shows (series_id-based). Structurally complements
        # find_franchise_titles' prefix heuristic; catches franchise
        # members whose titles don't share a prefix with the reference
        # (Naruto -> "Road of Naruto", "Boruto: Naruto Next Generations"
        # already gets caught by prefix, but Naruto -> "The Last - Naruto
        # the Movie" does NOT). Closes PR #75 known-bad #29.
        sibling_exclude_titles = _compute_sibling_exclude_titles(
            reference_profiles, resolved_titles
        )

        t0 = time.perf_counter()
        centroid_response = vector_store.search_by_centroid(
            resolved_titles,
            top_k=50,
            negative_anchors=resolved_negatives if resolved_negatives else None,
            additional_excluded_titles=sibling_exclude_titles,
            exclude_mal_ids=exclude_mal_ids,
        )
        timings["retrieval_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        search_results = centroid_response["results"]
        centroid_meta = centroid_response["meta"]

        if not search_results:
            timings["total_ms"] = round((time.perf_counter() - t_start) * 1000, 2)
            telemetry.log_search_journal({
                "mode": "shows_like_this",
                "reference_titles": resolved_titles,
                "negative_anchors": resolved_negatives,
                "title_resolution_notes": resolution_notes,
                "success": False,
                "error": "no_centroid_results",
                "timings": timings,
            })
            # "I Don't Know" mode: empty centroid pool is the DNA-side
            # analogue of Find a Show's empty-result branch. Fires on
            # ~9.6% of historical DNA searches — the largest source of
            # banner activations across the whole engine.
            return {
                "success": False,
                "error": "No matching targets found for this specific DNA blend.",
                "title_resolution_notes": resolution_notes,
                "low_confidence": True,
            }

        sim_lookup = {r["title"]: r["similarity"] for r in search_results}
        candidate_titles = [r["title"] for r in search_results]

        t0 = time.perf_counter()
        fusion_profiles = queries.fetch_fusion_profiles(candidate_titles)
        timings["candidate_hydration_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        title_order_dna = {t: i for i, t in enumerate(candidate_titles)}
        for p in fusion_profiles:
            p["semantic_similarity"] = sim_lookup.get(p["title"], 0.0)
        fusion_profiles.sort(key=lambda x: title_order_dna.get(x["title"], 999))
        chunk = fusion_profiles[:15]
        t0 = time.perf_counter()
        top_picks = self._rerank_dna_candidates(
            plan.intersection_summary,
            resolved_titles,
            chunk,
            negative_anchors=resolved_negatives if resolved_negatives else None,
            watchlist_exclusion_count=len(exclude_mal_ids) if exclude_mal_ids else 0,
        )
        timings["rerank_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        timings["rerank_type"] = "dna"

        # DNA rate-limit handling: prior to this branch, `top_picks == ["RATE_LIMIT_ERROR"]`
        # fell through to the for-loop and raised AttributeError on `pick.match_confidence`.
        # Mirrors the Find-a-Show short-circuit at the top of the LLM-reranked branch.
        if is_rate_limited(top_picks):
            return {
                "success": False,
                "error": _RATE_LIMIT_ERROR_MSG,
                "title_resolution_notes": resolution_notes,
            }
        if is_rerank_blocked(top_picks):
            # DNA reranker was blocked by Gemini policy — fall back to the
            # composite-ordered centroid pool with a fixed confidence and
            # honest reasoning string. Series-collapse and series-context
            # hydration downstream still apply.
            final_results = _build_composite_fallback(chunk)
            fallback_mode = "composite"
        else:
            final_results = _finalize_top_picks(top_picks, chunk, _DNA_FLOOR)

        # --- Same-series collapse (Phase D) ---
        # Same collapse semantics as Find-a-Show — if the rerank surfaces
        # multiple entries from the same series (rare on DNA since
        # same-series candidates are pre-filtered before rerank, but
        # possible if the pre-filter misses), keep the top scorer and
        # ride the rest as dropped_siblings.
        final_results = _collapse_same_series(final_results)

        # --- Series-context hydration (Phase C) ---
        # Same batched fetch + in-memory compute as the Find-a-Show
        # pipeline. DNA results benefit from series context too — users
        # often ask for "shows like X" where X is mid-franchise, and the
        # context helps disambiguate which entry to start with.
        _hydrate_series_context(final_results)

        timings["total_ms"] = round((time.perf_counter() - t_start) * 1000, 2)

        # --- Cloud telemetry (Supabase) ---
        # DNA wasn't previously logged to the cloud at all — only the local
        # journal. Now the admin dashboard sees Shows-Like-This latency
        # alongside Find-a-Show.
        if final_results:
            rec_titles = ", ".join([item['profile']['title'] for item in final_results[:5]])
            telemetry.log_engine_execution(
                prompt=", ".join(resolved_titles),
                lens="DNA Triangulation",
                sql="RAG:FAISS:CENTROID",
                candidate_count=len(chunk),
                success=True,
                error_msg="",
                recommendations=rec_titles,
                mode="shows_like_this",
                timings=timings,
            )

        # --- Search Journal: capture full DNA triangulation context ---
        telemetry.log_search_journal({
            "mode": "shows_like_this",
            "reference_titles": resolved_titles,
            "negative_anchors": resolved_negatives,
            "title_resolution_notes": resolution_notes,
            "intersection_summary": plan.intersection_summary,
            "retrieval_path": centroid_meta["retrieval_path"],
            "reference_count": centroid_meta["reference_count"],
            "negative_anchor_count": centroid_meta.get("negative_anchor_count", 0),
            "excluded_count": centroid_meta["excluded_count"],
            "prefiltered_count": centroid_meta.get("prefiltered_count", 0),
            "candidates_retrieved": centroid_meta["candidates_retrieved"],
            "candidates_after_rerank": len(final_results),
            # COA B / Option C — captures whether the DNA reranker was
            # blocked and the pool came from composite fallback instead.
            "fallback_mode": fallback_mode,
            "success": True,
            "timings": timings,
            "results": [
                {
                    "title": item["profile"]["title"],
                    "confidence": item["match_confidence"],
                    "similarity": item["profile"].get("semantic_similarity", 0.0),
                    "quality_score": item["profile"].get("quality_score", 0.0),
                }
                for item in final_results
            ],
        })

        # "I Don't Know" mode flag — empty pool OR top result below 75.
        # Historically the in-band case has been inert for DNA (top results
        # cluster at 100% in 90-100) but the check is kept symmetric with
        # Find a Show in case future reranker recalibration widens the band.
        # Composite-fallback pools score at _FALLBACK_CONFIDENCE (70) which
        # is below 75, so the banner fires naturally on fallback runs.
        low_confidence = (
            not final_results
            or final_results[0]["match_confidence"] < LOW_CONFIDENCE_THRESHOLD
        )

        return {
            "success": True,
            "intersection_summary": plan.intersection_summary,
            "data": final_results,
            "negative_anchors_used": resolved_negatives,
            "title_resolution_notes": resolution_notes,
            "low_confidence": low_confidence,
            "fallback_mode": fallback_mode,
            "diagnostics": {"sql_used": "RAG:FAISS"}
        }