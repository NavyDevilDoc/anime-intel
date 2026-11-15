"""
Tests for the HTTP Basic Auth dependency that gates admin endpoints.

Verifies:
  - Missing creds → 401 with WWW-Authenticate: Basic (so browsers prompt)
  - Wrong creds → 401 (timing-safe; doesn't leak which field is wrong)
  - Correct creds → returns the authenticated username
  - Missing env vars → 500 (fail closed; never silently open admin endpoints)

Run: pytest tests/test_basic_auth.py -v
"""
import os
import sys
import base64
import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient

TESTS_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from api.middleware.basic_auth import verify_basic_auth


# -----------------------------------------------------------------------------
# Test fixtures: minimal app with one endpoint behind verify_basic_auth.
# -----------------------------------------------------------------------------

def _build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/protected")
    def protected(user: str = Depends(verify_basic_auth)):
        return {"user": user}

    return app


def _basic_header(username: str, password: str) -> dict:
    creds = f"{username}:{password}".encode()
    return {"Authorization": "Basic " + base64.b64encode(creds).decode()}


# -----------------------------------------------------------------------------
# With creds configured
# -----------------------------------------------------------------------------

class TestBasicAuthWithCredsConfigured:
    """Env vars are set; verify the happy and unhappy paths."""

    def setup_method(self):
        os.environ["ADMIN_USERNAME"] = "expected-user"
        os.environ["ADMIN_PASSWORD"] = "expected-pass"
        self.client = TestClient(_build_app())

    def teardown_method(self):
        os.environ.pop("ADMIN_USERNAME", None)
        os.environ.pop("ADMIN_PASSWORD", None)

    def test_missing_auth_returns_401_with_www_authenticate(self):
        resp = self.client.get("/protected")
        assert resp.status_code == 401
        # Browser uses this header to decide to prompt for credentials.
        assert "Basic" in resp.headers.get("www-authenticate", "")

    def test_wrong_password_returns_401(self):
        resp = self.client.get("/protected", headers=_basic_header("expected-user", "wrong-pass"))
        assert resp.status_code == 401

    def test_wrong_username_returns_401(self):
        resp = self.client.get("/protected", headers=_basic_header("wrong-user", "expected-pass"))
        assert resp.status_code == 401

    def test_correct_credentials_return_200_with_username(self):
        resp = self.client.get("/protected", headers=_basic_header("expected-user", "expected-pass"))
        assert resp.status_code == 200
        assert resp.json() == {"user": "expected-user"}

    def test_malformed_bearer_token_returns_401(self):
        """A Bearer token is not Basic Auth — it should fail the credential check."""
        resp = self.client.get("/protected", headers={"Authorization": "Bearer some-token"})
        assert resp.status_code == 401

    def test_garbage_authorization_value_returns_401(self):
        resp = self.client.get("/protected", headers={"Authorization": "not-a-real-scheme"})
        assert resp.status_code == 401


# -----------------------------------------------------------------------------
# With creds NOT configured — fail closed
# -----------------------------------------------------------------------------

class TestBasicAuthFailsClosedOnMisconfiguration:
    """If env vars are unset, never allow access. 500 > silently exposing /admin."""

    def setup_method(self):
        # Ensure both vars are absent
        os.environ.pop("ADMIN_USERNAME", None)
        os.environ.pop("ADMIN_PASSWORD", None)
        self.client = TestClient(_build_app())

    def test_missing_env_returns_500_even_with_correct_looking_creds(self):
        resp = self.client.get("/protected", headers=_basic_header("admin", "anything"))
        assert resp.status_code == 500
        assert "not configured" in resp.json()["detail"].lower()

    def test_missing_env_returns_500_with_no_creds_too(self):
        # No Authorization header at all. FastAPI's HTTPBasic dep returns 401
        # for missing creds before our code runs — that's fine. The important
        # invariant is: admin endpoint is never reachable without creds.
        resp = self.client.get("/protected")
        assert resp.status_code in (401, 500)
        assert resp.status_code != 200


# -----------------------------------------------------------------------------
# Partial config — only one env var set
# -----------------------------------------------------------------------------

class TestBasicAuthFailsClosedOnPartialConfig:
    """Either var missing should also fail closed."""

    def teardown_method(self):
        os.environ.pop("ADMIN_USERNAME", None)
        os.environ.pop("ADMIN_PASSWORD", None)

    def test_only_username_set_returns_500(self):
        os.environ["ADMIN_USERNAME"] = "admin"
        os.environ.pop("ADMIN_PASSWORD", None)
        client = TestClient(_build_app())
        resp = client.get("/protected", headers=_basic_header("admin", "anything"))
        assert resp.status_code == 500

    def test_only_password_set_returns_500(self):
        os.environ.pop("ADMIN_USERNAME", None)
        os.environ["ADMIN_PASSWORD"] = "secret"
        client = TestClient(_build_app())
        resp = client.get("/protected", headers=_basic_header("anything", "secret"))
        assert resp.status_code == 500
