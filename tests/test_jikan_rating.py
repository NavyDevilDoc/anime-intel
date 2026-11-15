"""Unit tests for JikanClient.get_anime_rating().

Validates the three-state contract on the structured JikanFetchResult
return that the rating backfill and the quarterly drift refresh both
rely on:
  - success: `is_error=False`, `value` = verbatim rating string
  - checked-empty: `is_error=False`, `value=""` (Jikan reached MAL and
    the rating was null — typical of upcoming/unreleased shows)
  - failure: `is_error=True`, `value=""` (HTTP/network failure or
    exhausted 429 retries; callers leave the DB NULL for retry)
"""

from unittest.mock import MagicMock, patch

from src.jikan_client import JikanClient


def _fresh_client():
    """JikanClient with rate_limit_delay zeroed so tests don't sleep."""
    client = JikanClient()
    client.rate_limit_delay = 0
    return client


class TestGetAnimeRating:

    @patch("requests.Session.get")
    def test_returns_verbatim_string_on_success(self, mock_get):
        """Happy path: Jikan returns a rating string → method returns it verbatim."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {"rating": "R - 17+ (violence & profanity)"}
        }
        mock_get.return_value = mock_response

        result = _fresh_client().get_anime_rating(16498)

        assert not result.is_error
        assert result.value == "R - 17+ (violence & profanity)"

    @patch("requests.Session.get")
    def test_returns_empty_string_when_jikan_rating_is_null(self, mock_get):
        """
        Phase A confirmed Jikan returns JSON null for upcoming/unreleased
        shows. Contract says map that to "" (checked-empty) so callers
        can distinguish "checked, MAL has nothing" (is_error=False,
        value="") from "not yet checked" (DB stays NULL).
        """
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": {"rating": None}}
        mock_get.return_value = mock_response

        result = _fresh_client().get_anime_rating(99999)

        assert not result.is_error
        assert result.value == ""

    @patch("requests.Session.get")
    def test_returns_error_on_http_failure(self, mock_get):
        """
        404 (or any non-200/429) triggers raise_for_status, the except
        clause catches it, method returns JikanFetchResult(is_error=True).
        Callers leave DB NULL so the next backfill pass retries.
        """
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.raise_for_status.side_effect = Exception("404 Not Found")
        mock_get.return_value = mock_response

        result = _fresh_client().get_anime_rating(0)

        assert result.is_error
        assert result.error_kind == "unexpected"
        assert result.value == ""
