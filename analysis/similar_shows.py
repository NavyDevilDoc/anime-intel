"""Similar-shows generator (Item 3).

Deterministic build-time computation of "You might also like" recommendations
for every show in the vault. Runs pure FAISS + composite scoring + personnel
bonus + provenance-string generation — zero Gemini calls, zero per-visit
runtime cost.

The formula, filters, and design decisions were finalized during Item 3
Phase A/B. See ENGINE_WORK_PROGRESS.md for the empirical journey (Yatate
pseudonym filter, classical composer skip, candidate franchise dedup).

Ranking:
    composite = 0.55 * similarity_normalized
              + 0.15 * bayesian_quality_normalized
              + personnel_bonus

Personnel bonus (additive, one per shared role):
    Director            +0.15
    Music               +0.10
    Original Creator    +0.05  (skips studio pseudonyms + classical composers)
    Series Composition  +0.03
    Character Design    +0.03

Filters applied before ranking:
    - Drop candidates with mal_score < 6.0 (quality floor)
    - Drop reference show itself
    - Drop candidates in the same series_id as reference
    - Drop candidates matching reference by fuzzy title substring
      (catches franchise siblings the series_id clustering missed)

Post-ranking dedup:
    - Keep only the top-scoring entry per candidate series_id in the final
      top-K (prevents Gintama x4 filling Champloo's recommendations)

Public API:
    build_recommendations(engine_context, reference_id, top_k=6) -> list[dict]
    load_engine_context(db_path) -> EngineContext
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

# ------------------------------------------------------------------------
# Configuration constants (Phase A/B agreed)
# ------------------------------------------------------------------------

# Per-role allowlists of "signal-degenerate" personnel to skip.
# Splitting by role because pollution patterns differ — Original Creator
# has studio/IP-holder pseudonyms; Music has classical composers whose
# public-domain works get licensed into unrelated shows.
_SKIP_BY_ROLE: dict[str, frozenset[int]] = {
    "Original Creator": frozenset({
        2197,   # Hajime Yatate (Sunrise pseudonym)
        3940,   # Izumi Toudou (Toei Animation pseudonym)
        55301,  # Takaaki Kidani (Bushiroad business president)
        14955,  # Capcom (video game company IP)
        4960,   # Nitroplus (visual novel company)
    }),
    "Music": frozenset({
        9160,   # Ludwig van Beethoven
        9159,   # Johann Sebastian Bach
        9178,   # Pyotr Ilyich Tchaikovsky
        11768,  # Wolfgang Amadeus Mozart
        9162,   # Sergei Rachmaninoff
        11779,  # Georg Friedrich Handel
        24725,  # Charles Gounod
    }),
}

_ROLE_BONUS: dict[str, float] = {
    "Director": 0.15,
    "Music": 0.10,
    "Original Creator": 0.05,
    "Series Composition": 0.03,
    "Character Design": 0.03,
}

# Personnel-hook priority — first match wins for the "Also directed by /
# Music by / Adapted from" tagline. Series Composition and Character
# Design are omitted intentionally: they earn a scoring bonus but their
# overlap alone rarely reads as a compelling "why you'd like this"
# story to a user.
_PROVENANCE_TIER_ORDER: tuple[str, ...] = ("Director", "Music", "Original Creator")

_W_SIMILARITY = 0.55
_W_QUALITY = 0.15
_QUALITY_FLOOR = 6.0

# Mirror analysis/vector_store constants for Bayesian normalization
_MAL_SCORE_MIN = 2.87
_MAL_SCORE_MAX = 9.28
_BAYESIAN_GLOBAL_MEAN = 7.05
_BAYESIAN_MIN_VOTES = 5000

# Default recommendation-count target and candidate over-fetch factor
_DEFAULT_TOP_K = 6
_FAISS_FETCH_K = 100


# ------------------------------------------------------------------------
# Fuzzy franchise-sibling detector
# ------------------------------------------------------------------------

def share_franchise_signal(ref_title: str, cand_title: str,
                           min_shared: int = 10) -> bool:
    """Detects franchise siblings that series_id clustering missed.

    PR #77 established series_id clustering; PR #79 added 42 manual
    overrides. Some franchise spinoffs still slip through — e.g.,
    "Puella Magi Madoka Magica" and "Magia Record: Puella Magi Madoka
    Magica Side Story" have different series_ids but are clearly the
    same franchise from a user perspective.

    Heuristic: (a) shorter title fully contained in longer AND shorter is
    >= 5 chars, OR (b) both titles share a substring of length >= min_shared.
    The 10-char default catches "Code Geass" / "Puella Magi Madoka" / etc.
    while avoiding "K-On!" / "FLCL" false positives on very short titles.
    """
    ref = ref_title.lower()
    cand = cand_title.lower()
    shorter, longer = (ref, cand) if len(ref) <= len(cand) else (cand, ref)

    if len(shorter) >= 5 and shorter in longer:
        return True

    max_check = min(len(shorter), 30)
    for length in range(max_check, min_shared - 1, -1):
        for i in range(len(shorter) - length + 1):
            if shorter[i:i + length] in longer:
                return True
    return False


# ------------------------------------------------------------------------
# Scoring
# ------------------------------------------------------------------------

def bayesian_norm_quality(mal_score: float, scored_by: int) -> float:
    """Bayesian-adjusted quality normalized to [0, 1]. Mirrors the same
    formula used in analysis/vector_store._composite_rerank so shows are
    ranked consistently across the search and similar-shows surfaces."""
    v, m, C = scored_by, _BAYESIAN_MIN_VOTES, _BAYESIAN_GLOBAL_MEAN
    if v + m == 0:
        return 0.0
    bayesian = (v / (v + m)) * mal_score + (m / (v + m)) * C
    n = (bayesian - _MAL_SCORE_MIN) / (_MAL_SCORE_MAX - _MAL_SCORE_MIN)
    return max(0.0, min(1.0, n))


def compute_personnel_bonus(
    ref_staff: dict[str, set[int]],
    cand_staff: dict[str, set[int]],
) -> tuple[float, dict[str, set[int]]]:
    """Return (total_bonus, {role: {shared_person_ids}}).

    Both ref_staff and cand_staff are expected to already have signal-
    degenerate personnel filtered out (Yatate, classical composers) at
    load time. Empty sets produce a 0 bonus.
    """
    total = 0.0
    shared: dict[str, set[int]] = {}
    for role, weight in _ROLE_BONUS.items():
        overlap = ref_staff.get(role, set()) & cand_staff.get(role, set())
        if overlap:
            total += weight
            shared[role] = overlap
    return total, shared


def build_personnel_hook(
    shared: dict[str, set[int]],
    people_names: dict[int, str],
) -> str | None:
    """Return the personnel provenance string (Director > Music > Original
    Creator priority) when at least one high-priority role overlaps.
    Returns None when the reference and candidate share no personnel in
    the priority tiers — callers should render the thematic hint alone.
    """
    for role in _PROVENANCE_TIER_ORDER:
        if role in shared:
            # Pick lowest mal_id — stable arbitrary choice for reproducibility
            pid = min(shared[role])
            name = people_names.get(pid, f"person #{pid}")
            if role == "Director":
                return f"Also directed by {name}"
            if role == "Music":
                return f"Music by {name}"
            if role == "Original Creator":
                return f"Adapted from a work by {name}"
    return None


def extract_thematic_hint(cand_consensus_json: str | None) -> str | None:
    """Return the bare theme descriptor from consensus_json.thematic_vibe.

    Returns None when the field is missing or malformed. The frontend
    renders this with a `Theme:` label of its own — the string here is
    the raw descriptor so it can be composed differently in different UI
    surfaces (result card, Encyclopedia, etc.) without being locked to
    one label.
    """
    if not cand_consensus_json:
        return None
    try:
        cj = json.loads(cand_consensus_json)
    except (json.JSONDecodeError, TypeError):
        return None
    vibe = cj.get("thematic_vibe")
    if not vibe:
        return None
    vibe = vibe.split(".")[0].strip()
    if not vibe:
        return None
    return vibe[:80]


# ------------------------------------------------------------------------
# Data loading
# ------------------------------------------------------------------------

@dataclass
class EngineContext:
    """Bundle of preloaded lookups the ranker needs. Built once per full
    generation run so we don't re-query SQLite for every show."""
    title_lookup: dict[str, dict[str, Any]]   # english_title -> meta
    id_to_title: dict[int, str]               # anime_id -> english_title
    staff_by_anime: dict[int, dict[str, set[int]]]
    people_names: dict[int, str]
    faiss_index: Any                          # faiss.Index
    faiss_metadata: list[dict]                # position -> {english_title}
    title_to_pos: dict[str, int]              # english_title -> FAISS position


def load_engine_context(db_path: str, faiss_loader) -> EngineContext:
    """Load all data needed for similar-shows generation.

    `faiss_loader` is a callable returning (faiss_index, metadata) —
    injected so tests can mock the FAISS layer. In production callers
    should pass `analysis.vector_store._load_index`.
    """
    conn = sqlite3.connect(db_path)
    try:
        title_lookup: dict[str, dict[str, Any]] = {}
        id_to_title: dict[int, str] = {}
        cur = conn.cursor()
        for row in cur.execute("""
            SELECT id, english_title, mal_score, scored_by, series_id,
                   consensus_json, release_year
            FROM anime_info
            WHERE english_title IS NOT NULL
        """):
            aid, title, score, votes, sid, cjson, year = row
            title_lookup[title] = {
                "anime_id": aid,
                "mal_score": score or 0.0,
                "scored_by": votes or 0,
                "series_id": sid,
                "consensus_json": cjson,
                "release_year": year,
            }
            id_to_title[aid] = title

        # Staff — apply the signal-degeneracy skip lists at load time
        staff_by_anime: dict[int, dict[str, set[int]]] = {}
        for anime_id, person_id, role in cur.execute(
            "SELECT anime_id, person_mal_id, role FROM anime_staff"
        ):
            skip_set = _SKIP_BY_ROLE.get(role)
            if skip_set and person_id in skip_set:
                continue
            staff_by_anime.setdefault(anime_id, {}).setdefault(role, set()).add(person_id)

        people_names = {
            row[0]: row[1] for row in cur.execute(
                "SELECT person_mal_id, name_display FROM people"
            )
        }
    finally:
        conn.close()

    index, metadata = faiss_loader()
    title_to_pos = {m["english_title"]: i for i, m in enumerate(metadata)}

    return EngineContext(
        title_lookup=title_lookup,
        id_to_title=id_to_title,
        staff_by_anime=staff_by_anime,
        people_names=people_names,
        faiss_index=index,
        faiss_metadata=metadata,
        title_to_pos=title_to_pos,
    )


# ------------------------------------------------------------------------
# Ranking
# ------------------------------------------------------------------------

def build_recommendations(
    ctx: EngineContext,
    reference_id: int,
    top_k: int = _DEFAULT_TOP_K,
    faiss_fetch_k: int = _FAISS_FETCH_K,
    faiss_search_fn=None,
) -> list[dict[str, Any]]:
    """Compute the top-K similar shows for a single reference anime_id.

    Returns a list of dicts with keys: anime_id, title, release_year,
    mal_score, similarity, norm_quality, personnel_bonus, composite,
    shared_roles, provenance, series_id.

    `faiss_search_fn(embedding, fetch_k)` is injected so tests can bypass
    the real FAISS layer. In production callers should pass
    `analysis.vector_store._search_faiss_raw`.
    """
    reference_title = ctx.id_to_title.get(reference_id)
    if not reference_title:
        return []
    ref_meta = ctx.title_lookup.get(reference_title)
    if not ref_meta:
        return []
    ref_pos = ctx.title_to_pos.get(reference_title)
    if ref_pos is None:
        return []
    if faiss_search_fn is None:
        raise ValueError("faiss_search_fn is required")

    ref_series = ref_meta["series_id"]
    ref_staff = ctx.staff_by_anime.get(reference_id, {})

    # Reconstruct the reference embedding and do K-NN
    ref_embedding = ctx.faiss_index.reconstruct(ref_pos).reshape(1, -1)
    raw_results = faiss_search_fn(ref_embedding, faiss_fetch_k)

    scored: list[dict[str, Any]] = []
    for r in raw_results:
        cand_title = r["title"]
        if cand_title == reference_title:
            continue

        cand_meta = ctx.title_lookup.get(cand_title)
        if not cand_meta:
            continue

        if cand_meta["mal_score"] < _QUALITY_FLOOR:
            continue

        # Series-id and fuzzy-title franchise exclusion
        if ref_series and cand_meta["series_id"] == ref_series:
            continue
        if share_franchise_signal(reference_title, cand_title):
            continue

        cand_aid = cand_meta["anime_id"]
        cand_staff = ctx.staff_by_anime.get(cand_aid, {})

        similarity = r["similarity"]
        norm_quality = bayesian_norm_quality(
            cand_meta["mal_score"], cand_meta["scored_by"]
        )
        personnel_bonus, shared = compute_personnel_bonus(ref_staff, cand_staff)

        composite = (
            _W_SIMILARITY * similarity
            + _W_QUALITY * norm_quality
            + personnel_bonus
        )
        personnel_hook = build_personnel_hook(shared, ctx.people_names)
        thematic_hint = extract_thematic_hint(cand_meta["consensus_json"])

        scored.append({
            "anime_id": cand_aid,
            "title": cand_title,
            "series_id": cand_meta["series_id"],
            "release_year": cand_meta["release_year"],
            "mal_score": cand_meta["mal_score"],
            "similarity": round(similarity, 4),
            "norm_quality": round(norm_quality, 4),
            "personnel_bonus": round(personnel_bonus, 4),
            "composite": round(composite, 4),
            "shared_roles": {r_: sorted(pids) for r_, pids in shared.items()},
            "personnel_hook": personnel_hook,
            "thematic_hint": thematic_hint,
        })

    # Sort + candidate franchise dedup — one entry per unique series_id
    scored.sort(key=lambda x: x["composite"], reverse=True)
    seen_series: set[int] = set()
    deduped: list[dict[str, Any]] = []
    for cand in scored:
        sid = cand["series_id"]
        if sid is not None and sid in seen_series:
            continue
        if sid is not None:
            seen_series.add(sid)
        deduped.append(cand)
        if len(deduped) >= top_k:
            break
    return deduped
