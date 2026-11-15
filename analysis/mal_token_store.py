"""Supabase-backed singleton store for the project's MAL OAuth tokens.

Replaces the disk-based token_data.json write target on Railway, where
the writable filesystem layer is ephemeral and gets wiped on every
container restart. Source of truth for the MAL token in production;
local dev keeps the env / disk fallback chain intact.

CONTRACT
--------
MAL's `POST /v1/oauth2/token grant_type=refresh_token` endpoint returns:

    access_token   str   required
    refresh_token  str   present; rotated on every refresh
    token_type     str   "Bearer" (MAL's current behavior)
    expires_in     int   seconds (~31 days per MAL docs)

WHY THIS MODULE EXISTS
----------------------
The initial IMPLEMENTATION_PLAN framing said "MAL invalidates the prior
refresh_token on rotation, so durable persistence is required." PR #82
Phase E empirically disproved that (MAL implements the looser RFC 6749
§ 6 behavior — prior refresh_tokens remain valid). The actual pre-PR-#82
failure mode on Railway was subtler:

  1. `refresh_mal_token` in src/mal_auth_helper reads refresh_token
     from `token_data.json` on disk.
  2. Railway has no init step creating that file, and the workflow's
     `git write` step doesn't run inside the API container.
  3. Container start → disk file missing → refresh path can't read a
     refresh_token → 401 until manual re-seeding.

This module is still the right fix, just for a different reason —
Supabase is a durable, always-present source that survives container
restarts without needing an init step.

Service-role only — RLS enabled with no policies on the underlying
table. Matches the system_flags / telemetry_logs singleton+RLS pattern.
"""
import logging
import os
import sys

# --- BULLETPROOF PATH ANCHORING ---
SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from api.supabase_client import supabase_get, supabase_patch, supabase_post

logger = logging.getLogger(__name__)

TABLE = "mal_oauth_token"
SINGLETON_ID = 1


def _supabase_available() -> bool:
    """Both env vars must be present for any read/write to attempt."""
    return bool(
        os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY")
    )


def read_token() -> dict | None:
    """Read the singleton MAL token row from Supabase.

    Returns:
        Row dict with keys access_token, refresh_token, token_type,
        expires_in, refreshed_at. Returns None when:
          - Supabase is unconfigured (local dev without secrets in env)
          - The singleton row hasn't been seeded yet
          - Transport fails (defensive — never crash a refresh path)

    Callers chain through env / disk fallbacks on None.
    """
    if not _supabase_available():
        return None
    try:
        rows = supabase_get(TABLE, params={"id": f"eq.{SINGLETON_ID}"})
    except Exception as e:  # noqa: BLE001 — transport failure must degrade to the env / disk fallback chain, not crash the refresh path
        logger.error(f"mal_token_store.read_token transport failure: {e}")
        return None
    if not rows:
        return None
    return rows[0]


def write_token(payload: dict) -> bool:
    """Upsert the singleton MAL token row.

    Args:
        payload: Dict with at minimum access_token + refresh_token.
                 Optional token_type (defaults to "Bearer") and
                 expires_in. refreshed_at is set server-side via the
                 mal_oauth_token_refreshed_at_trg trigger; any value
                 the caller passes is silently overwritten.

    Returns:
        True when the row is durably persisted; False when:
          - Required fields are missing from payload
          - Supabase is unconfigured (refresh proceeds with local-only
            durability via the existing disk write — degraded but not
            broken)
          - Transport fails

    Upsert is implemented as PATCH-then-POST against the existing
    supabase_client primitives. The race window between the PATCH
    returning empty and the POST landing is negligible: only refresh
    code paths write this row, and refreshes are serialized by MAL's
    monthly token TTL.
    """
    access = payload.get("access_token")
    refresh = payload.get("refresh_token")
    if not access or not refresh:
        logger.error(
            "mal_token_store.write_token rejected payload missing "
            "access_token or refresh_token"
        )
        return False

    if not _supabase_available():
        return False

    fields = {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": payload.get("token_type", "Bearer"),
        "expires_in": payload.get("expires_in"),
    }

    try:
        updated = supabase_patch(
            TABLE,
            data=fields,
            params={"id": f"eq.{SINGLETON_ID}"},
        )
        if updated:
            return True
        # PATCH matched no rows — singleton hasn't been seeded yet.
        supabase_post(TABLE, data={"id": SINGLETON_ID, **fields})
        return True
    except Exception as e:  # noqa: BLE001 — transport failure returns False so the caller can fall back to disk write; must not crash the refresh path
        logger.error(f"mal_token_store.write_token transport failure: {e}")
        return False
