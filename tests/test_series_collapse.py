"""
Unit tests for the Phase D engine-level series helpers:
    _collapse_same_series — group rerank pool by series_id, keep top scorer
    _series_collapse_enabled — env-var rollback flag
    _compute_sibling_exclude_titles — SLT same-series exclusion set builder
    _hydrate_series_context <-> _collapse_same_series handoff (dropped_siblings)

These live in analysis/recommendation_engine.py since they orchestrate
between the rerank pool, the series_id column, and the centroid search;
the pure clustering primitives stay in analysis/series_clustering.py
(tested separately in test_series_clustering.py).
"""

import os
from unittest.mock import patch

from analysis.recommendation_engine import (
    _collapse_same_series,
    _compute_sibling_exclude_titles,
    _hydrate_series_context,
    _series_collapse_enabled,
)


def _entry(mal_id, series_id, confidence, title=None, con=None, spoiler=False):
    """Build a scored_pool entry the way the engine constructs them."""
    return {
        "ai_reasoning": "reasoning text",
        "match_confidence": confidence,
        "controversy_warning": None,
        "contains_spoiler": spoiler,
        "query_relevant_con": con,
        "profile": {
            "id": mal_id,
            "title": title or f"Show {mal_id}",
            "series_id": series_id,
        },
    }


# ---------------------------------------------------------------------
# _series_collapse_enabled
# ---------------------------------------------------------------------

class TestSeriesCollapseEnabled:

    def test_default_is_enabled(self):
        # No env var set -> default True
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SERIES_COLLAPSE_ENABLED", None)
            assert _series_collapse_enabled() is True

    def test_explicit_false_disables(self):
        for falsy in ("false", "False", "FALSE", "0", "no", "off"):
            with patch.dict(os.environ, {"SERIES_COLLAPSE_ENABLED": falsy}):
                assert _series_collapse_enabled() is False, falsy

    def test_truthy_values_enable(self):
        for truthy in ("true", "True", "1", "yes", "on", "anything-else"):
            with patch.dict(os.environ, {"SERIES_COLLAPSE_ENABLED": truthy}):
                assert _series_collapse_enabled() is True, truthy


# ---------------------------------------------------------------------
# _collapse_same_series
# ---------------------------------------------------------------------

class TestCollapseSameSeries:

    def teardown_method(self):
        os.environ.pop("SERIES_COLLAPSE_ENABLED", None)

    def test_empty_pool_passes_through(self):
        assert _collapse_same_series([]) == []

    def test_disabled_flag_returns_unchanged(self):
        pool = [_entry(1, "1", 95), _entry(2, "1", 80)]
        with patch.dict(os.environ, {"SERIES_COLLAPSE_ENABLED": "false"}):
            assert _collapse_same_series(pool) == pool

    def test_single_entry_passes_through(self):
        pool = [_entry(42, "42", 95)]
        out = _collapse_same_series(pool)
        assert len(out) == 1
        assert out[0]["profile"]["id"] == 42
        # No siblings -> no _dropped_siblings key
        assert "_dropped_siblings" not in out[0]

    def test_distinct_series_all_pass_through(self):
        pool = [_entry(1, "1", 95), _entry(2, "2", 90), _entry(3, "3", 85)]
        out = _collapse_same_series(pool)
        assert len(out) == 3

    def test_same_series_keeps_highest_scorer(self):
        """Caller passes pool sorted DESC by confidence — first per
        series_id is the top scorer and is kept."""
        pool = [
            _entry(16498, "16498", 95, title="Attack on Titan"),
            _entry(25777, "16498", 80, title="Attack on Titan Season 2"),
            _entry(35760, "16498", 70, title="Attack on Titan Season 3"),
        ]
        out = _collapse_same_series(pool)
        assert len(out) == 1
        assert out[0]["profile"]["id"] == 16498
        assert out[0]["match_confidence"] == 95

    def test_dropped_siblings_carry_confidence_and_con(self):
        """The visible result's _dropped_siblings list captures each
        dropped sibling's reranker output the user already paid for."""
        pool = [
            _entry(16498, "16498", 95, title="AoT", con="Slow opening arc"),
            _entry(25777, "16498", 80, title="AoT S2", con="Pacing dips",
                   spoiler=True),
        ]
        out = _collapse_same_series(pool)
        assert len(out) == 1
        dropped = out[0]["_dropped_siblings"]
        assert len(dropped) == 1
        assert dropped[0]["id"] == 25777
        assert dropped[0]["title"] == "AoT S2"
        assert dropped[0]["match_confidence"] == 80
        assert dropped[0]["query_relevant_con"] == "Pacing dips"
        assert dropped[0]["contains_spoiler"] is True

    def test_mixed_pool_collapses_only_same_series_groups(self):
        """Some clusters collapse, others (singletons) pass through."""
        pool = [
            _entry(1, "1", 95, title="Show 1"),
            _entry(20, "20", 90, title="Naruto"),
            _entry(2, "20", 85, title="Naruto Shippuden"),
            _entry(3, "3", 80, title="Standalone"),
            _entry(4, "20", 75, title="Boruto"),
        ]
        out = _collapse_same_series(pool)
        # 1, 20, 3 stay; 2 and 4 join 20 as dropped siblings
        assert len(out) == 3
        ids = [e["profile"]["id"] for e in out]
        assert ids == [1, 20, 3]
        # Verify 20 collected 2 and 4
        naruto = next(e for e in out if e["profile"]["id"] == 20)
        dropped_ids = sorted(s["id"] for s in naruto["_dropped_siblings"])
        assert dropped_ids == [2, 4]

    def test_missing_series_id_passes_through_undeduped(self):
        """Defensive: profiles missing series_id can't be safely
        deduped — pass through individually to avoid losing data."""
        pool = [
            _entry(1, None, 95),
            _entry(2, None, 90),
        ]
        out = _collapse_same_series(pool)
        assert len(out) == 2

    def test_collapse_preserves_input_order_for_kept_entries(self):
        """The output order matches the FIRST occurrence per series_id
        in the input. With a confidence-sorted input, this preserves
        the existing ranking."""
        pool = [
            _entry(10, "10", 95),
            _entry(20, "20", 92),
            _entry(11, "10", 88),  # dropped, no effect on order
            _entry(30, "30", 85),
        ]
        out = _collapse_same_series(pool)
        ids = [e["profile"]["id"] for e in out]
        assert ids == [10, 20, 30]


# ---------------------------------------------------------------------
# _compute_sibling_exclude_titles
# ---------------------------------------------------------------------

class TestComputeSiblingExcludeTitles:
    """The helper is in-process — patches queries.fetch_series_siblings
    to control sibling fetch output without touching the real DB."""

    def test_empty_profiles_returns_empty_set(self):
        assert _compute_sibling_exclude_titles([], []) == set()

    def test_profiles_missing_series_id_return_empty_set(self):
        profs = [{"id": 1, "title": "X"}, {"id": 2, "title": "Y", "series_id": None}]
        assert _compute_sibling_exclude_titles(profs, ["X", "Y"]) == set()

    def test_returns_sibling_titles_excluding_references(self):
        """The reference titles themselves stay OUT — search_by_centroid
        excludes them via its main `titles` param. The exclude set is
        purely the siblings."""
        profs = [{"id": 20, "title": "Naruto", "series_id": "20"}]
        sibling_map = {
            "20": [
                {"id": 20, "title": "Naruto", "release_year": 2002,
                 "mal_score": 8.3, "avg_sentiment": 0.5},
                {"id": 1735, "title": "Naruto: Shippuden", "release_year": 2007,
                 "mal_score": 8.2, "avg_sentiment": 0.5},
                {"id": 60816, "title": "Road of Naruto", "release_year": 2022,
                 "mal_score": 7.5, "avg_sentiment": 0.4},
            ]
        }
        with patch("analysis.recommendation_engine.queries.fetch_series_siblings",
                   return_value=sibling_map):
            out = _compute_sibling_exclude_titles(profs, ["Naruto"])
        assert "Naruto" not in out  # reference is kept out
        assert "Naruto: Shippuden" in out
        assert "Road of Naruto" in out  # the leak fix candidate

    def test_multiple_reference_series_union_all_siblings(self):
        profs = [
            {"id": 1, "title": "A", "series_id": "1"},
            {"id": 100, "title": "B", "series_id": "100"},
        ]
        sibling_map = {
            "1": [{"id": 1, "title": "A"}, {"id": 2, "title": "A Sequel"}],
            "100": [{"id": 100, "title": "B"}, {"id": 101, "title": "B Movie"}],
        }
        with patch("analysis.recommendation_engine.queries.fetch_series_siblings",
                   return_value=sibling_map):
            out = _compute_sibling_exclude_titles(profs, ["A", "B"])
        assert out == {"A Sequel", "B Movie"}

    def test_standalone_reference_returns_empty_set(self):
        """When a reference's series_id == its own id (standalone), the
        only sibling is itself, which is filtered out -> empty set."""
        profs = [{"id": 42, "title": "Lonely Show", "series_id": "42"}]
        sibling_map = {
            "42": [{"id": 42, "title": "Lonely Show"}],
        }
        with patch("analysis.recommendation_engine.queries.fetch_series_siblings",
                   return_value=sibling_map):
            out = _compute_sibling_exclude_titles(profs, ["Lonely Show"])
        assert out == set()


# ---------------------------------------------------------------------
# _hydrate_series_context — verifies the dropped_siblings handoff from
# the collapse step makes it into the final SeriesContext payload.
# ---------------------------------------------------------------------

class TestHydrationConsumesDroppedSiblings:

    def test_dropped_siblings_surface_on_series_context(self):
        """After collapse leaves _dropped_siblings on the kept entry,
        hydration must merge them into the SeriesContext payload AND
        strip the private key from the entry."""
        pool = [_entry(16498, "16498", 95, title="AoT")]
        # Simulate what collapse leaves behind for the kept entry
        pool[0]["_dropped_siblings"] = [
            {"id": 25777, "title": "AoT S2", "match_confidence": 80,
             "query_relevant_con": "Pacing dips", "contains_spoiler": False},
        ]
        sibling_rows = [
            {"id": 16498, "title": "AoT", "release_year": 2013,
             "mal_score": 8.5, "avg_sentiment": 0.5},
            {"id": 25777, "title": "AoT S2", "release_year": 2017,
             "mal_score": 8.4, "avg_sentiment": 0.5},
        ]
        with patch("analysis.recommendation_engine.queries.fetch_series_siblings",
                   return_value={"16498": sibling_rows}):
            _hydrate_series_context(pool)

        ctx = pool[0]["series_context"]
        assert ctx is not None
        assert ctx["series_size"] == 2
        assert ctx["position"] == 1
        assert "dropped_siblings" in ctx
        assert len(ctx["dropped_siblings"]) == 1
        assert ctx["dropped_siblings"][0]["title"] == "AoT S2"
        # Private key consumed
        assert "_dropped_siblings" not in pool[0]

    def test_no_dropped_siblings_means_empty_default(self):
        """Entries with no collapse-marked siblings get series_context
        with no dropped_siblings key (Pydantic default will fill empty
        list on the schema side)."""
        pool = [_entry(42, "42", 95)]
        sibling_rows = [{"id": 42, "title": "Show", "release_year": 2020,
                         "mal_score": 8.0, "avg_sentiment": 0.5}]
        with patch("analysis.recommendation_engine.queries.fetch_series_siblings",
                   return_value={"42": sibling_rows}):
            _hydrate_series_context(pool)
        ctx = pool[0]["series_context"]
        assert ctx is not None
        # Helper returns no dropped_siblings key when none were collapsed
        assert "dropped_siblings" not in ctx
