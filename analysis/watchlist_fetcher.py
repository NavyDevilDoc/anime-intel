"""
PR C / Initiative 2: MAL + AniList public-watchlist fetcher.

Phase A's empirical probe (no commit, results documented in PR description)
showed two structural facts that shaped this module:

1. Jikan's /users/{username}/animelist endpoint has been broken since
   ~2024 — MAL re-rendered their public list page as a JS-driven widget
   that Jikan's HTML scraper can no longer parse. Returns 404 for every
   user. The IMPLEMENTATION_PLAN's Phase B (written 2026-06-02) assumed
   Jikan would work; it doesn't. This module uses MAL's OFFICIAL API
   (api.myanimelist.net/v2) with the project's existing app-scoped
   OAuth token instead. End-user privacy contract is unchanged:
   end users supply a username only; our app credentials read the
   public list.

2. AniList GraphQL accepts an anonymous query for public lists and
   returns the entire watchlist in a single call (no pagination needed
   in the request layer — the response carries `media.idMal` directly
   so no second lookup is required). Rate limit is 30 req/min hard.

Status mapping
--------------
End users select statuses from the canonical set used by both API
contracts and the frontend UI: completed / dropped / watching /
plan_to_watch / on_hold. Each fetcher maps to its source-specific
strings before issuing the request:

  Canonical          MAL (?status=)      AniList (status=)
  -----------------  ------------------  -----------------------------
  completed          completed           COMPLETED
  dropped            dropped             DROPPED
  watching           watching            CURRENT + REPEATING (folded)
  plan_to_watch      plan_to_watch       PLANNING
  on_hold            on_hold             PAUSED

Token lifecycle
---------------
MAL fetcher uses the project's app-scoped OAuth token. On 401, the
fetcher calls src.mal_auth_helper.refresh_mal_token() (which reads
the refresh_token from disk and writes a new access_token back) and
retries once. A second 401 surfaces as a 503 — token is broken at the
upstream level and needs ops attention, not user fault.

Privacy
-------
This module accepts a username, fetches the list, returns a list of
MAL IDs to the caller. No persistence. No telemetry write. The
username is held in local variables for the duration of the request
and discarded. See Phase F's Privacy + HowItWorks copy for the
user-facing version of this contract.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Literal

import requests
from pydantic import BaseModel

SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

logger = logging.getLogger(__name__)

CanonicalStatus = Literal[
    "completed", "dropped", "watching", "plan_to_watch", "on_hold"
]
SourceName = Literal["mal", "anilist"]

DEFAULT_STATUSES: list[CanonicalStatus] = ["completed", "dropped"]

# Canonical → AniList status enum. AniList's REPEATING (rewatching) folds
# into "watching" semantically — the user is actively engaged with the
# show, so the exclusion semantics match.
_ANILIST_STATUS_MAP: dict[CanonicalStatus, list[str]] = {
    "completed": ["COMPLETED"],
    "dropped": ["DROPPED"],
    "watching": ["CURRENT", "REPEATING"],
    "plan_to_watch": ["PLANNING"],
    "on_hold": ["PAUSED"],
}

# Conservative network defaults. Defensive even when the upstream APIs
# don't enforce them — we don't want to be the reason MAL or AniList
# tightens public access for everyone.
_MAL_TIMEOUT = 10
_ANILIST_TIMEOUT = 15
_MAL_PER_PAGE = 1000
_MAX_MAL_PAGES = 10  # 10 × 1000 = 10k entries; supports the largest user lists


class WatchlistResult(BaseModel):
    """Result of a successful (or partial-success) watchlist fetch.

    `mal_ids` is the deduplicated set of MAL IDs across all requested
    statuses, ready to feed into Phase C's `_filtered_vector_search`
    exclude clause.

    `total_entries` is the gross count from the source (before dedup
    or unmappable filtering). `unmappable_entries` is AniList-specific
    — entries lacking `media.idMal`, which means the show exists in
    AniList's catalog but not MAL's, so we can't exclude it from a
    MAL-keyed vault. Surfaced to the frontend so users see the catalog
    gap explicitly rather than wondering why some shows still appear.

    `errors` collects per-status fetch failures (e.g., "AniList
    returned errors on status=DROPPED"). Empty list on full success.
    Non-empty + non-empty `mal_ids` means partial success — the
    fetcher succeeded on some statuses but not others. The frontend
    can surface the partial state without blocking the user.
    """
    source: SourceName
    mal_ids: list[int]
    total_entries: int
    unmappable_entries: int
    errors: list[str]


class WatchlistFetchError(Exception):
    """Raised for hard-stop conditions — user not found, list private,
    upstream completely unreachable, or token failure after refresh."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------
# MAL
# ---------------------------------------------------------------------

def _read_mal_token() -> str | None:
    """Read the project's MAL access token.

    Fallback chain (Initiative 3):
        1. Supabase mal_oauth_token singleton — production canonical;
           survives container churn (the staleness bug Initiative 3
           was built to fix).
        2. MAL_ACCESS_TOKEN env var — legacy path; remains for
           environments not yet wired to Supabase, and as a safety
           net while the Phase E live verification confirms the
           Supabase path is healthy in prod.
        3. token_data.json on disk — local dev fallback.

    Returns None when all three miss, in which case the caller raises
    a 503 — token-broken-at-source rather than user error.
    """
    from analysis.mal_token_store import read_token as _read_supabase_token

    stored = _read_supabase_token()
    if stored and stored.get("access_token"):
        return stored["access_token"]

    env_token = os.environ.get("MAL_ACCESS_TOKEN")
    if env_token:
        return env_token

    token_path = os.path.join(ROOT_DIR, "token_data.json")
    if os.path.exists(token_path):
        import json
        with open(token_path, encoding="utf-8") as f:
            return json.load(f).get("access_token")
    return None


def _refresh_mal_token() -> str | None:
    """Refresh the MAL token via the existing helper. Returns the new
    access_token on success, None on failure."""
    try:
        from src.mal_auth_helper import refresh_mal_token
    except ImportError:
        logger.error("MAL auth helper not importable; cannot refresh token.")
        return None
    try:
        ok = refresh_mal_token()
        if not ok:
            return None
        return _read_mal_token()
    except Exception as e:
        logger.error(f"MAL token refresh failed: {e}")
        return None


def _mal_request(url: str, params: dict, session: requests.Session) -> requests.Response:
    """Single MAL request with 401-triggered token refresh + one retry.

    Two-401-in-a-row triggers WatchlistFetchError(503) — refreshing
    didn't fix it, so the token chain is broken at the source.
    """
    token = _read_mal_token()
    if not token:
        raise WatchlistFetchError(
            "MAL OAuth token is not configured on this server.", status_code=503
        )

    headers = {"Authorization": f"Bearer {token}"}
    response = session.get(url, headers=headers, params=params, timeout=_MAL_TIMEOUT)

    if response.status_code == 401:
        new_token = _refresh_mal_token()
        if not new_token:
            raise WatchlistFetchError(
                "MAL OAuth token has expired and refresh failed.",
                status_code=503,
            )
        headers["Authorization"] = f"Bearer {new_token}"
        response = session.get(url, headers=headers, params=params, timeout=_MAL_TIMEOUT)
        if response.status_code == 401:
            raise WatchlistFetchError(
                "MAL OAuth token rejected even after refresh.", status_code=503
            )

    return response


def fetch_mal(
    username: str,
    statuses: list[CanonicalStatus] | None = None,
) -> WatchlistResult:
    """Fetch the public MAL animelist for `username` filtered by `statuses`.

    Uses MAL's official API at api.myanimelist.net/v2 with the project's
    app-scoped OAuth token (refresh-on-401). The user never sees this
    token; it's read from MAL_ACCESS_TOKEN env / token_data.json.

    Raises WatchlistFetchError on hard stops:
      - 404: user not found OR list is private (MAL conflates the two)
      - 503: token broken at the source (refresh failed)
    """
    if statuses is None:
        statuses = list(DEFAULT_STATUSES)
    if not statuses:
        return WatchlistResult(
            source="mal", mal_ids=[], total_entries=0, unmappable_entries=0, errors=[]
        )

    session = requests.Session()
    base_url = f"https://api.myanimelist.net/v2/users/{username}/animelist"
    mal_id_set: set[int] = set()
    total_entries = 0
    errors: list[str] = []

    for status in statuses:
        next_url: str | None = base_url
        params: dict = {
            "fields": "list_status",
            "limit": _MAL_PER_PAGE,
            "status": status,
        }
        pages_walked = 0

        while next_url and pages_walked < _MAX_MAL_PAGES:
            try:
                response = _mal_request(next_url, params, session)
            except WatchlistFetchError:
                raise
            except requests.RequestException as e:
                errors.append(f"MAL network error on status={status}: {e}")
                break

            if response.status_code == 404:
                # First page 404 → user-not-found / private. Per-status
                # 404 on subsequent pages would be unexpected; treat as
                # user-not-found at the username level.
                raise WatchlistFetchError(
                    f"MAL user '{username}' not found or list is private.",
                    status_code=404,
                )
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After", "60")
                errors.append(
                    f"MAL rate-limited on status={status} (retry after {retry_after}s)"
                )
                break
            if response.status_code >= 400:
                errors.append(
                    f"MAL returned HTTP {response.status_code} on status={status}"
                )
                break

            payload = response.json()
            entries = payload.get("data", [])
            for e in entries:
                node = e.get("node") or {}
                mal_id = node.get("id")
                if isinstance(mal_id, int):
                    mal_id_set.add(mal_id)
                    total_entries += 1

            # MAL API paginates via paging.next as an absolute URL.
            paging = payload.get("paging") or {}
            next_url = paging.get("next") or None
            # After page 1 the URL already carries the query params;
            # passing params= again would overwrite. Drop params for
            # subsequent pages.
            params = {}
            pages_walked += 1

    return WatchlistResult(
        source="mal",
        mal_ids=sorted(mal_id_set),
        total_entries=total_entries,
        unmappable_entries=0,  # MAL is MAL-keyed by definition
        errors=errors,
    )


# ---------------------------------------------------------------------
# AniList
# ---------------------------------------------------------------------

_ANILIST_QUERY = """
query ($userName: String, $type: MediaType) {
  MediaListCollection(userName: $userName, type: $type) {
    user { id name }
    lists {
      name
      status
      entries {
        status
        media {
          id
          idMal
          title { romaji english }
        }
      }
    }
  }
}
"""


def fetch_anilist(
    username: str,
    statuses: list[CanonicalStatus] | None = None,
) -> WatchlistResult:
    """Fetch the public AniList anime list for `username` filtered by `statuses`.

    Uses AniList's anonymous public GraphQL query. Returns the entire
    list in a single call, then filters to the requested statuses
    client-side (server-side filtering would require status-keyed
    aliases — same data volume, more query complexity).

    Raises WatchlistFetchError(404) when the username doesn't resolve
    or the list is private.
    """
    if statuses is None:
        statuses = list(DEFAULT_STATUSES)
    if not statuses:
        return WatchlistResult(
            source="anilist", mal_ids=[], total_entries=0, unmappable_entries=0, errors=[]
        )

    # Expand canonical statuses to AniList enum values
    allowed_anilist_statuses: set[str] = set()
    for status in statuses:
        for mapped in _ANILIST_STATUS_MAP.get(status, []):
            allowed_anilist_statuses.add(mapped)

    errors: list[str] = []
    try:
        response = requests.post(
            "https://graphql.anilist.co",
            json={"query": _ANILIST_QUERY, "variables": {"userName": username, "type": "ANIME"}},
            headers={"Content-Type": "application/json"},
            timeout=_ANILIST_TIMEOUT,
        )
    except requests.RequestException as e:
        raise WatchlistFetchError(
            f"AniList unreachable: {e}", status_code=502
        )

    if response.status_code == 404:
        raise WatchlistFetchError(
            f"AniList user '{username}' not found or list is private.",
            status_code=404,
        )
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After", "60")
        raise WatchlistFetchError(
            f"AniList rate limit reached (retry after {retry_after}s).",
            status_code=429,
        )
    if response.status_code >= 400:
        raise WatchlistFetchError(
            f"AniList returned HTTP {response.status_code}.",
            status_code=502,
        )

    payload = response.json()
    if payload.get("errors"):
        # GraphQL-level errors usually mean malformed query OR private
        # user. Surface a 404-shaped error so the frontend can show the
        # same "not found / private" copy as MAL.
        first_error = (payload["errors"] or [{}])[0].get("message", "unknown")
        raise WatchlistFetchError(
            f"AniList user '{username}' not found, list is private, "
            f"or query failed: {first_error}",
            status_code=404,
        )

    coll = (payload.get("data") or {}).get("MediaListCollection")
    if not coll:
        raise WatchlistFetchError(
            f"AniList user '{username}' has no animelist data.",
            status_code=404,
        )

    mal_id_set: set[int] = set()
    total_entries = 0
    unmappable_entries = 0

    for sublist in coll.get("lists") or []:
        # Status filtering at the list level — AniList groups entries
        # into per-status sublists, so we just filter the sublists.
        sublist_status = sublist.get("status")
        if sublist_status not in allowed_anilist_statuses:
            continue
        for entry in sublist.get("entries") or []:
            total_entries += 1
            media = entry.get("media") or {}
            mal_id = media.get("idMal")
            if isinstance(mal_id, int):
                mal_id_set.add(mal_id)
            else:
                unmappable_entries += 1

    return WatchlistResult(
        source="anilist",
        mal_ids=sorted(mal_id_set),
        total_entries=total_entries,
        unmappable_entries=unmappable_entries,
        errors=errors,
    )


def fetch_watchlist(
    source: SourceName,
    username: str,
    statuses: list[CanonicalStatus] | None = None,
) -> WatchlistResult:
    """Source-dispatching wrapper. Raises WatchlistFetchError for hard stops.

    Source mapping is exhaustive over the SourceName literal — adding a
    third source (e.g., Kitsu) requires extending here AND in the
    request-schema literal.
    """
    if source == "mal":
        return fetch_mal(username=username, statuses=statuses)
    if source == "anilist":
        return fetch_anilist(username=username, statuses=statuses)
    raise WatchlistFetchError(
        f"Unsupported watchlist source: {source!r}", status_code=400
    )
