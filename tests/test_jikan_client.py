"""Unit tests for src/jikan_client.JikanClient.get_anime_reviews.

Post-July-2026 Jikan /reviews 504 outage: verifies that
`ReviewFetchResult` distinguishes real failures from empty-success,
that 5xx errors retry-with-backoff (new behavior — pre-change they
silently returned []), and that error kinds are captured for
downstream quarantine-table logging.
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

TESTS_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from src.jikan_client import JikanClient, ReviewFetchResult


def _mock_response(status_code=200, json_data=None):
    """Build a MagicMock response matching the shape jikan_client reads."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else {"data": []}
    return resp


class TestReviewFetchResult:
    """The dataclass wrapper itself — proves error-vs-empty distinction."""

    def test_default_construction_is_not_error(self):
        r = ReviewFetchResult()
        assert r.is_error is False
        assert r.reviews == []
        assert r.summary == "OK: 0 reviews"

    def test_with_reviews_is_not_error(self):
        r = ReviewFetchResult(reviews=[{"content": "great"}], status_code=200)
        assert r.is_error is False
        assert "OK: 1 reviews" in r.summary

    def test_with_error_kind_is_error(self):
        r = ReviewFetchResult(
            status_code=504,
            error_kind="http_5xx",
            error_detail="HTTP 504 after 3 retries",
        )
        assert r.is_error is True
        assert "http_5xx" in r.summary
        assert "504" in r.summary


class TestGetAnimeReviewsSuccess:
    """Happy paths — verify structured return on 200 with data and 200 with empty data."""

    def test_200_with_data_returns_reviews(self):
        client = JikanClient()
        data = {
            "data": [
                {"review": "loved it", "score": 9, "tags": ["Recommended"], "reactions": {"nice": 5}},
                {"review": "meh", "score": 5, "tags": [], "reactions": {}},
            ]
        }
        with patch.object(client.session, "get", return_value=_mock_response(200, data)), \
             patch("src.jikan_client.time.sleep"):
            result = client.get_anime_reviews(mal_id=1)

        assert result.is_error is False
        assert len(result.reviews) == 2
        assert result.reviews[0]["content"] == "loved it"
        assert result.reviews[0]["score"] == 9
        assert result.status_code == 200

    def test_200_with_empty_data_is_success_not_error(self):
        """Genuinely-no-reviews (rare but real) → not an error, just empty."""
        client = JikanClient()
        with patch.object(client.session, "get", return_value=_mock_response(200, {"data": []})), \
             patch("src.jikan_client.time.sleep"):
            result = client.get_anime_reviews(mal_id=99999)

        assert result.is_error is False
        assert result.reviews == []
        assert result.status_code == 200


class TestGetAnimeReviewsFailureModes:
    """Error paths — the load-bearing behavior this PR is fixing.
    Pre-change, all of these returned `[]` and were indistinguishable
    from `test_200_with_empty_data_is_success_not_error` above."""

    def test_5xx_retries_then_gives_up_with_http_5xx_kind(self):
        """The July 2026 Jikan outage scenario. Retries max_attempts
        times, then returns structured error info."""
        client = JikanClient()
        with patch.object(client.session, "get", return_value=_mock_response(504)), \
             patch("src.jikan_client.time.sleep") as mock_sleep:
            result = client.get_anime_reviews(mal_id=1, max_attempts=3)

        assert result.is_error is True
        assert result.error_kind == "http_5xx"
        assert result.status_code == 504
        assert "504" in result.error_detail
        # Exponential backoff — 2, 4, 8 seconds across 3 retry attempts.
        # (Last attempt gives up, so 3 sleep calls at 2, 4, 8.)
        sleep_calls = [c.args[0] for c in mock_sleep.call_args_list]
        assert sleep_calls == [2, 4, 8]

    def test_4xx_does_not_retry_and_returns_http_4xx_kind(self):
        """4xx won't recover on retry — one attempt, immediate return."""
        client = JikanClient()
        with patch.object(client.session, "get", return_value=_mock_response(404)), \
             patch("src.jikan_client.time.sleep") as mock_sleep:
            result = client.get_anime_reviews(mal_id=1)

        assert result.is_error is True
        assert result.error_kind == "http_4xx"
        assert result.status_code == 404
        # No retries for 4xx.
        assert mock_sleep.call_count == 0

    def test_429_retries_then_gives_up_with_rate_limit_kind(self):
        """Existing 429 handling preserved through the refactor."""
        client = JikanClient()
        with patch.object(client.session, "get", return_value=_mock_response(429)), \
             patch("src.jikan_client.time.sleep"):
            result = client.get_anime_reviews(mal_id=1, max_attempts=3)

        assert result.is_error is True
        assert result.error_kind == "rate_limit_exhausted"
        assert result.status_code == 429

    def test_network_timeout_returns_network_kind(self):
        client = JikanClient()
        with patch.object(client.session, "get", side_effect=requests.Timeout("timed out")):
            result = client.get_anime_reviews(mal_id=1)

        assert result.is_error is True
        assert result.error_kind == "network"
        assert "Timeout" in result.error_detail

    def test_network_connection_error_returns_network_kind(self):
        client = JikanClient()
        with patch.object(
            client.session, "get", side_effect=requests.ConnectionError("dns lookup failed")
        ):
            result = client.get_anime_reviews(mal_id=1)

        assert result.is_error is True
        assert result.error_kind == "network"
        assert "ConnectionError" in result.error_detail

    def test_json_decode_error_returns_unexpected_kind(self):
        """Jikan returning malformed JSON — caught, categorized, logged."""
        client = JikanClient()
        broken_resp = MagicMock()
        broken_resp.status_code = 200
        broken_resp.json.side_effect = ValueError("Expecting value: line 1")
        with patch.object(client.session, "get", return_value=broken_resp):
            result = client.get_anime_reviews(mal_id=1)

        assert result.is_error is True
        assert result.error_kind == "unexpected"
        assert "ValueError" in result.error_detail


class TestRetryBackoffTiming:
    """Belt-and-suspenders — exponential backoff sequence is load-bearing
    for playing nice with Jikan when it degrades. Lock the numbers."""

    def test_5xx_backoff_is_exponential_powers_of_two(self):
        client = JikanClient()
        with patch.object(client.session, "get", return_value=_mock_response(503)), \
             patch("src.jikan_client.time.sleep") as mock_sleep:
            client.get_anime_reviews(mal_id=1, max_attempts=4)

        # attempts 1, 2, 3, 4 → sleep 2**1, 2**2, 2**3, 2**4 = 2, 4, 8, 16
        sleep_calls = [c.args[0] for c in mock_sleep.call_args_list]
        assert sleep_calls == [2, 4, 8, 16]


class TestReviewsPreliminaryParam:
    """The 2026-09-25 fix. Without preliminary=true, jikan-rest filters
    out every review flagged 'preliminary' — which for long-running or
    currently-airing shows (One Piece MAL 21, all seasonal shows still
    airing) means every recent review, silently returning `data: []`
    and causing the ingestor to quarantine the show for '0 reviews.'"""

    def test_preliminary_true_param_is_passed_to_session_get(self):
        client = JikanClient()
        captured = {}

        def _capture_get(url, **kwargs):
            captured["url"] = url
            captured["params"] = kwargs.get("params")
            return _mock_response(200, {"data": []})

        with patch.object(client.session, "get", side_effect=_capture_get), \
             patch("src.jikan_client.time.sleep"):
            client.get_anime_reviews(mal_id=21)

        assert captured["params"] == {"preliminary": "true"}

    def test_preliminary_param_persists_across_retries(self):
        """5xx retry path re-enters get_anime_reviews recursively —
        confirm the param stays on every attempt, not just the first."""
        client = JikanClient()
        captured_params = []

        def _capture_get(url, **kwargs):
            captured_params.append(kwargs.get("params"))
            return _mock_response(503)

        with patch.object(client.session, "get", side_effect=_capture_get), \
             patch("src.jikan_client.time.sleep"):
            client.get_anime_reviews(mal_id=21, max_attempts=3)

        # 3 attempts + 1 give-up call = 4 total calls, all with preliminary=true
        assert len(captured_params) == 4
        assert all(p == {"preliminary": "true"} for p in captured_params)


class TestJikanBaseUrlRequired:
    """Phase G: `JIKAN_BASE_URL` is required — the public api.jikan.moe
    fallback was retired because the upstream is in maintenance mode /
    brownout. JikanClient() must fail fast with an actionable error
    when the env is unset, empty, or whitespace-only."""

    def test_missing_env_raises_runtime_error(self, monkeypatch):
        monkeypatch.delenv("JIKAN_BASE_URL", raising=False)
        with pytest.raises(RuntimeError, match="JIKAN_BASE_URL env var is required"):
            JikanClient()

    def test_empty_env_raises_runtime_error(self, monkeypatch):
        """An unset GitHub secret becomes an empty string in a workflow
        context — we can't distinguish "intentionally blank" from "forgot
        to set", so both must fail fast with the same actionable error."""
        monkeypatch.setenv("JIKAN_BASE_URL", "")
        with pytest.raises(RuntimeError, match="JIKAN_BASE_URL env var is required"):
            JikanClient()

    def test_whitespace_only_env_raises_runtime_error(self, monkeypatch):
        """Belt-and-suspenders — a space or tab in the secret value also
        counts as "unset" for the fail-fast check."""
        monkeypatch.setenv("JIKAN_BASE_URL", "   ")
        with pytest.raises(RuntimeError, match="JIKAN_BASE_URL env var is required"):
            JikanClient()

    def test_env_override_sets_base_url(self, monkeypatch):
        custom = "https://jikan-rest-production-d553.up.railway.app/v4"
        monkeypatch.setenv("JIKAN_BASE_URL", custom)
        client = JikanClient()
        assert client.base_url == custom

    def test_env_value_is_stripped(self, monkeypatch):
        """Leading/trailing whitespace in the env value is stripped so a
        slightly-mangled GitHub secret value doesn't produce a URL with
        embedded whitespace that would break HTTP calls downstream."""
        monkeypatch.setenv(
            "JIKAN_BASE_URL",
            "  https://jikan-rest-production-d553.up.railway.app/v4  ",
        )
        client = JikanClient()
        assert client.base_url == "https://jikan-rest-production-d553.up.railway.app/v4"

    def test_override_flows_through_to_request_url(self, monkeypatch):
        """End-to-end proof — override applies to the actual endpoint hit,
        not just self.base_url. Guards against future code that hardcodes
        the URL somewhere instead of using self.base_url."""
        custom = "https://custom.example.com/v4"
        monkeypatch.setenv("JIKAN_BASE_URL", custom)
        client = JikanClient()
        captured_url = {}

        def _capture_get(url, **kwargs):
            captured_url["url"] = url
            return _mock_response(200, {"data": []})

        with patch.object(client.session, "get", side_effect=_capture_get), \
             patch("src.jikan_client.time.sleep"):
            client.get_anime_reviews(mal_id=1)

        assert captured_url["url"] == f"{custom}/anime/1/reviews"
