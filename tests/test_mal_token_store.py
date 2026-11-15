"""
Tests for analysis/mal_token_store — Supabase-backed MAL OAuth token
singleton. Initiative 3 Phase B.

Boundary: tests mock supabase_get / supabase_patch / supabase_post at
the analysis.mal_token_store import site, NOT at api.supabase_client.
This matches the existing test_lockdown.py pattern and isolates the
module's logic from the transport implementation.
"""
import os
import sys
from unittest.mock import patch

import pytest

TESTS_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from analysis import mal_token_store
from analysis.mal_token_store import read_token, write_token, TABLE, SINGLETON_ID


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------

@pytest.fixture
def supabase_configured(monkeypatch):
    """Ensure both env vars are set so _supabase_available() returns True."""
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-service-key")


@pytest.fixture
def supabase_unconfigured(monkeypatch):
    """Strip both env vars so _supabase_available() returns False."""
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)


VALID_PAYLOAD = {
    "access_token": "test-access-token-123",
    "refresh_token": "test-refresh-token-456",
    "token_type": "Bearer",
    "expires_in": 2678400,
}

EXISTING_ROW = {
    "id": 1,
    "access_token": "stored-access",
    "refresh_token": "stored-refresh",
    "token_type": "Bearer",
    "expires_in": 2678400,
    "refreshed_at": "2026-06-06T12:00:00+00:00",
}


# ---------------------------------------------------------------------
# read_token
# ---------------------------------------------------------------------

class TestReadToken:
    def test_returns_row_when_singleton_exists(self, supabase_configured):
        with patch.object(
            mal_token_store, "supabase_get", return_value=[EXISTING_ROW]
        ) as mock_get:
            result = read_token()
        assert result == EXISTING_ROW
        mock_get.assert_called_once_with(TABLE, params={"id": f"eq.{SINGLETON_ID}"})

    def test_returns_none_when_row_missing(self, supabase_configured):
        with patch.object(mal_token_store, "supabase_get", return_value=[]):
            assert read_token() is None

    def test_returns_none_when_supabase_unconfigured(self, supabase_unconfigured):
        # Should short-circuit WITHOUT calling supabase_get at all.
        with patch.object(mal_token_store, "supabase_get") as mock_get:
            assert read_token() is None
        mock_get.assert_not_called()

    def test_returns_none_on_transport_failure(self, supabase_configured):
        with patch.object(
            mal_token_store,
            "supabase_get",
            side_effect=Exception("Supabase 500"),
        ):
            # Defensive: must not crash even when Supabase is down.
            assert read_token() is None


# ---------------------------------------------------------------------
# write_token
# ---------------------------------------------------------------------

class TestWriteToken:
    def test_patches_existing_singleton(self, supabase_configured):
        with patch.object(
            mal_token_store, "supabase_patch", return_value=[EXISTING_ROW]
        ) as mock_patch, patch.object(
            mal_token_store, "supabase_post"
        ) as mock_post:
            assert write_token(VALID_PAYLOAD) is True

        # PATCH targeted the singleton.
        mock_patch.assert_called_once()
        _, kwargs = mock_patch.call_args
        assert kwargs["params"] == {"id": f"eq.{SINGLETON_ID}"}
        # PATCH carried the rotation fields; refreshed_at is server-side.
        assert kwargs["data"]["access_token"] == VALID_PAYLOAD["access_token"]
        assert kwargs["data"]["refresh_token"] == VALID_PAYLOAD["refresh_token"]
        assert kwargs["data"]["token_type"] == "Bearer"
        assert kwargs["data"]["expires_in"] == 2678400
        assert "refreshed_at" not in kwargs["data"]
        # POST never fired — PATCH succeeded.
        mock_post.assert_not_called()

    def test_posts_when_singleton_unseeded(self, supabase_configured):
        with patch.object(
            mal_token_store, "supabase_patch", return_value=[]
        ) as mock_patch, patch.object(
            mal_token_store, "supabase_post", return_value=[{"id": 1}]
        ) as mock_post:
            assert write_token(VALID_PAYLOAD) is True

        mock_patch.assert_called_once()
        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        assert kwargs["data"]["id"] == SINGLETON_ID
        assert kwargs["data"]["access_token"] == VALID_PAYLOAD["access_token"]

    def test_defaults_token_type_when_payload_omits_it(self, supabase_configured):
        payload = {"access_token": "a", "refresh_token": "r"}
        with patch.object(
            mal_token_store, "supabase_patch", return_value=[EXISTING_ROW]
        ) as mock_patch:
            assert write_token(payload) is True
        _, kwargs = mock_patch.call_args
        assert kwargs["data"]["token_type"] == "Bearer"
        # expires_in absent in payload → None in row (nullable column).
        assert kwargs["data"]["expires_in"] is None

    def test_rejects_payload_missing_access_token(self, supabase_configured):
        with patch.object(
            mal_token_store, "supabase_patch"
        ) as mock_patch, patch.object(
            mal_token_store, "supabase_post"
        ) as mock_post:
            assert write_token({"refresh_token": "only-refresh"}) is False
        mock_patch.assert_not_called()
        mock_post.assert_not_called()

    def test_rejects_payload_missing_refresh_token(self, supabase_configured):
        with patch.object(
            mal_token_store, "supabase_patch"
        ) as mock_patch, patch.object(
            mal_token_store, "supabase_post"
        ) as mock_post:
            assert write_token({"access_token": "only-access"}) is False
        mock_patch.assert_not_called()
        mock_post.assert_not_called()

    def test_returns_false_when_supabase_unconfigured(self, supabase_unconfigured):
        with patch.object(
            mal_token_store, "supabase_patch"
        ) as mock_patch, patch.object(
            mal_token_store, "supabase_post"
        ) as mock_post:
            assert write_token(VALID_PAYLOAD) is False
        mock_patch.assert_not_called()
        mock_post.assert_not_called()

    def test_returns_false_on_patch_transport_failure(self, supabase_configured):
        with patch.object(
            mal_token_store,
            "supabase_patch",
            side_effect=Exception("network broke"),
        ):
            # Defensive: caller decides whether to fall back to disk.
            assert write_token(VALID_PAYLOAD) is False

    def test_returns_false_on_post_transport_failure(self, supabase_configured):
        with patch.object(
            mal_token_store, "supabase_patch", return_value=[]
        ), patch.object(
            mal_token_store,
            "supabase_post",
            side_effect=Exception("constraint violation"),
        ):
            assert write_token(VALID_PAYLOAD) is False
