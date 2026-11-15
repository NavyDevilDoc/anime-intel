"""
Supabase REST API client for user data operations.
Uses the service_key to bypass RLS — the API layer validates auth via JWT,
then makes trusted server-side database calls.

All functions are synchronous (using httpx) for simplicity with FastAPI's
sync route handlers. For async routes, swap httpx.Client with httpx.AsyncClient.
"""
import os
import sys

import httpx

# --- BULLETPROOF PATH ANCHORING ---
SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT_DIR, "env_variables.env"))

def _get_config():
    """Read Supabase config from environment (not cached at import time)."""
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set. "
            "Add them to env_variables.env or Railway environment variables."
        )
    return url, key


# Sentinel value: pass as user_jwt to use anon role (RLS applies, no user identity).
# Used for public endpoints like shared list viewing where RLS policies
# control visibility (is_public = true) without requiring authentication.
ANON = "__anon__"


def _headers(user_jwt: str | None = None) -> dict:
    """
    Build headers for Supabase REST API requests.

    If user_jwt is a real JWT, requests are made AS that user (RLS applies).
    If user_jwt is ANON, uses the anon key (RLS applies, no user identity).
    If user_jwt is None, uses the service_role key (admin, bypasses RLS).
    """
    _, service_key = _get_config()
    if user_jwt == ANON:
        anon_key = os.environ.get("SUPABASE_ANON_KEY", "")
        if not anon_key:
            raise RuntimeError("SUPABASE_ANON_KEY must be set for public queries.")
        return {
            "apikey": anon_key,
            "Authorization": f"Bearer {anon_key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
    token = user_jwt or service_key
    return {
        "apikey": service_key,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def supabase_get(table: str, params: dict | None = None, user_jwt: str | None = None) -> list[dict]:
    """
    SELECT from a Supabase table. Returns a list of row dicts.

    Args:
        table: Table name (e.g., "watchlists")
        params: Query params for PostgREST filtering
        user_jwt: If provided, request is made as this user (RLS applies)
    """
    url, _ = _get_config()
    resp = httpx.get(f"{url}/rest/v1/{table}", headers=_headers(user_jwt), params=params or {})
    resp.raise_for_status()
    return resp.json()


def supabase_post(table: str, data: dict, user_jwt: str | None = None) -> list[dict]:
    """
    INSERT into a Supabase table. Returns the created row(s).

    Args:
        table: Table name
        data: Row data as a dict
        user_jwt: If provided, request is made as this user (RLS applies)
    """
    url, _ = _get_config()
    resp = httpx.post(f"{url}/rest/v1/{table}", headers=_headers(user_jwt), json=data)
    if resp.status_code >= 400:
        # Surface PostgREST's error body (constraint name, column, etc.) before
        # raising — otherwise httpx only logs the bare status code.
        print(f"[supabase_post] {table} {resp.status_code}: {resp.text}", flush=True)
    resp.raise_for_status()
    return resp.json()


def supabase_patch(table: str, data: dict, params: dict, user_jwt: str | None = None) -> list[dict]:
    """
    UPDATE rows in a Supabase table. Returns the updated row(s).

    Args:
        table: Table name
        data: Fields to update
        params: PostgREST filter params
        user_jwt: If provided, request is made as this user (RLS applies)
    """
    url, _ = _get_config()
    resp = httpx.patch(f"{url}/rest/v1/{table}", headers=_headers(user_jwt), json=data, params=params)
    resp.raise_for_status()
    return resp.json()


def supabase_rpc(function_name: str, params: dict | None = None, user_jwt: str | None = None) -> list[dict]:
    """
    Call a Supabase PostgreSQL function (RPC). Returns the function's
    result rows as a list of dicts.

    Args:
        function_name: Name of the SQL function (e.g., "get_pipeline_timings")
        params: Function arguments as a dict (e.g., {"p_window_hours": 24})
        user_jwt: If provided, request is made as this user (RLS applies)
    """
    url, _ = _get_config()
    resp = httpx.post(
        f"{url}/rest/v1/rpc/{function_name}",
        headers=_headers(user_jwt),
        json=params or {},
    )
    if resp.status_code >= 400:
        # Surface PostgREST's error body before raising — same pattern as supabase_post.
        print(f"[supabase_rpc] {function_name} {resp.status_code}: {resp.text}", flush=True)
    resp.raise_for_status()
    return resp.json()


def supabase_count(table: str, params: dict | None = None, user_jwt: str | None = None) -> int:
    """
    COUNT rows in a Supabase table matching the filter.

    Args:
        table: Table name
        params: PostgREST filter params
        user_jwt: If provided, request is made as this user (RLS applies)
    """
    url, _ = _get_config()
    hdrs = _headers(user_jwt)
    hdrs["Prefer"] = "count=exact"
    resp = httpx.head(f"{url}/rest/v1/{table}", headers=hdrs, params=params or {})
    resp.raise_for_status()
    content_range = resp.headers.get("content-range", "")
    if "/" in content_range:
        total = content_range.split("/")[1]
        return int(total) if total != "*" else 0
    return 0
