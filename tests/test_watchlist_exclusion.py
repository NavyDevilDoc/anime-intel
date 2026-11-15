"""
PR C / Initiative 2 Phase C — exclusion plumbing tests.

Covers cluster expansion (analysis.queries.expand_to_cluster_ids) and
the request-schema validation for the new exclude_mal_ids field. The
deeper engine-level integration (search + DNA endpoints actually
excluding shows from the rerank pool) lives in test_api.py since it
needs the FastAPI TestClient + mocked engine.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from analysis.queries import expand_to_cluster_ids  # noqa: E402


# =====================================================================
# Cluster expansion (the PR #77 fold-in)
# =====================================================================

class TestExpandToClusterIds:
    """Verifies the cluster-aware exclusion expansion against the live
    anime_intelligence_v2.db. These tests anchor on three well-known
    franchises from PR #77's clustering:
      - Naruto (id=20)         → cluster 594, 23 entries (post-PR-#79)
      - Attack on Titan (id=16498) → cluster 16498, 16 entries
      - Cowboy Bebop (id=1)    → cluster 1, ~2 entries (singleton + movie)
    """

    def test_empty_input_returns_empty(self):
        assert expand_to_cluster_ids([]) == set()
        assert expand_to_cluster_ids(None) == set()

    def test_input_id_always_included(self):
        """Even when the MAL ID is unknown / has no cluster, the input
        passes through. Prevents accidental data loss when the
        watchlist contains shows the vault hasn't ingested."""
        out = expand_to_cluster_ids([99999999])  # very likely not in vault
        assert 99999999 in out

    def test_naruto_expansion_includes_known_siblings(self):
        """Naruto (id=20) → cluster 594, which after PR #79 includes
        Naruto x UT (10075), Sunny Side Battle (19511), Konoha Gakuen
        Special (4134) — the cluster-override load-bearing IDs still
        in the vault. Road of Naruto (53236) was also a PR #79 override
        anchor but was pruned from the vault in PR #168's Music/CM/PV
        subtype sweep; its override pairs in series_cluster_overrides.json
        are latent (re-activate if the show is ever re-ingested)."""
        out = expand_to_cluster_ids([20])
        assert 20 in out
        # The IDs PR #79 manually clustered into Naruto:
        assert 10075 in out, "Naruto x UT should be in Naruto's cluster"
        assert 19511 in out, "Naruto Sunny Side Battle should be in Naruto's cluster"
        assert 4134 in out, "Naruto Konoha Gakuen should be in Naruto's cluster"
        # Naruto Shippuden (1735) sequel via standard rules:
        assert 1735 in out, "Naruto Shippuden should be in Naruto's cluster"

    def test_aot_expansion_size(self):
        """Attack on Titan (id=16498) → cluster 16498, ~14 entries.
        Asserts a lower bound that survives future cluster growth."""
        out = expand_to_cluster_ids([16498])
        assert 16498 in out
        assert len(out) >= 10, f"AoT cluster should be 10+ entries, got {len(out)}"

    def test_multiple_input_ids_unions_clusters(self):
        """Naruto + AoT in one input should union both clusters."""
        out = expand_to_cluster_ids([20, 16498])
        # Naruto siblings AND AoT siblings both present
        assert 10075 in out  # Naruto x UT (override-only sibling)
        assert 16498 in out  # AoT itself
        # Should be at least the sum of the larger cluster + a little
        naruto_only = expand_to_cluster_ids([20])
        aot_only = expand_to_cluster_ids([16498])
        assert len(out) >= max(len(naruto_only), len(aot_only))

    def test_singleton_cluster_returns_just_the_show(self):
        """A show with no cluster siblings comes back as a 1-element set."""
        # Cowboy Bebop is a small cluster — find one with no siblings.
        # Use a definitely-standalone show by checking against series_id.
        import sqlite3
        conn = sqlite3.connect(os.path.join(ROOT_DIR, "data", "anime_intelligence_v2.db"))
        # Find any row whose series_id has only one member.
        cur = conn.cursor()
        cur.execute("""
            SELECT id FROM anime_info
            WHERE series_id IN (
                SELECT series_id FROM anime_info
                WHERE series_id IS NOT NULL
                GROUP BY series_id HAVING COUNT(*) = 1
            )
            LIMIT 1
        """)
        row = cur.fetchone()
        conn.close()
        if not row:
            pytest.skip("No singleton clusters in vault")
        singleton_id = row[0]
        out = expand_to_cluster_ids([singleton_id])
        assert out == {singleton_id}

    def test_large_input_chunked_correctly(self):
        """When input exceeds the 999-param SQL cap, the helper chunks
        the lookup transparently. Build a synthetic large input mixing
        real IDs (so the SQL has hits) with bogus high IDs."""
        # Take a real Naruto ID + 1500 high non-existent IDs.
        large_input = [20] + list(range(100_000_000, 100_001_500))
        out = expand_to_cluster_ids(large_input)
        # Real Naruto siblings still in output
        assert 20 in out
        assert 1735 in out  # Naruto Shippuden (standard sequel-rule sibling)
        # Bogus IDs passed through as themselves (input always preserved)
        assert 100_000_000 in out

    def test_none_series_id_passes_through_self(self):
        """Rows with series_id IS NULL should still pass back their own
        ID — they're just treated as singletons-of-one."""
        import sqlite3
        conn = sqlite3.connect(os.path.join(ROOT_DIR, "data", "anime_intelligence_v2.db"))
        cur = conn.cursor()
        cur.execute("SELECT id FROM anime_info WHERE series_id IS NULL LIMIT 1")
        row = cur.fetchone()
        conn.close()
        if not row:
            pytest.skip("All rows have series_id populated (no NULL rows)")
        null_id = row[0]
        out = expand_to_cluster_ids([null_id])
        assert null_id in out


# =====================================================================
# Telemetry serialization — _filters_for_telemetry (PR C bug fix)
# =====================================================================

class TestFiltersForTelemetry:
    """The journal-shape filters dict must stay JSON-serializable, even
    when exclude_mal_ids is a large set. The set collapses to an int
    count under a renamed key so production telemetry rows stay small."""

    def test_exclude_set_collapses_to_count(self):
        import json
        from analysis.vector_store import _filters_for_telemetry
        filters = {
            "has_filters": True,
            "year_min": 2010,
            "exclude_mal_ids": {1, 5, 16498, 20},
        }
        out = _filters_for_telemetry(filters)
        assert out["exclude_mal_ids_count"] == 4
        assert "exclude_mal_ids" not in out
        # And the result is JSON-serializable end-to-end
        json.dumps(out)

    def test_has_filters_marker_stripped(self):
        from analysis.vector_store import _filters_for_telemetry
        out = _filters_for_telemetry({"has_filters": True, "year_min": 2020})
        assert "has_filters" not in out

    def test_falsy_values_stripped(self):
        from analysis.vector_store import _filters_for_telemetry
        out = _filters_for_telemetry({
            "has_filters": True,
            "year_min": 0,
            "year_max": 2020,
            "genres": [],
            "studio": "",
        })
        # Only truthy values survive (matches original filter-dict snapshot)
        assert out == {"year_max": 2020}

    def test_empty_exclude_set_yields_zero_count(self):
        from analysis.vector_store import _filters_for_telemetry
        # An empty set is falsy so it's stripped entirely (consistent with
        # how empty genres lists are handled). exclude_mal_ids_count
        # only appears when there's a non-empty exclusion.
        out = _filters_for_telemetry({"has_filters": True, "exclude_mal_ids": set()})
        assert "exclude_mal_ids_count" not in out

    def test_non_set_exclude_value_handled(self):
        """Defensive: if a caller passes a list instead of a set, count still works."""
        from analysis.vector_store import _filters_for_telemetry
        out = _filters_for_telemetry({"has_filters": True, "exclude_mal_ids": [1, 2, 3]})
        assert out["exclude_mal_ids_count"] == 3
