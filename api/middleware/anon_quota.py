"""Per-IP daily quota enforcement for anonymous users.

The security control that lets the three core discovery features
(Intelligent Search, DNA Triangulation, Encyclopedia) be public without
exposing the personal Google API key to abuse. Wired into
`api.routes.search`, `api.routes.dna`, and `api.routes.anon_usage`.

Design decisions:

  - Caps are env-tunable so an incident response can dial them down via
    Railway env vars without a redeploy. Defaults: 30 search / 10 DNA
    per UTC day per IP (see DEFAULT_SEARCH_CAP / DEFAULT_DNA_CAP
    below). Encyclopedia is uncapped (cheap to serve — pure SQLite).

  - ANON_ACCESS_ENABLED is a master kill switch. Setting it to "false"
    in Railway makes this module raise 401 on every call within seconds,
    restoring the auth wall behavior without touching code.

  - IP addresses are hashed with sha256 + a pinned salt before being
    written to Supabase. The salt lives in ANON_IP_SALT (Railway secret)
    and must NEVER rotate, because rotation would orphan all existing
    counters and give every attacker a free reset. A leaked anon_usage
    row reveals nothing about the original IP.

  - Counter storage uses a Supabase table (not in-memory) so Railway
    restarts and multi-worker deployments cannot reset counters under
    an attacker's feet.

  - Client IP extraction goes through api.middleware.client_ip, which
    prefers X-Real-IP and falls back to the LEFTMOST X-Forwarded-For
    entry (Railway's edge proxy overwrites both from the actual client
    IP it observes, so the leftmost XFF entry is un-spoofable in this
    topology). See that module for the full threat model — the common
    "rightmost XFF" advice doesn't apply here because there are TWO
    Railway proxies in front of the app, not one.

  - The check is a two-step GET-then-PATCH pattern. There is a small
    TOCTOU race window: two concurrent requests from the same IP could
    both read count=N and both increment, resulting in cap+1 effective
    requests in the worst case. This is a deliberate trade-off — true
    atomicity would require a Postgres RPC function, and a one-request
    overshoot is not a security failure for this threat model.

  - check_anon_quota returns AnonRemaining(search_remaining, dna_remaining)
    reflecting the post-increment state. Route handlers thread this into
    response bodies so the frontend's display stays in lockstep with the
    server. get_anon_remaining is the read-only twin used by the
    GET /api/anon-usage endpoint that seeds the frontend on page load.

Schema (composite PK as of PR #84, see
tools/supabase_setup_anon_usage_composite.sql for the migration):

    CREATE TABLE anon_usage (
      ip_hash       TEXT        NOT NULL,
      usage_date    DATE        NOT NULL,
      search_count  INT         NOT NULL DEFAULT 0,
      dna_count     INT         NOT NULL DEFAULT 0,
      last_seen     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      PRIMARY KEY (ip_hash, usage_date)
    );
    CREATE INDEX idx_anon_usage_date ON anon_usage(usage_date);
    ALTER TABLE anon_usage ENABLE ROW LEVEL SECURITY;
    -- No policies = deny all client access. Backend uses service_role,
    -- which bypasses RLS, so it still works server-side.

Composite PK on (ip_hash, usage_date) preserves prior-day rows
instead of overwriting them on day rollover. Quota lookups filter
to today's row only; historical rows feed the admin dashboard's
unique-IP analytics.
"""
import hashlib
import os
from datetime import datetime, timedelta, timezone
from typing import Literal, NamedTuple

from fastapi import HTTPException, Request

from api.middleware.client_ip import get_client_ip
from api.supabase_client import supabase_get, supabase_patch, supabase_post

# --- Defaults (overridable via Railway env vars on every call) ---
DEFAULT_SEARCH_CAP = 30
DEFAULT_DNA_CAP = 10
DEFAULT_ACCESS_ENABLED = "true"

QuotaType = Literal["search", "dna"]


class AnonRemaining(NamedTuple):
    """Post-action remaining counts for both quota types."""
    search_remaining: int
    dna_remaining: int


def _is_enabled() -> bool:
    """Master kill switch. Set ANON_ACCESS_ENABLED=false in Railway to wall everyone."""
    return os.environ.get("ANON_ACCESS_ENABLED", DEFAULT_ACCESS_ENABLED).lower() == "true"


def _get_cap(quota_type: QuotaType) -> int:
    """Read the current cap from env. Re-reads on every call so Railway tweaks land instantly."""
    if quota_type == "search":
        return int(os.environ.get("ANON_SEARCH_CAP", str(DEFAULT_SEARCH_CAP)))
    return int(os.environ.get("ANON_DNA_CAP", str(DEFAULT_DNA_CAP)))


def _get_salt() -> str:
    """
    The IP-hashing salt. Pinned per Railway env var.

    If ANON_IP_SALT is unset, we fall back to a fixed sentinel string so
    the module is still importable in test/dev environments. In production
    Railway must set this to a 32+ character random secret. A missing salt
    in production weakens the privacy guarantee but does not break the cap.
    """
    return os.environ.get("ANON_IP_SALT", "anon-quota-dev-salt-do-not-use-in-prod")


def _hash_ip(ip: str) -> str:
    """Salted SHA-256 of the client IP, truncated to 32 hex chars (128 bits)."""
    digest = hashlib.sha256((_get_salt() + ip).encode("utf-8")).hexdigest()
    return digest[:32]


def _today_utc() -> str:
    """Today's date in UTC as ISO string. Day boundaries are UTC, not local."""
    return datetime.now(timezone.utc).date().isoformat()


def _next_midnight_utc() -> str:
    """ISO timestamp of the next UTC midnight — used as the reset_at hint in 429s."""
    now = datetime.now(timezone.utc)
    tomorrow = now.date() + timedelta(days=1)
    midnight = datetime.combine(tomorrow, datetime.min.time(), tzinfo=timezone.utc)
    return midnight.isoformat()


def _raise_cap_reached(quota_type: QuotaType, cap: int) -> None:
    """Raise the structured 429 the frontend AnonLimitModal listens for."""
    raise HTTPException(
        status_code=429,
        detail={
            "detail": "Anonymous daily limit reached",
            "limit_type": quota_type,
            "cap": cap,
            "reset_at": _next_midnight_utc(),
            "upgrade_hint": "Create a free account for higher limits.",
        },
    )


def _raise_disabled() -> None:
    """Raise 401 when the master kill switch is off — mimics require_auth."""
    raise HTTPException(status_code=401, detail="Authentication required.")


def _fetch_anon_row(request: Request) -> tuple[str, dict | None]:
    """
    Look up today's anon_usage row for this request's IP.

    Composite PK is (ip_hash, usage_date) — query filters to today only.
    Historical rows for prior days exist alongside but are not quota
    state; the admin dashboard reads those separately for analytics.
    Pure read, no writes.
    """
    ip = get_client_ip(request)
    ip_hash = _hash_ip(ip)
    rows = supabase_get(
        "anon_usage",
        {
            "ip_hash": f"eq.{ip_hash}",
            "usage_date": f"eq.{_today_utc()}",
        },
    )
    return ip_hash, (rows[0] if rows else None)


def _remaining_from_counts(
    search_used: int, dna_used: int, search_cap: int, dna_cap: int
) -> AnonRemaining:
    """Compute remaining counts, floored at zero so over-cap reads don't go negative."""
    return AnonRemaining(
        max(0, search_cap - search_used),
        max(0, dna_cap - dna_used),
    )


def get_anon_remaining(request: Request) -> AnonRemaining:
    """
    Pure-read view of a caller's remaining anon quota for today.

    No DB writes, no row insertion. If the caller has no row for today,
    returns full caps — same logical state the next increment would
    start from. Prior-day rows exist but are not part of today's quota
    state, so they're ignored here.

    Used by GET /api/anon-usage so the frontend can show truthful
    counters without consuming any quota itself.
    """
    if not _is_enabled():
        _raise_disabled()
    search_cap = _get_cap("search")
    dna_cap = _get_cap("dna")
    _, row = _fetch_anon_row(request)
    if row is None:
        return _remaining_from_counts(0, 0, search_cap, dna_cap)
    return _remaining_from_counts(
        row.get("search_count", 0) or 0,
        row.get("dna_count", 0) or 0,
        search_cap,
        dna_cap,
    )


def check_anon_quota(request: Request, quota_type: QuotaType) -> AnonRemaining:
    """
    Verify (and atomically-ish increment) an anonymous user's daily quota.

    Call this from a route handler ONLY when get_current_user returned
    None — i.e. only on truly anonymous traffic. Authenticated users go
    through api/middleware/usage.check_quota instead.

    Raises:
        HTTPException 401 if ANON_ACCESS_ENABLED is "false".
        HTTPException 429 with structured detail if the daily cap is hit.

    Returns:
        AnonRemaining(search_remaining, dna_remaining) reflecting the
        post-increment state. Callers thread these into response bodies
        so the frontend's display stays in sync with the server.
    """
    if not _is_enabled():
        _raise_disabled()

    search_cap = _get_cap("search")
    dna_cap = _get_cap("dna")
    cap = search_cap if quota_type == "search" else dna_cap
    if cap < 1:
        # Cap of 0 is a valid soft-disable: deny without touching the DB.
        _raise_cap_reached(quota_type, cap)

    today = _today_utc()
    counter_field = f"{quota_type}_count"
    other_counter = "dna_count" if quota_type == "search" else "search_count"
    now_iso = datetime.now(timezone.utc).isoformat()

    ip_hash, row = _fetch_anon_row(request)

    if row is None:
        # No row for today — either first request ever from this IP, or
        # first request of the day (prior days are separate historical
        # rows under composite-PK schema and don't affect today's quota).
        # Fresh insert at count=1.
        supabase_post(
            "anon_usage",
            {
                "ip_hash": ip_hash,
                "usage_date": today,
                counter_field: 1,
                other_counter: 0,
                "last_seen": now_iso,
            },
        )
        if quota_type == "search":
            return _remaining_from_counts(1, 0, search_cap, dna_cap)
        return _remaining_from_counts(0, 1, search_cap, dna_cap)

    current = row.get(counter_field, 0) or 0
    if current >= cap:
        # At or over cap — reject without writing. Avoids running the
        # counter past the limit, which would just waste DB writes.
        _raise_cap_reached(quota_type, cap)

    # PATCH targets today's row specifically. Without usage_date in the
    # filter, this would PATCH every historical row for this IP — the
    # composite-PK schema makes the filter required, not optional.
    supabase_patch(
        "anon_usage",
        {
            counter_field: current + 1,
            "last_seen": now_iso,
        },
        {
            "ip_hash": f"eq.{ip_hash}",
            "usage_date": f"eq.{today}",
        },
    )

    # Compute remaining from the synthesized post-patch state.
    other_current = row.get(other_counter, 0) or 0
    if quota_type == "search":
        return _remaining_from_counts(current + 1, other_current, search_cap, dna_cap)
    return _remaining_from_counts(other_current, current + 1, search_cap, dna_cap)
