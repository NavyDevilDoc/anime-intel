"""Tests for Gemini embedding rate-limit error handling.

Covers the fix for the "red 500 error block" surfaced when the Gemini
embedding free-tier RPM/daily bucket saturates:

  - vector_store._embed raises `EmbeddingRateLimited` (typed) after the
    existing retry loop (5 attempts, exponential backoff) is exhausted
    on sustained 429s. Non-429 exceptions continue to propagate unchanged.
  - recommendation_engine's _rate_limit_safe decorator catches the typed
    exception at both engine entry points (fetch_vault_pool and
    execute_dna_triangulation) and converts to a `success: False` dict
    with user-friendly copy. Routes already know how to render such
    dicts via the existing low-confidence / error path.

Run: pytest tests/test_rate_limit_handling.py -v
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

TESTS_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

os.environ["TESTING"] = "1"

from analysis import vector_store  # noqa: E402
from analysis.recommendation_engine import (  # noqa: E402
    RecommendationEngine,
    TriangulationPlan,
    _RATE_LIMITED_DNA_RESPONSE,
    _RATE_LIMITED_SEARCH_RESPONSE,
    _rate_limit_safe,
)
from analysis.vector_store import EmbeddingRateLimited  # noqa: E402


# ---------------------------------------------------------------------
# vector_store._embed — retry loop + typed raise
# ---------------------------------------------------------------------

class _FakeRateLimitError(Exception):
    """Mimics the shape Gemini's SDK raises — the discriminator is the
    "429" substring in the string representation, which is what _embed's
    is_rate_limit check keys on."""

    def __str__(self) -> str:
        return "google.genai.errors.ClientError: 429 RESOURCE_EXHAUSTED"


class TestEmbedRateLimitBehavior:
    """Verifies _embed's conversion of sustained 429s into a typed
    EmbeddingRateLimited exception. time.sleep is patched out so tests
    don't actually wait 30 seconds through the backoff."""

    def test_exhausted_429_retries_raises_typed_exception(self):
        with patch("analysis.vector_store.gemini_client") as mock_client, \
             patch("analysis.vector_store.time.sleep") as mock_sleep, \
             patch("analysis.vector_store._get_client") as mock_get_client:
            mock_get_client.return_value = MagicMock()
            mock_client.embed_content.side_effect = _FakeRateLimitError()

            with pytest.raises(EmbeddingRateLimited) as exc_info:
                vector_store._embed(["any query"])

            # Should mention the attempt count so operators can tell
            # retries ran before the surrender
            assert "5 attempts" in str(exc_info.value)
            # 4 retries fire before the final attempt raises (first attempt
            # has no preceding sleep)
            assert mock_sleep.call_count == 4
            # Original exception preserved as __cause__ for traceback hygiene
            assert isinstance(exc_info.value.__cause__, _FakeRateLimitError)

    def test_non_429_exception_propagates_unchanged(self):
        """A ValueError from the SDK (hypothetical: malformed payload) must
        NOT be caught and re-raised as EmbeddingRateLimited — the decorator
        would then wrongly convert a bug into a rate-limit message."""
        with patch("analysis.vector_store.gemini_client") as mock_client, \
             patch("analysis.vector_store._get_client") as mock_get_client:
            mock_get_client.return_value = MagicMock()
            mock_client.embed_content.side_effect = ValueError("malformed content")

            with pytest.raises(ValueError, match="malformed"):
                vector_store._embed(["any query"])

    def test_transient_429_still_recovers_via_retry(self):
        """Sustained 429s raise EmbeddingRateLimited, but a transient blip
        that resolves after one retry should succeed silently — the existing
        retry logic shouldn't be broken by the new error-handling branch."""
        with patch("analysis.vector_store.gemini_client") as mock_client, \
             patch("analysis.vector_store.time.sleep"), \
             patch("analysis.vector_store._get_client") as mock_get_client, \
             patch("analysis.vector_store.faiss.normalize_L2"):
            mock_get_client.return_value = MagicMock()
            # First attempt: 429. Second attempt: success.
            success_result = MagicMock()
            success_result.embeddings = [MagicMock(values=[0.1] * 3072)]
            mock_client.embed_content.side_effect = [
                _FakeRateLimitError(),
                success_result,
            ]

            # Should NOT raise — retry succeeds on second attempt
            result = vector_store._embed(["any query"])
            assert result is not None
            assert mock_client.embed_content.call_count == 2


# ---------------------------------------------------------------------
# _rate_limit_safe decorator — unit tests
# ---------------------------------------------------------------------

class TestRateLimitSafeDecorator:
    def test_catches_typed_exception_returns_template_copy(self):
        template = {"success": False, "error": "friendly msg", "low_confidence": False}

        @_rate_limit_safe(template)
        def _raises(self):
            raise EmbeddingRateLimited("sustained 429")

        result = _raises(None)
        assert result == template
        # Returns a copy, not the template itself — caller mutations
        # must not leak back into the module-level constant.
        assert result is not template

    def test_passes_through_normal_returns(self):
        template = {"success": False, "error": "x"}
        happy_path = {"success": True, "pool": [1, 2, 3]}

        @_rate_limit_safe(template)
        def _works(self):
            return happy_path

        assert _works(None) is happy_path

    def test_does_not_catch_other_exceptions(self):
        template = {"success": False, "error": "x"}

        @_rate_limit_safe(template)
        def _blows_up(self):
            raise RuntimeError("something else")

        with pytest.raises(RuntimeError, match="something else"):
            _blows_up(None)


# ---------------------------------------------------------------------
# Engine-level integration — mock _embed, call real engine method
# ---------------------------------------------------------------------

class TestEngineHandlesRateLimit:
    """Mock the vector_store calls to raise, then exercise the real
    fetch_vault_pool / execute_dna_triangulation methods. The decorator
    runs for real; only the embedding is faked. Verifies the end-to-end
    behavior that the search/dna routes depend on."""

    def _make_engine(self):
        return RecommendationEngine(api_key="test-not-used")

    def test_fetch_vault_pool_returns_rate_limit_response(self):
        engine = self._make_engine()
        with patch("analysis.vector_store.search") as mock_search:
            mock_search.side_effect = EmbeddingRateLimited("sustained 429")
            result = engine.fetch_vault_pool("any query")

        assert result["success"] is False
        assert "rate-limited" in result["error"].lower()
        assert "try again in a minute" in result["error"].lower()
        assert result["low_confidence"] is False
        # Fields the search route needs are present
        assert "fallback_mode" in result

    def test_execute_dna_triangulation_returns_rate_limit_response(self):
        engine = self._make_engine()
        # DNA pipeline: title resolution (DB) → Gemini "DNA synthesis"
        # reranker call → search_by_centroid (embedding). To exercise the
        # EmbeddingRateLimited path specifically, we have to let title
        # resolution run naturally (needs real DB), mock the reranker
        # call to return a valid plan, then raise from search_by_centroid.
        with patch("analysis.vector_store.search_by_centroid") as mock_centroid, \
             patch("analysis.recommendation_engine.gemini_client") as mock_gc:
            mock_response = MagicMock()
            mock_response.parsed = TriangulationPlan(
                intersection_summary="test triangulation summary"
            )
            mock_gc.generate_content.return_value = mock_response
            mock_centroid.side_effect = EmbeddingRateLimited("sustained 429")

            result = engine.execute_dna_triangulation(["Cowboy Bebop"])

        assert result["success"] is False
        assert "rate-limited" in result["error"].lower()
        assert result["low_confidence"] is False
        # DNA-specific field present too (routes read this)
        assert "title_resolution_notes" in result


class TestResponseTemplateIntegrity:
    """Smoke tests for the module-level canonical response constants —
    both routes read these fields, so a typo in the constant would ship
    as a user-visible bug."""

    def test_search_response_has_required_fields(self):
        for key in ("success", "error", "low_confidence", "fallback_mode"):
            assert key in _RATE_LIMITED_SEARCH_RESPONSE
        assert _RATE_LIMITED_SEARCH_RESPONSE["success"] is False

    def test_dna_response_has_required_fields(self):
        for key in ("success", "error", "low_confidence", "fallback_mode",
                    "title_resolution_notes"):
            assert key in _RATE_LIMITED_DNA_RESPONSE
        assert _RATE_LIMITED_DNA_RESPONSE["success"] is False
