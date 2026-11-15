"""
Unit tests for analysis.series_clustering.

Covers: classification of each relation type, the SEPARATE-wins
tie-breaker, vault-membership filtering, the Alt Version TV/non-TV
subtype heuristic, override application (force_same and force_separate),
and canonical-id selection by release_year.
"""

from analysis.series_clustering import (
    UnionFind,
    assign_series_ids,
    classify_relation,
    compute_series_context,
)


# ---------------------------------------------------------------------
# classify_relation
# ---------------------------------------------------------------------

class TestClassifyRelation:

    def test_same_relations(self):
        for name in ("Sequel", "Prequel", "Side Story", "Summary", "Full Story"):
            assert classify_relation(name) == "SAME", name

    def test_separate_relations(self):
        assert classify_relation("Alternative Setting") == "SEPARATE"
        assert classify_relation("Spin-Off") == "SEPARATE"

    def test_alternative_version_is_hybrid(self):
        """Alt Version defers to cluster-level resolution in
        assign_series_ids — can't decide at edge level."""
        assert classify_relation("Alternative Version") == "HYBRID"

    def test_parent_story_is_soft(self):
        """Parent Story is used by MAL for franchise-IP lineage rather
        than narrative parent (e.g., Gundam IBO → Gundam 1979), so it
        must NOT auto-cluster."""
        assert classify_relation("Parent Story") == "SOFT"

    def test_adaptation_character_other_are_soft(self):
        for name in ("Adaptation", "Character", "Other"):
            assert classify_relation(name) == "SOFT"

    def test_unknown_relation_is_soft(self):
        assert classify_relation("Made Up Relation") == "SOFT"

    def test_case_insensitive(self):
        assert classify_relation("sequel") == "SAME"
        assert classify_relation("SEQUEL") == "SAME"
        assert classify_relation("  Side Story  ") == "SAME"
        assert classify_relation("alternative version") == "HYBRID"


# ---------------------------------------------------------------------
# UnionFind
# ---------------------------------------------------------------------

class TestUnionFind:

    def test_singleton_is_its_own_root(self):
        uf = UnionFind()
        uf.add(1)
        assert uf.find(1) == 1

    def test_union_merges_two_nodes(self):
        uf = UnionFind()
        uf.union(1, 2)
        assert uf.find(1) == uf.find(2)

    def test_transitive_union(self):
        uf = UnionFind()
        uf.union(1, 2)
        uf.union(2, 3)
        uf.union(3, 4)
        assert uf.find(1) == uf.find(4)

    def test_separate_clusters_stay_separate(self):
        uf = UnionFind()
        uf.union(1, 2)
        uf.union(3, 4)
        assert uf.find(1) != uf.find(3)

    def test_components_returns_groups(self):
        uf = UnionFind()
        uf.union(1, 2)
        uf.union(3, 4)
        groups = uf.components([1, 2, 3, 4, 5])
        sizes = sorted(len(g) for g in groups.values())
        assert sizes == [1, 2, 2]  # {1,2}, {3,4}, {5}

    def test_path_compression_keeps_find_correct(self):
        """Stress: long chain shouldn't change find() correctness."""
        uf = UnionFind()
        for i in range(100):
            uf.union(i, i + 1)
        for i in range(101):
            assert uf.find(i) == uf.find(0)


# ---------------------------------------------------------------------
# assign_series_ids — clustering integration
# ---------------------------------------------------------------------

def _relations(*pairs):
    """Helper: build the relations_by_source mapping from (source, [(rel, target_id, target_type)])
    tuples for readable test fixtures."""
    out = {}
    for source, edges in pairs:
        rels = {}
        for rel_name, target_id, target_type in edges:
            rels.setdefault(rel_name, []).append({
                "mal_id": target_id,
                "type": target_type or "anime",
                "name": f"#{target_id}",
            })
        out[source] = [
            {"relation": k, "entry": v} for k, v in rels.items()
        ]
    return out


class TestAssignSeriesIds:

    def test_two_sequel_chained_seasons_share_series_id(self):
        """AoT-style: S1 -> S2 (Sequel) -> S3 (Sequel). All three cluster."""
        vault = {1, 2, 3}
        years = {1: 2013, 2: 2017, 3: 2018}
        relations = _relations(
            (1, [("Sequel", 2, "anime")]),
            (2, [("Prequel", 1, "anime"), ("Sequel", 3, "anime")]),
            (3, [("Prequel", 2, "anime")]),
        )
        result = assign_series_ids(vault, relations, {}, years)
        assert result[1] == result[2] == result[3]
        assert result[1] == "1"  # earliest year is 2013 → MAL 1 is canonical

    def test_canonical_picks_earliest_year_not_lowest_mal_id(self):
        """Canonical selection prefers earliest release_year over lowest MAL ID."""
        vault = {500, 100}  # MAL 500 released earlier than MAL 100
        years = {500: 2010, 100: 2020}
        relations = _relations((500, [("Sequel", 100, "anime")]))
        result = assign_series_ids(vault, relations, {}, years)
        assert result[500] == result[100] == "500"

    def test_canonical_ties_broken_by_lowest_mal_id(self):
        vault = {10, 20}
        years = {10: 2015, 20: 2015}  # same year
        relations = _relations((10, [("Sequel", 20, "anime")]))
        result = assign_series_ids(vault, relations, {}, years)
        assert result[10] == result[20] == "10"  # lower MAL ID wins tie

    def test_spin_off_keeps_clusters_separate(self):
        """MHA → Vigilante: spin-off must NOT cluster with the main series."""
        vault = {31964, 60593}
        years = {31964: 2016, 60593: 2025}
        relations = _relations(
            (31964, [("Spin-Off", 60593, "anime")]),
        )
        result = assign_series_ids(vault, relations, {}, years)
        assert result[31964] != result[60593]
        assert result[31964] == "31964"
        assert result[60593] == "60593"

    def test_alt_version_reboot_pair_stays_separate(self):
        """FMA 2003 ↔ FMA Brotherhood: both clusters contain TV (each is
        TV itself) → reboot evidence → SEPARATE."""
        vault = {121, 5114}
        years = {121: 2003, 5114: 2009}
        subtypes = {121: "TV", 5114: "TV"}
        relations = _relations(
            (121, [("Alternative Version", 5114, "anime")]),
            (5114, [("Alternative Version", 121, "anime")]),
        )
        result = assign_series_ids(vault, relations, subtypes, years)
        assert result[121] != result[5114]

    def test_alt_version_tv_to_movie_clusters(self):
        """Gintama TV → Gintama Movie 1: TV cluster has TV, movie cluster
        has no TV → retelling evidence → merge."""
        vault = {918, 7472}
        years = {918: 2006, 7472: 2010}
        subtypes = {918: "TV", 7472: "Movie"}
        relations = _relations(
            (918, [("Alternative Version", 7472, "anime")]),
        )
        result = assign_series_ids(vault, relations, subtypes, years)
        assert result[918] == result[7472] == "918"

    def test_alt_version_bidirectional_tv_movie_clusters(self):
        """Code Geass main TV ↔ Recap Movie I: bidirectional Alt Version
        edges. Movie cluster (just the movie) has no TV → merge."""
        vault = {1575, 34438}
        years = {1575: 2007, 34438: 2017}
        subtypes = {1575: "TV", 34438: "Movie"}
        relations = _relations(
            (1575, [("Alternative Version", 34438, "anime")]),
            (34438, [("Alternative Version", 1575, "anime")]),
        )
        result = assign_series_ids(vault, relations, subtypes, years)
        assert result[1575] == result[34438] == "1575"

    def test_reboot_pair_does_not_bridge_via_shared_ova(self):
        """Regression for the HxH 1999 ↔ HxH 2011 case: a TV reboot
        with Alt Version edges to multiple entries in the original
        TV's cluster (including its OVAs) must NOT transitively merge
        via the OVA. The OVA itself is in the original TV's cluster
        via Sequel, so the cluster-level TV check sees TV on both
        sides and blocks the merge."""
        # HxH 1999 cluster: 136 (TV) ←Sequel→ 137 (OVA) ←Sequel→ 138 (OVA)
        # HxH 2011: 11061 (TV)
        # Alt Version edges: 11061 → {136, 137, 138}
        vault = {136, 137, 138, 11061}
        years = {136: 1999, 137: 2002, 138: 2003, 11061: 2011}
        subtypes = {136: "TV", 137: "OVA", 138: "OVA", 11061: "TV"}
        relations = _relations(
            (136, [("Sequel", 137, "anime")]),
            (137, [("Prequel", 136, "anime"), ("Sequel", 138, "anime")]),
            (138, [("Prequel", 137, "anime")]),
            (11061, [
                ("Alternative Version", 136, "anime"),
                ("Alternative Version", 137, "anime"),
                ("Alternative Version", 138, "anime"),
            ]),
        )
        result = assign_series_ids(vault, relations, subtypes, years)
        # 1999 cluster all share an id
        assert result[136] == result[137] == result[138]
        # 11061 (2011 reboot) is in its own cluster
        assert result[11061] != result[136]

    def test_alt_version_when_neither_cluster_has_tv(self):
        """Two Alt-Version movies in separate post-pass-1 clusters,
        neither containing a TV → merge (retelling evidence)."""
        vault = {1, 2}
        years = {1: 2010, 2: 2012}
        subtypes = {1: "Movie", 2: "Movie"}
        relations = _relations(
            (1, [("Alternative Version", 2, "anime")]),
        )
        result = assign_series_ids(vault, relations, subtypes, years)
        assert result[1] == result[2]

    def test_alt_version_missing_subtypes_defaults_to_merge(self):
        """When subtypes are unknown on either side, the cluster-level
        TV check sees no TV anywhere → merges (over-clustering is
        recoverable via overrides; under-clustering is not)."""
        vault = {1, 2}
        years = {1: 2010, 2: 2012}
        relations = _relations(
            (1, [("Alternative Version", 2, "anime")]),
        )
        result = assign_series_ids(vault, relations, {}, years)
        assert result[1] == result[2]

    def test_separate_wins_over_same_in_tie_breaker(self):
        """If both SAME and SEPARATE classifications apply to the same
        pair (e.g., MAL lists a target as both Sequel and Spin-Off from
        the same source), SEPARATE wins."""
        vault = {1, 2}
        years = {1: 2010, 2: 2012}
        # Hypothetical: source lists target as both Sequel (SAME) and Spin-Off (SEPARATE)
        relations = _relations(
            (1, [("Sequel", 2, "anime"), ("Spin-Off", 2, "anime")]),
        )
        result = assign_series_ids(vault, relations, {}, years)
        assert result[1] != result[2]

    def test_separate_wins_across_reverse_direction(self):
        """A → B via Sequel, B → A via Spin-Off. Tie-breaker still fires."""
        vault = {1, 2}
        years = {1: 2010, 2: 2012}
        relations = _relations(
            (1, [("Sequel", 2, "anime")]),
            (2, [("Spin-Off", 1, "anime")]),
        )
        result = assign_series_ids(vault, relations, {}, years)
        assert result[1] != result[2]

    def test_non_vault_targets_ignored(self):
        """Relations pointing to MAL IDs not in the vault contribute nothing."""
        vault = {1}
        years = {1: 2010}
        relations = _relations(
            (1, [("Sequel", 999, "anime")]),  # 999 not in vault
        )
        result = assign_series_ids(vault, relations, {}, years)
        assert result[1] == "1"
        assert 999 not in result

    def test_manga_entry_type_ignored(self):
        """Adaptation entries pointing to manga have type='manga'; they
        must not be considered for clustering."""
        vault = {1, 2}
        years = {1: 2010, 2: 2012}
        relations = {
            1: [{"relation": "Sequel", "entry": [
                {"mal_id": 100, "type": "manga", "name": "manga"},
                {"mal_id": 2, "type": "anime", "name": "#2"},
            ]}],
        }
        result = assign_series_ids(vault, relations, {}, years)
        assert result[1] == result[2]
        assert 100 not in result

    def test_parent_story_does_not_cluster(self):
        """Gundam IBO → Gundam (1979) via Parent Story should NOT cluster
        — they're distinct timelines. This is the dropped rule."""
        vault = {31251, 80}
        years = {31251: 2015, 80: 1979}
        relations = _relations(
            (31251, [("Parent Story", 80, "anime")]),
        )
        result = assign_series_ids(vault, relations, {}, years)
        assert result[31251] != result[80]
        assert result[31251] == "31251"
        assert result[80] == "80"

    def test_force_same_override_unions_pair(self):
        """force_same_cluster bonds two MAL IDs even if rules leave them apart."""
        vault = {1, 2}
        years = {1: 2010, 2: 2012}
        relations = _relations(
            (1, [("Other", 2, "anime")]),  # SOFT, no edge
        )
        overrides = {"force_same_cluster": [[1, 2]], "force_separate_cluster": []}
        result = assign_series_ids(vault, relations, {}, years, overrides)
        assert result[1] == result[2] == "1"

    def test_force_separate_override_splits_pair(self):
        """force_separate_cluster splits a pair that rules would have unioned."""
        vault = {1, 2}
        years = {1: 2010, 2: 2012}
        relations = _relations(
            (1, [("Sequel", 2, "anime")]),  # would normally cluster
        )
        overrides = {"force_same_cluster": [], "force_separate_cluster": [[1, 2]]}
        result = assign_series_ids(vault, relations, {}, years, overrides)
        assert result[1] != result[2]

    def test_force_separate_wins_over_force_same(self):
        """If a pair appears in BOTH override lists, SEPARATE still wins
        (consistent with the rule tie-breaker)."""
        vault = {1, 2}
        years = {1: 2010, 2: 2012}
        relations = _relations(
            (1, [("Sequel", 2, "anime")]),
        )
        overrides = {
            "force_same_cluster": [[1, 2]],
            "force_separate_cluster": [[1, 2]],
        }
        result = assign_series_ids(vault, relations, {}, years, overrides)
        assert result[1] != result[2]

    # Regression scenarios that lock in the intent of the
    # force_separate_cluster additions for the Gundam Wing/SEED/Evolve
    # anthology-bridge case and the Victory/∀/Reconguista bad-Sequel
    # case — both real-world Gundam-cluster miscategorizations.

    def test_anthology_bridge_split_via_force_separate(self):
        """Real-world case: Gundam Evolve (id=3288) is a multi-metaverse
        anthology OVA with Side Story edges to both Wing (After Colony)
        and SEED (Cosmic Era). Without overrides, Pass 1 unions all three
        via the transitive Wing→Evolve→SEED chain. force_separate on both
        anthology-bridge edges cuts the chain — Wing stands alone, SEED
        keeps its own family (via its own edges to other in-vault SEED
        entries), Evolve becomes a singleton (accurate for a multi-metaverse
        anthology).
        """
        wing, seed, evolve, seed_destiny = 90, 93, 3288, 94
        vault = {wing, seed, evolve, seed_destiny}
        years = {wing: 1995, seed: 2002, seed_destiny: 2004, evolve: 2007}
        subtypes = {wing: "TV", seed: "TV", seed_destiny: "TV", evolve: "OVA"}
        relations = _relations(
            (wing, [("Side Story", evolve, "anime")]),
            (seed, [("Side Story", evolve, "anime"),
                    ("Sequel", seed_destiny, "anime")]),
            (evolve, [("Parent Story", wing, "anime"),
                      ("Parent Story", seed, "anime")]),
            (seed_destiny, [("Prequel", seed, "anime")]),
        )
        # Without overrides: transitive bridge merges all four.
        result_no_overrides = assign_series_ids(vault, relations, subtypes, years)
        assert (result_no_overrides[wing] == result_no_overrides[seed]
                == result_no_overrides[evolve] == result_no_overrides[seed_destiny])

        # With overrides: cutting both bridge edges yields 3 clusters.
        overrides = {
            "force_same_cluster": [],
            "force_separate_cluster": [[wing, evolve], [seed, evolve]],
        }
        result = assign_series_ids(vault, relations, subtypes, years, overrides)
        # Wing standalone
        assert result[wing] == str(wing)
        # SEED family stays together (SEED + SEED Destiny linked by Sequel)
        assert result[seed] == result[seed_destiny]
        assert result[seed] != result[wing]
        # Evolve as singleton
        assert result[evolve] == str(evolve)
        assert result[evolve] != result[wing]
        assert result[evolve] != result[seed]

    def test_bad_sequel_label_split_via_force_separate(self):
        """Real-world case: MAL labels Victory Gundam (Universal Century)
        with Sequel edges to both ∀ Gundam (Correct Century) and
        Reconguista in G (Regild Century) — three distinct metaverses
        merged via a factually-wrong Sequel classification. force_separate
        on both bad edges yields three separate clusters.
        """
        victory, turn_a, reconguista = 89, 95, 23259
        vault = {victory, turn_a, reconguista}
        years = {victory: 1993, turn_a: 2000, reconguista: 2014}
        subtypes = {victory: "TV", turn_a: "TV", reconguista: "TV"}
        relations = _relations(
            (victory, [("Sequel", turn_a, "anime"),
                       ("Sequel", reconguista, "anime")]),
            (turn_a, [("Prequel", victory, "anime")]),
            (reconguista, [("Prequel", victory, "anime")]),
        )
        # Without overrides: bad Sequel labels merge all three.
        result_no_overrides = assign_series_ids(vault, relations, subtypes, years)
        assert (result_no_overrides[victory] == result_no_overrides[turn_a]
                == result_no_overrides[reconguista])

        # With overrides: three separate clusters.
        overrides = {
            "force_same_cluster": [],
            "force_separate_cluster": [[victory, turn_a], [victory, reconguista]],
        }
        result = assign_series_ids(vault, relations, subtypes, years, overrides)
        assert result[victory] != result[turn_a]
        assert result[victory] != result[reconguista]
        assert result[turn_a] != result[reconguista]

    def test_standalone_show_gets_self_series_id(self):
        """A row with no relations gets series_id = str(its own id)."""
        vault = {42}
        years = {42: 2020}
        result = assign_series_ids(vault, {}, {}, years)
        assert result[42] == "42"

    def test_all_vault_rows_get_a_series_id(self):
        """Every vault row must appear in the output mapping (sets up the
        downstream NOT NULL contract on the column)."""
        vault = {1, 2, 3, 100, 200}
        years = {1: 2010, 2: 2012, 3: 2014, 100: 2015, 200: 2020}
        relations = _relations((1, [("Sequel", 2, "anime")]))
        result = assign_series_ids(vault, relations, {}, years)
        assert set(result.keys()) == vault

    def test_long_sequel_chain_clusters_into_one(self):
        """Gundam UC-style chain: 5 entries connected pairwise by Sequel.
        Earliest-year canonical wins; cluster contains all five."""
        vault = {80, 87, 6336, 88, 37764}
        years = {80: 1979, 87: 1988, 6336: 2010, 88: 1991, 37764: 2018}
        relations = _relations(
            (80, [("Sequel", 87, "anime")]),
            (87, [("Prequel", 80, "anime"), ("Sequel", 6336, "anime")]),
            (6336, [("Prequel", 87, "anime"), ("Sequel", 88, "anime")]),
            (88, [("Prequel", 6336, "anime"), ("Sequel", 37764, "anime")]),
            (37764, [("Prequel", 88, "anime")]),
        )
        result = assign_series_ids(vault, relations, {}, years)
        assert len({result[mid] for mid in vault}) == 1
        assert result[80] == "80"  # 1979 wins


# ---------------------------------------------------------------------
# compute_series_context — hydration helper
# ---------------------------------------------------------------------

def _sibling(mal_id, year, score=None, sentiment=None, subtype=None):
    return {
        "id": mal_id,
        "release_year": year,
        "mal_score": score,
        "avg_sentiment": sentiment,
        "subtype": subtype,
    }


class TestComputeSeriesContext:

    def test_returns_none_for_empty_siblings(self):
        assert compute_series_context([], target_mal_id=42, series_id="42") is None

    def test_returns_none_when_target_not_in_siblings(self):
        siblings = [_sibling(1, 2010), _sibling(2, 2012)]
        assert compute_series_context(siblings, target_mal_id=99, series_id="1") is None

    def test_standalone_show(self):
        """series_size=1, position=1, trajectory=None for a singleton cluster."""
        siblings = [_sibling(42, 2020, score=8.0, sentiment=0.5)]
        ctx = compute_series_context(siblings, target_mal_id=42, series_id="42")
        assert ctx["series_id"] == "42"
        assert ctx["series_size"] == 1
        assert ctx["position"] == 1
        assert ctx["score_min"] == 8.0
        assert ctx["score_max"] == 8.0
        assert ctx["sentiment_min"] == 0.5
        assert ctx["sentiment_max"] == 0.5
        assert ctx["trajectory"] is None

    def test_position_is_chronological(self):
        """Position is 1-indexed by (release_year, id) ASC."""
        siblings = [
            _sibling(20, 2015, score=8.0),
            _sibling(10, 2013, score=7.5),
            _sibling(30, 2018, score=8.5),
        ]
        ctx_first = compute_series_context(siblings, target_mal_id=10, series_id="10")
        ctx_middle = compute_series_context(siblings, target_mal_id=20, series_id="10")
        ctx_last = compute_series_context(siblings, target_mal_id=30, series_id="10")
        assert ctx_first["position"] == 1
        assert ctx_middle["position"] == 2
        assert ctx_last["position"] == 3

    def test_score_and_sentiment_ranges(self):
        siblings = [
            _sibling(1, 2013, score=7.5, sentiment=0.3),
            _sibling(2, 2015, score=8.5, sentiment=0.7),
            _sibling(3, 2018, score=8.0, sentiment=0.5),
        ]
        ctx = compute_series_context(siblings, target_mal_id=1, series_id="1")
        assert ctx["score_min"] == 7.5
        assert ctx["score_max"] == 8.5
        assert ctx["sentiment_min"] == 0.3
        assert ctx["sentiment_max"] == 0.7

    def test_zero_scores_excluded_from_range(self):
        """mal_score=0 is the ingestion default for 'no score yet' —
        must be excluded so it doesn't drag score_min to 0."""
        siblings = [
            _sibling(1, 2013, score=8.0),
            _sibling(2, 2015, score=0),  # upcoming/unscored
            _sibling(3, 2018, score=8.5),
        ]
        ctx = compute_series_context(siblings, target_mal_id=1, series_id="1")
        assert ctx["score_min"] == 8.0
        assert ctx["score_max"] == 8.5

    def test_negative_sentiment_included(self):
        """avg_sentiment can be negative for divisive shows. The range
        must include negatives — only None gets excluded."""
        siblings = [
            _sibling(1, 2013, sentiment=-0.2),
            _sibling(2, 2015, sentiment=0.4),
        ]
        ctx = compute_series_context(siblings, target_mal_id=1, series_id="1")
        assert ctx["sentiment_min"] == -0.2
        assert ctx["sentiment_max"] == 0.4

    def test_all_missing_scores_returns_none_range(self):
        siblings = [_sibling(1, 2013), _sibling(2, 2015)]
        ctx = compute_series_context(siblings, target_mal_id=1, series_id="1")
        assert ctx["score_min"] is None
        assert ctx["score_max"] is None
        assert ctx["sentiment_min"] is None
        assert ctx["sentiment_max"] is None
        assert ctx["trajectory"] is None  # no scores → no trajectory

    def test_trajectory_stable_when_all_deltas_within_threshold(self):
        siblings = [
            _sibling(1, 2013, score=8.5),
            _sibling(2, 2015, score=8.6),
            _sibling(3, 2018, score=8.4),
        ]
        ctx = compute_series_context(siblings, target_mal_id=1, series_id="1")
        assert ctx["trajectory"] == "stable"

    def test_trajectory_decline_when_monotonic_and_cumulative_threshold(self):
        """Tokyo Ghoul-style — quality drops noticeably across seasons."""
        siblings = [
            _sibling(1, 2014, score=8.5),
            _sibling(2, 2015, score=7.8),
            _sibling(3, 2018, score=6.5),
        ]
        ctx = compute_series_context(siblings, target_mal_id=1, series_id="1")
        assert ctx["trajectory"] == "decline"

    def test_trajectory_improve_when_monotonic_and_cumulative_threshold(self):
        """Re:Zero-style — adaptation finds its footing in later seasons."""
        siblings = [
            _sibling(1, 2016, score=7.5),
            _sibling(2, 2020, score=8.0),
            _sibling(3, 2024, score=8.5),
        ]
        ctx = compute_series_context(siblings, target_mal_id=1, series_id="1")
        assert ctx["trajectory"] == "improve"

    def test_trajectory_mixed_when_direction_reverses(self):
        siblings = [
            _sibling(1, 2013, score=8.5),
            _sibling(2, 2015, score=7.0),
            _sibling(3, 2018, score=8.5),
        ]
        ctx = compute_series_context(siblings, target_mal_id=1, series_id="1")
        assert ctx["trajectory"] == "mixed"

    def test_trajectory_mixed_when_monotonic_but_drift_below_threshold(self):
        """Monotonic decline of 0.3 (below threshold of 0.5) and not
        all-stable (one delta exceeds ±0.2) → mixed, not stable, not
        decline. Edge case for the threshold logic."""
        siblings = [
            _sibling(1, 2013, score=8.0),
            _sibling(2, 2015, score=7.7),
            _sibling(3, 2018, score=7.7),
        ]
        ctx = compute_series_context(siblings, target_mal_id=1, series_id="1")
        # max delta 0.3 > 0.2 stable threshold → not stable
        # monotonic non-increasing, cumulative 0.3 < 0.5 drift threshold → not decline
        # falls through to mixed
        assert ctx["trajectory"] == "mixed"

    def test_trajectory_skips_missing_scores(self):
        """Entries with mal_score=None/0 don't break trajectory — they're
        filtered from the chronological score list."""
        siblings = [
            _sibling(1, 2013, score=8.5),
            _sibling(2, 2015, score=None),  # missing
            _sibling(3, 2018, score=8.4),
        ]
        ctx = compute_series_context(siblings, target_mal_id=1, series_id="1")
        # After filtering, scores=[8.5, 8.4], delta=-0.1 → stable
        assert ctx["trajectory"] == "stable"

    def test_trajectory_returns_none_for_one_valid_score(self):
        """Need at least 2 valid scores to compute a trajectory."""
        siblings = [
            _sibling(1, 2013, score=8.5),
            _sibling(2, 2015),  # no score
        ]
        ctx = compute_series_context(siblings, target_mal_id=1, series_id="1")
        assert ctx["trajectory"] is None


class TestComputeSeriesContextSubtypeFiltering:
    """Subtype-aware score-range filter and tv_position field
    computation."""

    def test_score_range_filters_out_mini_anime(self):
        """Frieren-shaped cluster: TV S1 at 9.3 + Mini Anime at 7.4. The
        filtered range hides the Mini Anime so the visible range reads
        as the main offering, not the spinoff drag."""
        siblings = [
            _sibling(1, 2023, score=9.3, subtype="TV"),
            _sibling(2, 2024, score=7.4, subtype="ONA"),  # Mini Anime equivalent
        ]
        ctx = compute_series_context(siblings, target_mal_id=1, series_id="1")
        assert ctx["score_min"] == 9.3
        assert ctx["score_max"] == 9.3
        assert ctx["score_range_filtered"] is True

    def test_score_range_includes_tv_and_movie_and_tv_special(self):
        """All three SCORE_RANGE_SUBTYPES count. TV Special's score
        contributes to the visible range even when there's no TV entry."""
        siblings = [
            _sibling(1, 2010, score=8.0, subtype="TV Special"),
            _sibling(2, 2012, score=8.5, subtype="Movie"),
            _sibling(3, 2015, score=7.0, subtype="OVA"),
        ]
        ctx = compute_series_context(siblings, target_mal_id=1, series_id="1")
        assert ctx["score_min"] == 8.0
        assert ctx["score_max"] == 8.5
        assert ctx["score_range_filtered"] is True

    def test_score_range_falls_back_when_no_tv_or_movie(self):
        """OVA-only cluster — no TV/Movie/TV Special siblings — falls
        back to the full set so the user still sees a range."""
        siblings = [
            _sibling(1, 2010, score=7.5, subtype="OVA"),
            _sibling(2, 2012, score=8.2, subtype="OVA"),
        ]
        ctx = compute_series_context(siblings, target_mal_id=1, series_id="1")
        assert ctx["score_min"] == 7.5
        assert ctx["score_max"] == 8.2
        assert ctx["score_range_filtered"] is False

    def test_score_range_filtered_false_when_filter_doesnt_change_range(self):
        """All siblings are TV+Movie — filter applies but doesn't
        narrow anything. The flag must stay False so the frontend
        doesn't surface a misleading caption."""
        siblings = [
            _sibling(1, 2013, score=8.0, subtype="TV"),
            _sibling(2, 2015, score=8.5, subtype="Movie"),
            _sibling(3, 2018, score=8.3, subtype="TV"),
        ]
        ctx = compute_series_context(siblings, target_mal_id=1, series_id="1")
        assert ctx["score_min"] == 8.0
        assert ctx["score_max"] == 8.5
        assert ctx["score_range_filtered"] is False

    def test_score_range_filtered_false_when_excluded_siblings_inside_range(self):
        """OVA score sits within the TV+Movie range — filter applies
        but doesn't narrow min/max. Flag stays False."""
        siblings = [
            _sibling(1, 2013, score=8.0, subtype="TV"),
            _sibling(2, 2015, score=8.3, subtype="OVA"),
            _sibling(3, 2018, score=8.6, subtype="Movie"),
        ]
        ctx = compute_series_context(siblings, target_mal_id=1, series_id="1")
        assert ctx["score_min"] == 8.0
        assert ctx["score_max"] == 8.6
        assert ctx["score_range_filtered"] is False

    def test_score_range_filtered_true_only_when_narrowing_occurs(self):
        """OVA score sits outside the TV+Movie range — filter narrows
        the visible range. Flag flips True."""
        siblings = [
            _sibling(1, 2013, score=8.0, subtype="TV"),
            _sibling(2, 2015, score=9.5, subtype="ONA"),  # outlier excluded
            _sibling(3, 2018, score=8.6, subtype="Movie"),
        ]
        ctx = compute_series_context(siblings, target_mal_id=1, series_id="1")
        assert ctx["score_min"] == 8.0
        assert ctx["score_max"] == 8.6
        assert ctx["score_range_filtered"] is True

    def test_tv_position_one_indexed_among_tv_siblings(self):
        """AoT-shaped cluster: 4 TV seasons + 1 movie. tv_position is
        1-indexed among TV only; movies don't shift the numbering."""
        siblings = [
            _sibling(1, 2013, subtype="TV"),       # S1 → tv_position 1
            _sibling(2, 2017, subtype="TV"),       # S2 → tv_position 2
            _sibling(3, 2018, subtype="Movie"),    # film, no tv_position
            _sibling(4, 2018, subtype="TV"),       # S3 → tv_position 3
            _sibling(5, 2022, subtype="TV"),       # S4 → tv_position 4
        ]
        ctx_s2 = compute_series_context(siblings, target_mal_id=2, series_id="1")
        assert ctx_s2["tv_position"] == 2
        assert ctx_s2["tv_count"] == 4

        ctx_s4 = compute_series_context(siblings, target_mal_id=5, series_id="1")
        assert ctx_s4["tv_position"] == 4
        assert ctx_s4["tv_count"] == 4

    def test_tv_position_none_when_target_is_movie(self):
        """A movie within a TV-heavy cluster gets tv_position=None and
        tv_count=None — falls back to the generic 'Entry X of N' label."""
        siblings = [
            _sibling(1, 2013, subtype="TV"),
            _sibling(2, 2017, subtype="TV"),
            _sibling(3, 2018, subtype="Movie"),   # target
            _sibling(4, 2022, subtype="TV"),
        ]
        ctx = compute_series_context(siblings, target_mal_id=3, series_id="1")
        assert ctx["tv_position"] is None
        assert ctx["tv_count"] is None
        assert ctx["position"] == 3  # still has the generic position

    def test_tv_position_none_when_target_is_ova(self):
        siblings = [
            _sibling(1, 2013, subtype="TV"),
            _sibling(2, 2015, subtype="OVA"),   # target
            _sibling(3, 2017, subtype="TV"),
        ]
        ctx = compute_series_context(siblings, target_mal_id=2, series_id="1")
        assert ctx["tv_position"] is None
        assert ctx["tv_count"] is None

    def test_tv_special_does_not_count_as_tv_for_position(self):
        """TV Special is broadcast but not a numbered season. The target
        being TV Special should NOT get a tv_position; other TV Special
        entries in the cluster should NOT count toward tv_count."""
        siblings = [
            _sibling(1, 2010, subtype="TV"),          # tv_position 1
            _sibling(2, 2012, subtype="TV Special"),  # NOT counted
            _sibling(3, 2014, subtype="TV"),          # tv_position 2
        ]
        ctx_tv_special = compute_series_context(siblings, target_mal_id=2, series_id="1")
        assert ctx_tv_special["tv_position"] is None
        assert ctx_tv_special["tv_count"] is None

        ctx_tv = compute_series_context(siblings, target_mal_id=3, series_id="1")
        assert ctx_tv["tv_position"] == 2
        assert ctx_tv["tv_count"] == 2  # TV Special excluded

    def test_subtype_missing_falls_back_gracefully(self):
        """Pre-backfill state: subtype=None on every sibling. Filter
        sees no SCORE_RANGE_SUBTYPES matches, falls back to full set.
        tv_position/tv_count both None. No crash."""
        siblings = [
            _sibling(1, 2013, score=8.0),  # subtype defaults to None
            _sibling(2, 2015, score=8.5),
        ]
        ctx = compute_series_context(siblings, target_mal_id=1, series_id="1")
        assert ctx["score_min"] == 8.0
        assert ctx["score_max"] == 8.5
        assert ctx["score_range_filtered"] is False
        assert ctx["tv_position"] is None
        assert ctx["tv_count"] is None

    def test_standalone_tv_show_gets_tv_position_one(self):
        """Singleton TV cluster: tv_position=1, tv_count=1. The frontend
        will fall back to the generic label when tv_count == 1, but the
        backend still reports the position cleanly."""
        siblings = [_sibling(42, 2020, score=8.5, subtype="TV")]
        ctx = compute_series_context(siblings, target_mal_id=42, series_id="42")
        assert ctx["tv_position"] == 1
        assert ctx["tv_count"] == 1
