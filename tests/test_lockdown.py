"""
Tests for the global lockdown middleware.

Tests the middleware in isolation with its own FastAPI app instances
to avoid interference with the shared test client in test_api.py.
"""
import os
import sys
import pytest
from unittest.mock import patch
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

TESTS_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from api.middleware.lockdown import (
    LockdownMiddleware,
    LOCKDOWN_EXEMPT_PATHS,
    reset_lockdown_cache,
    _CACHE_TTL_SECONDS,
)


def _make_app(lockdown_flag: bool = False):
    """Create a minimal FastAPI app with LockdownMiddleware for testing."""
    test_app = FastAPI()
    test_app.add_middleware(LockdownMiddleware)

    @test_app.get("/api/health")
    def health():
        return {"status": "ok"}

    @test_app.get("/api/admin/dashboard")
    def admin_dashboard():
        return {"metrics": "ok"}

    @test_app.get("/api/admin/lockdown")
    def admin_lockdown():
        return {"lockdown_active": lockdown_flag}

    @test_app.post("/api/admin/lockdown")
    def toggle_lockdown():
        return {"lockdown_active": True}

    @test_app.get("/api/search")
    def search():
        return {"results": []}

    @test_app.post("/api/search")
    def search_post():
        return {"results": []}

    @test_app.get("/api/dna")
    def dna():
        return {"results": []}

    @test_app.get("/api/encyclopedia")
    def encyclopedia():
        return {"results": []}

    return test_app


class TestLockdownMiddleware:
    """Verify the lockdown middleware blocks non-exempt routes when active."""

    def setup_method(self):
        reset_lockdown_cache()

    def teardown_method(self):
        reset_lockdown_cache()

    @patch("api.middleware.lockdown.supabase_get", return_value=[{"flag_value": True}])
    def test_lockdown_blocks_search(self, mock_get):
        app = _make_app()
        client = TestClient(app)
        resp = client.get("/api/search")
        assert resp.status_code == 503
        assert "maintenance" in resp.json()["detail"].lower()

    @patch("api.middleware.lockdown.supabase_get", return_value=[{"flag_value": True}])
    def test_lockdown_blocks_post_search(self, mock_get):
        app = _make_app()
        client = TestClient(app)
        resp = client.post("/api/search", json={"query": "test"})
        assert resp.status_code == 503

    @patch("api.middleware.lockdown.supabase_get", return_value=[{"flag_value": True}])
    def test_lockdown_blocks_dna(self, mock_get):
        app = _make_app()
        client = TestClient(app)
        resp = client.get("/api/dna")
        assert resp.status_code == 503

    @patch("api.middleware.lockdown.supabase_get", return_value=[{"flag_value": True}])
    def test_lockdown_blocks_encyclopedia(self, mock_get):
        app = _make_app()
        client = TestClient(app)
        resp = client.get("/api/encyclopedia")
        assert resp.status_code == 503

    @patch("api.middleware.lockdown.supabase_get", return_value=[{"flag_value": False}])
    def test_no_lockdown_allows_search(self, mock_get):
        app = _make_app()
        client = TestClient(app)
        resp = client.get("/api/search")
        assert resp.status_code == 200

    @patch("api.middleware.lockdown.supabase_get", return_value=[])
    def test_empty_flags_table_allows_requests(self, mock_get):
        app = _make_app()
        client = TestClient(app)
        resp = client.get("/api/search")
        assert resp.status_code == 200


class TestLockdownExemptPaths:
    """Verify that admin and health routes remain accessible during lockdown."""

    def setup_method(self):
        reset_lockdown_cache()

    def teardown_method(self):
        reset_lockdown_cache()

    @patch("api.middleware.lockdown.supabase_get", return_value=[{"flag_value": True}])
    def test_health_exempt_during_lockdown(self, mock_get):
        app = _make_app()
        client = TestClient(app)
        resp = client.get("/api/health")
        assert resp.status_code == 200

    @patch("api.middleware.lockdown.supabase_get", return_value=[{"flag_value": True}])
    def test_admin_dashboard_exempt_during_lockdown(self, mock_get):
        app = _make_app()
        client = TestClient(app)
        resp = client.get("/api/admin/dashboard")
        assert resp.status_code == 200

    @patch("api.middleware.lockdown.supabase_get", return_value=[{"flag_value": True}])
    def test_admin_lockdown_get_exempt(self, mock_get):
        app = _make_app()
        client = TestClient(app)
        resp = client.get("/api/admin/lockdown")
        assert resp.status_code == 200

    @patch("api.middleware.lockdown.supabase_get", return_value=[{"flag_value": True}])
    def test_admin_lockdown_post_exempt(self, mock_get):
        app = _make_app()
        client = TestClient(app)
        resp = client.post("/api/admin/lockdown", json={"enabled": False})
        assert resp.status_code == 200

    @patch("api.middleware.lockdown.supabase_get", return_value=[{"flag_value": True}])
    def test_options_exempt_during_lockdown(self, mock_get):
        """CORS preflight must always pass through."""
        app = _make_app()
        client = TestClient(app)
        resp = client.options("/api/search")
        # OPTIONS on a route without explicit handling returns 405,
        # but the important thing is it's NOT 503 (not blocked by lockdown).
        assert resp.status_code != 503


class TestLockdownFailOpen:
    """Verify the middleware fails open when Supabase is unreachable."""

    def setup_method(self):
        reset_lockdown_cache()

    def teardown_method(self):
        reset_lockdown_cache()

    @patch("api.middleware.lockdown.supabase_get", side_effect=Exception("Supabase down"))
    def test_supabase_failure_allows_requests(self, mock_get):
        """If Supabase is unreachable, don't lock everyone out."""
        app = _make_app()
        client = TestClient(app)
        resp = client.get("/api/search")
        assert resp.status_code == 200


class TestLockdownCache:
    """Verify the TTL cache behavior."""

    def setup_method(self):
        reset_lockdown_cache()

    def teardown_method(self):
        reset_lockdown_cache()

    @patch("api.middleware.lockdown.supabase_get", return_value=[{"flag_value": False}])
    def test_cache_prevents_repeated_db_calls(self, mock_get):
        app = _make_app()
        client = TestClient(app)
        # First request populates cache
        client.get("/api/search")
        # Second request should use cache
        client.get("/api/search")
        # Only one Supabase call (cached on second)
        assert mock_get.call_count == 1

    @patch("api.middleware.lockdown.supabase_get", return_value=[{"flag_value": True}])
    def test_reset_cache_forces_refresh(self, mock_get):
        app = _make_app()
        client = TestClient(app)
        # First request populates cache
        client.get("/api/search")
        assert mock_get.call_count == 1
        # Reset cache
        reset_lockdown_cache()
        # Next request must re-read from Supabase
        client.get("/api/search")
        assert mock_get.call_count == 2

    def test_exempt_paths_include_critical_routes(self):
        """Ensure all admin and health paths are in the exempt set."""
        assert "/api/health" in LOCKDOWN_EXEMPT_PATHS
        assert "/api/admin/dashboard" in LOCKDOWN_EXEMPT_PATHS
        assert "/api/admin/lockdown" in LOCKDOWN_EXEMPT_PATHS
