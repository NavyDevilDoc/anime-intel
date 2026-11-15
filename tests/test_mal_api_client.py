"""Unit tests for src/mal_api_client.MALClient token loading.

PR #82 follow-up: verify the Supabase → env → disk fallback pattern
lands correctly in the ingestor-side MAL client. Same fallback shape
as analysis/watchlist_fetcher._read_mal_token.

2026-09-25 additions: refresh_and_reload() + get_with_auto_refresh()
close the workflow-vs-local self-healing gap surfaced during Phase A
of the Vault Coverage Investigation — the ingest code path now handles
401 the same way the seasonal-ingest workflow's pulse check does.
"""
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

TESTS_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from src.mal_api_client import MALClient


class TestMALClientTokenFallback:
    """The 3-tier fallback: Supabase → env → disk. Each tier stubbable
    in isolation so we can exercise each path deterministically."""

    def _clear_env(self, monkeypatch):
        monkeypatch.delenv("MAL_ACCESS_TOKEN", raising=False)

    def test_supabase_wins_when_available(self, monkeypatch, tmp_path):
        """Supabase-first: when the singleton has a token, that's what
        gets loaded — env and disk are not consulted."""
        self._clear_env(monkeypatch)
        with patch(
            "src.mal_api_client.MALClient._read_supabase_token",
            return_value="supabase_token_abc",
        ):
            # Point token_path at a nonexistent file — proves disk was
            # never attempted.
            client = MALClient(token_path=str(tmp_path / "nope.json"))
        assert client.token == "supabase_token_abc"
        assert client.headers["Authorization"] == "Bearer supabase_token_abc"

    def test_env_wins_when_supabase_none(self, monkeypatch, tmp_path):
        """Supabase returns None → env var picked up."""
        monkeypatch.setenv("MAL_ACCESS_TOKEN", "env_token_xyz")
        with patch(
            "src.mal_api_client.MALClient._read_supabase_token",
            return_value=None,
        ):
            client = MALClient(token_path=str(tmp_path / "nope.json"))
        assert client.token == "env_token_xyz"

    def test_disk_wins_when_supabase_and_env_both_none(self, monkeypatch, tmp_path):
        """Supabase None + env unset → disk file read as last resort."""
        self._clear_env(monkeypatch)
        token_file = tmp_path / "token_data.json"
        token_file.write_text(json.dumps({"access_token": "disk_token_123"}))
        with patch(
            "src.mal_api_client.MALClient._read_supabase_token",
            return_value=None,
        ):
            client = MALClient(token_path=str(token_file))
        assert client.token == "disk_token_123"

    def test_raises_when_all_three_sources_miss(self, monkeypatch, tmp_path):
        """No Supabase, no env, no disk → RuntimeError with a helpful
        message pointing at the bootstrap tool."""
        self._clear_env(monkeypatch)
        with patch(
            "src.mal_api_client.MALClient._read_supabase_token",
            return_value=None,
        ):
            with pytest.raises(RuntimeError, match="seed_mal_token"):
                MALClient(token_path=str(tmp_path / "nope.json"))

    def test_supabase_read_failure_falls_through(self, monkeypatch, tmp_path):
        """Supabase transport failure returns None from
        _read_supabase_token; fallback continues cleanly to env."""
        monkeypatch.setenv("MAL_ACCESS_TOKEN", "env_after_supabase_fail")
        # _read_supabase_token catches the exception internally and
        # returns None; simulate that end-state.
        with patch(
            "src.mal_api_client.MALClient._read_supabase_token",
            return_value=None,
        ):
            client = MALClient(token_path=str(tmp_path / "nope.json"))
        assert client.token == "env_after_supabase_fail"

    def test_authorization_header_wired_onto_session(self, monkeypatch, tmp_path):
        """Whichever source wins, the token lands on both self.headers
        and session.headers — the two places downstream code reads."""
        self._clear_env(monkeypatch)
        with patch(
            "src.mal_api_client.MALClient._read_supabase_token",
            return_value="header_check_token",
        ):
            client = MALClient(token_path=str(tmp_path / "nope.json"))
        assert client.session.headers["Authorization"] == "Bearer header_check_token"


def _mock_http_response(status_code=200, json_data=None):
    """Build a MagicMock response matching the shape callers read."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else {}
    return resp


class TestRefreshAndReload:
    """Rotate the OAuth token in-place — refresh_mal_token writes new
    credentials, then _load_token re-reads the fresh state."""

    def test_calls_refresh_then_reloads_headers(self, monkeypatch, tmp_path):
        monkeypatch.delenv("MAL_ACCESS_TOKEN", raising=False)
        # First _read_supabase_token: returns "old" during init.
        # Second _read_supabase_token: returns "new" — simulating the
        # post-refresh state.
        read_supabase_calls = [None, "new_token_after_refresh"]
        read_supabase_calls[0] = "old_token_before_refresh"

        with patch(
            "src.mal_api_client.MALClient._read_supabase_token",
            side_effect=read_supabase_calls,
        ):
            client = MALClient(token_path=str(tmp_path / "nope.json"))
            assert client.token == "old_token_before_refresh"

            # Patch refresh_mal_token where refresh_and_reload imports it.
            with patch("src.mal_auth_helper.refresh_mal_token") as mock_refresh:
                client.refresh_and_reload()
                mock_refresh.assert_called_once()

        # After refresh_and_reload, the headers reflect the new token.
        assert client.token == "new_token_after_refresh"
        assert client.headers["Authorization"] == "Bearer new_token_after_refresh"
        assert client.session.headers["Authorization"] == "Bearer new_token_after_refresh"


class TestGetWithAutoRefresh:
    """The self-healing GET wrapper — the 2026-09-25 fix that closes
    the workflow-vs-local resilience gap. Load-bearing for any local
    ingest run (targeted-strike, backfill scripts) against a stale
    MAL token."""

    def _client_with_token(self, monkeypatch, tmp_path, token="initial_token"):
        monkeypatch.delenv("MAL_ACCESS_TOKEN", raising=False)
        with patch(
            "src.mal_api_client.MALClient._read_supabase_token",
            return_value=token,
        ):
            return MALClient(token_path=str(tmp_path / "nope.json"))

    def test_200_returns_response_without_refresh(self, monkeypatch, tmp_path):
        client = self._client_with_token(monkeypatch, tmp_path)
        mock_get = MagicMock(return_value=_mock_http_response(200, {"ok": True}))
        with patch("src.mal_api_client.requests.get", mock_get), \
             patch.object(client, "refresh_and_reload") as mock_refresh:
            resp = client.get_with_auto_refresh("https://api.myanimelist.net/v2/anime/1")

        assert resp.status_code == 200
        assert mock_get.call_count == 1
        mock_refresh.assert_not_called()

    def test_401_triggers_refresh_and_retries_once(self, monkeypatch, tmp_path):
        """The load-bearing behavior — stale token gets rotated and the
        retry succeeds."""
        client = self._client_with_token(monkeypatch, tmp_path)
        responses = [
            _mock_http_response(401, {"error": "invalid_token"}),
            _mock_http_response(200, {"data": "recovered"}),
        ]
        mock_get = MagicMock(side_effect=responses)
        with patch("src.mal_api_client.requests.get", mock_get), \
             patch.object(client, "refresh_and_reload") as mock_refresh:
            resp = client.get_with_auto_refresh("https://api.myanimelist.net/v2/anime/1")

        assert resp.status_code == 200
        assert mock_get.call_count == 2
        mock_refresh.assert_called_once()

    def test_401_persists_after_refresh_returns_second_401(self, monkeypatch, tmp_path):
        """Refresh succeeded (didn't raise) but MAL still 401s — return
        the second response so caller sees the real failure. No infinite
        retry."""
        client = self._client_with_token(monkeypatch, tmp_path)
        mock_get = MagicMock(return_value=_mock_http_response(401))
        with patch("src.mal_api_client.requests.get", mock_get), \
             patch.object(client, "refresh_and_reload"):
            resp = client.get_with_auto_refresh("https://api.myanimelist.net/v2/anime/1")

        assert resp.status_code == 401
        assert mock_get.call_count == 2  # exactly two — no third try

    def test_network_error_propagates_without_refresh(self, monkeypatch, tmp_path):
        """Timeouts / connection errors aren't auth failures — no refresh,
        no retry, exception surfaces to caller as-is."""
        client = self._client_with_token(monkeypatch, tmp_path)
        with patch(
            "src.mal_api_client.requests.get",
            side_effect=requests.Timeout("timed out"),
        ), patch.object(client, "refresh_and_reload") as mock_refresh:
            with pytest.raises(requests.Timeout):
                client.get_with_auto_refresh("https://api.myanimelist.net/v2/anime/1")
            mock_refresh.assert_not_called()

    def test_non_401_4xx_returned_without_refresh(self, monkeypatch, tmp_path):
        """404 is a legitimate 'not found' — refresh wouldn't help, don't
        even try. Only 401 triggers refresh."""
        client = self._client_with_token(monkeypatch, tmp_path)
        mock_get = MagicMock(return_value=_mock_http_response(404))
        with patch("src.mal_api_client.requests.get", mock_get), \
             patch.object(client, "refresh_and_reload") as mock_refresh:
            resp = client.get_with_auto_refresh("https://api.myanimelist.net/v2/anime/99999999")

        assert resp.status_code == 404
        assert mock_get.call_count == 1
        mock_refresh.assert_not_called()

    def test_passes_params_and_timeout(self, monkeypatch, tmp_path):
        """The helper preserves params + honors a default timeout so a
        MAL hang doesn't block the ingester indefinitely (same class of
        fix as jikan_client's explicit timeout)."""
        client = self._client_with_token(monkeypatch, tmp_path)
        captured = {}

        def _capture(url, **kwargs):
            captured["url"] = url
            captured["params"] = kwargs.get("params")
            captured["timeout"] = kwargs.get("timeout")
            captured["headers"] = kwargs.get("headers")
            return _mock_http_response(200)

        with patch("src.mal_api_client.requests.get", side_effect=_capture):
            client.get_with_auto_refresh(
                "https://api.myanimelist.net/v2/anime/1",
                params={"fields": "synopsis"},
            )

        assert captured["url"] == "https://api.myanimelist.net/v2/anime/1"
        assert captured["params"] == {"fields": "synopsis"}
        assert captured["timeout"] == 30
        assert captured["headers"] == {"Authorization": "Bearer initial_token"}
