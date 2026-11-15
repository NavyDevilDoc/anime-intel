"""
Global lockdown middleware.
When the 'global_lockdown' flag is set in the Supabase system_flags table,
all non-admin, non-health requests return 503 Service Unavailable.

The flag is cached in-memory with a short TTL to avoid hitting Supabase
on every single request. The cache refreshes every 10 seconds, meaning
a lockdown toggle takes effect across all requests within 10 seconds.
"""
import time

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from api.supabase_client import supabase_get

# Paths that must remain accessible during lockdown
LOCKDOWN_EXEMPT_PATHS = {
    "/api/health",
    "/api/admin/dashboard",
    "/api/admin/lockdown",
    "/docs",
    "/openapi.json",
}

# --- In-memory cache for lockdown flag ---
_lockdown_active: bool = False
_lockdown_checked_at: float = 0.0
_CACHE_TTL_SECONDS: float = 10.0


def _refresh_lockdown_flag() -> bool:
    """Fetch the global_lockdown flag from Supabase, with TTL caching."""
    global _lockdown_active, _lockdown_checked_at

    now = time.time()
    if now - _lockdown_checked_at < _CACHE_TTL_SECONDS:
        return _lockdown_active

    try:
        rows = supabase_get("system_flags", {
            "flag_name": "eq.global_lockdown",
            "select": "flag_value",
        })
        _lockdown_active = bool(rows and rows[0].get("flag_value", False))
    except Exception:  # noqa: BLE001 — fail open on Supabase transport failure; see inline comment
        # If Supabase is unreachable, fail open — don't lock out everyone
        # because the database is down.
        _lockdown_active = False

    _lockdown_checked_at = now
    return _lockdown_active


def reset_lockdown_cache() -> None:
    """Force the next request to re-read the flag from Supabase.
    Called after the admin toggles lockdown so it takes effect immediately."""
    global _lockdown_checked_at
    _lockdown_checked_at = 0.0


class LockdownMiddleware(BaseHTTPMiddleware):
    """Returns 503 for all non-exempt requests when global lockdown is active."""

    async def dispatch(self, request: Request, call_next):
        # Always allow CORS preflight
        if request.method == "OPTIONS":
            return await call_next(request)

        # Always allow exempt paths
        if request.url.path in LOCKDOWN_EXEMPT_PATHS:
            return await call_next(request)

        # Check lockdown flag (cached, refreshes every 10s)
        if _refresh_lockdown_flag():
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "Anime Intel is temporarily unavailable for maintenance. Please try again later."
                },
            )

        return await call_next(request)
