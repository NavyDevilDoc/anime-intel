"""
HTTP Basic Auth dependency for admin endpoints.

Replaces the Supabase JWT/JWKS verification stack that lived in auth.py.
Single-operator admin access via a username/password pair sourced from
Railway env vars (ADMIN_USERNAME / ADMIN_PASSWORD). Uses constant-time
comparison on both fields to defeat timing attacks.

Missing credentials produce 401 with `WWW-Authenticate: Basic`, which is
what triggers the browser's native credential dialog on first visit to
an admin route.

Usage:
    @router.get("/api/admin/dashboard")
    def admin_dashboard(user: str = Depends(verify_basic_auth)):
        ...

`user` is the authenticated username string — handy for audit columns
(e.g., the lockdown toggle records who flipped it).
"""
import os
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

_security = HTTPBasic()
_REALM = 'Basic realm="Admin"'


def verify_basic_auth(
    credentials: HTTPBasicCredentials = Depends(_security),  # noqa: B008 — FastAPI-idiomatic dependency injection; `Depends(...)` must be a call in the default so the DI resolver sees it
) -> str:
    """
    Verify HTTP Basic Auth credentials against ADMIN_USERNAME / ADMIN_PASSWORD.

    Returns:
        The authenticated username (for audit logging in handlers that record
        who took a destructive action).

    Raises:
        HTTPException 500 if the env vars are unset — fail closed rather than
        leaving admin endpoints reachable on a misconfigured deploy.
        HTTPException 401 with WWW-Authenticate: Basic on any credential
        mismatch, which prompts browsers to re-display the credential dialog.
    """
    expected_username = os.environ.get("ADMIN_USERNAME", "")
    expected_password = os.environ.get("ADMIN_PASSWORD", "")

    if not expected_username or not expected_password:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Admin credentials are not configured.",
        )

    # compare_digest works on bytes — encode both sides as UTF-8 first.
    username_ok = secrets.compare_digest(
        credentials.username.encode("utf-8"),
        expected_username.encode("utf-8"),
    )
    password_ok = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        expected_password.encode("utf-8"),
    )

    # AND the two booleans AFTER both compares run. Short-circuiting on the
    # first mismatch would leak which field was wrong via response timing.
    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials.",
            headers={"WWW-Authenticate": _REALM},
        )

    return credentials.username
