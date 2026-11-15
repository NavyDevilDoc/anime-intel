"""
FastAPI endpoint tests.
Uses FastAPI's TestClient (backed by httpx) for all API tests.
Validation tests (422 responses) don't hit the Gemini API.
Integration tests that exercise the full pipeline are marked @api.

Run all:       pytest tests/test_api.py -v
Run offline:   pytest tests/test_api.py -m "not api" -v
"""
import os
import sys
import pytest
from fastapi.testclient import TestClient

TESTS_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT_DIR, "env_variables.env"))

# Disable rate limiting for the shared test client — rate limit tests
# use their own isolated app instances with custom limits.
os.environ["TESTING"] = "1"

# Known admin credentials for Basic Auth tests. Must be set BEFORE importing
# the app, because verify_basic_auth reads these at call time but the routes
# that depend on it are wired at import.
os.environ["ADMIN_USERNAME"] = "test-admin"
os.environ["ADMIN_PASSWORD"] = "test-password"

import base64
from unittest.mock import MagicMock, patch
from api.main import app
from api.dependencies import get_engine

# Patch check_anon_quota at the route module level so offline tests don't
# hit Supabase. Tests that exercise the real anon quota logic live in
# tests/test_anon_quota.py and patch the underlying supabase_get/post/patch
# directly at the middleware level — those are unaffected by this patch.
#
# Return a real AnonRemaining tuple so routes that thread .search_remaining /
# .dna_remaining into their response bodies don't crash on the bypass path.
from api.middleware.anon_quota import AnonRemaining
_BYPASS_REMAINING = AnonRemaining(30, 10)
for _mod in ("api.routes.search", "api.routes.dna"):
    patch(f"{_mod}.check_anon_quota", return_value=_BYPASS_REMAINING).start()

client = TestClient(app)

# Pre-encoded Basic Auth header for admin tests
_ADMIN_AUTH_HEADER = {
    "Authorization": "Basic " + base64.b64encode(b"test-admin:test-password").decode(),
}

api = pytest.mark.api
has_key = os.environ.get("GOOGLE_API_KEY") is not None
skip_no_key = pytest.mark.skipif(not has_key, reason="GOOGLE_API_KEY not set")
has_supabase = os.environ.get("SUPABASE_URL") is not None and os.environ.get("SUPABASE_SERVICE_KEY") is not None
skip_no_supabase = pytest.mark.skipif(not has_supabase, reason="SUPABASE_URL/SUPABASE_SERVICE_KEY not set")


@pytest.fixture
def mock_engine():
    """Fresh MagicMock engine registered as the ``get_engine`` dependency
    override for the test's duration, then removed. Tests configure the
    mock's return values (``.fetch_vault_pool.return_value = ...`` or
    ``.execute_dna_triangulation.return_value = ...``) directly."""
    m = MagicMock()
    app.dependency_overrides[get_engine] = lambda: m
    yield m
    app.dependency_overrides.pop(get_engine, None)


# =====================================================================
# HEALTH ENDPOINT
# =====================================================================

class TestHealthEndpoint:
    """Verify the health check returns system status."""

    def test_health_returns_200(self):
        response = client.get("/api/health")
        assert response.status_code == 200

    def test_health_contains_required_fields(self):
        response = client.get("/api/health")
        data = response.json()
        assert "status" in data
        assert "anime_count" in data
        assert "index_dims" in data
        assert "db_online" in data

    def test_health_reports_anime_count(self):
        response = client.get("/api/health")
        data = response.json()
        assert data["anime_count"] > 6000, f"Expected ~6236 anime, got {data['anime_count']}"

    def test_health_reports_index_dims(self):
        response = client.get("/api/health")
        data = response.json()
        assert data["index_dims"] == 3072, f"Expected 3072 dims, got {data['index_dims']}"

    def test_health_db_online(self):
        response = client.get("/api/health")
        data = response.json()
        assert data["db_online"] is True


# =====================================================================
# SEARCH ENDPOINT — VALIDATION (no API key needed)
# =====================================================================

class TestSearchValidation:
    """Verify request validation returns 422 for bad inputs."""

    def test_empty_query_returns_422(self):
        response = client.post("/api/search", json={"query": ""})
        assert response.status_code == 422

    def test_missing_query_returns_422(self):
        response = client.post("/api/search", json={})
        assert response.status_code == 422

    def test_oversized_query_returns_422(self):
        response = client.post("/api/search", json={"query": "a" * 501})
        assert response.status_code == 422

    def test_invalid_lens_returns_422(self):
        response = client.post("/api/search", json={
            "query": "test",
            "lens": "Invalid Mode"
        })
        assert response.status_code == 422

    def test_valid_lens_accepted(self):
        """Objective Rankings doesn't hit Gemini, so this works without API key."""
        response = client.post("/api/search", json={
            "query": "top shounen anime",
            "lens": "Objective Rankings"
        })
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["pool_size"] > 0

    def test_era_filter_passed_through(self):
        """Objective Rankings with year filter."""
        response = client.post("/api/search", json={
            "query": "top romance anime from 2023",
            "lens": "Objective Rankings"
        })
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    def test_response_shape(self):
        """Verify the response matches the documented schema."""
        response = client.post("/api/search", json={
            "query": "top anime",
            "lens": "Objective Rankings"
        })
        data = response.json()
        assert "success" in data
        assert "pool" in data
        assert "pool_size" in data
        if data["pool"]:
            item = data["pool"][0]
            assert "ai_reasoning" in item
            assert "match_confidence" in item
            assert "contains_spoiler" in item
            assert "profile" in item
            profile = item["profile"]
            assert "title" in profile
            assert "quality_score" in profile
            assert "studio" in profile


# =====================================================================
# SEARCH ENDPOINT — RATING FILTER VALIDATION
# =====================================================================

class TestSearchRatingFilterValidation:
    """Verify rating_max is validated and threaded to the engine."""

    def test_rating_max_null_accepted(self):
        """Default behavior — no rating filter."""
        response = client.post("/api/search", json={
            "query": "top anime",
            "lens": "Objective Rankings",
            "rating_max": None,
        })
        assert response.status_code == 200

    def test_rating_max_omitted_accepted(self):
        """Backwards-compatible: missing field is treated as null."""
        response = client.post("/api/search", json={
            "query": "top anime",
            "lens": "Objective Rankings",
        })
        assert response.status_code == 200

    @pytest.mark.parametrize("ceiling", ["G", "PG", "PG-13", "R"])
    def test_valid_ceilings_accepted(self, ceiling):
        response = client.post("/api/search", json={
            "query": "top anime",
            "lens": "Objective Rankings",
            "rating_max": ceiling,
        })
        # Objective Rankings doesn't apply the rating filter, but the
        # request must still validate cleanly.
        assert response.status_code == 200

    @pytest.mark.parametrize("invalid", ["R+", "Rx", "X", "g", "pg", "  R  ", ""])
    def test_invalid_ceilings_returns_422(self, invalid):
        """R+ and Rx are not exposed as ceilings (R already excludes them).
        Case must be exact. Whitespace and empty string rejected."""
        response = client.post("/api/search", json={
            "query": "top anime",
            "rating_max": invalid,
        })
        assert response.status_code == 422

    def test_rating_max_reaches_engine(self, mock_engine):
        """End-to-end plumbing: the validated value must be passed to
        fetch_vault_pool's rating_max kwarg."""
        mock_engine.fetch_vault_pool.return_value = {
            "success": True, "pool": [], "sql_used": "RAG:FAISS", "search_id": "test-id",
        }
        response = client.post("/api/search", json={
            "query": "wholesome adventure",
            "rating_max": "PG",
        })
        assert response.status_code == 200
        _, kwargs = mock_engine.fetch_vault_pool.call_args
        assert kwargs["rating_max"] == "PG"

    def test_null_rating_max_threads_as_none(self, mock_engine):
        """Backwards-compat: when no rating_max sent, engine receives None."""
        mock_engine.fetch_vault_pool.return_value = {
            "success": True, "pool": [], "sql_used": "RAG:FAISS", "search_id": "test-id",
        }
        response = client.post("/api/search", json={"query": "test"})
        assert response.status_code == 200
        _, kwargs = mock_engine.fetch_vault_pool.call_args
        assert kwargs["rating_max"] is None


# =====================================================================
# SEARCH ENDPOINT — STAFF/STUDIO MODE
# =====================================================================

class TestStaffStudioValidation:
    """Verify Staff/Studio request validation and the mode-conditional
    query/chip requirements."""

    def test_staff_studio_lens_accepted(self):
        response = client.post("/api/search", json={
            "query": "",
            "lens": "Staff/Studio",
            "staff_filter": {
                "persons": [{"person_mal_id": 2009, "role": "Director"}],
                "studios": [],
            },
        })
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    def test_staff_studio_missing_chips_returns_422(self):
        """No chips of any kind — cross-field validator rejects."""
        response = client.post("/api/search", json={
            "query": "",
            "lens": "Staff/Studio",
        })
        assert response.status_code == 422

    def test_staff_studio_empty_chip_lists_returns_422(self):
        """Explicit empty lists — same rejection as no staff_filter."""
        response = client.post("/api/search", json={
            "query": "",
            "lens": "Staff/Studio",
            "staff_filter": {"persons": [], "studios": []},
        })
        assert response.status_code == 422

    def test_staff_studio_studio_only_accepted(self):
        response = client.post("/api/search", json={
            "query": "",
            "lens": "Staff/Studio",
            "staff_filter": {"persons": [], "studios": ["Ghibli"]},
        })
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    def test_staff_studio_any_role_chip_accepted(self):
        """role=None is the 'Any role' pinned option — must accept."""
        response = client.post("/api/search", json={
            "query": "",
            "lens": "Staff/Studio",
            "staff_filter": {
                "persons": [{"person_mal_id": 2009, "role": None}],
                "studios": [],
            },
        })
        assert response.status_code == 200

    def test_staff_studio_bad_role_returns_422(self):
        """Role must be one of the five StaffRole literals."""
        response = client.post("/api/search", json={
            "query": "",
            "lens": "Staff/Studio",
            "staff_filter": {
                "persons": [{"person_mal_id": 2009, "role": "Bogus Role"}],
                "studios": [],
            },
        })
        assert response.status_code == 422

    def test_staff_studio_person_id_must_be_positive(self):
        response = client.post("/api/search", json={
            "query": "",
            "lens": "Staff/Studio",
            "staff_filter": {
                "persons": [{"person_mal_id": 0, "role": "Director"}],
                "studios": [],
            },
        })
        assert response.status_code == 422

    def test_intelligent_search_still_requires_query(self):
        """Regression guard — IS mode with empty query is still 422 after
        the mode-conditional validator was added for Staff/Studio."""
        response = client.post("/api/search", json={
            "query": "",
            "lens": "Intelligent Search",
        })
        assert response.status_code == 422

    def test_staff_studio_returns_expected_shape(self):
        """Pool items should carry the same ScoredResult shape as other
        modes so the frontend renders through the shared code path."""
        response = client.post("/api/search", json={
            "query": "",
            "lens": "Staff/Studio",
            "staff_filter": {
                "persons": [{"person_mal_id": 2009, "role": "Director"}],
                "studios": [],
            },
        })
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["pool_size"] >= 1
        item = data["pool"][0]
        assert item["match_confidence"] == 100  # deterministic filter, not scored
        assert "profile" in item
        assert "title" in item["profile"]

    def test_staff_studio_watanabe_director_returns_bebop(self):
        """Content check — Watanabe (MAL 2009) as Director should surface
        Cowboy Bebop, his canonical work."""
        response = client.post("/api/search", json={
            "query": "",
            "lens": "Staff/Studio",
            "staff_filter": {
                "persons": [{"person_mal_id": 2009, "role": "Director"}],
                "studios": [],
            },
        })
        assert response.status_code == 200
        data = response.json()
        titles = [item["profile"]["title"] for item in data["pool"]]
        assert "Cowboy Bebop" in titles

    def test_staff_studio_does_not_collapse_same_series(self):
        """Regression guard — Staff/Studio mode must NOT run the
        `_collapse_same_series` step that IS/OR modes use.

        Rationale: chip-filtering by a studio (or director) is
        catalog-shaped — users want to see every season of every show the
        studio produced. A MAPPA filter collapsed to one entry per
        franchise would drop most of the surface the user asked for.
        """
        response = client.post("/api/search", json={
            "query": "",
            "lens": "Staff/Studio",
            "staff_filter": {"persons": [], "studios": ["MAPPA"]},
        })
        assert response.status_code == 200
        data = response.json()
        titles = [item["profile"]["title"] for item in data["pool"]]
        # If _collapse_same_series were running, we'd expect ~1 AoT and
        # ~1 JJK. The catalog for MAPPA has multiple seasons of both, so
        # we assert at least 2 franchise members for at least one series.
        aot_count = sum(1 for t in titles if "Attack on Titan" in t)
        jjk_count = sum(1 for t in titles if "Jujutsu Kaisen" in t)
        assert aot_count >= 2 or jjk_count >= 2, (
            f"Staff/Studio pool appears to have been collapsed: got "
            f"{len(titles)} results with aot={aot_count}, jjk={jjk_count}. "
            f"Expected multiple same-franchise entries per the "
            f"catalog-view design. Titles: {titles}"
        )


# =====================================================================
# "I DON'T KNOW" MODE — low_confidence flag plumbing
# =====================================================================

class TestLowConfidenceFlag:
    """Verify the low_confidence flag flows from engine → route → response
    on both modes, in both empty-pool and in-band trigger conditions."""

    def test_search_threads_low_confidence_true_when_engine_signals(self, mock_engine):
        mock_engine.fetch_vault_pool.return_value = {
            "success": True,
            "pool": [],
            "sql_used": "RAG:FAISS",
            "search_id": "test-id",
            "low_confidence": True,
        }
        response = client.post("/api/search", json={"query": "obscure gibberish"})
        assert response.status_code == 200
        assert response.json()["low_confidence"] is True

    def test_search_low_confidence_false_when_engine_silent(self, mock_engine):
        """Backwards compat — engine returns no low_confidence field, route
        defaults to False (existing clients see no behavior change)."""
        mock_engine.fetch_vault_pool.return_value = {
            "success": True,
            "pool": [],
            "sql_used": "RAG:FAISS",
            "search_id": "test-id",
        }
        response = client.post("/api/search", json={"query": "test"})
        assert response.status_code == 200
        assert response.json()["low_confidence"] is False

    def test_search_low_confidence_present_on_failure_path(self, mock_engine):
        """The empty-result early-exit in fetch_vault_pool returns
        success=False with low_confidence=True. Route must propagate
        the flag even on the error path so the frontend renders the
        yellow banner instead of the red error block."""
        mock_engine.fetch_vault_pool.return_value = {
            "success": False,
            "error": "No classified intelligence matched...",
            "low_confidence": True,
        }
        response = client.post("/api/search", json={"query": "test"})
        data = response.json()
        assert response.status_code == 200
        assert data["success"] is False
        assert data["low_confidence"] is True
        assert data["error"]  # error string preserved alongside the flag

    def test_dna_threads_low_confidence_true_when_engine_signals(self, mock_engine):
        mock_engine.execute_dna_triangulation.return_value = {
            "success": False,
            "error": "No matching targets found for this specific DNA blend.",
            "title_resolution_notes": None,
            "low_confidence": True,
        }
        response = client.post("/api/dna", json={"titles": ["Cowboy Bebop"]})
        data = response.json()
        assert response.status_code == 200
        assert data["success"] is False
        assert data["low_confidence"] is True

    def test_dna_low_confidence_false_on_success(self, mock_engine):
        """Real DNA success path returning data should default
        low_confidence to False."""
        mock_engine.execute_dna_triangulation.return_value = {
            "success": True,
            "intersection_summary": "Shared themes",
            "data": [],
            "negative_anchors_used": None,
            "title_resolution_notes": None,
            "low_confidence": False,
            "diagnostics": {"sql_used": "RAG:FAISS"},
        }
        response = client.post("/api/dna", json={"titles": ["Cowboy Bebop"]})
        data = response.json()
        assert response.status_code == 200
        assert data["success"] is True
        assert data["low_confidence"] is False


# =====================================================================
# QUERY-RELEVANT CON — API plumbing
# =====================================================================

class TestQueryRelevantConPlumbing:
    """Verify the new field on ScoredResult flows from engine -> route ->
    response JSON on both modes. The schema-default behavior (absent key
    becomes null) is the load-bearing backwards-compat contract."""

    def _scored(self, title="Show A", con=None, omit_con_key=False):
        """Build a single pool item the way the engine constructs them."""
        item = {
            "ai_reasoning": "thematic match",
            "match_confidence": 92,
            "controversy_warning": None,
            "contains_spoiler": False,
            "profile": {"title": title},
        }
        if not omit_con_key:
            item["query_relevant_con"] = con
        return item

    # --- /api/search ---

    def test_search_threads_con_value(self, mock_engine):
        mock_engine.fetch_vault_pool.return_value = {
            "success": True,
            "pool": [self._scored(con="Slow first 4 episodes.")],
            "sql_used": "RAG:FAISS",
            "search_id": "test-id",
        }
        response = client.post("/api/search", json={"query": "test"})
        assert response.status_code == 200
        data = response.json()
        assert data["pool"][0]["query_relevant_con"] == "Slow first 4 episodes."

    def test_search_threads_con_null(self, mock_engine):
        mock_engine.fetch_vault_pool.return_value = {
            "success": True,
            "pool": [self._scored(con=None)],
            "sql_used": "RAG:FAISS",
            "search_id": "test-id",
        }
        response = client.post("/api/search", json={"query": "test"})
        data = response.json()
        assert data["pool"][0]["query_relevant_con"] is None

    def test_search_defaults_con_null_when_key_missing(self, mock_engine):
        """Backwards compat: legacy callers that omit the field entirely
        must still produce a valid ScoredResult with the field as null."""
        mock_engine.fetch_vault_pool.return_value = {
            "success": True,
            "pool": [self._scored(omit_con_key=True)],
            "sql_used": "RAG:FAISS",
            "search_id": "test-id",
        }
        response = client.post("/api/search", json={"query": "test"})
        assert response.status_code == 200
        data = response.json()
        # Pydantic default kicks in — key is present, value is null
        assert data["pool"][0]["query_relevant_con"] is None

    # --- /api/dna ---

    def test_dna_threads_con_value(self, mock_engine):
        mock_engine.execute_dna_triangulation.return_value = {
            "success": True,
            "intersection_summary": "shared DNA",
            "data": [self._scored(title="Black Clover",
                                  con="Generic shonen tropes and cliches.")],
            "negative_anchors_used": None,
            "title_resolution_notes": None,
            "low_confidence": False,
            "diagnostics": {"sql_used": "RAG:FAISS"},
        }
        response = client.post("/api/dna", json={"titles": ["Naruto"]})
        assert response.status_code == 200
        data = response.json()
        assert data["data"][0]["query_relevant_con"] == "Generic shonen tropes and cliches."

    def test_dna_threads_con_null(self, mock_engine):
        mock_engine.execute_dna_triangulation.return_value = {
            "success": True,
            "intersection_summary": "shared DNA",
            "data": [self._scored(title="Show A", con=None)],
            "negative_anchors_used": None,
            "title_resolution_notes": None,
            "low_confidence": False,
            "diagnostics": {"sql_used": "RAG:FAISS"},
        }
        response = client.post("/api/dna", json={"titles": ["Naruto"]})
        data = response.json()
        # DNA's `data: list` field is untyped (passes dicts through verbatim),
        # so a null value flows directly and the key is preserved.
        assert data["data"][0]["query_relevant_con"] is None

    def test_objective_rankings_field_is_present_and_null(self, mock_engine):
        """Objective Rankings bypasses the LLM reranker; the engine
        explicitly sets query_relevant_con=None per item. The API
        contract should reflect that — the field should be present in
        the JSON response, not absent."""
        mock_engine.fetch_vault_pool.return_value = {
            "success": True,
            "pool": [self._scored(con=None)],
            "sql_used": "RAG:OBJECTIVE",
            "search_id": "test-id",
        }
        response = client.post("/api/search", json={
            "query": "top anime", "lens": "Objective Rankings",
        })
        data = response.json()
        assert "query_relevant_con" in data["pool"][0]
        assert data["pool"][0]["query_relevant_con"] is None


class TestSeriesContextPlumbing:
    """Verify the SeriesContext field on ScoredResult flows from
    engine -> route -> response JSON. The engine hydrates this
    server-side via _hydrate_series_context; the route doesn't
    transform it. Absent-key case must default to None
    (backwards-compat with code paths that don't hydrate)."""

    def _scored(self, title="Show A", series_context=None, omit_key=False):
        item = {
            "ai_reasoning": "thematic match",
            "match_confidence": 92,
            "controversy_warning": None,
            "contains_spoiler": False,
            "query_relevant_con": None,
            "profile": {"title": title, "id": 1, "series_id": "1"},
        }
        if not omit_key:
            item["series_context"] = series_context
        return item

    def test_search_threads_series_context_when_present(self, mock_engine):
        ctx = {
            "series_id": "16498",
            "series_size": 4,
            "position": 1,
            "score_min": 8.5,
            "score_max": 9.1,
            "sentiment_min": 0.4,
            "sentiment_max": 0.8,
            "trajectory": "stable",
        }
        mock_engine.fetch_vault_pool.return_value = {
            "success": True,
            "pool": [self._scored(series_context=ctx)],
            "sql_used": "RAG:FAISS",
            "search_id": "test-id",
        }
        response = client.post("/api/search", json={"query": "test"})
        assert response.status_code == 200
        data = response.json()
        out = data["pool"][0]["series_context"]
        assert out["series_id"] == "16498"
        assert out["series_size"] == 4
        assert out["position"] == 1
        assert out["trajectory"] == "stable"

    def test_search_series_context_null_when_engine_returns_none(self, mock_engine):
        mock_engine.fetch_vault_pool.return_value = {
            "success": True,
            "pool": [self._scored(series_context=None)],
            "sql_used": "RAG:FAISS",
            "search_id": "test-id",
        }
        response = client.post("/api/search", json={"query": "test"})
        data = response.json()
        assert data["pool"][0]["series_context"] is None

    def test_search_series_context_defaults_to_null_when_key_omitted(self, mock_engine):
        """Legacy callers that don't hydrate (or in-progress code paths)
        omit the key entirely. The Pydantic default kicks in → null."""
        mock_engine.fetch_vault_pool.return_value = {
            "success": True,
            "pool": [self._scored(omit_key=True)],
            "sql_used": "RAG:FAISS",
            "search_id": "test-id",
        }
        response = client.post("/api/search", json={"query": "test"})
        assert response.status_code == 200
        data = response.json()
        assert data["pool"][0]["series_context"] is None


# =====================================================================
# SEARCH ENDPOINT — INTEGRATION (requires API key)
# =====================================================================

@skip_no_key
class TestSearchIntegration:
    """Full pipeline tests that hit the Gemini API."""

    @api
    def test_intelligent_search_returns_results(self):
        response = client.post("/api/search", json={
            "query": "dark psychological thriller",
        })
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["pool_size"] > 0
        # Verify confidence floor
        for item in data["pool"]:
            assert item["match_confidence"] >= 50

    @api
    def test_search_with_era_filter(self):
        response = client.post("/api/search", json={
            "query": "romance anime",
            "year_min": 2020,
            "era_label": "Recent (2020+)",
        })
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    @api
    def test_same_series_collapse_dedupes_visible_results(self):
        """When same-series collapse is enabled (default), no two visible
        results in the pool share a series_id. Uses a query likely to
        surface multi-season franchises so the collapse pass has actual
        work to do. Singletons (series_context=None) can't conflict with
        each other since each is its own cluster."""
        response = client.post("/api/search", json={
            "query": "shounen action with great battles",
        })
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["pool_size"] > 0

        series_ids_seen = []
        for item in data["pool"]:
            ctx = item.get("series_context")
            if ctx is None:
                continue
            series_ids_seen.append(ctx["series_id"])

        # No duplicates across the multi-entry clusters in the pool.
        assert len(series_ids_seen) == len(set(series_ids_seen)), (
            f"Found duplicate series_ids in pool: {series_ids_seen}. "
            f"Collapse should have deduped these."
        )


# =====================================================================
# DNA ENDPOINT — VALIDATION (no API key needed)
# =====================================================================

class TestDNAValidation:
    """Verify DNA request validation returns 422 for bad inputs."""

    def test_empty_titles_returns_422(self):
        response = client.post("/api/dna", json={"titles": []})
        assert response.status_code == 422

    def test_missing_titles_returns_422(self):
        response = client.post("/api/dna", json={})
        assert response.status_code == 422

    def test_too_many_titles_returns_422(self):
        response = client.post("/api/dna", json={
            "titles": ["Show A", "Show B", "Show C", "Show D"]
        })
        assert response.status_code == 422

    def test_whitespace_only_titles_returns_422(self):
        response = client.post("/api/dna", json={"titles": ["   ", "  "]})
        assert response.status_code == 422

    def test_oversized_title_returns_422(self):
        response = client.post("/api/dna", json={"titles": ["a" * 101]})
        assert response.status_code == 422

    def test_unknown_title_returns_error(self):
        """Valid request shape but show doesn't exist — engine returns error, not 422."""
        response = client.post("/api/dna", json={
            "titles": ["ZZZZQQQQ XXYYZZ 99999"]
        })
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "Target MIA" in data["error"]

    def test_response_shape(self):
        """Verify response has documented fields even on error."""
        response = client.post("/api/dna", json={
            "titles": ["ZZZZQQQQ XXYYZZ 99999"]
        })
        data = response.json()
        assert "success" in data
        assert "data" in data
        assert "error" in data

    # --- negative_anchors validation ---

    def test_negative_anchors_field_is_optional(self):
        """A DNA request without negative_anchors should validate just like the legacy shape."""
        response = client.post("/api/dna", json={
            "titles": ["ZZZZQQQQ XXYYZZ 99999"]
        })
        # 200 with engine-level error, not 422 from schema
        assert response.status_code == 200

    def test_negative_anchors_too_many_returns_422(self):
        response = client.post("/api/dna", json={
            "titles": ["Show A"],
            "negative_anchors": ["Neg A", "Neg B", "Neg C"],
        })
        assert response.status_code == 422

    def test_oversized_negative_anchor_returns_422(self):
        response = client.post("/api/dna", json={
            "titles": ["Show A"],
            "negative_anchors": ["a" * 101],
        })
        assert response.status_code == 422

    def test_negative_anchors_accepts_none(self):
        """Explicit null for negative_anchors should validate."""
        response = client.post("/api/dna", json={
            "titles": ["ZZZZQQQQ XXYYZZ 99999"],
            "negative_anchors": None,
        })
        assert response.status_code == 200

    def test_whitespace_only_negative_anchors_accepted(self):
        """Whitespace-only negative anchors are silently dropped — request validates."""
        response = client.post("/api/dna", json={
            "titles": ["ZZZZQQQQ XXYYZZ 99999"],
            "negative_anchors": ["  ", ""],
        })
        assert response.status_code == 200


# =====================================================================
# DNA ENDPOINT — TITLE RESOLUTION NOTES (no API key needed)
# =====================================================================

class TestDNAResolutionNotes:
    """Verify title_resolution_notes surface for substitution, skipped, and
    collision cases. These tests target the no_centroid_results failure path
    so they don't need a Gemini API key — resolution runs before any LLM
    call, so failure-path responses still carry the notes."""

    def test_substitution_note_when_positive_resolves_to_different_title(self):
        """User input that gets fuzzy-matched to a different vault title produces a substitution note."""
        with patch('analysis.queries.resolve_show_title', return_value=["Different Title"]):
            response = client.post("/api/dna", json={"titles": ["weird input"]})
            assert response.status_code == 200
            data = response.json()
            notes = data.get("title_resolution_notes") or []
            assert any(
                "weird input" in n and "Different Title" in n and "isn't in the vault" in n
                for n in notes
            ), f"Expected substitution note in {notes}"

    def test_no_substitution_note_when_input_matches_resolved_exactly(self):
        """When input equals resolved title (exact match), no substitution note is emitted."""
        with patch('analysis.queries.resolve_show_title', return_value=["Exact Match Title"]):
            response = client.post("/api/dna", json={"titles": ["Exact Match Title"]})
            data = response.json()
            notes = data.get("title_resolution_notes") or []
            assert not any("isn't in the vault" in n for n in notes), (
                f"Should not emit substitution note when input matches resolved exactly. Got: {notes}"
            )

    def test_no_substitution_note_on_case_only_difference(self):
        """Case-only differences ('attack on titan' vs 'Attack on Titan') should not produce a note."""
        with patch('analysis.queries.resolve_show_title', return_value=["Attack on Titan"]):
            response = client.post("/api/dna", json={"titles": ["attack on titan"]})
            data = response.json()
            notes = data.get("title_resolution_notes") or []
            assert not any("isn't in the vault" in n for n in notes), (
                f"Case-only differences should be ignored. Got: {notes}"
            )

    def test_no_substitution_note_on_hyphen_vs_space_difference(self):
        """User types 'my dress up darling' (spaces); vault has 'My Dress-Up Darling'
        (hyphenated). Same show — surfacing this as a substitution reads as the
        system being pedantic about formatting the user can't see."""
        with patch('analysis.queries.resolve_show_title', return_value=["My Dress-Up Darling"]):
            response = client.post("/api/dna", json={"titles": ["my dress up darling"]})
            data = response.json()
            notes = data.get("title_resolution_notes") or []
            assert not any("isn't in the vault" in n for n in notes), (
                f"Hyphen-vs-space differences should be silenced. Got: {notes}"
            )

    def test_no_substitution_note_on_punctuation_difference(self):
        """User omits colons / commas / exclamation marks in input;
        vault has full punctuation. Same show, silent."""
        with patch('analysis.queries.resolve_show_title', return_value=["Kaguya-sama: Love is War"]):
            response = client.post("/api/dna", json={"titles": ["kaguya sama love is war"]})
            data = response.json()
            notes = data.get("title_resolution_notes") or []
            assert not any("isn't in the vault" in n for n in notes), (
                f"Punctuation-only differences should be silenced. Got: {notes}"
            )

    def test_no_substitution_note_on_collapsed_whitespace(self):
        """Doubled whitespace in user input should not register as different."""
        with patch('analysis.queries.resolve_show_title', return_value=["Attack on Titan"]):
            response = client.post("/api/dna", json={"titles": ["Attack  on   Titan"]})
            data = response.json()
            notes = data.get("title_resolution_notes") or []
            assert not any("isn't in the vault" in n for n in notes), (
                f"Collapsed-whitespace differences should be silenced. Got: {notes}"
            )

    def test_substitution_note_still_fires_for_token_level_difference(self):
        """The normalization fix must NOT silence meaningful differences —
        when the resolved title contains different *words*, the note still fires."""
        # "my first girlfriend is a gal" vs "Domestic Girlfriend" — different words,
        # different show, banner should fire.
        with patch('analysis.queries.resolve_show_title', return_value=["Domestic Girlfriend"]):
            response = client.post("/api/dna", json={"titles": ["my first girlfriend is a gal"]})
            data = response.json()
            notes = data.get("title_resolution_notes") or []
            assert any(
                "my first girlfriend is a gal" in n and "Domestic Girlfriend" in n
                for n in notes
            ), f"Token-level differences must still surface a note. Got: {notes}"

    def test_skipped_note_for_unresolvable_negative(self):
        """A negative anchor that fails to resolve produces a 'Skipped from avoid list' note."""

        def fake_resolve(t):
            if t == "Attack on Titan":
                return ["Attack on Titan"]
            return []  # unresolvable

        with patch('analysis.queries.resolve_show_title', side_effect=fake_resolve):
            response = client.post("/api/dna", json={
                "titles": ["Attack on Titan"],
                "negative_anchors": ["nonexistent garbage title"],
            })
            data = response.json()
            notes = data.get("title_resolution_notes") or []
            assert any(
                "nonexistent garbage title" in n and "Skipped from avoid" in n
                for n in notes
            ), f"Expected skipped note in {notes}"

    def test_collision_note_when_negative_resolves_to_positive(self):
        """A negative that fuzzy-matches the same title as a positive produces a 'Removed from avoid list' note."""
        # Both inputs resolve to the same vault title — collision.
        with patch('analysis.queries.resolve_show_title', return_value=["Attack on Titan"]):
            response = client.post("/api/dna", json={
                "titles": ["AoT"],
                "negative_anchors": ["Attack on Titan"],
            })
            data = response.json()
            notes = data.get("title_resolution_notes") or []
            assert any(
                "Attack on Titan" in n and "Removed from avoid" in n
                for n in notes
            ), f"Expected collision note in {notes}"

    def test_resolution_notes_present_on_error_response_too(self):
        """When the engine fails after resolution (e.g., no centroid results),
        notes are still threaded through so the user can see *why* the failure
        happened — e.g., a substitute they didn't intend produced no matches."""
        with patch('analysis.queries.resolve_show_title', return_value=["Bogus Title Not In Vault"]):
            response = client.post("/api/dna", json={"titles": ["something the user typed"]})
            data = response.json()
            # Should be a failure response (the substitute isn't in FAISS either)
            assert data.get("success") is False
            # But the notes should still be present
            notes = data.get("title_resolution_notes") or []
            assert any("something the user typed" in n for n in notes), (
                f"Notes must be present on error responses. Got: {notes}"
            )


# =====================================================================
# DNA ENDPOINT — INTEGRATION (requires API key)
# =====================================================================

@skip_no_key
class TestDNAIntegration:
    """Full DNA pipeline tests that hit the Gemini API."""

    @api
    def test_single_show_dna(self):
        response = client.post("/api/dna", json={
            "titles": ["Steins;Gate"]
        })
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["intersection_summary"] is not None
        assert data["data_size"] > 0
        # Verify confidence floor (40% for DNA)
        for item in data["data"]:
            assert item["match_confidence"] >= 40

    @api
    def test_multi_show_dna(self):
        response = client.post("/api/dna", json={
            "titles": ["Attack on Titan", "Death Note"]
        })
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data_size"] > 0
        # Verify no franchise leaks
        titles = [item["profile"]["title"] for item in data["data"]]
        assert "Attack on Titan" not in titles
        assert "Death Note" not in titles


# =====================================================================
# ENCYCLOPEDIA ENDPOINT
# =====================================================================

class TestEncyclopedia:
    """Verify encyclopedia show lookup."""

    def test_exact_match_returns_profile(self):
        response = client.get("/api/show/Attack on Titan")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["profile"] is not None
        assert data["profile"]["title"] == "Attack on Titan"
        assert data["profile"]["studio"] != ""

    def test_profile_has_required_fields(self):
        response = client.get("/api/show/Death Note")
        data = response.json()
        profile = data["profile"]
        for field in ["title", "studio", "quality_score", "scored_by",
                      "release_year", "synopsis", "audience_consensus",
                      "pros", "cons", "controversy_score", "mal_rating"]:
            assert field in profile, f"Missing field: {field}"

    def test_profile_includes_mal_rating(self):
        """Encyclopedia endpoint serializes mal_rating verbatim from DB."""
        response = client.get("/api/show/Attack on Titan")
        assert response.status_code == 200
        data = response.json()
        assert data["profile"]["mal_rating"] == "R - 17+ (violence & profanity)"

    def test_synopsis_strips_mal_attribution(self):
        """AoT's raw mal_synopsis ends with "[Written by MAL Rewrite]". The
        route must strip it before serializing — the tag is display debris
        and the global footer attribution covers the data-source story."""
        response = client.get("/api/show/Attack on Titan")
        synopsis = response.json()["profile"]["synopsis"]
        assert synopsis, "Expected a non-empty synopsis for AoT"
        assert "[Written by MAL Rewrite]" not in synopsis
        assert "(Source:" not in synopsis
        assert not synopsis.rstrip().endswith("]")

    def test_synopsis_body_preserved(self):
        """The strip is end-anchored and keyword-whitelisted. The body of
        the synopsis — characters, plot — must come through unchanged."""
        response = client.get("/api/show/Attack on Titan")
        synopsis = response.json()["profile"]["synopsis"]
        # AoT's synopsis contains "Titans" — a core canonical term that
        # could not plausibly land inside a trailing attribution tag.
        assert "Titan" in synopsis

    def test_fuzzy_match_returns_candidates(self):
        """Partial/typo match should return multiple candidates."""
        response = client.get("/api/show/Stiens Gate")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert len(data["matches"]) >= 1

    def test_no_match_returns_error(self):
        response = client.get("/api/show/ZZZZQQQQ XXYYZZ 99999")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert data["error"] is not None

    def test_oversized_title_returns_error(self):
        response = client.get(f"/api/show/{'a' * 101}")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False

    def test_response_shape(self):
        response = client.get("/api/show/Naruto")
        data = response.json()
        assert "success" in data
        assert "matches" in data
        assert "profile" in data or "error" in data


# =====================================================================
# RATE LIMITING
# =====================================================================

class TestRateLimiting:
    """Verify rate limiting middleware triggers correctly."""

    def _make_fresh_client(self, burst_limit=3, sustained_limit=5):
        """Create a test app with low limits for fast testing."""
        from fastapi import FastAPI
        from api.middleware.rate_limit import RateLimitMiddleware

        test_app = FastAPI()
        test_app.add_middleware(
            RateLimitMiddleware,
            burst_limit=burst_limit,
            sustained_limit=sustained_limit,
        )

        @test_app.get("/test")
        def test_endpoint():
            return {"ok": True}

        @test_app.get("/api/health")
        def health():
            return {"ok": True}

        return TestClient(test_app)

    def test_burst_protection_triggers_429(self):
        """Rapid-fire requests beyond burst limit should return 429."""
        tc = self._make_fresh_client(burst_limit=3, sustained_limit=100)
        responses = [tc.get("/test") for _ in range(5)]
        status_codes = [r.status_code for r in responses]
        assert 429 in status_codes, f"Expected 429 in burst test, got {status_codes}"

    def test_sustained_rate_triggers_429(self):
        """Exceeding sustained limit should return 429."""
        tc = self._make_fresh_client(burst_limit=100, sustained_limit=5)
        responses = []
        for _ in range(8):
            responses.append(tc.get("/test"))
        status_codes = [r.status_code for r in responses]
        assert 429 in status_codes, f"Expected 429 in sustained test, got {status_codes}"

    def test_429_includes_retry_after(self):
        """429 responses must include Retry-After header."""
        tc = self._make_fresh_client(burst_limit=2, sustained_limit=100)
        for _ in range(5):
            resp = tc.get("/test")
        # Find the 429 response
        tc2 = self._make_fresh_client(burst_limit=2, sustained_limit=100)
        responses = [tc2.get("/test") for _ in range(5)]
        blocked = [r for r in responses if r.status_code == 429]
        if blocked:
            assert "retry-after" in blocked[0].headers

    def test_health_exempt_from_rate_limit(self):
        """Health endpoint should never be rate limited."""
        tc = self._make_fresh_client(burst_limit=2, sustained_limit=3)
        # Exhaust the limit on /test
        for _ in range(5):
            tc.get("/test")
        # Health should still work
        resp = tc.get("/api/health")
        assert resp.status_code == 200


# =====================================================================
# API SECURITY SWEEP
# =====================================================================

class TestAPISecurity:
    """Verify API-layer security properties."""

    def test_error_responses_no_stack_traces(self):
        """Error responses should return clean JSON, never stack traces."""
        # 422 from bad input
        resp = client.post("/api/search", json={"query": ""})
        assert resp.status_code == 422
        data = resp.json()
        assert "detail" in data
        # Verify no Python traceback leaked
        text = str(data)
        assert "Traceback" not in text
        assert "File \"" not in text

    def test_nonexistent_endpoint_returns_404(self):
        """Unknown routes should return 404, not crash."""
        resp = client.get("/api/nonexistent")
        assert resp.status_code == 404

    def test_wrong_method_returns_405(self):
        """GET on a POST endpoint should return 405."""
        resp = client.get("/api/search")
        assert resp.status_code == 405

    def test_search_null_bytes_stripped(self):
        """Null bytes in query should be sanitized, not crash."""
        resp = client.post("/api/search", json={
            "query": "test\x00injection",
            "lens": "Objective Rankings"
        })
        assert resp.status_code == 200

    def test_dna_control_chars_handled(self):
        """Control characters in titles should not crash the API."""
        resp = client.post("/api/dna", json={
            "titles": ["Attack\x00on\x01Titan"]
        })
        # Should either resolve the title or return a clean error
        assert resp.status_code == 200

    def test_encyclopedia_encoded_path(self):
        """URL-encoded characters in path should be handled."""
        resp = client.get("/api/show/Attack%20on%20Titan")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    def test_cors_headers_on_options(self):
        """OPTIONS preflight should return CORS headers for allowed origins."""
        resp = client.options("/api/search", headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        })
        # FastAPI CORS middleware should respond
        assert resp.status_code == 200

    def test_search_response_consistent_on_error(self):
        """All error responses from search should have the same shape."""
        # Invalid lens → 422
        resp1 = client.post("/api/search", json={"query": "test", "lens": "Bad"})
        assert "detail" in resp1.json()

        # Valid but no results → 200 with success=false
        resp2 = client.post("/api/search", json={
            "query": "top anime",
            "lens": "Objective Rankings"
        })
        data2 = resp2.json()
        assert "success" in data2
        assert "pool" in data2

    def test_dna_response_consistent_on_error(self):
        """DNA error responses should have the documented shape."""
        resp = client.post("/api/dna", json={
            "titles": ["ZZZZQQQQ XXYYZZ 99999"]
        })
        data = resp.json()
        assert "success" in data
        assert "error" in data
        assert "data" in data


# =====================================================================
# SUPABASE CLIENT
# =====================================================================

@skip_no_supabase
class TestSupabaseClient:
    """Verify Supabase REST API client connectivity.

    Targets `search_feedback` (a still-active table post-PR-#60/Phase-C cleanup).
    The earlier versions of these tests targeted `profiles` and `watchlists`,
    both of which were dropped when the account-features schema was archived.
    """

    def test_read_returns_list(self):
        from api.supabase_client import supabase_get
        rows = supabase_get("search_feedback")
        assert isinstance(rows, list)

    def test_count_returns_int(self):
        from api.supabase_client import supabase_count
        count = supabase_count("search_feedback")
        assert isinstance(count, int)
        assert count >= 0

    def test_filtered_read_returns_empty(self):
        from api.supabase_client import supabase_get
        # rating only accepts 'up' or 'down' (CHECK constraint); a synthetic
        # value matches no rows and exercises the filter syntax.
        items = supabase_get("search_feedback", {
            "rating": "eq.__nonexistent__"
        })
        assert items == []


# =====================================================================
# SEARCH FEEDBACK
# =====================================================================

class TestSearchFeedbackValidation:
    """Validate search feedback endpoint input handling."""

    @patch("api.routes.feedback.supabase_get", return_value=[])
    @patch("api.routes.feedback.supabase_post", return_value=[{}])
    @patch("api.routes.feedback.telemetry")
    def test_valid_feedback_accepted(self, mock_telemetry, mock_post, mock_get):
        resp = client.post("/api/search-feedback", json={
            "search_id": "abc-123-def",
            "rating": "up",
        })
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        mock_post.assert_called_once()

    @patch("api.routes.feedback.supabase_get", return_value=[])
    @patch("api.routes.feedback.supabase_post", return_value=[{}])
    @patch("api.routes.feedback.telemetry")
    def test_thumbs_down_accepted(self, mock_telemetry, mock_post, mock_get):
        resp = client.post("/api/search-feedback", json={
            "search_id": "abc-123",
            "rating": "down",
        })
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    @patch("api.routes.feedback.supabase_get", return_value=[{"id": 1}])
    @patch("api.routes.feedback.supabase_post")
    @patch("api.routes.feedback.telemetry")
    def test_duplicate_feedback_rejected_with_409(self, mock_telemetry, mock_post, mock_get):
        """Second submission for the same search_id should 409 — prevents repeat-voting."""
        resp = client.post("/api/search-feedback", json={
            "search_id": "already-rated-id",
            "rating": "up",
        })
        assert resp.status_code == 409
        mock_post.assert_not_called()

    def test_invalid_rating_rejected(self):
        resp = client.post("/api/search-feedback", json={
            "search_id": "abc-123",
            "rating": "maybe",
        })
        assert resp.status_code == 422

    def test_missing_search_id_rejected(self):
        resp = client.post("/api/search-feedback", json={
            "rating": "up",
        })
        assert resp.status_code == 422

    def test_empty_search_id_rejected(self):
        resp = client.post("/api/search-feedback", json={
            "search_id": "",
            "rating": "up",
        })
        assert resp.status_code == 422

    def test_malicious_search_id_rejected(self):
        resp = client.post("/api/search-feedback", json={
            "search_id": "abc'; DROP TABLE--",
            "rating": "up",
        })
        assert resp.status_code == 422

    @patch("api.routes.feedback.supabase_get", return_value=[])
    @patch("api.routes.feedback.supabase_post", return_value=[{}])
    @patch("api.routes.feedback.telemetry")
    def test_feedback_logs_to_journal(self, mock_telemetry, mock_post, mock_get):
        resp = client.post("/api/search-feedback", json={
            "search_id": "test-search-id",
            "rating": "up",
        })
        assert resp.status_code == 200
        mock_telemetry.log_search_journal.assert_called_once()
        journal_entry = mock_telemetry.log_search_journal.call_args[0][0]
        assert journal_entry["mode"] == "feedback"
        assert journal_entry["search_id"] == "test-search-id"
        assert journal_entry["rating"] == "up"


# =====================================================================
# ADMIN ENDPOINTS (Basic Auth)
# =====================================================================


class TestAdminDashboard:
    """Verify the admin dashboard returns aggregate metrics with valid Basic Auth."""

    @patch("api.routes.admin.supabase_rpc", return_value=[])
    @patch("api.routes.admin.supabase_count", return_value=42)
    @patch("api.routes.admin.supabase_get", return_value=[])
    def test_dashboard_returns_200(self, mock_get, mock_count, mock_rpc):
        resp = client.get("/api/admin/dashboard", headers=_ADMIN_AUTH_HEADER)
        assert resp.status_code == 200

    @patch("api.routes.admin.supabase_rpc", return_value=[])
    @patch("api.routes.admin.supabase_count", return_value=10)
    @patch("api.routes.admin.supabase_get", return_value=[])
    def test_dashboard_contains_required_fields(self, mock_get, mock_count, mock_rpc):
        resp = client.get("/api/admin/dashboard", headers=_ADMIN_AUTH_HEADER)
        data = resp.json()
        assert "feedback_summary" in data
        assert "contact_messages" in data
        assert "anon_traffic" in data
        assert "lockdown_active" in data
        assert "api_costs" in data
        assert "pipeline_timings" in data

    @patch("api.routes.admin.supabase_rpc", return_value=[])
    @patch("api.routes.admin.supabase_count", return_value=1)
    @patch("api.routes.admin.supabase_get", return_value=[])
    def test_dashboard_pipeline_timings_shape(self, mock_get, mock_count, mock_rpc):
        """The pipeline_timings field carries the percentile schema even when empty."""
        resp = client.get("/api/admin/dashboard", headers=_ADMIN_AUTH_HEADER)
        timings = resp.json()["pipeline_timings"]
        assert "window_hours" in timings
        assert "profiles" in timings
        assert "has_data" in timings
        assert "error" in timings
        assert isinstance(timings["profiles"], list)
        assert isinstance(timings["has_data"], bool)
        assert timings["has_data"] is False

    @patch("api.routes.admin.supabase_rpc", side_effect=RuntimeError("connection refused"))
    @patch("api.routes.admin.supabase_count", return_value=1)
    @patch("api.routes.admin.supabase_get", return_value=[])
    def test_pipeline_timings_fail_soft_on_rpc_error(self, mock_get, mock_count, mock_rpc):
        """An RPC failure populates `error` instead of returning a 500."""
        resp = client.get("/api/admin/dashboard", headers=_ADMIN_AUTH_HEADER)
        assert resp.status_code == 200
        timings = resp.json()["pipeline_timings"]
        assert timings["has_data"] is False
        assert timings["profiles"] == []
        assert timings["error"] is not None
        assert "RuntimeError" in timings["error"]

    @patch("api.routes.admin.supabase_rpc")
    @patch("api.routes.admin.supabase_count", return_value=1)
    @patch("api.routes.admin.supabase_get", return_value=[])
    def test_pipeline_timings_with_data(self, mock_get, mock_count, mock_rpc):
        """When the RPC returns rows, profiles flow through to the response."""
        mock_rpc.return_value = [{
            "rerank_type": "fusion",
            "sample_count": 10,
            "retrieval_p50": 40.0, "retrieval_p95": 80.0, "retrieval_p99": 100.0,
            "hydration_p50": 12.0, "hydration_p95": 20.0, "hydration_p99": 25.0,
            "rerank_p50": 1800.0, "rerank_p95": 2800.0, "rerank_p99": 3500.0,
            "total_p50": 1850.0, "total_p95": 2900.0, "total_p99": 3625.0,
        }]
        resp = client.get("/api/admin/dashboard", headers=_ADMIN_AUTH_HEADER)
        timings = resp.json()["pipeline_timings"]
        assert timings["has_data"] is True
        assert len(timings["profiles"]) == 1
        prof = timings["profiles"][0]
        assert prof["rerank_type"] == "fusion"
        assert prof["sample_count"] == 10
        assert prof["retrieval"]["p50_ms"] == 40.0
        assert prof["rerank"]["p95_ms"] == 2800.0
        assert prof["total"]["p99_ms"] == 3625.0
        # Verify the RPC was called with the correct window
        mock_rpc.assert_called_once_with("get_pipeline_timings", {"p_window_hours": 24})

    @patch("api.routes.admin.supabase_rpc", return_value=[])
    @patch("api.routes.admin.supabase_count")
    @patch("api.routes.admin.supabase_get", return_value=[])
    def test_dashboard_feedback_summary(self, mock_get, mock_count, mock_rpc):
        # supabase_count call order: thumbs_up, thumbs_down, contact_messages
        mock_count.side_effect = [30, 5, 3]
        resp = client.get("/api/admin/dashboard", headers=_ADMIN_AUTH_HEADER)
        data = resp.json()
        assert data["feedback_summary"]["thumbs_up"] == 30
        assert data["feedback_summary"]["thumbs_down"] == 5
        assert data["contact_messages"] == 3

    @patch("api.routes.admin.supabase_rpc", return_value=[])
    @patch("api.routes.admin.supabase_count", return_value=1)
    @patch("api.routes.admin.supabase_get", return_value=[])
    def test_dashboard_contains_api_costs(self, mock_get, mock_count, mock_rpc):
        resp = client.get("/api/admin/dashboard", headers=_ADMIN_AUTH_HEADER)
        data = resp.json()
        costs = data["api_costs"]
        assert "total_searches_today" in costs
        assert "total_dna_today" in costs
        assert "gemini_cost_today_usd" in costs
        # Real cumulative Gemini spend MTD from the gemini_calls ledger,
        # plus the Gemini-only ceiling ($35 = $50 total - $15 Railway
        # fixed) that drives the headline tile's color coding.
        assert "gemini_mtd_usd" in costs
        assert "gemini_ceiling_usd" in costs
        # Production-only projection — deliberately scoped to just anon
        # search/DNA traffic, not dev/regression/probe origins.
        assert "production_projection_monthly_usd" in costs
        assert "railway_monthly_usd" in costs
        assert "total_projected_monthly_usd" in costs
        assert "budget_ceiling_usd" in costs
        # Cost-attribution fields from the gemini_calls ledger.
        assert "last_7d_total_usd" in costs
        assert "last_7d_by_origin" in costs

    @patch("api.routes.admin.supabase_rpc", return_value=[])
    @patch("api.routes.admin.supabase_count", return_value=1)
    @patch("api.routes.admin.supabase_get")
    def test_dashboard_cost_calculation(self, mock_get, mock_count, mock_rpc):
        """Verify the production-projection heuristic math using the
        post-2026-10-02 recalibrated constants.

        Per-action costs (back-calculated from the real gemini_calls
        ledger — see constant definitions in admin.py):
            SEARCH:  $0.0063 per action
            DNA:     $0.0113 per action

        20 searches × $0.0063 + 5 DNA × $0.0113 = $0.1825 today;
        projected $5.47/mo + $15 Railway = $20.48 total."""
        # supabase_get call order: anon_usage today, anon_usage 7d,
        # anon_usage 30d, system_flags, gemini_calls 7d window,
        # gemini_calls MTD.
        mock_get.side_effect = [
            [{"search_count": 20, "dna_count": 5}],
            [],  # 7d distinct-IP query
            [],  # 30d distinct-IP query
            [],  # system_flags
            [],  # gemini_calls 7d attribution query
            [],  # gemini_calls MTD query
        ]
        resp = client.get("/api/admin/dashboard", headers=_ADMIN_AUTH_HEADER)
        data = resp.json()
        costs = data["api_costs"]
        assert costs["total_searches_today"] == 20
        assert costs["total_dna_today"] == 5
        assert costs["gemini_cost_today_usd"] == 0.1825
        assert costs["production_projection_monthly_usd"] == 5.47
        assert costs["total_projected_monthly_usd"] == 20.48
        # Empty MTD slot → real Gemini MTD is zero in this scenario.
        assert costs["gemini_mtd_usd"] == 0.0

    @patch("api.routes.admin.supabase_rpc", return_value=[])
    @patch("api.routes.admin.supabase_count", return_value=1)
    @patch("api.routes.admin.supabase_get", return_value=[])
    def test_dashboard_zero_traffic_costs(self, mock_get, mock_count, mock_rpc):
        """Zero traffic should show only Railway fixed cost and zero MTD."""
        resp = client.get("/api/admin/dashboard", headers=_ADMIN_AUTH_HEADER)
        costs = resp.json()["api_costs"]
        assert costs["gemini_cost_today_usd"] == 0.0
        assert costs["total_projected_monthly_usd"] == 15.0
        # Empty gemini_calls ledger → MTD is zero.
        assert costs["gemini_mtd_usd"] == 0.0

    @patch("api.routes.admin.supabase_rpc", return_value=[])
    @patch("api.routes.admin.supabase_count", return_value=1)
    @patch("api.routes.admin.supabase_get")
    def test_dashboard_anon_unique_ips_7d_30d(self, mock_get, mock_count, mock_rpc):
        """7d and 30d unique-IP counts are distinct ip_hash sets over the
        respective windows. The composite-PK schema preserves prior-day
        rows so the aggregate over multiple days actually means something.
        Duplicate ip_hashes within a window collapse via set semantics."""
        mock_get.side_effect = [
            # anon_usage today
            [{"search_count": 5, "dna_count": 1}],
            # anon_usage 7d — 3 distinct IPs across 4 (ip_a appears twice
            # on different days, proving the prior-day rows are visible)
            [
                {"ip_hash": "ip_a"},
                {"ip_hash": "ip_b"},
                {"ip_hash": "ip_a"},  # same IP, different day
                {"ip_hash": "ip_c"},
            ],
            # anon_usage 30d — 5 distinct IPs
            [
                {"ip_hash": "ip_a"},
                {"ip_hash": "ip_b"},
                {"ip_hash": "ip_c"},
                {"ip_hash": "ip_d"},
                {"ip_hash": "ip_e"},
            ],
            # system_flags
            [],
            # gemini_calls 7d attribution
            [],
            # gemini_calls MTD
            [],
        ]
        resp = client.get("/api/admin/dashboard", headers=_ADMIN_AUTH_HEADER)
        anon = resp.json()["anon_traffic"]
        assert anon["anon_unique_ips_7d"] == 3
        assert anon["anon_unique_ips_30d"] == 5

    @patch("api.routes.admin.supabase_rpc", return_value=[])
    @patch("api.routes.admin.supabase_count", return_value=1)
    @patch("api.routes.admin.supabase_get")
    def test_dashboard_anon_window_queries_use_correct_date_filters(
        self, mock_get, mock_count, mock_rpc
    ):
        """The 7d / 30d queries must use gte.<date> with the right offsets
        (6 days back and 29 days back, inclusive of today). Regression
        guard against off-by-one errors in the date math."""
        from datetime import datetime, timedelta, timezone
        mock_get.return_value = []

        client.get("/api/admin/dashboard", headers=_ADMIN_AUTH_HEADER)

        today = datetime.now(timezone.utc).date()
        expected_7d = (today - timedelta(days=6)).isoformat()
        expected_30d = (today - timedelta(days=29)).isoformat()

        # mock_get.call_args_list: [today_query, 7d_query, 30d_query,
        # system_flags, gemini_calls_7d, gemini_calls_mtd]
        seven_d_params = mock_get.call_args_list[1][0][1]
        thirty_d_params = mock_get.call_args_list[2][0][1]
        assert seven_d_params["usage_date"] == f"gte.{expected_7d}"
        assert thirty_d_params["usage_date"] == f"gte.{expected_30d}"
        assert seven_d_params["select"] == "ip_hash"
        assert thirty_d_params["select"] == "ip_hash"

    @patch("api.routes.admin.supabase_rpc", return_value=[])
    @patch("api.routes.admin.supabase_count", return_value=1)
    @patch("api.routes.admin.supabase_get")
    def test_dashboard_cost_attribution_by_origin(
        self, mock_get, mock_count, mock_rpc
    ):
        """gemini_calls rows aggregate by origin into last_7d_by_origin.
        Total = sum across all origins. Mixed origins in the same window
        should produce per-origin sub-totals plus a clean total."""
        mock_get.side_effect = [
            [],  # anon_usage today
            [],  # anon_usage 7d
            [],  # anon_usage 30d
            [],  # system_flags
            # gemini_calls — three origins, six rows
            [
                {"origin": "production", "cost_usd_estimate": 0.028},
                {"origin": "production", "cost_usd_estimate": 0.028},
                {"origin": "regression", "cost_usd_estimate": 1.50},
                {"origin": "regression", "cost_usd_estimate": 0.75},
                {"origin": "probe", "cost_usd_estimate": 0.0001},
                {"origin": "dev", "cost_usd_estimate": 0.10},
            ],
            # gemini_calls MTD — not exercised by this assertion
            [],
        ]
        resp = client.get("/api/admin/dashboard", headers=_ADMIN_AUTH_HEADER)
        costs = resp.json()["api_costs"]
        by_origin = costs["last_7d_by_origin"]
        assert by_origin["production"] == pytest.approx(0.056, abs=1e-4)
        assert by_origin["regression"] == pytest.approx(2.25, abs=1e-4)
        assert by_origin["probe"] == pytest.approx(0.0001, abs=1e-6)
        assert by_origin["dev"] == pytest.approx(0.10, abs=1e-4)
        assert "ingestion" not in by_origin  # no rows → not surfaced
        assert "test" not in by_origin
        assert costs["last_7d_total_usd"] == pytest.approx(2.4061, abs=1e-4)

    @patch("api.routes.admin.supabase_rpc", return_value=[])
    @patch("api.routes.admin.supabase_count", return_value=1)
    @patch("api.routes.admin.supabase_get")
    def test_dashboard_cost_attribution_window_filter(
        self, mock_get, mock_count, mock_rpc
    ):
        """The gemini_calls query must use event_at >= (today - 7 days).
        Regression guard on the date math at the attribution boundary."""
        from datetime import datetime, timezone, timedelta
        mock_get.return_value = []

        client.get("/api/admin/dashboard", headers=_ADMIN_AUTH_HEADER)

        # 5th call (index 4) is the gemini_calls 7d attribution query;
        # this test only exercises the 7d window filter so the MTD call
        # at index 5 is irrelevant here.
        gemini_params = mock_get.call_args_list[4][0][1]
        assert gemini_params["select"] == "origin,cost_usd_estimate"
        # event_at filter should be gte. with a recent UTC timestamp
        event_filter = gemini_params["event_at"]
        assert event_filter.startswith("gte.")
        # Pull out the timestamp and confirm it's within ~10 seconds of
        # 7 days ago — protects against drift in the window calculation.
        ts_str = event_filter[len("gte."):]
        ts = datetime.fromisoformat(ts_str)
        delta = abs((datetime.now(timezone.utc) - timedelta(days=7)) - ts).total_seconds()
        assert delta < 10, f"7d window timestamp drift: {delta}s"

    @patch("api.routes.admin.supabase_rpc", return_value=[])
    @patch("api.routes.admin.supabase_count", return_value=1)
    @patch("api.routes.admin.supabase_get")
    def test_dashboard_gemini_mtd_uses_month_boundary(
        self, mock_get, mock_count, mock_rpc
    ):
        """gemini_mtd_usd sums cost_usd_estimate across rows returned by
        the MTD query. The Python aggregation must match the sum of the
        raw row values so the dashboard's headline tile reads correctly."""
        mock_get.side_effect = [
            [],  # anon_usage today
            [],  # anon_usage 7d
            [],  # anon_usage 30d
            [],  # system_flags
            [],  # gemini_calls 7d attribution
            # gemini_calls MTD — three rows at varying per-call costs
            [
                {"cost_usd_estimate": 0.0210},
                {"cost_usd_estimate": 0.0185},
                {"cost_usd_estimate": 0.0244},
            ],
        ]
        resp = client.get("/api/admin/dashboard", headers=_ADMIN_AUTH_HEADER)
        costs = resp.json()["api_costs"]
        assert costs["gemini_mtd_usd"] == pytest.approx(0.0639, abs=1e-4)
        assert costs["gemini_ceiling_usd"] == 35.0

    @patch("api.routes.admin.supabase_rpc", return_value=[])
    @patch("api.routes.admin.supabase_count", return_value=1)
    @patch("api.routes.admin.supabase_get")
    def test_dashboard_gemini_mtd_query_uses_first_of_month(
        self, mock_get, mock_count, mock_rpc
    ):
        """The MTD query must filter event_at >= first of the current UTC
        month at 00:00:00. Regression guard on the month-boundary math so
        UTC rollover resets the tile cleanly."""
        from datetime import datetime, timezone
        mock_get.return_value = []

        client.get("/api/admin/dashboard", headers=_ADMIN_AUTH_HEADER)

        # 6th call (index 5) is the gemini_calls MTD query.
        mtd_params = mock_get.call_args_list[5][0][1]
        assert mtd_params["select"] == "cost_usd_estimate"
        event_filter = mtd_params["event_at"]
        assert event_filter.startswith("gte.")
        ts_str = event_filter[len("gte."):]
        ts = datetime.fromisoformat(ts_str)
        now = datetime.now(timezone.utc)
        assert ts.year == now.year
        assert ts.month == now.month
        assert ts.day == 1
        assert ts.hour == 0
        assert ts.minute == 0
        assert ts.second == 0


class TestAdminAuth:
    """Verify admin endpoint rejects requests without valid Basic Auth credentials."""

    def test_admin_rejects_anonymous(self):
        resp = client.get("/api/admin/dashboard")
        assert resp.status_code == 401
        assert "Basic" in resp.headers.get("www-authenticate", "")

    def test_admin_rejects_wrong_password(self):
        bad = "Basic " + base64.b64encode(b"test-admin:wrong-password").decode()
        resp = client.get("/api/admin/dashboard", headers={"Authorization": bad})
        assert resp.status_code == 401

    def test_admin_rejects_wrong_username(self):
        bad = "Basic " + base64.b64encode(b"wrong-user:test-password").decode()
        resp = client.get("/api/admin/dashboard", headers={"Authorization": bad})
        assert resp.status_code == 401

    def test_admin_rejects_bearer_token(self):
        """A Bearer token is not Basic Auth — should 401."""
        resp = client.get(
            "/api/admin/dashboard",
            headers={"Authorization": "Bearer invalid-garbage-token"},
        )
        assert resp.status_code == 401

    def test_admin_endpoint_is_read_only(self):
        """POST/PATCH/DELETE to admin dashboard endpoint should return 405."""
        resp = client.post("/api/admin/dashboard", json={}, headers=_ADMIN_AUTH_HEADER)
        assert resp.status_code == 405
        resp = client.patch("/api/admin/dashboard", json={}, headers=_ADMIN_AUTH_HEADER)
        assert resp.status_code == 405
        resp = client.delete("/api/admin/dashboard", headers=_ADMIN_AUTH_HEADER)
        assert resp.status_code == 405


# =====================================================================
# LOCKDOWN TOGGLE
# =====================================================================

class TestLockdownEndpoint:
    """Verify the lockdown toggle endpoint with valid Basic Auth."""

    @patch("api.routes.admin.reset_lockdown_cache")
    @patch("api.routes.admin.supabase_patch", return_value=[])
    def test_lockdown_toggle_returns_200(self, mock_patch, mock_cache):
        resp = client.post("/api/admin/lockdown", json={"enabled": True},
                           headers=_ADMIN_AUTH_HEADER)
        assert resp.status_code == 200
        data = resp.json()
        assert data["lockdown_active"] is True
        # admin_id is now the Basic Auth username
        assert data["updated_by"] == "test-admin"
        assert "updated_at" in data

    @patch("api.routes.admin.reset_lockdown_cache")
    @patch("api.routes.admin.supabase_patch", return_value=[])
    def test_lockdown_disable_returns_false(self, mock_patch, mock_cache):
        resp = client.post("/api/admin/lockdown", json={"enabled": False},
                           headers=_ADMIN_AUTH_HEADER)
        assert resp.status_code == 200
        assert resp.json()["lockdown_active"] is False

    @patch("api.routes.admin.reset_lockdown_cache")
    @patch("api.routes.admin.supabase_patch", return_value=[])
    def test_lockdown_toggle_calls_supabase_patch(self, mock_patch, mock_cache):
        client.post("/api/admin/lockdown", json={"enabled": True},
                    headers=_ADMIN_AUTH_HEADER)
        mock_patch.assert_called_once()
        call_args = mock_patch.call_args
        assert call_args[0][0] == "system_flags"
        assert call_args[0][1]["flag_value"] is True
        assert call_args[0][1]["updated_by"] == "test-admin"

    @patch("api.routes.admin.reset_lockdown_cache")
    @patch("api.routes.admin.supabase_patch", return_value=[])
    def test_lockdown_toggle_resets_cache(self, mock_patch, mock_cache):
        client.post("/api/admin/lockdown", json={"enabled": True},
                    headers=_ADMIN_AUTH_HEADER)
        mock_cache.assert_called_once()

    @patch("api.routes.admin.supabase_get", return_value=[
        {"flag_value": True, "updated_at": "2026-04-12T00:00:00Z", "updated_by": "admin-id"},
    ])
    def test_lockdown_get_returns_status(self, mock_get):
        resp = client.get("/api/admin/lockdown", headers=_ADMIN_AUTH_HEADER)
        assert resp.status_code == 200
        assert resp.json()["lockdown_active"] is True

    @patch("api.routes.admin.supabase_get", return_value=[])
    def test_lockdown_get_defaults_to_false(self, mock_get):
        resp = client.get("/api/admin/lockdown", headers=_ADMIN_AUTH_HEADER)
        assert resp.status_code == 200
        assert resp.json()["lockdown_active"] is False

    def test_lockdown_toggle_requires_body(self):
        resp = client.post("/api/admin/lockdown", json={}, headers=_ADMIN_AUTH_HEADER)
        assert resp.status_code == 422

    @patch("api.routes.admin.supabase_count", return_value=1)
    @patch("api.routes.admin.supabase_get", return_value=[])
    def test_dashboard_includes_lockdown_active(self, mock_get, mock_count):
        resp = client.get("/api/admin/dashboard", headers=_ADMIN_AUTH_HEADER)
        assert resp.status_code == 200
        assert "lockdown_active" in resp.json()


class TestLockdownAuth:
    """Verify lockdown endpoint rejects requests without valid Basic Auth."""

    def test_lockdown_post_rejects_anonymous(self):
        resp = client.post("/api/admin/lockdown", json={"enabled": True})
        assert resp.status_code == 401

    def test_lockdown_get_rejects_anonymous(self):
        resp = client.get("/api/admin/lockdown")
        assert resp.status_code == 401

    def test_lockdown_post_rejects_wrong_credentials(self):
        bad = "Basic " + base64.b64encode(b"test-admin:wrong-password").decode()
        resp = client.post("/api/admin/lockdown", json={"enabled": True},
                           headers={"Authorization": bad})
        assert resp.status_code == 401


# =====================================================================
# WATCHLIST FETCH ENDPOINT
# =====================================================================

class TestWatchlistFetchValidation:
    """Pure validation cases — no upstream call expected. Verifies the
    Pydantic schema rejects malformed inputs before the fetcher runs."""

    def test_unsupported_source_returns_422(self):
        resp = client.post("/api/fetch-watchlist", json={
            "source": "kitsu", "username": "u", "statuses": ["completed"],
        })
        assert resp.status_code == 422

    def test_empty_username_returns_422(self):
        resp = client.post("/api/fetch-watchlist", json={
            "source": "mal", "username": "", "statuses": ["completed"],
        })
        assert resp.status_code == 422

    def test_username_over_50_chars_returns_422(self):
        resp = client.post("/api/fetch-watchlist", json={
            "source": "mal", "username": "a" * 51, "statuses": ["completed"],
        })
        assert resp.status_code == 422

    def test_invalid_status_returns_422(self):
        resp = client.post("/api/fetch-watchlist", json={
            "source": "mal", "username": "u", "statuses": ["not_a_status"],
        })
        assert resp.status_code == 422

    def test_missing_source_returns_422(self):
        resp = client.post("/api/fetch-watchlist", json={
            "username": "u", "statuses": ["completed"],
        })
        assert resp.status_code == 422


class TestWatchlistFetchKillSwitch:
    """WATCHLIST_EXCLUSION_ENABLED=false short-circuits to 503."""

    def test_disabled_endpoint_returns_503(self, monkeypatch):
        monkeypatch.setenv("WATCHLIST_EXCLUSION_ENABLED", "false")
        resp = client.post("/api/fetch-watchlist", json={
            "source": "mal", "username": "u", "statuses": ["completed"],
        })
        assert resp.status_code == 503

    def test_enabled_endpoint_proceeds_to_fetch(self, monkeypatch):
        """Sanity check the inverse — env=true reaches the fetcher
        (which we mock so we don't hit MAL)."""
        monkeypatch.setenv("WATCHLIST_EXCLUSION_ENABLED", "true")
        from analysis.watchlist_fetcher import WatchlistResult
        with patch("api.routes.watchlist.fetch_watchlist") as mock_fetch:
            mock_fetch.return_value = WatchlistResult(
                source="mal", mal_ids=[1, 2], total_entries=2,
                unmappable_entries=0, errors=[],
            )
            resp = client.post("/api/fetch-watchlist", json={
                "source": "mal", "username": "u", "statuses": ["completed"],
            })
        assert resp.status_code == 200
        assert resp.json()["mal_ids"] == [1, 2]


class TestWatchlistFetchErrorPropagation:
    """WatchlistFetchError status codes map cleanly to HTTP responses."""

    def _patch_fetch_to_raise(self, status_code: int, message: str):
        from analysis.watchlist_fetcher import WatchlistFetchError
        return patch(
            "api.routes.watchlist.fetch_watchlist",
            side_effect=WatchlistFetchError(message, status_code=status_code),
        )

    def test_404_user_not_found(self, monkeypatch):
        monkeypatch.setenv("WATCHLIST_EXCLUSION_ENABLED", "true")
        with self._patch_fetch_to_raise(404, "MAL user 'nobody' not found"):
            resp = client.post("/api/fetch-watchlist", json={
                "source": "mal", "username": "nobody", "statuses": ["completed"],
            })
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_503_token_broken(self, monkeypatch):
        monkeypatch.setenv("WATCHLIST_EXCLUSION_ENABLED", "true")
        with self._patch_fetch_to_raise(503, "MAL OAuth token has expired"):
            resp = client.post("/api/fetch-watchlist", json={
                "source": "mal", "username": "u", "statuses": ["completed"],
            })
        assert resp.status_code == 503

    def test_429_rate_limited_upstream(self, monkeypatch):
        monkeypatch.setenv("WATCHLIST_EXCLUSION_ENABLED", "true")
        with self._patch_fetch_to_raise(429, "AniList rate limit reached"):
            resp = client.post("/api/fetch-watchlist", json={
                "source": "anilist", "username": "u", "statuses": ["completed"],
            })
        assert resp.status_code == 429

    def test_502_upstream_unreachable(self, monkeypatch):
        monkeypatch.setenv("WATCHLIST_EXCLUSION_ENABLED", "true")
        with self._patch_fetch_to_raise(502, "AniList unreachable"):
            resp = client.post("/api/fetch-watchlist", json={
                "source": "anilist", "username": "u", "statuses": ["completed"],
            })
        assert resp.status_code == 502


class TestWatchlistFetchDefaults:
    """Default statuses (completed + dropped) applied when omitted."""

    def test_omitted_statuses_uses_defaults(self, monkeypatch):
        monkeypatch.setenv("WATCHLIST_EXCLUSION_ENABLED", "true")
        from analysis.watchlist_fetcher import WatchlistResult
        with patch("api.routes.watchlist.fetch_watchlist") as mock_fetch:
            mock_fetch.return_value = WatchlistResult(
                source="mal", mal_ids=[], total_entries=0,
                unmappable_entries=0, errors=[],
            )
            resp = client.post("/api/fetch-watchlist", json={
                "source": "mal", "username": "u",
            })
            assert resp.status_code == 200
            # Caller's statuses arg should have been the default pair
            statuses_arg = mock_fetch.call_args.kwargs.get("statuses") \
                or mock_fetch.call_args.args[2]
            assert sorted(statuses_arg) == ["completed", "dropped"]

    def test_response_includes_unmappable_count(self, monkeypatch):
        """AniList catalog-gap surface: frontend uses this to render the
        'N AniList entries without MAL links won't be excluded' caption."""
        monkeypatch.setenv("WATCHLIST_EXCLUSION_ENABLED", "true")
        from analysis.watchlist_fetcher import WatchlistResult
        with patch("api.routes.watchlist.fetch_watchlist") as mock_fetch:
            mock_fetch.return_value = WatchlistResult(
                source="anilist", mal_ids=[10, 20], total_entries=15,
                unmappable_entries=13, errors=[],
            )
            resp = client.post("/api/fetch-watchlist", json={
                "source": "anilist", "username": "u",
            })
        assert resp.status_code == 200
        body = resp.json()
        assert body["unmappable_entries"] == 13
        assert body["total_entries"] == 15


# =====================================================================
# WATCHLIST EXCLUSION PLUMBING
# =====================================================================

class TestSearchExcludeMalIdsValidation:
    """The exclude_mal_ids field on SearchRequest accepts valid input
    and rejects malformed payloads early."""

    def test_omitted_field_defaults_to_none(self):
        """Backward-compat: omitting exclude_mal_ids must not break
        existing request shapes from clients that haven't updated yet."""
        from api.schemas.requests import SearchRequest
        req = SearchRequest(query="anything")
        assert req.exclude_mal_ids is None

    def test_list_of_ids_accepted(self):
        from api.schemas.requests import SearchRequest
        req = SearchRequest(query="anything", exclude_mal_ids=[1, 2, 3])
        assert req.exclude_mal_ids == [1, 2, 3]

    def test_oversize_list_rejected(self):
        """Payload cap at 5,000 entries prevents DOS-via-massive-POST."""
        resp = client.post("/api/search", json={
            "query": "test", "exclude_mal_ids": list(range(5001)),
        })
        assert resp.status_code == 422

    def test_negative_ids_filtered_out(self):
        """Sanitizer drops negative and non-int values silently."""
        from api.schemas.requests import SearchRequest
        req = SearchRequest(query="x", exclude_mal_ids=[1, -5, 0, 7])
        assert req.exclude_mal_ids == [1, 7]

    def test_all_invalid_normalizes_to_none(self):
        """Empty-after-sanitize → None so downstream code can skip
        cluster expansion."""
        from api.schemas.requests import SearchRequest
        req = SearchRequest(query="x", exclude_mal_ids=[-1, 0])
        assert req.exclude_mal_ids is None

    def test_dna_request_field_same_shape(self):
        """DNARequest mirrors SearchRequest on this field."""
        from api.schemas.requests import DNARequest
        req = DNARequest(titles=["x"], exclude_mal_ids=[1, 2])
        assert req.exclude_mal_ids == [1, 2]


class TestSearchExcludeMalIdsThreading:
    """Verifies exclude_mal_ids reaches fetch_vault_pool when present.

    Uses FastAPI's dependency_overrides — the canonical way to substitute
    Depends() injections in tests.
    """

    def test_search_request_with_excludes_threads_through(self, mock_engine):
        mock_engine.fetch_vault_pool.return_value = {
            "success": True, "pool": [], "search_id": "x",
            "low_confidence": False,
        }
        resp = client.post("/api/search", json={
            "query": "cozy slice of life",
            "exclude_mal_ids": [20, 16498],
        })
        assert resp.status_code == 200
        kwargs = mock_engine.fetch_vault_pool.call_args.kwargs
        assert kwargs.get("exclude_mal_ids") == [20, 16498]

    def test_search_omitted_excludes_passes_none(self, mock_engine):
        mock_engine.fetch_vault_pool.return_value = {
            "success": True, "pool": [], "search_id": "x",
            "low_confidence": False,
        }
        resp = client.post("/api/search", json={"query": "anything"})
        assert resp.status_code == 200
        kwargs = mock_engine.fetch_vault_pool.call_args.kwargs
        assert kwargs.get("exclude_mal_ids") is None


class TestDNAExcludeMalIdsThreading:
    """Same plumbing check on the DNA endpoint."""

    def test_dna_request_with_excludes_threads_through(self, mock_engine):
        mock_engine.execute_dna_triangulation.return_value = {
            "success": True,
            "intersection_summary": "test",
            "data": [],
            "low_confidence": False,
        }
        resp = client.post("/api/dna", json={
            "titles": ["Cowboy Bebop"],
            "exclude_mal_ids": [20, 16498],
        })
        assert resp.status_code == 200
        kwargs = mock_engine.execute_dna_triangulation.call_args.kwargs
        assert kwargs.get("exclude_mal_ids") == [20, 16498]

    def test_dna_omitted_excludes_passes_none(self, mock_engine):
        mock_engine.execute_dna_triangulation.return_value = {
            "success": True, "intersection_summary": "x",
            "data": [], "low_confidence": False,
        }
        resp = client.post("/api/dna", json={"titles": ["Cowboy Bebop"]})
        assert resp.status_code == 200
        kwargs = mock_engine.execute_dna_triangulation.call_args.kwargs
        assert kwargs.get("exclude_mal_ids") is None


class TestFilteredVectorSearchExclusion:
    """Direct vector_store.search end-to-end smoke against the live vault.
    Confirms the cluster-expanded exclusion + SQL row drop honors the
    trust contract (excluded IDs absent from results)."""

    def test_excluded_ids_not_in_search_results(self, db_conn):
        """Through the public `search()` API so filter extraction runs
        correctly. Excludes 'Naruto' (id=20) and verifies the entire
        Naruto cluster is absent from the results."""
        from analysis.vector_store import search
        from analysis.queries import expand_to_cluster_ids
        # Expected exclusion set from cluster expansion
        excluded_set = expand_to_cluster_ids([20])
        # Get the title for each excluded ID
        placeholders = ",".join(["?"] * len(excluded_set))
        excluded_titles = {
            row[0] for row in db_conn.execute(
                f"SELECT english_title FROM anime_info WHERE id IN ({placeholders})",
                sorted(excluded_set),
            ).fetchall()
        }

        # Use a query with implicit year_min so the filtered path engages.
        # "shounen battle anime" — plenty of post-1990 results that aren't Naruto.
        response = search(
            "shounen battle anime",
            top_k=50,
            year_min=2000,
            exclude_mal_ids=[20],
        )
        result_titles = {r["title"] for r in response["results"]}
        leaks = excluded_titles & result_titles
        assert not leaks, \
            f"Excluded Naruto cluster titles leaked into results: {leaks}"
