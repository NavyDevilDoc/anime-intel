"""
In-memory sliding window rate limiter.
Two tiers: per-IP sustained rate + burst protection.
Authenticated users get higher limits (keyed by user ID from JWT).
No external dependencies (no Redis needed at this scale).

IP extraction goes through api.middleware.client_ip.get_client_ip so the
"per-IP" key is the actual client IP, not the Railway proxy IP. See that
module's docstring for the rightmost-XFF reasoning.
"""
import time
from collections import defaultdict

import jwt
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from api.middleware.client_ip import get_client_ip

# --- DEFAULT CONFIGURATION ---
DEFAULT_SUSTAINED_LIMIT = 30        # Anonymous: max requests per window
DEFAULT_AUTH_SUSTAINED_LIMIT = 60   # Authenticated: max requests per window
DEFAULT_SUSTAINED_WINDOW = 60       # Window size in seconds (1 minute)
DEFAULT_BURST_LIMIT = 5             # Max requests per burst window
DEFAULT_BURST_WINDOW = 1            # Burst window in seconds

# Paths exempt from rate limiting
EXEMPT_PATHS = {"/api/health", "/docs", "/openapi.json"}


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Sliding window rate limiter keyed by client IP.
    Returns 429 Too Many Requests with Retry-After header on violation.
    """

    def __init__(self, app, sustained_limit=None, auth_sustained_limit=None,
                 sustained_window=None, burst_limit=None, burst_window=None):
        super().__init__(app)
        self.sustained_limit = sustained_limit or DEFAULT_SUSTAINED_LIMIT
        self.auth_sustained_limit = auth_sustained_limit or DEFAULT_AUTH_SUSTAINED_LIMIT
        self.sustained_window = sustained_window or DEFAULT_SUSTAINED_WINDOW
        self.burst_limit = burst_limit or DEFAULT_BURST_LIMIT
        self.burst_window = burst_window or DEFAULT_BURST_WINDOW
        self._requests = defaultdict(list)

    def _clean_window(self, timestamps, window):
        """Remove timestamps outside the sliding window."""
        cutoff = time.time() - window
        return [t for t in timestamps if t > cutoff]

    def _extract_user_id(self, request: Request) -> str | None:
        """Try to extract user ID from JWT in Authorization header (best-effort, no validation)."""
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return None
        try:
            # Decode without verification — just to extract the subject claim
            # for rate limiting keying. Full validation happens in the auth middleware.
            payload = jwt.decode(auth[7:], options={"verify_signature": False})
            return payload.get("sub")
        except Exception:  # noqa: BLE001 — malformed / corrupt / expired JWTs all degrade to anonymous keying; auth middleware still enforces validity downstream
            return None

    async def dispatch(self, request: Request, call_next):
        # Skip CORS preflight requests — let CORSMiddleware handle them
        if request.method == "OPTIONS":
            return await call_next(request)

        # Skip rate limiting for exempt paths
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        # Key by user ID if authenticated, otherwise by real client IP
        # (proxy-aware — see api.middleware.client_ip).
        user_id = self._extract_user_id(request)
        client_ip = get_client_ip(request)
        rate_key = f"user:{user_id}" if user_id else f"ip:{client_ip}"
        limit = self.auth_sustained_limit if user_id else self.sustained_limit

        now = time.time()

        # Clean and check sustained rate
        self._requests[rate_key] = self._clean_window(
            self._requests[rate_key], self.sustained_window
        )

        # Burst check: count requests in the last burst_window seconds
        recent = [t for t in self._requests[rate_key] if t > now - self.burst_window]
        if len(recent) >= self.burst_limit:
            return JSONResponse(
                status_code=429,
                content={"detail": f"Too many requests. Max {self.burst_limit} per second."},
                headers={"Retry-After": str(self.burst_window)},
            )

        # Sustained rate check
        if len(self._requests[rate_key]) >= limit:
            oldest = self._requests[rate_key][0]
            retry_after = int(oldest + self.sustained_window - now) + 1
            return JSONResponse(
                status_code=429,
                content={"detail": f"Rate limit exceeded. Max {limit} requests per minute."},
                headers={"Retry-After": str(max(1, retry_after))},
            )

        # Record this request
        self._requests[rate_key].append(now)

        return await call_next(request)
