"""
Tests for GET /api/anon-usage.

This endpoint exists so a freshly-opened tab can show truthful remaining
counts instead of the hardcoded daily cap. Without it, the frontend
seeded its display from the cap and decremented its own in-memory counter
— which lied across tabs and across reloads.

Coverage:
  - Default state (no row) returns full caps
  - Existing row returns actual remaining
  - Stale prior-day row reads as full caps (server treats it as no usage today)
  - Kill switch returns 401
  - Response shape matches the AnonUsageResponse contract the frontend expects

Run: pytest tests/test_anon_usage_route.py -v
"""
import os
import sys
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

TESTS_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# Disable rate limiting + lockdown for the shared test client
os.environ["TESTING"] = "1"

from api.main import app

client = TestClient(app)


class TestAnonUsageEndpointShape:
    """Response shape is the contract the frontend AnonBanner depends on."""

    @patch("api.middleware.anon_quota.supabase_get", return_value=[])
    def test_default_state_returns_full_caps(self, _mock_get):
        response = client.get("/api/anon-usage")
        assert response.status_code == 200
        data = response.json()
        assert data["search_remaining"] == 30
        assert data["dna_remaining"] == 10
        assert data["search_cap"] == 30
        assert data["dna_cap"] == 10
        assert "reset_at" in data

    @patch("api.middleware.anon_quota.supabase_get", return_value=[])
    def test_all_required_fields_present(self, _mock_get):
        """If any field is missing the frontend's AnonUsage type breaks."""
        response = client.get("/api/anon-usage")
        data = response.json()
        for field in ("search_remaining", "dna_remaining",
                      "search_cap", "dna_cap", "reset_at"):
            assert field in data, f"missing required field: {field}"

    @patch("api.middleware.anon_quota.supabase_get", return_value=[])
    def test_reset_at_is_iso_timestamp(self, _mock_get):
        """reset_at must be ISO format so the frontend can parse it."""
        from datetime import datetime
        response = client.get("/api/anon-usage")
        # Will raise if format is wrong
        datetime.fromisoformat(response.json()["reset_at"])


class TestAnonUsageEndpointBehavior:
    """Endpoint must reflect real Supabase state without mutating it."""

    @patch("api.middleware.anon_quota.supabase_post")
    @patch("api.middleware.anon_quota.supabase_patch")
    @patch("api.middleware.anon_quota.supabase_get")
    def test_existing_row_returns_actual_remaining(
        self, mock_get, mock_patch, mock_post
    ):
        from api.middleware.anon_quota import _today_utc
        mock_get.return_value = [{
            "ip_hash": "abc123",
            "usage_date": _today_utc(),
            "search_count": 12,
            "dna_count": 6,
        }]

        response = client.get("/api/anon-usage")
        data = response.json()

        assert data["search_remaining"] == 18  # 30 - 12
        assert data["dna_remaining"] == 4      # 10 - 6
        # Critical: this is a pure read — no DB writes allowed
        mock_patch.assert_not_called()
        mock_post.assert_not_called()

    @patch("api.middleware.anon_quota.supabase_post")
    @patch("api.middleware.anon_quota.supabase_patch")
    @patch("api.middleware.anon_quota.supabase_get")
    def test_no_today_row_returns_full_caps_without_writing(
        self, mock_get, mock_patch, mock_post
    ):
        """Under composite-PK schema, _fetch_anon_row's today-filter excludes
        prior-day rows at the DB level — get_anon_remaining never sees a
        stale row. No row for today reads as 'no usage today' and returns
        full caps. Crucially: this endpoint does NOT eagerly insert a row,
        because that would consume a write on every page load. The next
        real search/dna call does the insert."""
        mock_get.return_value = []  # today-filter returns no row

        response = client.get("/api/anon-usage")
        data = response.json()

        assert data["search_remaining"] == 30
        assert data["dna_remaining"] == 10
        mock_patch.assert_not_called()
        mock_post.assert_not_called()

    @patch("api.middleware.anon_quota.supabase_get")
    def test_kill_switch_returns_401(self, mock_get):
        with patch.dict(os.environ, {"ANON_ACCESS_ENABLED": "false"}):
            response = client.get("/api/anon-usage")
        assert response.status_code == 401
        mock_get.assert_not_called()
