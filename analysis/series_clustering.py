"""Pure-logic clustering for multi-season anime series.

No I/O, no DB, no HTTP. Callers (vault_enrichment.enrich_series) fetch
the Jikan relations data and the Alt-Version-endpoint subtypes, then
pass them in along with the override file's contents. This module
decides which MAL IDs share a `series_id`.

Verified ruleset (Jikan returns Title Case; comparisons case-insensitive):
    SAME:     Sequel, Prequel, Side Story, Summary, Full Story
    SEPARATE: Alternative Setting, Spin-Off
    HYBRID:   Alternative Version — DEFERRED to cluster-level resolution
              in assign_series_ids (see two-pass algorithm below).
    SOFT:     Parent Story, Adaptation, Character, Other (never cluster).

Tie-breaker: if a pair of MAL IDs is connected via BOTH a SAME and a
SEPARATE relation (in either direction), SEPARATE wins. Phase A's
Tenchi-GXP-listed-as-both-Spin-Off-and-Alternative-Setting case
established the rule.

Why HYBRID is cluster-level, not edge-level:
    MAL's `Alternative Version` is overloaded. It connects:
      (a) true reboot pairs (FMA 2003 ↔ Brotherhood — both TV)
      (b) arc-retelling films within the same continuity
          (Gintama TV ↔ Gintama Movie 1; Code Geass TV ↔ recap movies)
      (c) OVA recompilations to TV (Gundam Unicorn OVA ↔ RE:0096 TV)
    An edge-level heuristic on target subtype is direction-asymmetric:
    TV→Movie correctly returns SAME, but Movie→TV returns SEPARATE,
    and the SEPARATE-wins tie-breaker incorrectly splits retelling
    pairs. A pair-level (both-subtypes) heuristic correctly merges
    TV↔Movie pairs but mis-merges reboot pairs that share OVAs (HxH
    1999↔OVA↔HxH 2011 transitively bridges the reboot via the OVA).

    The cluster-level rule looks at non-HYBRID structure first, then
    asks: "do both endpoints' clusters contain a TV?" If yes, the Alt
    Version edge is reboot evidence and stays SEPARATE. If only one
    cluster has TV (or neither), the edge is retelling evidence and
    merges. This preserves FMA/HxH reboot separation AND Code Geass/
    Gintama recap-film clustering.

Canonical `series_id` per cluster: stringified MAL ID of the
earliest-released entry, ties broken by lowest MAL ID. Standalone shows
(no SAME edges to any vault entry) get `series_id = str(id)`.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Literal

# Trajectory thresholds. See compute_series_context for usage and the
# IMPLEMENTATION_PLAN's Phase C section for the rationale.
TRAJECTORY_STABLE_DELTA = 0.2   # max abs(delta) for "stable"
TRAJECTORY_DRIFT_THRESHOLD = 0.5  # min cumulative delta for "decline"/"improve"

# Subtype buckets for compute_series_context's score-range filter and
# tv_position computation. PR B (subtype bundle) Phase C.
#
# SCORE_RANGE_SUBTYPES — TV + TV Special + Movie. The "main offering"
# entries that define a franchise's quality envelope. Excludes OVA /
# Special / ONA / Music / PV / CM (promotional / bonus / short content
# whose ratings often drag the visible range below what the franchise
# actually delivers).
#
# TV_POSITION_SUBTYPE — strict "TV" only. The "Season X of Y" mental
# model refers to numbered broadcast seasons. TV Special is a one-off
# broadcast event, not a numbered season, so it's excluded here even
# though it counts for the score-range bucket above.
SCORE_RANGE_SUBTYPES: frozenset[str] = frozenset({"TV", "TV Special", "Movie"})
TV_POSITION_SUBTYPE: str = "TV"


SAME_RELATIONS: frozenset[str] = frozenset({
    "sequel",
    "prequel",
    "side story",
    "summary",
    "full story",
})

SEPARATE_RELATIONS: frozenset[str] = frozenset({
    "alternative setting",
    "spin-off",
})

HYBRID_RELATION: str = "alternative version"


def classify_relation(relation_name: str) -> str:
    """Apply the verified ruleset to a single relation name.

    Returns one of:
        "SAME"      — Sequel, Prequel, Side Story, Summary, Full Story
        "SEPARATE"  — Alternative Setting, Spin-Off
        "HYBRID"    — Alternative Version (cluster-level resolution
                      in assign_series_ids; edge-level can't decide)
        "SOFT"      — Parent Story, Adaptation, Character, Other,
                      or any unrecognized relation. Never clusters.
    """
    key = relation_name.strip().lower()
    if key in SAME_RELATIONS:
        return "SAME"
    if key in SEPARATE_RELATIONS:
        return "SEPARATE"
    if key == HYBRID_RELATION:
        return "HYBRID"
    return "SOFT"


class UnionFind:
    """Union-Find with path compression. Add nodes lazily on first use."""

    def __init__(self) -> None:
        self._parent: dict[int, int] = {}

    def add(self, x: int) -> None:
        if x not in self._parent:
            self._parent[x] = x

    def find(self, x: int) -> int:
        self.add(x)
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb

    def components(self, nodes: Iterable[int]) -> dict[int, list[int]]:
        """Group nodes by their root. Nodes not yet added become singletons."""
        groups: dict[int, list[int]] = defaultdict(list)
        for n in nodes:
            groups[self.find(n)].append(n)
        return dict(groups)


def assign_series_ids(
    vault_ids: set[int],
    relations_by_source: dict[int, list[dict]],
    subtype_lookup: dict[int, str],
    year_by_id: dict[int, int],
    overrides: dict[str, list[list[int]]] | None = None,
) -> dict[int, str]:
    """Compute the series_id assignment for every MAL ID in vault_ids.

    Two-pass algorithm:
      Pass 1: Build clusters from non-HYBRID relations only. Sequel/
        Prequel/Side Story/Summary/Full Story edges union members;
        Alternative Setting/Spin-Off edges (and any SEPARATE override
        pair) block unions via the SEPARATE-wins tie-breaker.
      Pass 2: For each HYBRID (Alternative Version) pair, check whether
        BOTH endpoints' pass-1 clusters contain a TV anime. If yes,
        the pair is reboot evidence — leave separate. If only one (or
        neither) cluster has TV, the pair is retelling/recap evidence
        — merge them.

    Args:
        vault_ids: every MAL ID in anime_info.
        relations_by_source: source MAL ID → list of Jikan relation
            dicts ({"relation": str, "entry": [{"mal_id", "type", ...}]}).
        subtype_lookup: MAL ID → anime subtype string. Only the literal
            "TV" value is load-bearing (Pass 2 checks cluster-has-TV);
            every other subtype (Movie, OVA, Special, ONA, ...) is
            treated as non-TV. Missing entries default to non-TV — safer
            for cluster convergence, since missed retellings are worse
            than missed splits (splits are recoverable via overrides).
        year_by_id: MAL ID → release_year for canonical selection.
        overrides: optional {"force_same_cluster": [[a, b], ...],
            "force_separate_cluster": [[a, b], ...]}.

    Returns:
        Mapping {mal_id: series_id_str} covering every vault_id.
    """
    overrides = overrides or {}
    force_same = overrides.get("force_same_cluster") or []
    force_separate = overrides.get("force_separate_cluster") or []

    # --- Edge collection ---
    # edge_classes carries SAME / SEPARATE classifications for non-HYBRID
    # relations. hybrid_pairs is the set of unordered pairs connected by
    # at least one HYBRID edge — these defer to the cluster-level pass.
    edge_classes: dict[tuple[int, int], set[str]] = defaultdict(set)
    hybrid_pairs: set[tuple[int, int]] = set()

    def _norm_pair(a: int, b: int) -> tuple[int, int]:
        return (min(a, b), max(a, b))

    for source_id, relations in relations_by_source.items():
        if source_id not in vault_ids:
            continue
        for rel in relations:
            rel_name = rel.get("relation", "")
            classification = classify_relation(rel_name)
            if classification == "SOFT":
                continue
            for entry in rel.get("entry", []):
                if entry.get("type") != "anime":
                    continue
                target_id = entry.get("mal_id")
                if target_id is None or target_id == source_id:
                    continue
                if target_id not in vault_ids:
                    continue
                pair = _norm_pair(source_id, target_id)
                if classification == "HYBRID":
                    hybrid_pairs.add(pair)
                else:
                    edge_classes[pair].add(classification)

    # force_separate seeds the SEPARATE side of the tie-breaker.
    for pair in force_separate:
        if len(pair) == 2:
            a, b = int(pair[0]), int(pair[1])
            if a != b and a in vault_ids and b in vault_ids:
                edge_classes[_norm_pair(a, b)].add("SEPARATE")

    # --- Pass 1: union via non-HYBRID SAME edges ---
    uf = UnionFind()
    for mal_id in vault_ids:
        uf.add(mal_id)
    for pair, classes in edge_classes.items():
        if "SEPARATE" not in classes and "SAME" in classes:
            uf.union(pair[0], pair[1])

    # --- Pass 2: HYBRID at cluster level ---
    # Pre-compute which clusters contain at least one TV anime.
    cluster_has_tv: dict[int, bool] = {}
    for mal_id in vault_ids:
        if subtype_lookup.get(mal_id) == "TV":
            cluster_has_tv[uf.find(mal_id)] = True

    for a, b in hybrid_pairs:
        # SEPARATE classifications from non-HYBRID relations (or
        # force_separate overrides) keep the pair apart unconditionally.
        if "SEPARATE" in edge_classes.get(_norm_pair(a, b), set()):
            continue
        root_a = uf.find(a)
        root_b = uf.find(b)
        if root_a == root_b:
            continue
        if cluster_has_tv.get(root_a, False) and cluster_has_tv.get(root_b, False):
            # Reboot evidence: both pass-1 clusters contain TV. Block
            # the merge AND mark SEPARATE so a stray force_same can't
            # accidentally re-bridge them.
            edge_classes[_norm_pair(a, b)].add("SEPARATE")
            continue
        merged_has_tv = (
            cluster_has_tv.get(root_a, False)
            or cluster_has_tv.get(root_b, False)
        )
        uf.union(a, b)
        new_root = uf.find(a)
        cluster_has_tv.pop(root_a, None)
        cluster_has_tv.pop(root_b, None)
        if merged_has_tv:
            cluster_has_tv[new_root] = True

    # --- force_same overrides apply after both passes ---
    for pair in force_same:
        if len(pair) == 2:
            a, b = int(pair[0]), int(pair[1])
            if a != b and a in vault_ids and b in vault_ids:
                p = _norm_pair(a, b)
                if "SEPARATE" not in edge_classes.get(p, set()):
                    uf.union(a, b)

    # --- Canonical ID assignment ---
    components = uf.components(vault_ids)
    out: dict[int, str] = {}
    for members in components.values():
        canonical = min(members, key=lambda m: (year_by_id.get(m, 9999), m))
        sid = str(canonical)
        for m in members:
            out[m] = sid
    return out


def _trajectory(scores: list[float]) -> Literal["stable", "decline", "improve", "mixed"]:
    """Classify the trajectory of a chronological score sequence.

    Pure helper called by compute_series_context. Operates on the
    cleaned score list (no zeros or None) in chronological order.
    Assumes len(scores) >= 2; the caller short-circuits trivial cases.

    Rules (in priority order):
        1. "stable"  — every adjacent delta within ±TRAJECTORY_STABLE_DELTA.
        2. "decline" — every adjacent delta ≤ 0 AND cumulative drop
                       ≥ TRAJECTORY_DRIFT_THRESHOLD.
        3. "improve" — every adjacent delta ≥ 0 AND cumulative rise
                       ≥ TRAJECTORY_DRIFT_THRESHOLD.
        4. "mixed"   — direction reverses across the series, or
                       monotonic but cumulative drift below threshold.
    """
    deltas = [scores[i + 1] - scores[i] for i in range(len(scores) - 1)]
    all_stable = all(abs(d) <= TRAJECTORY_STABLE_DELTA for d in deltas)
    if all_stable:
        return "stable"
    cumulative = scores[-1] - scores[0]
    all_non_increasing = all(d <= 0 for d in deltas)
    all_non_decreasing = all(d >= 0 for d in deltas)
    if all_non_increasing and abs(cumulative) >= TRAJECTORY_DRIFT_THRESHOLD:
        return "decline"
    if all_non_decreasing and cumulative >= TRAJECTORY_DRIFT_THRESHOLD:
        return "improve"
    return "mixed"


def compute_series_context(
    siblings: list[dict],
    target_mal_id: int,
    series_id: str,
) -> dict | None:
    """Compute the SeriesContext payload for a given anime within its cluster.

    Pure logic, no I/O. The caller (recommendation_engine) fetches the
    sibling rows for the show's series_id and passes them in along with
    the series_id itself (from the profile). Returns a dict suitable for
    the SeriesContext Pydantic model, or None when target_mal_id isn't
    found in the siblings list.

    siblings must be a list of dicts with keys: id, release_year,
    mal_score, avg_sentiment, subtype. Ordering inside the function is
    by (release_year, id) ASC — matches fetch_series_siblings — and
    `position` is the 1-indexed rank of target_mal_id in that order.

    PR B (subtype bundle) adds two enhancements:

    - **Score range filtered to TV + TV Special + Movie subtypes.**
      Excludes OVA/Special/ONA/Music/PV/CM entries whose ratings often
      drag the visible range below what the franchise actually delivers
      (e.g., Frieren's Mini Anime at 7.4 vs the main S1 at 9.3). Falls
      back to the full set when no TV-or-Movie siblings exist
      (OVA-only franchises). `score_range_filtered` reports whether
      the filter changed the visible range, so the frontend can surface
      a caption.

    - **tv_position / tv_count** for "Season X of Y" labels. tv_position
      is the 1-indexed rank of target_mal_id among TV-only siblings
      (TV Special excluded — not a numbered season). Both None when the
      target itself isn't `subtype == "TV"` (a movie or OVA doesn't get
      a TV-relative position).

    Standalones (siblings length 1) get position=1, series_size=1, and
    trajectory=None. score/sentiment ranges come from non-zero
    mal_score and any avg_sentiment value; either may be None when no
    sibling has data for that dimension.
    """
    if not siblings:
        return None

    ordered = sorted(siblings, key=lambda s: (s.get("release_year") or 9999, s.get("id") or 0))
    series_size = len(ordered)

    position = None
    target_sibling = None
    for idx, s in enumerate(ordered, start=1):
        if s.get("id") == target_mal_id:
            position = idx
            target_sibling = s
            break
    if position is None:
        return None

    # Score range: pull mal_score values where non-zero (zero is the
    # ingestion default for "no score yet" — exclude rather than skew).
    # Filtered to TV + TV Special + Movie subtypes when at least one
    # sibling in those buckets has a score; otherwise fall back to the
    # full set so OVA-only franchises still get a visible range.
    full_scores = [s["mal_score"] for s in ordered if s.get("mal_score")]
    filtered_scores = [
        s["mal_score"]
        for s in ordered
        if s.get("mal_score") and s.get("subtype") in SCORE_RANGE_SUBTYPES
    ]
    if filtered_scores:
        score_min = min(filtered_scores)
        score_max = max(filtered_scores)
        full_min = min(full_scores) if full_scores else None
        full_max = max(full_scores) if full_scores else None
        score_range_filtered = (
            full_min is not None
            and full_max is not None
            and (full_min < score_min or full_max > score_max)
        )
    else:
        score_min = min(full_scores) if full_scores else None
        score_max = max(full_scores) if full_scores else None
        score_range_filtered = False

    # Sentiment range: avg_sentiment can legitimately be 0.0 or negative
    # for divisive shows, so include any non-None values.
    sentiments = [s["avg_sentiment"] for s in ordered if s.get("avg_sentiment") is not None]
    sentiment_min = min(sentiments) if sentiments else None
    sentiment_max = max(sentiments) if sentiments else None

    # Trajectory only meaningful for 2+ entries with valid scores.
    # `full_scores` above is already ordered chronologically (via
    # `ordered`) and filtered to non-zero mal_score, matching what
    # `_trajectory` expects.
    trajectory = _trajectory(full_scores) if len(full_scores) >= 2 else None

    # tv_position / tv_count — Season X of Y label support. Strict TV
    # only (TV Special excluded — broadcast event, not numbered season).
    # Both None when the target itself isn't TV-subtype.
    if target_sibling.get("subtype") == TV_POSITION_SUBTYPE:
        tv_siblings = [s for s in ordered if s.get("subtype") == TV_POSITION_SUBTYPE]
        tv_count = len(tv_siblings)
        tv_position = None
        for idx, s in enumerate(tv_siblings, start=1):
            if s.get("id") == target_mal_id:
                tv_position = idx
                break
    else:
        tv_position = None
        tv_count = None

    return {
        "series_id": series_id,
        "series_size": series_size,
        "position": position,
        "score_min": score_min,
        "score_max": score_max,
        "score_range_filtered": score_range_filtered,
        "sentiment_min": sentiment_min,
        "sentiment_max": sentiment_max,
        "trajectory": trajectory,
        "tv_position": tv_position,
        "tv_count": tv_count,
    }
