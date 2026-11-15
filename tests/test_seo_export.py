"""
Tests for the SEO data export script (tools/export_seo_data.py).
Validates JSON structure, data integrity, slug uniqueness, and Bayesian scoring.
"""

import json
import os
import re
import sys
import pytest

# --- Path anchoring ---
TESTS_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from tools.export_seo_data import export_profiles, _slugify, _bayesian_score

# Load profiles once for the entire test module
_data = None


def _get_data():
    global _data
    if _data is None:
        _data = export_profiles()
    return _data


class TestSEOExportDataIntegrity:
    """Validates the exported data against the live SQLite database."""

    def test_json_is_valid_and_has_expected_keys(self):
        data = _get_data()
        assert "anime" in data
        assert "count" in data
        assert "generated_at" in data
        assert isinstance(data["anime"], list)
        assert data["count"] == len(data["anime"])

    def test_profile_count_matches_database(self):
        import sqlite3

        db_path = os.path.join(ROOT_DIR, "data", "anime_intelligence_v2.db")
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM anime_info")
        db_count = cursor.fetchone()[0]
        conn.close()

        data = _get_data()
        assert data["count"] == db_count

    def test_all_required_fields_present(self):
        """Every profile must have the expected fields."""
        required_fields = {
            "id", "title", "romaji_title", "slug", "studio",
            "quality_score", "raw_mal_score", "scored_by", "avg_sentiment",
            "release_year", "season", "genres", "synopsis",
            "consensus_summary", "pros", "cons", "controversy_score",
            "thematic_vibe", "has_narrative", "mal_rating",
            "poster_url_large", "poster_url_medium",
        }
        data = _get_data()
        for profile in data["anime"]:
            missing = required_fields - set(profile.keys())
            assert not missing, f"Profile {profile.get('id')} missing fields: {missing}"

    def test_poster_url_coverage_above_threshold(self):
        """Backfill landed PR #148 at 99.97%. Guard against future export regressions."""
        data = _get_data()
        total = data["count"]
        with_large = sum(1 for p in data["anime"] if p["poster_url_large"])
        coverage = with_large / total if total else 0
        assert coverage > 0.99, (
            f"poster_url_large coverage dropped to {coverage:.2%} "
            f"({with_large}/{total}) — expected >99%"
        )

    def test_poster_urls_are_mal_cdn_when_present(self):
        """When set, poster URLs must point at MAL's CDN — nothing else should
        ever reach the exporter, and the SEO page attribution overlay would
        lie if it did."""
        data = _get_data()
        for profile in data["anime"]:
            for field in ("poster_url_large", "poster_url_medium"):
                url = profile[field]
                if url:
                    assert url.startswith("https://cdn.myanimelist.net/"), (
                        f"Profile {profile['id']} has non-MAL-CDN {field}: {url}"
                    )

    def test_no_empty_titles(self):
        data = _get_data()
        for profile in data["anime"]:
            assert profile["title"], f"Profile {profile['id']} has empty title"

    def test_spot_check_attack_on_titan(self):
        """Verify a known show has correct data."""
        data = _get_data()
        aot = [p for p in data["anime"] if p["id"] == 16498]
        assert len(aot) == 1, "Attack on Titan (MAL ID 16498) not found"
        a = aot[0]
        assert a["title"] == "Attack on Titan"
        assert a["quality_score"] > 8.0
        assert a["release_year"] == 2013
        assert len(a["pros"]) > 0
        assert len(a["cons"]) > 0
        assert a["consensus_summary"] is not None
        assert a["has_narrative"] is True
        assert a["mal_rating"] == "R - 17+ (violence & profanity)"


class TestSlugGeneration:
    """Validates slug format and uniqueness."""

    def test_all_slugs_unique(self):
        data = _get_data()
        slugs = [p["slug"] for p in data["anime"]]
        assert len(slugs) == len(set(slugs)), "Slug collision detected"

    def test_slug_format_valid(self):
        """Every slug must match {digits}-{lowercase-alphanumeric-hyphens}."""
        pattern = re.compile(r"^[0-9]+(-[a-z0-9-]*)?$")
        data = _get_data()
        for profile in data["anime"]:
            assert pattern.match(profile["slug"]), (
                f"Invalid slug format: '{profile['slug']}' for '{profile['title']}'"
            )

    def test_slugify_basic(self):
        assert _slugify(16498, "Attack on Titan") == "16498-attack-on-titan"

    def test_slugify_special_characters(self):
        assert _slugify(1, "Steins;Gate") == "1-steinsgate"

    def test_slugify_empty_title(self):
        assert _slugify(999, "") == "999"
        assert _slugify(999, None) == "999"

    def test_slugify_unicode(self):
        slug = _slugify(100, "Re:ZERO -Starting Life in Another World-")
        assert slug.startswith("100-")
        assert " " not in slug


class TestBayesianScoring:
    """Validates the Bayesian quality score calculation."""

    def test_high_vote_show_close_to_raw(self):
        """Shows with millions of votes should have quality_score ≈ raw_mal_score."""
        score = _bayesian_score(8.5, 2_000_000)
        assert abs(score - 8.5) < 0.1

    def test_low_vote_show_pulled_toward_mean(self):
        """Shows with few votes should be pulled toward 7.05."""
        score = _bayesian_score(9.0, 100)
        assert score < 8.0  # Heavily shrunk toward 7.05

    def test_none_inputs_return_zero(self):
        assert _bayesian_score(None, 1000) == 0.0
        assert _bayesian_score(8.0, None) == 0.0

    def test_quality_scores_in_valid_range(self):
        """All exported quality_scores should be between 1.0 and 10.0 (or 0.0 for missing)."""
        data = _get_data()
        for profile in data["anime"]:
            qs = profile["quality_score"]
            assert qs == 0.0 or (1.0 <= qs <= 10.0), (
                f"quality_score {qs} out of range for '{profile['title']}'"
            )
