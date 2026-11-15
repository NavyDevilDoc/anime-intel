"""
SecurityHeadersMiddleware — HSTS + X-Content-Type-Options injection.

Closes the API Sentinel transport-layer audit finding:
  CRITICAL — Strict-Transport-Security header is missing

Coverage:
  - nosniff always present (any transport, every endpoint, error responses)
  - HSTS gated on HTTPS per RFC 6797 (Section 7.2):
      * Present when X-Forwarded-Proto=https (Railway's edge proxy contract)
      * Absent on plain HTTP — local dev correctness, RFC compliance
  - Exact header values match what API Sentinel expects to see
  - Headers reach error paths (404 from Starlette's fallback router)
  - Headers reach CORS preflight responses (which CORS handles internally
    without invoking the inner middleware chain — proves the middleware
    really is outermost)

Run: pytest tests/test_security_headers.py -v
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

os.environ["TESTING"] = "1"

from api.main import app

client = TestClient(app)


# =====================================================================
# nosniff — always present
# =====================================================================

class TestNosniff:
    """X-Content-Type-Options: nosniff is emitted on every response,
    regardless of transport or response status."""

    def test_nosniff_present_on_health(self):
        response = client.get("/api/health")
        assert response.headers.get("x-content-type-options") == "nosniff"

    def test_nosniff_present_on_404(self):
        """Header must reach error responses produced by Starlette's
        fallback — confirms the middleware wraps the entire stack."""
        response = client.get("/api/this-route-does-not-exist")
        assert response.status_code == 404
        assert response.headers.get("x-content-type-options") == "nosniff"

    def test_nosniff_present_on_anon_usage_get(self):
        """Smoke check on the new endpoint introduced in PR #64."""
        with patch("api.middleware.anon_quota.supabase_get", return_value=[]):
            response = client.get("/api/anon-usage")
        assert response.status_code == 200
        assert response.headers.get("x-content-type-options") == "nosniff"

    def test_nosniff_value_is_exact(self):
        """API Sentinel checks for the exact 'nosniff' token — typos
        like 'nosniff;' or 'no-sniff' would re-trigger the finding."""
        response = client.get("/api/health")
        assert response.headers["x-content-type-options"] == "nosniff"


# =====================================================================
# HSTS — present only when the request was over HTTPS (RFC 6797)
# =====================================================================

class TestHSTSGating:
    """HSTS is bound by RFC 6797 section 7.2 to HTTPS responses only.
    The middleware checks X-Forwarded-Proto (Railway's edge contract) and
    falls back to the ASGI scope's scheme for direct-TLS deployments."""

    def test_hsts_absent_without_forwarded_proto(self):
        """Local dev / direct HTTP — no HSTS, per RFC."""
        response = client.get("/api/health")
        assert "strict-transport-security" not in response.headers

    def test_hsts_present_with_forwarded_proto_https(self):
        """Railway sets X-Forwarded-Proto=https — HSTS must emit."""
        response = client.get(
            "/api/health",
            headers={"X-Forwarded-Proto": "https"},
        )
        assert "strict-transport-security" in response.headers

    def test_hsts_absent_with_forwarded_proto_http(self):
        """Explicit http proto — still no HSTS."""
        response = client.get(
            "/api/health",
            headers={"X-Forwarded-Proto": "http"},
        )
        assert "strict-transport-security" not in response.headers

    def test_hsts_uses_leftmost_proto_in_chain(self):
        """If a future proxy adds a second X-Forwarded-Proto entry, the
        leftmost is what the outermost trusted proxy observed — same
        trust model as client_ip. 'http' on the right (a sub-proxy that
        terminated TLS internally) must NOT downgrade the policy."""
        response = client.get(
            "/api/health",
            headers={"X-Forwarded-Proto": "https, http"},
        )
        assert "strict-transport-security" in response.headers

    def test_hsts_case_insensitive_proto_value(self):
        """Some proxies emit 'HTTPS' uppercase; we lowercase before comparing."""
        response = client.get(
            "/api/health",
            headers={"X-Forwarded-Proto": "HTTPS"},
        )
        assert "strict-transport-security" in response.headers


# =====================================================================
# HSTS — exact value (API Sentinel will re-test this)
# =====================================================================

class TestHSTSValue:
    """The audit tool inspects the header value, not just presence.
    Locking the exact string here prevents accidental drift (e.g.,
    accidentally adding 'preload' which is hard to walk back)."""

    def test_hsts_value_max_age_one_year_with_subdomains(self):
        response = client.get(
            "/api/health",
            headers={"X-Forwarded-Proto": "https"},
        )
        assert response.headers["strict-transport-security"] == \
            "max-age=31536000; includeSubDomains"

    def test_hsts_does_not_include_preload(self):
        """preload commits to the HSTS preload list — months to reverse.
        Explicitly verify it's absent to prevent future accidents."""
        response = client.get(
            "/api/health",
            headers={"X-Forwarded-Proto": "https"},
        )
        assert "preload" not in response.headers["strict-transport-security"]


# =====================================================================
# HSTS — reaches error responses
# =====================================================================

class TestHSTSOnErrors:
    """HSTS must land on every response, not just successes. A 404 or
    405 served over HTTPS still needs the header so the browser remembers
    the policy for subsequent requests."""

    def test_hsts_present_on_404_when_https(self):
        response = client.get(
            "/api/this-route-does-not-exist",
            headers={"X-Forwarded-Proto": "https"},
        )
        assert response.status_code == 404
        assert "strict-transport-security" in response.headers

    def test_hsts_present_on_405_when_https(self):
        """Wrong method against an existing route — Starlette's response."""
        response = client.delete(
            "/api/health",
            headers={"X-Forwarded-Proto": "https"},
        )
        assert response.status_code == 405
        assert "strict-transport-security" in response.headers


# =====================================================================
# Outermost-middleware property — preflight responses carry headers
# =====================================================================

class TestPreflightHeaders:
    """CORS handles OPTIONS preflight internally without invoking the
    inner middleware chain. The only way security headers can appear on
    preflight is if SecurityHeadersMiddleware sits OUTSIDE CORS in the
    add_middleware order. This test would fail if someone reorders the
    stack and accidentally puts CORS outermost again."""

    def test_nosniff_on_cors_preflight(self):
        response = client.options(
            "/api/health",
            headers={
                "Origin": "https://animeintel.app",
                "Access-Control-Request-Method": "GET",
            },
        )
        # Preflight returns 200 or 204 from CORSMiddleware
        assert response.status_code in (200, 204)
        assert response.headers.get("x-content-type-options") == "nosniff"

    def test_hsts_on_cors_preflight_when_https(self):
        response = client.options(
            "/api/health",
            headers={
                "Origin": "https://animeintel.app",
                "Access-Control-Request-Method": "GET",
                "X-Forwarded-Proto": "https",
            },
        )
        assert response.status_code in (200, 204)
        assert "strict-transport-security" in response.headers
