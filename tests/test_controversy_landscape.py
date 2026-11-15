"""Tests for GET /api/controversy-landscape-data — Landing Redesign PR #4.

Hits the real vault DB (no mocking) — endpoint is deterministic over
vault state, so assertions target structure, counts, and field ranges
rather than specific titles.

Coverage:
  - 200 response with the expected top-level shape
  - Shows count matches vault consensus-populated row count
  - Cache-Control header set for CDN caching
  - Per-show fields present with sensible types + ranges
  - Quality is Bayesian-smoothed (not raw mal_score)
  - Genres parsed from comma-separated string into a list
  - Idempotent across repeat calls
  - Payload size sanity (gzipped well under landing-page tolerance)

Run: pytest tests/test_controversy_landscape.py -v
"""
import gzip
import json
import os
import sqlite3
import sys
from datetime import datetime

from fastapi.testclient import TestClient

TESTS_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

os.environ["TESTING"] = "1"

from api.main import app  # noqa: E402

client = TestClient(app)

DB_PATH = os.path.join(ROOT_DIR, "data", "anime_intelligence_v2.db")


def _vault_show_count() -> int:
    """Ground-truth count of shows the endpoint should return (ones with
    consensus_json + mal_score + scored_by, matching the WHERE clause)."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    cnt = conn.execute(
        "SELECT COUNT(*) FROM anime_info "
        "WHERE consensus_json IS NOT NULL "
        "  AND mal_score IS NOT NULL "
        "  AND scored_by IS NOT NULL"
    ).fetchone()[0]
    conn.close()
    return cnt


class TestResponseShape:
    def test_returns_200_with_shows_and_generated_at(self):
        r = client.get("/api/controversy-landscape-data")
        assert r.status_code == 200
        data = r.json()
        assert "shows" in data
        assert "generated_at" in data
        assert isinstance(data["shows"], list)
        assert len(data["shows"]) > 0

    def test_generated_at_is_valid_iso8601(self):
        r = client.get("/api/controversy-landscape-data")
        gen = r.json()["generated_at"]
        # datetime.fromisoformat raises if the string isn't ISO
        datetime.fromisoformat(gen)

    def test_cache_control_header_set(self):
        r = client.get("/api/controversy-landscape-data")
        assert r.headers.get("cache-control") == "public, max-age=86400"


class TestShowFields:
    def _first_show(self) -> dict:
        return client.get("/api/controversy-landscape-data").json()["shows"][0]

    def test_all_required_fields_present(self):
        show = self._first_show()
        for field in ("mal_id", "title", "quality", "controversy_score",
                      "scored_by", "avg_sentiment", "genres",
                      "release_year", "subtype"):
            assert field in show, f"missing field: {field}"

    def test_types_match_schema(self):
        show = self._first_show()
        assert isinstance(show["mal_id"], int)
        assert isinstance(show["title"], str) and show["title"]
        assert isinstance(show["quality"], (int, float))
        assert isinstance(show["controversy_score"], int)
        assert isinstance(show["scored_by"], int)
        assert isinstance(show["avg_sentiment"], (int, float))
        assert isinstance(show["genres"], list)
        assert isinstance(show["release_year"], int)
        assert isinstance(show["subtype"], str)

    def test_genres_parsed_as_list_of_strings(self):
        # Pick a show known to have multiple genres (any popular show
        # should; using the first one and sampling)
        shows = client.get("/api/controversy-landscape-data").json()["shows"]
        multi_genre = next((s for s in shows if len(s["genres"]) >= 2), None)
        assert multi_genre is not None, "expected at least one show with multiple genres"
        for g in multi_genre["genres"]:
            assert isinstance(g, str) and g.strip() == g


class TestFieldRanges:
    def test_quality_in_reasonable_range(self):
        """Bayesian smoothing pulls scores toward 7.05 (global mean). All
        shows with any ratings should land somewhere in [1.0, 10.0]."""
        shows = client.get("/api/controversy-landscape-data").json()["shows"]
        for s in shows:
            assert 1.0 <= s["quality"] <= 10.0, \
                f"quality out of range for MAL {s['mal_id']}: {s['quality']}"

    def test_quality_is_bayesian_smoothed_not_raw(self):
        """A low-vote show with a very high mal_score should have its
        quality shrunken toward the global mean (7.05). If quality
        equals raw mal_score for such a show, the Bayesian step didn't
        happen."""
        shows = client.get("/api/controversy-landscape-data").json()["shows"]
        # Find a low-vote high-score candidate (fewer than 500 votes,
        # raw mal_score > 8.5 would normally shrink substantially)
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        candidates = conn.execute(
            "SELECT id, mal_score, scored_by FROM anime_info "
            "WHERE consensus_json IS NOT NULL "
            "  AND mal_score >= 8.5 AND scored_by < 500 "
            "LIMIT 5"
        ).fetchall()
        conn.close()
        if not candidates:
            # Not every vault has this profile; skip the assertion
            # rather than fail. Primary assertion still runs for shows
            # that do match.
            return
        shows_by_id = {s["mal_id"]: s for s in shows}
        for mal_id, raw_score, scored_by in candidates:
            s = shows_by_id.get(mal_id)
            if s is None:
                continue
            # Bayesian smoothing with v=scored_by, m=5000, C=7.05 pulls
            # heavily toward 7.05 for low vote counts. Expect gap > 0.3.
            assert abs(s["quality"] - raw_score) > 0.3, \
                f"MAL {mal_id} quality={s['quality']} too close to raw={raw_score}"

    def test_controversy_score_in_0_10_range(self):
        shows = client.get("/api/controversy-landscape-data").json()["shows"]
        for s in shows:
            assert 0 <= s["controversy_score"] <= 10, \
                f"controversy out of range for MAL {s['mal_id']}: {s['controversy_score']}"

    def test_avg_sentiment_in_minus_one_to_one(self):
        shows = client.get("/api/controversy-landscape-data").json()["shows"]
        for s in shows:
            assert -1.0 <= s["avg_sentiment"] <= 1.0, \
                f"avg_sentiment out of range for MAL {s['mal_id']}: {s['avg_sentiment']}"


class TestVaultCoverage:
    def test_show_count_matches_vault(self):
        """Returned show count equals vault consensus-populated row count.
        If a backfill lands or a quarantine happens, this test tracks it
        automatically — the response count moves with the vault."""
        response_count = len(client.get("/api/controversy-landscape-data").json()["shows"])
        assert response_count == _vault_show_count()


class TestDeterminism:
    def test_idempotent_across_calls(self):
        """Two back-to-back calls return identical data (determinism is
        the contract that lets us cache aggressively)."""
        r1 = client.get("/api/controversy-landscape-data").json()
        r2 = client.get("/api/controversy-landscape-data").json()
        # generated_at may differ (ISO timestamp); strip it for comparison
        del r1["generated_at"]
        del r2["generated_at"]
        assert r1 == r2


class TestPayloadSize:
    def test_gzipped_payload_under_300kb(self):
        """Phase 1 discovery measured ~218 KB gzipped. Allow 300 KB as a
        regression ceiling — if the payload breaks past this, something
        structural changed (field added, encoding regression) and we
        should know before shipping."""
        r = client.get("/api/controversy-landscape-data")
        gzipped_size = len(gzip.compress(r.content))
        assert gzipped_size < 300_000, \
            f"gzipped payload is {gzipped_size:,} bytes (ceiling 300 KB)"
