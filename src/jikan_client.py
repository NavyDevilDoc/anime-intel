"""Jikan REST client with structured error returns.

Every fetch method returns a `JikanFetchResult` (or `ReviewFetchResult`
for the pre-existing reviews path) so callers can distinguish real
failures from genuine empty results. `error_kind` is one of a small
labeled vocabulary (`http_5xx`, `http_4xx`, `network`,
`rate_limit_exhausted`, `unexpected`) that flows into log lines and
quarantine rows for diagnostic clarity.
"""
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Generic, TypeVar

import requests

logger = logging.getLogger(__name__)

# JIKAN_BASE_URL env var is required — no fallback. The public
# `api.jikan.moe` fallback was retired after the Jikan Migration
# Initiative (PRs #95-#96) proved the self-hosted Railway instance was
# the only reliable upstream; the public endpoint has been in
# maintenance mode / brownout since mid-2026, so a silent fallback was
# a footgun (looked like success in logs but degraded to broken
# responses). Scripts and workflows must set JIKAN_BASE_URL explicitly
# to the self-hosted instance (or a known-good alternative) before
# constructing JikanClient.
_MISSING_JIKAN_URL_ERROR = (
    "JIKAN_BASE_URL env var is required. The public api.jikan.moe "
    "fallback was retired — set JIKAN_BASE_URL to the self-hosted "
    "Railway instance "
    "(https://jikan-rest-production-d553.up.railway.app/v4) or a "
    "known-good alternative before constructing JikanClient."
)


# Shared error-kind vocabulary used by ReviewFetchResult + JikanFetchResult:
#   'http_5xx'             — Jikan server-side error (retries exhausted)
#   'http_4xx'             — client-side (bad ID, unauthorized, etc.)
#   'network'              — connection refused, timeout, DNS
#   'rate_limit_exhausted' — 429 retries exhausted
#   'unexpected'           — any other exception (JSON parse, schema drift, ...)


@dataclass
class ReviewFetchResult:
    """Structured return from JikanClient.get_anime_reviews.

    Pre-PR-#93, the method returned `list` on both success and every
    failure mode, so a Jikan 504 was indistinguishable from a genuinely
    empty review list. The ingester's quarantine table logged "Returned
    0 reviews. Cannot distill." for both cases, which made diagnosing
    the July 2026 Jikan `/reviews` outage invisible in the DB — 65
    Spring 2026 shows silently quarantined while the workflow reported
    SUCCESS.

    Kept as a distinct type (rather than folded into `JikanFetchResult`)
    because the seasonal ingestor code uses `.reviews` by name and the
    review-fetch path is on the critical per-show ingest path.
    """
    reviews: list = field(default_factory=list)
    status_code: int | None = None
    error_kind: str | None = None
    error_detail: str | None = None

    @property
    def is_error(self) -> bool:
        return self.error_kind is not None

    @property
    def summary(self) -> str:
        """One-line description for quarantine logging."""
        if not self.is_error:
            return f"OK: {len(self.reviews)} reviews"
        return f"{self.error_kind} ({self.status_code or 'no status'}): {self.error_detail or 'no detail'}"


@dataclass
class AnimeDetail:
    """Narrow projection of Jikan's `/anime/{id}` payload.

    Carries only the short-string metadata fields that backfill tools
    read today (media_type, source). Extend as new backfill needs land,
    but keep it narrow — this is a shared contract between the client
    and multiple backfill tools, not a MAL mirror. Each new field pays
    a small compatibility cost for every caller.
    """
    media_type: str = ""  # Jikan "type" field; see get_anime_detail docstring for enum
    source: str = ""      # Jikan "source" field; see get_anime_detail docstring for enum


@dataclass
class ScoreSnapshot:
    """Live score + vote count from a Jikan /anime/{id} fetch.

    Distinct from AnimeDetail: score/scored_by are dynamic (change
    weekly as community members rate a show); media_type/source are
    static metadata set at release. Keeping them in separate dataclasses
    signals their different rates of change and avoids conflating a
    "first-ingest detail fetch" with a "weekly score refresh" into the
    same call.

    Both fields are None when the show has no MAL score yet
    (pre-airing, or very early airing with zero votes).
    """
    score: float | None = None
    scored_by: int | None = None


T = TypeVar("T")


@dataclass
class JikanFetchResult(Generic[T]):
    """Generic structured return for the non-review Jikan endpoints.

    Parallels `ReviewFetchResult` (see PR #93) for the methods that
    previously returned bare `None` / `[]` on failure —
    `get_anime_genres`, `get_anime_rating`, `get_anime_relations`,
    `get_anime_detail`, `get_episode_count`, `get_episode_detail`. Prior
    to this change, a caller couldn't tell whether "no relations" meant
    "MAL says empty" or "Jikan 504" — both returned the sentinel. Now
    callers check `is_error` first, then read `value`.

    On error `value` is set to a type-appropriate empty default
    (`[]` / `""` / `{}`) so callers that want to unconditionally
    unwrap it (falling back to empty on failure) can do so without
    a None-check.
    """
    value: T
    status_code: int | None = None
    error_kind: str | None = None
    error_detail: str | None = None

    @property
    def is_error(self) -> bool:
        return self.error_kind is not None

    @property
    def summary(self) -> str:
        """One-line description for log lines."""
        if not self.is_error:
            return "OK"
        return f"{self.error_kind} ({self.status_code or 'no status'}): {self.error_detail or 'no detail'}"


class JikanClient:
    def __init__(self):
        base_url = (os.environ.get("JIKAN_BASE_URL") or "").strip()
        if not base_url:
            # Fail fast with an actionable message. Empty-string + whitespace-
            # only values are treated the same as unset — an unset GitHub
            # secret resolves to empty-string in a workflow context, so we
            # can't distinguish "intentionally blank" from "forgot to set."
            raise RuntimeError(_MISSING_JIKAN_URL_ERROR)
        self.base_url = base_url
        self.rate_limit_delay = 3.0
        # PERFORMANCE: Use a session to pool TCP connections
        self.session = requests.Session()

    def get_anime_reviews(self, mal_id, attempt=1, max_attempts=3) -> ReviewFetchResult:
        endpoint = f"{self.base_url}/anime/{mal_id}/reviews"

        try:
            # preliminary=true is required for long-running / currently-airing
            # shows — without it, every recent review on any incomplete show
            # is filtered out as "preliminary" and jikan-rest silently returns
            # `data: []`. One Piece (MAL 21) hit this 2026-08-30 and got
            # quarantined for "0 reviews" despite having thousands.
            # Explicit timeout — pre-change was no timeout, so a Jikan hang
            # would block the ingester indefinitely.
            response = self.session.get(endpoint, params={"preliminary": "true"}, timeout=15)

            if response.status_code == 429:
                if attempt > max_attempts:
                    logger.error(f"❌ Jikan Rate Limit: Max retries exhausted for ID {mal_id}")
                    return ReviewFetchResult(
                        status_code=429,
                        error_kind="rate_limit_exhausted",
                        error_detail=f"HTTP 429 after {max_attempts} retries",
                    )
                wait_time = 2 ** attempt
                logger.warning(f"⚠️ Rate limited by Jikan. Cooling down for {wait_time}s (Attempt {attempt})...")
                time.sleep(wait_time)
                return self.get_anime_reviews(mal_id, attempt + 1, max_attempts)

            # 5xx retry-with-backoff — added post-July-2026 Jikan outage
            # where /reviews returned 504 across every anime. Pre-change,
            # 5xx hit `raise_for_status()` → caught by broad Exception →
            # returned []. Now surfaces as explicit http_5xx error_kind.
            if response.status_code >= 500:
                if attempt > max_attempts:
                    logger.error(f"❌ Jikan {response.status_code}: Max retries exhausted for ID {mal_id}")
                    return ReviewFetchResult(
                        status_code=response.status_code,
                        error_kind="http_5xx",
                        error_detail=f"HTTP {response.status_code} after {max_attempts} retries",
                    )
                wait_time = 2 ** attempt
                logger.warning(f"⚠️ Jikan {response.status_code} for ID {mal_id}. Retrying in {wait_time}s (Attempt {attempt})...")
                time.sleep(wait_time)
                return self.get_anime_reviews(mal_id, attempt + 1, max_attempts)

            # 4xx: no retry (won't recover on retry), but distinguish
            # from empty-success so the caller can log a meaningful
            # reason in the quarantine table.
            if 400 <= response.status_code < 500:
                logger.error(f"❌ Jikan HTTP {response.status_code} for ID {mal_id}")
                return ReviewFetchResult(
                    status_code=response.status_code,
                    error_kind="http_4xx",
                    error_detail=f"HTTP {response.status_code}",
                )

            data = response.json()
            reviews = []
            for item in data.get('data', []):
                reviews.append({
                    "content": item.get('review', ""),
                    "score": item.get('score', 0),
                    "tags": item.get('tags', []),
                    "reactions": item.get('reactions', {})  # Preserved for VADER
                })

            time.sleep(self.rate_limit_delay)
            return ReviewFetchResult(reviews=reviews, status_code=response.status_code)

        except (requests.ConnectionError, requests.Timeout) as e:
            # Network-level failures — distinct from HTTP errors so the
            # quarantine reason names the class of problem accurately.
            logger.error(f"❌ Jikan network error for ID {mal_id}: {type(e).__name__}: {e}")
            return ReviewFetchResult(
                error_kind="network",
                error_detail=f"{type(e).__name__}: {e}",
            )
        except Exception as e:  # noqa: BLE001 — JSONDecodeError / schema drift / unexpected exceptions all funnel to the caller as a structured error_kind='unexpected'
            # Catch-all for JSONDecodeError, unexpected schema changes,
            # etc. Preserves the exception type in error_detail so future
            # diagnostics have a chance.
            logger.error(f"❌ Jikan unexpected error for ID {mal_id}: {type(e).__name__}: {e}")
            return ReviewFetchResult(
                error_kind="unexpected",
                error_detail=f"{type(e).__name__}: {e}",
            )

    def get_anime_genres(self, mal_id, attempt=1, max_attempts=3) -> JikanFetchResult[list[str]]:
        """Fetch genre / theme / demographic tags for an anime from Jikan.

        Returns a JikanFetchResult whose `value` is a sorted,
        deduplicated list of tag names on success (e.g. `["Action",
        "School", "Shounen", "Super Power"]`) and `[]` on failure.
        Callers should check `.is_error` before treating an empty list
        as "no tags on MAL" vs "Jikan transport failure."
        """
        endpoint = f"{self.base_url}/anime/{mal_id}"

        try:
            response = self.session.get(endpoint)

            if response.status_code == 429:
                if attempt > max_attempts:
                    logger.error(f"❌ Jikan Rate Limit: Max retries exhausted for ID {mal_id}")
                    return JikanFetchResult(
                        value=[],
                        status_code=429,
                        error_kind="rate_limit_exhausted",
                        error_detail=f"HTTP 429 after {max_attempts} retries",
                    )
                wait_time = 2 ** attempt
                logger.warning(f"⚠️ Rate limited by Jikan. Cooling down for {wait_time}s (Attempt {attempt})...")
                time.sleep(wait_time)
                return self.get_anime_genres(mal_id, attempt + 1, max_attempts)

            response.raise_for_status()
            data = response.json().get("data", {})

            tags = set()
            for field in ("genres", "explicit_genres", "themes", "demographics"):
                for entry in data.get(field, []):
                    name = entry.get("name")
                    if name:
                        tags.add(name)

            time.sleep(self.rate_limit_delay)
            return JikanFetchResult(value=sorted(tags), status_code=response.status_code)

        except Exception as e:  # noqa: BLE001 — JSONDecodeError / HTTPError / schema drift all route to the caller as a structured error
            logger.error(f"❌ Jikan Genre Error for ID {mal_id}: {e}")
            return JikanFetchResult(
                value=[],
                error_kind="unexpected",
                error_detail=f"{type(e).__name__}: {e}",
            )

    def get_anime_rating(self, mal_id, attempt=1, max_attempts=3) -> JikanFetchResult[str]:
        """Fetch MAL's content-rating string for an anime from Jikan.

        Three-state contract preserved on the JikanFetchResult wrapper —
        callers who need the empty-string-vs-NULL DB distinction can
        still read it off `result.value`:

          - success:  `is_error=False`, `value` = verbatim rating string
                      (e.g., "PG-13 - Teens 13 or older", "Rx - Hentai").
          - checked-empty: `is_error=False`, `value=""` — Jikan reached
                      MAL and the `rating` field was null (genuine "no
                      rating set," typical of upcoming/unreleased
                      entries). Persist as empty string to mark
                      "checked, MAL has nothing."
          - failure:  `is_error=True`, `value=""`. Callers leave the DB
                      column NULL so the next backfill picks the row
                      up again.
        """
        endpoint = f"{self.base_url}/anime/{mal_id}"

        try:
            response = self.session.get(endpoint)

            if response.status_code == 429:
                if attempt > max_attempts:
                    logger.error(f"❌ Jikan Rate Limit: Max retries exhausted for ID {mal_id}")
                    return JikanFetchResult(
                        value="",
                        status_code=429,
                        error_kind="rate_limit_exhausted",
                        error_detail=f"HTTP 429 after {max_attempts} retries",
                    )
                wait_time = 2 ** attempt
                logger.warning(f"⚠️ Rate limited by Jikan. Cooling down for {wait_time}s (Attempt {attempt})...")
                time.sleep(wait_time)
                return self.get_anime_rating(mal_id, attempt + 1, max_attempts)

            response.raise_for_status()
            data = response.json().get("data", {})
            rating = data.get("rating")

            time.sleep(self.rate_limit_delay)
            return JikanFetchResult(
                value="" if rating is None else rating,
                status_code=response.status_code,
            )

        except Exception as e:  # noqa: BLE001 — see get_anime_genres for rationale
            logger.error(f"❌ Jikan Rating Error for ID {mal_id}: {e}")
            return JikanFetchResult(
                value="",
                error_kind="unexpected",
                error_detail=f"{type(e).__name__}: {e}",
            )

    def get_anime_relations(self, mal_id, attempt=1, max_attempts=3) -> JikanFetchResult[list[dict]]:
        """Fetch the MAL relations graph for an anime from Jikan.

        On success, `result.value` is a list of relation dicts in Jikan's
        native shape:
            [{"relation": "Sequel", "entry": [{"mal_id": ..., "type":
             "anime", "name": "..."}, ...]}, ...]
        Anime with no relations returns `value=[]` and `is_error=False`
        — distinct from the failure case where `is_error=True` and
        `value=[]`.
        """
        endpoint = f"{self.base_url}/anime/{mal_id}/relations"

        try:
            response = self.session.get(endpoint)

            if response.status_code == 429:
                if attempt > max_attempts:
                    logger.error(f"❌ Jikan Rate Limit: Max retries exhausted for ID {mal_id}")
                    return JikanFetchResult(
                        value=[],
                        status_code=429,
                        error_kind="rate_limit_exhausted",
                        error_detail=f"HTTP 429 after {max_attempts} retries",
                    )
                wait_time = 2 ** attempt
                logger.warning(f"⚠️ Rate limited by Jikan. Cooling down for {wait_time}s (Attempt {attempt})...")
                time.sleep(wait_time)
                return self.get_anime_relations(mal_id, attempt + 1, max_attempts)

            response.raise_for_status()
            data = response.json().get("data", [])

            time.sleep(self.rate_limit_delay)
            return JikanFetchResult(value=data, status_code=response.status_code)

        except Exception as e:  # noqa: BLE001 — see get_anime_genres for rationale
            logger.error(f"❌ Jikan Relations Error for ID {mal_id}: {e}")
            return JikanFetchResult(
                value=[],
                error_kind="unexpected",
                error_detail=f"{type(e).__name__}: {e}",
            )

    def get_anime_detail(self, mal_id, attempt=1, max_attempts=3) -> JikanFetchResult["AnimeDetail"]:
        """Fetch MAL's anime detail payload (media_type + source) from Jikan.

        Replaces the earlier `get_anime_type` single-field reader. One
        Jikan round-trip returns multiple short-string metadata fields,
        so grouping them into one call avoids per-field N× rate-limit
        waits and keeps the backfill tools' API footprint consistent as
        we add more fields (source material added here; future candidates
        include aired_from, rating, duration).

        Observed `media_type` values (9-value enum from PR B subtype bundle):
          "TV", "Movie", "OVA", "Special", "TV Special", "ONA", "Music",
          "PV", "CM"
        Observed `source` values (empirically 17 distinct, full-vault
        backfill against Jikan on 2026-10-10):
          "Manga" (2459), "Original" (1467), "Light novel" (793),
          "Game" (445), "Web manga" (300), "Visual novel" (255),
          "Novel" (210), "4-koma manga" (176), "Other" (159),
          "Web novel" (123), "Unknown" (84), "Mixed media" (79),
          "Card game" (47), "Book" (29), "Picture book" (21),
          "Music" (20), "Radio" (2)
        "Other" and "Unknown" are Jikan's catch-all buckets — not
        synonyms; "Other" means "we know what it is and it's not any of
        the above", "Unknown" means "we don't know."

        Three-state contract per field mirrors `get_anime_rating`:
          - success:       `value` = AnimeDetail with populated strings
          - checked-empty: field value `""` when MAL has that specific
                           field unset (rare; mainly upcoming entries)
          - failure:       `is_error=True`, `value` is a zero-value
                           `AnimeDetail()`
        """
        endpoint = f"{self.base_url}/anime/{mal_id}"

        try:
            response = self.session.get(endpoint, timeout=15)

            if response.status_code == 429:
                if attempt > max_attempts:
                    logger.error(f"❌ Jikan Rate Limit: Max retries exhausted for ID {mal_id}")
                    return JikanFetchResult(
                        value=AnimeDetail(),
                        status_code=429,
                        error_kind="rate_limit_exhausted",
                        error_detail=f"HTTP 429 after {max_attempts} retries",
                    )
                wait_time = 2 ** attempt
                logger.warning(f"⚠️ Rate limited by Jikan. Cooling down for {wait_time}s (Attempt {attempt})...")
                time.sleep(wait_time)
                return self.get_anime_detail(mal_id, attempt + 1, max_attempts)

            response.raise_for_status()
            data = response.json().get("data", {})
            media_type = data.get("type")
            source = data.get("source")

            time.sleep(self.rate_limit_delay)
            return JikanFetchResult(
                value=AnimeDetail(
                    media_type="" if media_type is None else media_type,
                    source="" if source is None else source,
                ),
                status_code=response.status_code,
            )

        except Exception as e:  # noqa: BLE001 — see get_anime_genres for rationale
            logger.error(f"❌ Jikan Detail Error for ID {mal_id}: {e}")
            return JikanFetchResult(
                value=AnimeDetail(),
                error_kind="unexpected",
                error_detail=f"{type(e).__name__}: {e}",
            )

    def get_anime_score(self, mal_id, attempt=1, max_attempts=3) -> "JikanFetchResult[ScoreSnapshot]":
        """Fetch the current MAL score + scored_by count from Jikan.

        Uses the same `/anime/{id}` endpoint as `get_anime_detail` but
        returns only the two dynamic rating fields. Called weekly by
        `tools/refresh_airing_scores.py` to keep `anime_info.mal_score`
        and `anime_info.scored_by` current for the airing cohort, so
        `predict_airing_shows.py` reads fresh values when it snapshots
        `actual_score_at_time` into `score_prediction_log`.

        Return contract:
          - success:       `value.score` is float or None (unscored show);
                           `value.scored_by` is int or None
          - failure:       `is_error=True`; `value = ScoreSnapshot()`
                           (both fields None)

        Retry semantics match `get_anime_detail`: 429 and 5xx retry with
        exponential backoff; 4xx no retry; broad except → `unexpected`.
        """
        endpoint = f"{self.base_url}/anime/{mal_id}"

        try:
            response = self.session.get(endpoint, timeout=15)

            if response.status_code == 429:
                if attempt > max_attempts:
                    logger.error(
                        f"❌ Jikan Rate Limit: Max retries exhausted for ID {mal_id}"
                    )
                    return JikanFetchResult(
                        value=ScoreSnapshot(),
                        status_code=429,
                        error_kind="rate_limit_exhausted",
                        error_detail=f"HTTP 429 after {max_attempts} retries",
                    )
                wait_time = 2 ** attempt
                logger.warning(
                    f"⚠️ Rate limited by Jikan. Cooling down for {wait_time}s "
                    f"(Attempt {attempt})..."
                )
                time.sleep(wait_time)
                return self.get_anime_score(mal_id, attempt + 1, max_attempts)

            if response.status_code >= 500:
                if attempt > max_attempts:
                    logger.error(
                        f"❌ Jikan {response.status_code}: Max retries exhausted "
                        f"for ID {mal_id}"
                    )
                    return JikanFetchResult(
                        value=ScoreSnapshot(),
                        status_code=response.status_code,
                        error_kind="http_5xx",
                        error_detail=(
                            f"HTTP {response.status_code} after {max_attempts} retries"
                        ),
                    )
                wait_time = 2 ** attempt
                logger.warning(
                    f"⚠️ Jikan {response.status_code} for ID {mal_id}. "
                    f"Retrying in {wait_time}s (Attempt {attempt})..."
                )
                time.sleep(wait_time)
                return self.get_anime_score(mal_id, attempt + 1, max_attempts)

            if 400 <= response.status_code < 500:
                logger.error(f"❌ Jikan HTTP {response.status_code} for ID {mal_id}")
                return JikanFetchResult(
                    value=ScoreSnapshot(),
                    status_code=response.status_code,
                    error_kind="http_4xx",
                    error_detail=f"HTTP {response.status_code}",
                )

            data = response.json().get("data", {})
            raw_score = data.get("score")
            raw_scored_by = data.get("scored_by")

            time.sleep(self.rate_limit_delay)
            return JikanFetchResult(
                value=ScoreSnapshot(
                    score=float(raw_score) if raw_score is not None else None,
                    scored_by=int(raw_scored_by) if raw_scored_by is not None else None,
                ),
                status_code=response.status_code,
            )

        except Exception as e:  # noqa: BLE001 — see get_anime_genres for rationale
            logger.error(f"❌ Jikan Score Error for ID {mal_id}: {e}")
            return JikanFetchResult(
                value=ScoreSnapshot(),
                error_kind="unexpected",
                error_detail=f"{type(e).__name__}: {e}",
            )

    def get_episode_count(self, mal_id, attempt=1, max_attempts=3) -> JikanFetchResult[list[int]]:
        """Fetch the episode-list numbers for an anime from Jikan.

        On success, `result.value` is a list of MAL episode IDs across
        all pagination. On failure, `is_error=True` and `value=[]`.
        Synopsis is NOT available on this endpoint — use
        `get_episode_detail`.
        """
        all_numbers: list[int] = []
        page = 1
        last_status: int | None = None

        while True:
            endpoint = f"{self.base_url}/anime/{mal_id}/episodes?page={page}"

            try:
                response = self.session.get(endpoint)
                last_status = response.status_code

                if response.status_code == 429:
                    if attempt > max_attempts:
                        logger.error(f"❌ Jikan Rate Limit: Max retries exhausted for ID {mal_id}")
                        return JikanFetchResult(
                            value=[],
                            status_code=429,
                            error_kind="rate_limit_exhausted",
                            error_detail=f"HTTP 429 after {max_attempts} retries",
                        )
                    wait_time = 2 ** attempt
                    logger.warning(f"⚠️ Rate limited by Jikan. Cooling down for {wait_time}s (Attempt {attempt})...")
                    time.sleep(wait_time)
                    return self.get_episode_count(mal_id, attempt + 1, max_attempts)

                response.raise_for_status()
                data = response.json()

                for ep in data.get("data", []):
                    all_numbers.append(ep.get("mal_id", 0))

                if data.get("pagination", {}).get("has_next_page", False):
                    page += 1
                    time.sleep(self.rate_limit_delay)
                else:
                    break

            except Exception as e:  # noqa: BLE001 — see get_anime_genres for rationale
                logger.error(f"❌ Jikan Episode List Error for ID {mal_id}: {e}")
                return JikanFetchResult(
                    value=[],
                    error_kind="unexpected",
                    error_detail=f"{type(e).__name__}: {e}",
                )

        time.sleep(self.rate_limit_delay)
        return JikanFetchResult(value=all_numbers, status_code=last_status)

    def get_episode_detail(self, mal_id, ep_number, attempt=1, max_attempts=3) -> JikanFetchResult[dict]:
        """Fetch the synopsis for a single episode from Jikan.

        On success, `result.value` is `{"number": int, "title": str,
        "synopsis": str}`. On failure, `is_error=True` and `value={}`.
        A 404 counts as "checked, MAL has nothing" — returns a
        `value` dict with empty title/synopsis and `is_error=False`
        (mirrors the checked-empty semantics of `get_anime_rating` /
        `get_anime_detail`).
        """
        endpoint = f"{self.base_url}/anime/{mal_id}/episodes/{ep_number}"

        try:
            response = self.session.get(endpoint)

            if response.status_code == 429:
                if attempt > max_attempts:
                    logger.error(f"❌ Jikan Rate Limit: Max retries for ID {mal_id} ep {ep_number}")
                    return JikanFetchResult(
                        value={},
                        status_code=429,
                        error_kind="rate_limit_exhausted",
                        error_detail=f"HTTP 429 after {max_attempts} retries",
                    )
                wait_time = 2 ** attempt
                logger.warning(f"⚠️ Rate limited. Cooling down {wait_time}s (Attempt {attempt})...")
                time.sleep(wait_time)
                return self.get_episode_detail(mal_id, ep_number, attempt + 1, max_attempts)

            if response.status_code == 404:
                return JikanFetchResult(
                    value={"number": ep_number, "title": "", "synopsis": ""},
                    status_code=404,
                )

            response.raise_for_status()
            data = response.json().get("data", {})

            time.sleep(self.rate_limit_delay)
            return JikanFetchResult(
                value={
                    "number": ep_number,
                    "title": data.get("title", ""),
                    "synopsis": (data.get("synopsis") or "").strip(),
                },
                status_code=response.status_code,
            )

        except Exception as e:  # noqa: BLE001 — see get_anime_genres for rationale
            logger.error(f"❌ Jikan Episode Detail Error for ID {mal_id} ep {ep_number}: {e}")
            return JikanFetchResult(
                value={},
                error_kind="unexpected",
                error_detail=f"{type(e).__name__}: {e}",
            )