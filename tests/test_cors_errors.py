"""CORS header propagation across all response paths.

Regression test for the diagnostic visibility issue surfaced in PR #60:
unhandled exceptions used to be caught by Starlette's outermost
ServerErrorMiddleware (outside CORS), so the 500 response was missing
Access-Control-Allow-Origin and browsers reported "CORS error" instead
of exposing the real status to the frontend JS. That's what hid the
SearchFeedback 400 for as long as it did.

The fix in api/main.py is a global @app.exception_handler(Exception)
that runs inside the middleware stack, ensuring CORS wraps the response
on the way out. This file locks the behavior in for success, redirect-y
error paths (401/422), and unhandled exception paths.
"""
import base64
import os
import sys

TESTS_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT_DIR, "env_variables.env"))

os.environ["TESTING"] = "1"
# Force the test credentials regardless of what's in the local env_variables.env —
# the pre-encoded _ADMIN_AUTH_HEADER below relies on exact-match.
os.environ["ADMIN_USERNAME"] = "test-admin"
os.environ["ADMIN_PASSWORD"] = "test-password"

from unittest.mock import patch
from fastapi.testclient import TestClient
from api.main import app

# Patch check_anon_quota so offline CORS tests don't hit Supabase.
# Must return an AnonRemaining tuple so route handlers that thread the
# remaining counts into response bodies don't crash on the bypass path.
# Module-level patch.start() persists across the test session; the LAST
# module to call .start() on a given target wins, so this value must
# match the bypass shape used by test_api.py to avoid order-dependent
# breakage when both files are collected together.
from api.middleware.anon_quota import AnonRemaining
_BYPASS_REMAINING = AnonRemaining(30, 10)
for _mod in ("api.routes.search", "api.routes.dna"):
    patch(f"{_mod}.check_anon_quota", return_value=_BYPASS_REMAINING).start()

# raise_server_exceptions=False so TestClient returns the actual response
# (500 + JSON body) instead of re-raising the exception to the test code.
# That's what a real browser sees, and what we need to inspect headers on.
client = TestClient(app, raise_server_exceptions=False)

_ALLOWED_ORIGIN = "https://animeintel.app"
_ORIGIN_HEADER = {"Origin": _ALLOWED_ORIGIN}
_ADMIN_AUTH_HEADER = {
    "Authorization": "Basic " + base64.b64encode(b"test-admin:test-password").decode(),
}
_CORS_HEADER_KEY = "access-control-allow-origin"


def test_200_success_includes_cors_header():
    """Sanity: success responses carry CORS headers."""
    resp = client.get("/api/health", headers=_ORIGIN_HEADER)
    assert resp.status_code == 200
    assert resp.headers.get(_CORS_HEADER_KEY) == _ALLOWED_ORIGIN


def test_401_unauthorized_includes_cors_header():
    """401 responses (HTTPException path) carry CORS headers."""
    # Scope env vars per-test — test_basic_auth.py clears these to verify
    # fail-closed behavior, and pytest doesn't always restore the module-level
    # state we set at import time before subsequent test files run.
    with patch.dict(os.environ, {
        "ADMIN_USERNAME": "test-admin",
        "ADMIN_PASSWORD": "test-password",
    }):
        resp = client.get("/api/admin/dashboard", headers=_ORIGIN_HEADER)
    assert resp.status_code == 401
    assert resp.headers.get(_CORS_HEADER_KEY) == _ALLOWED_ORIGIN


def test_422_validation_includes_cors_header():
    """422 validation responses (RequestValidationError path) carry CORS headers."""
    resp = client.post(
        "/api/search-feedback",
        json={"search_id": "valid-search-id", "rating": "NOT_VALID"},
        headers=_ORIGIN_HEADER,
    )
    assert resp.status_code == 422
    assert resp.headers.get(_CORS_HEADER_KEY) == _ALLOWED_ORIGIN


def test_unhandled_exception_500_includes_cors_header():
    """
    Core regression test: when a route raises an unhandled exception (e.g.,
    httpx.HTTPStatusError from a supabase_* helper), the resulting 500 must
    carry CORS headers so the frontend can read the response body.

    Before the fix, this exception propagated past CORS to Starlette's
    ServerErrorMiddleware, which returned a 500 without headers — surfacing
    as "CORS error" in the browser.
    """
    # Scope env vars per-test — test_basic_auth.py clears these to verify
    # fail-closed behavior. Without this, the auth step 500s with "Admin
    # credentials are not configured." BEFORE the route runs, and we'd never
    # exercise the unhandled-exception path the test is meant to validate.
    with patch.dict(os.environ, {
        "ADMIN_USERNAME": "test-admin",
        "ADMIN_PASSWORD": "test-password",
    }):
        with patch(
            "api.routes.admin.supabase_count",
            side_effect=RuntimeError("forced exception for CORS test"),
        ):
            resp = client.get(
                "/api/admin/dashboard",
                headers={**_ORIGIN_HEADER, **_ADMIN_AUTH_HEADER},
            )
    assert resp.status_code == 500
    assert resp.json() == {"detail": "Internal Server Error"}
    assert resp.headers.get(_CORS_HEADER_KEY) == _ALLOWED_ORIGIN
