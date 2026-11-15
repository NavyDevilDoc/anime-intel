"""Tests for GET /api/spotlight — Today's Spotlight algorithmic rotation.

Coverage:
  - Weekday-to-category mapping is correct across all 7 days
  - Same date returns the same show (determinism guarantee)
  - Default date (no ?date=) uses today (UTC)
  - Invalid ?date= returns 400
  - Response shape matches SpotlightResponse contract
  - Cache-Control header is set for CDN caching
  - Poster URLs surface from the DB (PR #148's backfill)
  - Category-specific criteria hold for the primary show
  - Franchise / behind-the-scenes / vibe categories populate their
    supplementary_shows + supplementary_context

Tests hit the real vault DB (no mocking) — determinism means the same
date always returns the same show, so assertions on structure rather
than specific titles are stable across runs.

Run: pytest tests/test_spotlight.py -v
"""
import json
import os
import sqlite3
import sys
from datetime import date

from fastapi.testclient import TestClient

TESTS_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

os.environ["TESTING"] = "1"

from api.main import app  # noqa: E402
from api.routes.spotlight import CATEGORY_ROTATION  # noqa: E402

client = TestClient(app)

# Reference dates chosen for each weekday. datetime(2026, 9, 28) is a
# Monday; each subsequent day walks the rotation. Frozen so tests don't
# depend on runtime clock.
MONDAY = "2026-09-28"
TUESDAY = "2026-09-29"
WEDNESDAY = "2026-09-30"
THURSDAY = "2026-10-01"
FRIDAY = "2026-10-02"
SATURDAY = "2026-10-03"
SUNDAY = "2026-10-04"


class TestWeekdayRotation:
    """Each weekday maps to a specific category. Regression guard against
    accidental reordering of CATEGORY_ROTATION."""

    def test_all_seven_weekdays_return_expected_category(self):
        # Sanity — the constant itself is what the endpoint depends on
        assert CATEGORY_ROTATION == (
            "hidden_gem",              # Mon
            "controversial_classic",   # Tue
            "deep_cut",                # Wed
            "franchise_spotlight",     # Thu
            "weekend_watch",           # Fri
            "behind_the_scenes",       # Sat
            "vibe_of_the_week",        # Sun
        )
        # And the endpoint honors it
        for iso_date, expected in [
            (MONDAY, "hidden_gem"),
            (TUESDAY, "controversial_classic"),
            (WEDNESDAY, "deep_cut"),
            (THURSDAY, "franchise_spotlight"),
            (FRIDAY, "weekend_watch"),
            (SATURDAY, "behind_the_scenes"),
            (SUNDAY, "vibe_of_the_week"),
        ]:
            r = client.get(f"/api/spotlight?date={iso_date}")
            assert r.status_code == 200, f"{iso_date}: {r.status_code} {r.text}"
            data = r.json()
            assert data["category"] == expected, \
                f"{iso_date}: expected {expected}, got {data['category']}"
            # Weekday parses out to the same Python day-of-week the
            # endpoint uses to index CATEGORY_ROTATION
            expected_weekday = date.fromisoformat(iso_date).weekday()
            assert CATEGORY_ROTATION[expected_weekday] == expected


class TestDeterminism:
    def test_same_date_returns_same_primary_show(self):
        r1 = client.get(f"/api/spotlight?date={MONDAY}")
        r2 = client.get(f"/api/spotlight?date={MONDAY}")
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["primary_show"]["id"] == r2.json()["primary_show"]["id"]

    def test_different_dates_typically_return_different_shows(self):
        """Sanity — two dates on the same category (both Hidden Gem)
        shouldn't collapse to the same show. Not a strict guarantee
        (two seeds could collide in a small pool) but is with pools of
        hundreds of hidden-gem candidates."""
        r1 = client.get(f"/api/spotlight?date={MONDAY}")           # Sep 28 Mon
        r2 = client.get(f"/api/spotlight?date=2026-10-05")         # Oct 5 Mon
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["category"] == "hidden_gem"
        assert r2.json()["category"] == "hidden_gem"
        assert r1.json()["primary_show"]["id"] != r2.json()["primary_show"]["id"]


class TestParamValidation:
    def test_default_date_uses_today(self):
        """No ?date= param falls back to today (UTC). Should always succeed
        against a healthy vault."""
        r = client.get("/api/spotlight")
        assert r.status_code == 200
        data = r.json()
        # Response echoes the resolved date so the frontend can label it
        assert "date" in data
        assert len(data["date"]) == 10  # YYYY-MM-DD

    def test_invalid_date_returns_400(self):
        r = client.get("/api/spotlight?date=not-a-date")
        assert r.status_code == 400
        assert "Invalid date" in r.json()["detail"]

    def test_out_of_range_date_still_returns_deterministic_result(self):
        """A date the user contrived (far future / far past) should still
        pick a show — the endpoint doesn't validate against a range."""
        r = client.get("/api/spotlight?date=2050-01-01")
        assert r.status_code == 200


class TestResponseShape:
    def test_required_fields_present(self):
        r = client.get(f"/api/spotlight?date={MONDAY}")
        data = r.json()
        for field in ("category", "category_label", "date", "primary_show"):
            assert field in data, f"missing top-level field: {field}"
        for field in ("id", "title", "release_year", "mal_score",
                      "thematic_vibe", "consensus_summary", "pros",
                      "controversy_score", "poster_url_large", "poster_url_medium"):
            assert field in data["primary_show"], f"missing primary_show field: {field}"

    def test_cache_control_header_set(self):
        r = client.get(f"/api/spotlight?date={MONDAY}")
        assert r.status_code == 200
        assert r.headers.get("cache-control") == "public, max-age=3600"

    def test_primary_show_has_poster_url(self):
        """Every category's pool filters for poster_url_large IS NOT NULL,
        so the primary show should always have a poster after PR #148."""
        r = client.get(f"/api/spotlight?date={MONDAY}")
        data = r.json()
        assert data["primary_show"]["poster_url_large"] is not None
        assert data["primary_show"]["poster_url_large"].startswith(
            "https://cdn.myanimelist.net/"
        )


class TestCategoryCriteria:
    """The picked show's DB row satisfies the category's SELECT criteria.
    These tests verify the pool builder matches the category contract."""

    def _fetch_row(self, mal_id: int) -> dict:
        conn = sqlite3.connect(f"file:{os.path.join(ROOT_DIR, 'data', 'anime_intelligence_v2.db')}?mode=ro",
                               uri=True)
        row = conn.execute(
            "SELECT mal_score, scored_by, consensus_json, subtype "
            "FROM anime_info WHERE id = ?", (mal_id,)
        ).fetchone()
        conn.close()
        return {"mal_score": row[0], "scored_by": row[1],
                "consensus_json": row[2], "subtype": row[3]}

    def test_hidden_gem_satisfies_criteria(self):
        r = client.get(f"/api/spotlight?date={MONDAY}")
        row = self._fetch_row(r.json()["primary_show"]["id"])
        assert row["mal_score"] >= 7.5
        assert 5000 <= row["scored_by"] <= 50000

    def test_weekend_watch_is_movie_or_special(self):
        r = client.get(f"/api/spotlight?date={FRIDAY}")
        row = self._fetch_row(r.json()["primary_show"]["id"])
        assert row["subtype"] in ("Movie", "TV Special")
        assert row["mal_score"] >= 7.5


class TestSupplementaryPayloads:
    def test_franchise_spotlight_has_series_context(self):
        r = client.get(f"/api/spotlight?date={THURSDAY}")
        data = r.json()
        assert data["category"] == "franchise_spotlight"
        assert data["supplementary_context"] is not None
        assert "series_id" in data["supplementary_context"]
        assert data["supplementary_context"]["series_size"] >= 5
        # Primary + siblings — the plan targets series of 5+, so at
        # least a few supplementary entries should come through
        assert len(data["supplementary_shows"]) >= 1

    def test_behind_the_scenes_has_person_context(self):
        r = client.get(f"/api/spotlight?date={SATURDAY}")
        data = r.json()
        assert data["category"] == "behind_the_scenes"
        assert data["supplementary_context"] is not None
        assert "person_name" in data["supplementary_context"]
        assert "role" in data["supplementary_context"]
        assert data["supplementary_context"]["role"] in (
            "Director", "Music", "Original Creator",
            "Series Composition", "Character Design",
        )

    def test_vibe_of_the_week_has_vibe_context(self):
        r = client.get(f"/api/spotlight?date={SUNDAY}")
        data = r.json()
        assert data["category"] == "vibe_of_the_week"
        assert data["supplementary_context"] is not None
        assert "vibe" in data["supplementary_context"]
        # All supplementary shows share the picked vibe
        picked_vibe = data["supplementary_context"]["vibe"]
        # Primary show's consensus_json.thematic_vibe matches the picked
        # vibe (the pool selected by vibe, so this must hold)
        assert data["primary_show"]["thematic_vibe"] == picked_vibe

    def test_vibe_of_the_week_rating_filter_excludes_r_plus_and_rx(self):
        """Marketing guard: the vibe rotation excludes R+ / Rx-rated
        exemplars from both pool selection and exemplar surfacing so the
        Sunday landing spotlight doesn't surface ecchi/hentai clusters."""
        # Test across multiple Sundays — if the filter is off for any date,
        # we'd eventually surface a filtered show
        for iso_date in ("2026-10-04", "2026-10-11", "2026-10-18", "2026-10-25"):
            r = client.get(f"/api/spotlight?date={iso_date}")
            data = r.json()
            assert data["category"] == "vibe_of_the_week"
            # Fetch mal_rating for primary + supplementary from DB
            all_ids = [data["primary_show"]["id"]] + [
                s["id"] for s in data["supplementary_shows"]
            ]
            conn = sqlite3.connect(
                f"file:{os.path.join(ROOT_DIR, 'data', 'anime_intelligence_v2.db')}?mode=ro",
                uri=True,
            )
            placeholders = ",".join(["?"] * len(all_ids))
            rows = conn.execute(
                f"SELECT id, mal_rating FROM anime_info WHERE id IN ({placeholders})",
                all_ids,
            ).fetchall()
            conn.close()
            for mal_id, rating in rows:
                assert rating is not None, f"{iso_date} MAL {mal_id}: NULL rating leaked into vibe surface"
                assert not rating.startswith("R+"), \
                    f"{iso_date} MAL {mal_id}: R+ rating leaked into vibe surface: {rating!r}"
                assert not rating.startswith("Rx"), \
                    f"{iso_date} MAL {mal_id}: Rx rating leaked into vibe surface: {rating!r}"
