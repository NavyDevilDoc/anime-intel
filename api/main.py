"""
FastAPI application entry point.
Serves the Anime Intelligence Vault recommendation API.
"""
import os
import sys
import traceback

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.middleware.lockdown import LockdownMiddleware
from api.middleware.rate_limit import RateLimitMiddleware
from api.middleware.security_headers import SecurityHeadersMiddleware
from api.routes import (
    admin,
    anon_usage,
    autocomplete,
    contact,
    controversy_landscape,
    dna,
    encyclopedia,
    feedback,
    health,
    search,
    spotlight,
    watchlist,
)


class CatchUnhandledExceptionsMiddleware:
    """
    Pure-ASGI middleware that catches any exception escaping the inner app
    and returns a JSON 500 response in its place.

    Why this exists (and why it's a class, not @app.middleware("http")):
    - Starlette's outermost ServerErrorMiddleware catches unhandled exceptions
      but sits OUTSIDE CORS, so its 500 response never picks up CORS headers.
      Browsers then report the failure as "CORS error" in the Network tab,
      hiding the real status from the frontend JS. That's what hid the
      SearchFeedback 400 for as long as it did (PR #60 Phase A diagnosis).
    - @app.exception_handler(Exception) doesn't help — Starlette specifically
      routes handlers registered for `Exception` to ServerErrorMiddleware (the
      outermost), so the response still escapes CORS.
    - @app.middleware("http") uses BaseHTTPMiddleware which has a known
      async-context exception-propagation bug: exceptions raised by the inner
      app sometimes don't reach the wrapping try/except.
    - A pure ASGI middleware class catches reliably and, when added AFTER
      CORSMiddleware via add_middleware(), sits inside CORS's send-wrapper —
      so the 500 it generates picks up Access-Control-Allow-Origin on the way
      out, and the browser can read the response body.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def _send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except Exception as exc:  # noqa: BLE001 — catching the entire class is the whole point of this middleware (see class docstring above)
            path = scope.get("path", "<unknown>")
            method = scope.get("method", "<unknown>")
            print(
                f"[unhandled] {method} {path}: {type(exc).__name__}: {exc}",
                flush=True,
            )
            traceback.print_exception(
                type(exc), exc, exc.__traceback__, file=sys.stderr
            )
            if not response_started:
                response = JSONResponse(
                    status_code=500,
                    content={"detail": "Internal Server Error"},
                )
                await response(scope, receive, send)
            # Swallow the exception — Starlette's ServerErrorMiddleware would
            # otherwise overwrite our response with its own header-less 500.

app = FastAPI(
    title="Anime Intelligence Vault API",
    description="Semantic search and recommendation engine for anime discovery.",
    version="1.0.0",
)

# CORS — allow local dev tools and future React frontend.
# The Railway API URL itself doesn't need CORS (same-origin requests
# from the /docs page work without it). CORS is for cross-origin
# requests from a React frontend on a different domain.
ALLOWED_ORIGINS = [
    "http://localhost:8501",    # Local Streamlit
    "http://localhost:3000",    # Local React dev
    "http://localhost:5173",    # Vite dev server
    "https://animeintel.app",  # Production (custom domain)
    "https://www.animeintel.app",  # Production (www redirect)
]
# Add production frontend URL from env if set (supports legacy Vercel domain)
_frontend_url = os.environ.get("FRONTEND_URL")
if _frontend_url and _frontend_url not in ALLOWED_ORIGINS:
    ALLOWED_ORIGINS.append(_frontend_url)

# IMPORTANT — middleware order:
#
# Starlette's `add_middleware` PREPENDS to the stack (insert(0, ...)), so the
# LAST middleware added ends up OUTERMOST.
#
# Two correctness requirements drive this order:
#  1. CORSMiddleware must wrap every error-producing middleware (lockdown 503,
#     rate-limit 429, the unhandled-exception 500) so their responses carry
#     Access-Control-Allow-Origin and the browser can read the body.
#  2. SecurityHeadersMiddleware must wrap CORS, because CORS handles OPTIONS
#     preflight internally and returns the preflight response WITHOUT invoking
#     the inner chain — so security headers added inside CORS would miss
#     preflight. Placing security headers outermost gets HSTS + nosniff onto
#     every single outgoing response, preflight included.
#
# Final stack (outermost first): SecurityHeaders -> CORS -> RateLimit -> Lockdown -> CatchUnhandled -> app
#
# Add order below: inner-most first, security headers LAST.

# Unhandled-exception catcher — innermost user middleware. Sits inside CORS,
# Lockdown, and RateLimit. Catches anything escaping the routes (e.g.,
# httpx.HTTPStatusError from supabase_*) and returns a JSON 500. Without this,
# such exceptions bubble up to Starlette's outermost ServerErrorMiddleware,
# whose 500 response never picks up CORS headers — browsers then surface the
# failure as "CORS error" instead of exposing the real status to JS.
app.add_middleware(CatchUnhandledExceptionsMiddleware)

# Global lockdown — returns 503 for all non-admin requests when active.
# Disabled during testing to avoid interference with other test suites.
if not os.environ.get("TESTING"):
    app.add_middleware(LockdownMiddleware)

# Rate limiting — per-IP sliding window.
# Disabled during testing via TESTING env var to avoid cross-test interference.
if not os.environ.get("TESTING"):
    app.add_middleware(RateLimitMiddleware)

# CORS — wraps every other middleware's responses so their error responses
# carry CORS headers and the browser can read the body.
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=r"http://localhost:\d+",
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
    expose_headers=[],
)

# Security headers — added LAST so it's OUTERMOST. Injects HSTS (HTTPS only,
# per RFC 6797) and X-Content-Type-Options on every outgoing response,
# including CORS preflight 204s that never enter the inner chain.
app.add_middleware(SecurityHeadersMiddleware)

# Route registration
app.include_router(health.router)
app.include_router(search.router)
app.include_router(dna.router)
app.include_router(encyclopedia.router)
app.include_router(contact.router)
app.include_router(feedback.router)
app.include_router(admin.router)
app.include_router(anon_usage.router)
app.include_router(watchlist.router)
app.include_router(autocomplete.router)
app.include_router(spotlight.router)
app.include_router(controversy_landscape.router)
