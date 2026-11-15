"""
Retrieval quality regression tests.
Verifies that known-good queries return expected results after any pipeline changes.
These tests require GOOGLE_API_KEY (they hit the embedding API for query embedding).
Mark with @pytest.mark.api to allow skipping when offline.

Run all:       pytest tests/
Run offline:   pytest tests/ -m "not api"
Run API only:  pytest tests/ -m api
"""

import os
import sys
import pytest

TESTS_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

DB_PATH = os.path.join(ROOT_DIR, "data", "anime_intelligence_v2.db")


def _has_api_key():
    """Check if GOOGLE_API_KEY is available."""
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT_DIR, "env_variables.env"))
    return bool(os.environ.get("GOOGLE_API_KEY"))


api = pytest.mark.api
skip_no_key = pytest.mark.skipif(not _has_api_key(), reason="GOOGLE_API_KEY not set")


# ---------------------------------------------------------------------
# Module-level test helpers
# ---------------------------------------------------------------------

def _minimal_profile(title="Test Show", overrides=None):
    """Build a minimal fusion profile dict for reranker tests. The default
    shape covers every key the four rerank prompts read from a profile;
    override only what the test cares about."""
    profile = {
        "title": title, "synopsis": "X",
        "audience_consensus": "Y", "controversy_score": 3,
        "semantic_similarity": 0.85, "scored_by": 50_000,
        "quality_score": 8.0, "audience_sentiment": 0.5,
    }
    if overrides:
        profile.update(overrides)
    return profile


def _capture_rerank_gemini_call(method_name, profile_overrides=None):
    """Drive an engine._rerank_* method with a single-profile batch and a
    mocked Gemini client, then return the full kwargs dict that was passed
    to generate_content. Callers pick out ``contents`` (the prompt) or
    ``config`` (temperature, response_schema, ...) as needed."""
    from unittest.mock import patch, MagicMock
    from analysis.recommendation_engine import RecommendationEngine

    engine = RecommendationEngine(api_key="test-not-used")
    profile = _minimal_profile("Test Show", profile_overrides)

    mock_response = MagicMock()
    mock_response.parsed = []
    with patch.object(engine.client.models, "generate_content",
                      return_value=mock_response) as mock_gen:
        method = getattr(engine, method_name)
        if method_name == "_rerank_constrained_candidates":
            method("test query", "Quality above 8.0", [profile])
        elif method_name == "_rerank_dna_candidates":
            method("Shared themes", ["Cowboy Bebop"], [profile])
        else:
            method("test query", [profile])
        return mock_gen.call_args.kwargs


# =====================================================================
# FAISS INDEX INTEGRITY
# =====================================================================

class TestFAISSIndex:
    """Verify the FAISS index and metadata are consistent."""

    def test_index_exists(self):
        import faiss
        index_path = os.path.join(ROOT_DIR, "data", "anime_vector_index.faiss")
        assert os.path.exists(index_path), "FAISS index file missing"
        index = faiss.read_index(index_path)
        assert index.ntotal > 6000, f"Index has only {index.ntotal} vectors (expected ~6236)"

    def test_metadata_matches_index(self):
        import json
        import faiss
        index_path = os.path.join(ROOT_DIR, "data", "anime_vector_index.faiss")
        meta_path = os.path.join(ROOT_DIR, "data", "anime_vector_metadata.json")

        index = faiss.read_index(index_path)
        with open(meta_path, "r") as f:
            metadata = json.load(f)

        assert len(metadata) == index.ntotal, (
            f"Metadata ({len(metadata)}) != index ({index.ntotal})"
        )

    def test_embedding_dimensions(self):
        import faiss
        index_path = os.path.join(ROOT_DIR, "data", "anime_vector_index.faiss")
        index = faiss.read_index(index_path)
        assert index.d == 3072, f"Expected 3072 dims (gemini-embedding-001), got {index.d}"


# =====================================================================
# OBJECTIVE RANKINGS GENRE PRECISION
# =====================================================================

class TestObjectiveRankingsGenres:
    """Verify that Objective Rankings genre filter uses authoritative MAL tags only."""

    def test_sports_returns_only_sports(self, db_conn):
        """No non-sports shows in 'top sports anime' results."""
        from analysis.vector_store import objective_rankings_search
        results = objective_rankings_search("top sports anime", top_k=20)
        sports_tags = {"Sports", "Team Sports", "Combat Sports", "Racing"}
        for r in results:
            genres = db_conn.execute(
                "SELECT genres FROM anime_info WHERE english_title = ?", (r["title"],)
            ).fetchone()
            assert genres and genres[0], f"No genres for {r['title']}"
            show_tags = {t.strip() for t in genres[0].split(",")}
            assert show_tags & sports_tags, (
                f"'{r['title']}' has genres [{genres[0]}] — none are sports tags"
            )

    def test_mecha_returns_only_mecha(self, db_conn):
        """No non-mecha shows in 'best mecha anime' results."""
        from analysis.vector_store import objective_rankings_search
        results = objective_rankings_search("best mecha anime", top_k=20)
        for r in results:
            genres = db_conn.execute(
                "SELECT genres FROM anime_info WHERE english_title = ?", (r["title"],)
            ).fetchone()
            assert genres and genres[0], f"No genres for {r['title']}"
            assert "Mecha" in genres[0], (
                f"'{r['title']}' has genres [{genres[0]}] — missing Mecha tag"
            )

    def test_comedy_returns_only_comedy(self, db_conn):
        """No non-comedy shows in 'top comedy anime' results."""
        from analysis.vector_store import objective_rankings_search
        results = objective_rankings_search("top comedy anime", top_k=20)
        comedy_tags = {"Comedy", "Gag Humor", "Parody"}
        for r in results:
            genres = db_conn.execute(
                "SELECT genres FROM anime_info WHERE english_title = ?", (r["title"],)
            ).fetchone()
            assert genres and genres[0], f"No genres for {r['title']}"
            show_tags = {t.strip() for t in genres[0].split(",")}
            assert show_tags & comedy_tags, (
                f"'{r['title']}' has genres [{genres[0]}] — none are comedy tags"
            )

    def test_temporal_filter_works(self, db_conn):
        """Year filter should restrict results to the correct year."""
        from analysis.vector_store import objective_rankings_search
        results = objective_rankings_search("top anime from 2023", top_k=10)
        assert len(results) > 0
        for r in results:
            year = db_conn.execute(
                "SELECT release_year FROM anime_info WHERE english_title = ?",
                (r["title"],)
            ).fetchone()
            assert year and year[0] == 2023, (
                f"'{r['title']}' has release_year={year[0] if year else 'NULL'}, expected 2023"
            )

    def test_no_genre_returns_global_top(self):
        """Queries without genre terms should return global top by MAL score."""
        from analysis.vector_store import objective_rankings_search
        results = objective_rankings_search("top anime", top_k=5)
        assert len(results) == 5

    def test_genre_mapping_coverage(self):
        """Every concept in _CONCEPT_VOCABULARY should have a MAL genre mapping."""
        from analysis.vector_store import _CONCEPT_TO_MAL_GENRES, _CONCEPT_VOCABULARY
        unmapped = set(_CONCEPT_VOCABULARY.keys()) - set(_CONCEPT_TO_MAL_GENRES.keys())
        assert not unmapped, f"Concepts without MAL genre mapping: {unmapped}"

    def test_season_genre_combo_returns_results(self):
        """Season + genre queries must not return empty (vibe gate removed)."""
        from analysis.vector_store import objective_rankings_search
        results = objective_rankings_search("top sports anime from winter 2023")
        assert len(results) > 0, "Winter 2023 sports should return results"

    def test_top_n_limits_results(self):
        """'top N' in query should cap results at N."""
        from analysis.vector_store import objective_rankings_search
        results = objective_rankings_search("top 3 romance anime")
        assert len(results) == 3, f"Expected 3 results, got {len(results)}"

    def test_top_n_parsing(self):
        """_parse_objective_query should extract top_n from queries."""
        from analysis.vector_store import _parse_objective_query
        assert _parse_objective_query("top 10 mecha anime")["top_n"] == 10
        assert _parse_objective_query("top 5 anime from 2023")["top_n"] == 5
        assert _parse_objective_query("best anime")["top_n"] is None

    def test_between_year_range_parsing(self):
        """'between YYYY and YYYY' should parse as a year range, not exact year."""
        from analysis.vector_store import _parse_objective_query
        parsed = _parse_objective_query("top 15 mecha between 2009 and 2016")
        assert parsed["year_min"] == 2009
        assert parsed["year_max"] == 2016
        assert parsed["year"] is None, "Should be a range, not an exact year"

    def test_year_range_with_genre(self, db_conn):
        """Genre + year range queries should return results within the range."""
        from analysis.vector_store import objective_rankings_search
        results = objective_rankings_search("top 5 mecha anime from 2008 to 2015")
        assert len(results) == 5, f"Expected 5 results, got {len(results)}"
        for r in results:
            row = db_conn.execute(
                "SELECT release_year, genres FROM anime_info WHERE english_title = ?",
                (r["title"],)
            ).fetchone()
            assert row, f"Show not found: {r['title']}"
            assert 2008 <= row[0] <= 2015, f"'{r['title']}' year {row[0]} outside 2008-2015"
            assert "Mecha" in row[1], f"'{r['title']}' missing Mecha tag"


# =====================================================================
# NUMERIC CONSTRAINT PARSING
# =====================================================================

class TestNumericConstraintParsing:
    """Verify that _extract_filters correctly parses quality and sentiment constraints."""

    def test_quality_range_between(self):
        from analysis.vector_store import _extract_filters
        f = _extract_filters("mecha with quality score between 6.5 and 7.5")
        assert f["quality_min"] == 6.5
        assert f["quality_max"] == 7.5
        assert f["has_numeric_constraints"] is True

    def test_quality_above(self):
        from analysis.vector_store import _extract_filters
        f = _extract_filters("romance anime with score above 8")
        assert f["quality_min"] == 8.0
        assert f["quality_max"] is None
        assert f["has_numeric_constraints"] is True

    def test_quality_under(self):
        from analysis.vector_store import _extract_filters
        f = _extract_filters("horror anime with quality under 7")
        assert f["quality_min"] is None
        assert f["quality_max"] == 7.0
        assert f["has_numeric_constraints"] is True

    def test_high_mood(self):
        from analysis.vector_store import _extract_filters
        f = _extract_filters("comedy with high mood")
        assert f["sentiment_min"] == 0.3
        assert f["has_numeric_constraints"] is True

    def test_negative_sentiment(self):
        from analysis.vector_store import _extract_filters
        f = _extract_filters("dark anime with negative sentiment")
        assert f["sentiment_max"] == -0.1
        assert f["has_numeric_constraints"] is True

    def test_no_numeric_in_vibe_query(self):
        """Pure vibe queries must NOT trigger numeric constraints."""
        from analysis.vector_store import _extract_filters
        f = _extract_filters("gritty military drama")
        assert f["has_numeric_constraints"] is False
        assert f["quality_min"] is None
        assert f["quality_max"] is None

    def test_no_numeric_in_temporal_query(self):
        """Temporal queries must NOT trigger numeric constraints."""
        from analysis.vector_store import _extract_filters
        f = _extract_filters("seinen anime after 2020")
        assert f["has_numeric_constraints"] is False

    def test_stripped_query_is_clean(self):
        """Numeric constraint phrases should be stripped from embedding query."""
        from analysis.vector_store import _extract_filters, _strip_filter_terms
        f = _extract_filters("mecha shows with quality score between 6.5 and 7.5 but high mood score")
        stripped = _strip_filter_terms("mecha shows with quality score between 6.5 and 7.5 but high mood score", f)
        assert "6.5" not in stripped
        assert "7.5" not in stripped
        assert "quality" not in stripped.lower()
        assert "mood" not in stripped.lower()
        assert "mecha" in stripped.lower()


class TestPopularityAndStudioParsing:
    """Verify that _extract_filters correctly parses popularity and studio constraints."""

    def test_underrated_sets_scored_by_max(self):
        from analysis.vector_store import _extract_filters
        f = _extract_filters("underrated romance anime")
        assert f["scored_by_max"] == 50_000
        assert f["has_filters"] is True

    def test_obscure_sets_scored_by_max(self):
        from analysis.vector_store import _extract_filters
        f = _extract_filters("obscure horror anime")
        assert f["scored_by_max"] == 50_000

    def test_popular_sets_scored_by_min(self):
        from analysis.vector_store import _extract_filters
        f = _extract_filters("popular action anime")
        assert f["scored_by_min"] == 100_000
        assert f["has_filters"] is True

    def test_fewer_than_50k_ratings(self):
        from analysis.vector_store import _extract_filters
        f = _extract_filters("anime with fewer than 50k ratings")
        assert f["scored_by_max"] == 50_000

    def test_more_than_100k_ratings(self):
        from analysis.vector_store import _extract_filters
        f = _extract_filters("shows with more than 100k ratings")
        assert f["scored_by_min"] == 100_000

    def test_studio_by_mappa(self):
        from analysis.vector_store import _extract_filters
        f = _extract_filters("anything by MAPPA")
        assert f["studio"].lower() == "mappa"
        assert f["has_filters"] is True

    def test_studio_bones(self):
        from analysis.vector_store import _extract_filters
        f = _extract_filters("studio Bones anime")
        assert f["studio"].lower() == "bones"

    def test_studio_from_sunrise(self):
        from analysis.vector_store import _extract_filters
        f = _extract_filters("mecha from Sunrise")
        assert f["studio"].lower() == "sunrise"

    def test_no_false_positive_studio(self):
        from analysis.vector_store import _extract_filters
        f = _extract_filters("gritty military drama")
        assert f.get("studio") is None

    def test_no_false_positive_popularity(self):
        from analysis.vector_store import _extract_filters
        f = _extract_filters("gritty military drama")
        assert f.get("scored_by_min") is None
        assert f.get("scored_by_max") is None

    def test_stripped_query_removes_popularity_terms(self):
        from analysis.vector_store import _extract_filters, _strip_filter_terms
        f = _extract_filters("underrated mecha anime")
        stripped = _strip_filter_terms("underrated mecha anime", f)
        assert "underrated" not in stripped.lower()
        assert "mecha" in stripped.lower()

    def test_stripped_query_removes_studio(self):
        from analysis.vector_store import _extract_filters, _strip_filter_terms
        f = _extract_filters("action anime by MAPPA")
        stripped = _strip_filter_terms("action anime by MAPPA", f)
        assert "mappa" not in stripped.lower()
        assert "action" in stripped.lower()

    # --- Pattern 2 tests (bare studio + anime/shows) ---
    def test_studio_sunrise_anime(self):
        """'Sunrise anime' should detect Sunrise as a studio."""
        from analysis.vector_store import _extract_filters
        f = _extract_filters("Sunrise anime with high mood")
        assert f["studio"] is not None
        assert f["studio"].lower() == "sunrise"

    def test_studio_madhouse_shows(self):
        from analysis.vector_store import _extract_filters
        f = _extract_filters("Madhouse shows")
        assert f["studio"].lower() == "madhouse"

    def test_no_false_positive_genre_anime(self):
        """'Action anime' should NOT detect 'Action' as a studio."""
        from analysis.vector_store import _extract_filters
        f = _extract_filters("Action anime after 2020")
        assert f.get("studio") is None

    def test_no_false_positive_romance_shows(self):
        """'Romance shows' should NOT detect 'Romance' as a studio."""
        from analysis.vector_store import _extract_filters
        f = _extract_filters("Romance shows from 2010")
        assert f.get("studio") is None

    def test_no_false_positive_top_anime(self):
        """'Top anime' should NOT detect 'Top' as a studio."""
        from analysis.vector_store import _extract_filters
        f = _extract_filters("Top anime of all time")
        assert f.get("studio") is None

    # --- Anime medium/format negation (TV, OVA, movie) ---
    def test_no_false_positive_tv_series(self):
        """'TV series' must not surface 'TV' as a studio — the resulting
        `studio LIKE '%TV%'` clause zeros the filtered pool and silently
        drops UI era bounds to the unfiltered fallback."""
        from analysis.vector_store import _extract_filters
        f = _extract_filters("90s TV series with mature themes")
        assert f.get("studio") is None

    def test_no_false_positive_ova_anime(self):
        from analysis.vector_store import _extract_filters
        f = _extract_filters("OVA anime with mecha")
        assert f.get("studio") is None

    def test_no_false_positive_movie_shows(self):
        from analysis.vector_store import _extract_filters
        f = _extract_filters("movie shows from Ghibli")
        assert (f.get("studio") or "").lower() != "movie"

    def test_ui_era_bounds_reach_filtered_path_for_tv_series_query(self):
        """End-to-end guard: with a UI era bound and a "TV series" query,
        the filtered SQL path must fire (not the unfiltered fallback).
        Regression for the studio-negation bug that silently dropped
        year_min/year_max when 'TV' was mis-captured as a studio."""
        from analysis import vector_store
        result = vector_store.search(
            "90s TV series with mature themes",
            top_k=10,
            year_min=1990,
            year_max=1999,
        )
        assert result["meta"]["retrieval_path"].startswith("filtered:"), (
            f"expected filtered path; got {result['meta']['retrieval_path']!r}"
        )
        assert "after=1990" in result["meta"]["retrieval_path"]
        assert "before=1999" in result["meta"]["retrieval_path"]

    # --- Objective Rankings studio detection ---
    def test_objective_query_studio_by_mappa(self):
        """Objective Rankings should detect studio in 'top 10 by MAPPA'."""
        from analysis.vector_store import _parse_objective_query
        parsed = _parse_objective_query("top 10 by MAPPA")
        assert parsed.get("studio") is not None
        assert parsed["studio"].lower() == "mappa"
        assert parsed["top_n"] == 10

    def test_objective_query_studio_sunrise_anime(self):
        """Objective Rankings should detect studio in 'top 10 Sunrise anime'."""
        from analysis.vector_store import _parse_objective_query
        parsed = _parse_objective_query("top 10 Sunrise anime")
        assert parsed.get("studio") is not None
        assert parsed["studio"].lower() == "sunrise"

    def test_objective_rankings_studio_filter(self, db_conn):
        """objective_rankings_search should filter by studio."""
        from analysis.vector_store import objective_rankings_search
        results = objective_rankings_search("top 10 by Sunrise", top_k=10)
        assert len(results) > 0
        # Verify all results are actually Sunrise shows
        for r in results:
            row = db_conn.execute(
                "SELECT studio FROM anime_info WHERE english_title = ?",
                (r["title"],)
            ).fetchone()
            assert row is not None, f"{r['title']} not found in DB"
            assert "sunrise" in row[0].lower(), f"{r['title']} studio is {row[0]}, not Sunrise"


@skip_no_key
class TestConstrainedSearch:
    """Verify that constrained searches enforce numeric bounds at retrieval level."""

    @api
    def test_quality_range_enforced(self, db_conn):
        """All results must have at least one DB entry with quality in the specified range.
        Some titles have multiple entries (e.g., Broken Blade) — at least one must match."""
        from analysis.vector_store import search
        response = search("mecha with quality score between 6.5 and 7.5", top_k=10)
        assert response["meta"].get("has_numeric_constraints") is True
        for r in response["results"]:
            rows = db_conn.execute(
                "SELECT mal_score, scored_by FROM anime_info WHERE english_title = ?",
                (r["title"],)
            ).fetchall()
            assert rows, f"Show not found: {r['title']}"
            # At least one entry must have quality in range (handles duplicate titles)
            scores = []
            for row in rows:
                v, m, C = row[1], 5000, 7.05
                q = round((v / (v + m)) * row[0] + (m / (v + m)) * C, 1)
                scores.append(q)
            assert any(6.5 <= q <= 7.5 for q in scores), (
                f"'{r['title']}' quality scores {scores} — none within 6.5-7.5"
            )

    @api
    def test_standard_search_not_affected(self):
        """A pure vibe query must NOT use the constrained path."""
        from analysis.vector_store import search
        response = search("gritty military drama", top_k=5)
        assert response["meta"].get("has_numeric_constraints") in (None, False)
        assert len(response["results"]) > 0


# =====================================================================
# SEARCH QUALITY REGRESSION
# =====================================================================

@skip_no_key
class TestSearchQuality:
    """Known-good queries that must return plausible results."""

    @api
    def test_mecha_query_returns_mecha(self):
        from analysis.vector_store import search
        response = search("mecha anime with giant robots", top_k=10)
        assert "results" in response and "meta" in response, "search() must return {results, meta}"
        results = response["results"]
        titles = [r["title"] for r in results]
        mecha_keywords = ["Gundam", "Evangelion", "Code Geass", "86", "Gurren Lagann", "Franxx",
                          "Mazinger", "Mazinkaiser", "Tetsujin", "Sidonia", "Dynazenon", "Kuromukuro",
                          "Broken Blade", "Megaton", "Heavy Object", "GaoGaiGar"]
        hits = sum(1 for t in titles if any(k.lower() in t.lower() for k in mecha_keywords))
        assert hits >= 2, f"Expected >=2 mecha shows in top 10, got {hits}. Titles: {titles}"

    @api
    def test_sports_query_returns_sports(self):
        from analysis.vector_store import search
        response = search("intense sports competition", top_k=10)
        results = response["results"]
        titles = [r["title"] for r in results]
        sports_keywords = ["Haikyuu", "Blue Lock", "Slam Dunk", "Kuroko", "Aoashi", "Run with the Wind"]
        hits = sum(1 for t in titles if any(k.lower() in t.lower() for k in sports_keywords))
        assert hits >= 2, f"Expected >=2 sports shows in top 10, got {hits}. Titles: {titles}"

    @api
    def test_filtered_search_respects_year(self, db_conn):
        """Every result must actually be a 2024 show — the year-filter
        path is the SQL pre-filter that runs before FAISS retrieval."""
        from analysis.vector_store import search
        response = search("action anime from 2024", top_k=10)
        results = response["results"]
        assert len(results) > 0, "Filtered search returned no results"
        for r in results:
            row = db_conn.execute(
                "SELECT release_year FROM anime_info WHERE english_title = ?",
                (r["title"],),
            ).fetchone()
            assert row and row[0] == 2024, (
                f"'{r['title']}' has release_year={row[0] if row else 'NULL'}, "
                f"expected 2024 — year-filter path leaked a non-2024 show"
            )

    @api
    def test_search_returns_similarity_scores(self):
        from analysis.vector_store import search
        response = search("dark psychological thriller", top_k=5)
        results = response["results"]
        assert len(results) > 0
        for r in results:
            assert "title" in r and "similarity" in r
            assert 0.0 <= r["similarity"] <= 1.0, f"Similarity out of range: {r['similarity']}"

    @api
    def test_search_returns_retrieval_metadata(self):
        from analysis.vector_store import search
        response = search("romance anime", top_k=5)
        meta = response["meta"]
        assert "retrieval_path" in meta, "meta must include retrieval_path"
        assert "candidates_retrieved" in meta, "meta must include candidates_retrieved"
        assert meta["candidates_retrieved"] > 0


# =====================================================================
# DNA CENTROID SEARCH
# =====================================================================

class TestDNACentroidSearch:
    """Centroid search uses pre-computed vectors — no API key needed."""

    def test_centroid_returns_results(self):
        from analysis.vector_store import search_by_centroid
        response = search_by_centroid(["Attack on Titan"], top_k=10)
        assert "results" in response, "Centroid response must contain 'results' key"
        assert "meta" in response, "Centroid response must contain 'meta' key"
        assert len(response["results"]) > 0, "Centroid search returned no results"

    def test_centroid_excludes_reference(self):
        from analysis.vector_store import search_by_centroid
        response = search_by_centroid(["Attack on Titan"], top_k=10)
        titles = [r["title"] for r in response["results"]]
        assert "Attack on Titan" not in titles, "Reference show should be excluded"

    def test_centroid_multi_show(self):
        from analysis.vector_store import search_by_centroid
        response = search_by_centroid(["Attack on Titan", "Death Note"], top_k=10)
        assert len(response["results"]) > 0, "Multi-show centroid returned no results"

    def test_centroid_unknown_show(self):
        from analysis.vector_store import search_by_centroid
        response = search_by_centroid(["This Show Does Not Exist 12345"], top_k=10)
        assert response["results"] == [], "Unknown show should return empty results"

    # --- negative_anchors behaviour ---

    def test_centroid_with_negative_anchors_returns_results(self):
        from analysis.vector_store import search_by_centroid
        response = search_by_centroid(
            ["Attack on Titan"],
            top_k=10,
            negative_anchors=["Death Note"],
        )
        assert "results" in response
        assert "meta" in response
        assert len(response["results"]) > 0, "Negative-anchored centroid should still return results"

    def test_centroid_excludes_negative_anchors_from_output(self):
        """Negative anchors should be excluded from results, same as positive references."""
        from analysis.vector_store import search_by_centroid
        response = search_by_centroid(
            ["Attack on Titan"],
            top_k=50,
            negative_anchors=["Death Note"],
        )
        titles = [r["title"] for r in response["results"]]
        assert "Attack on Titan" not in titles, "Reference show should be excluded"
        assert "Death Note" not in titles, "Negative anchor should be excluded from results"

    def test_centroid_meta_reports_negative_anchor_count(self):
        from analysis.vector_store import search_by_centroid
        response = search_by_centroid(
            ["Attack on Titan"],
            top_k=10,
            negative_anchors=["Death Note"],
        )
        assert response["meta"]["negative_anchor_count"] == 1
        assert "prefiltered_count" in response["meta"]

    def test_centroid_unresolvable_negative_silently_skipped(self):
        """An unresolvable negative anchor is logged-and-skipped, not raised."""
        from analysis.vector_store import search_by_centroid
        response = search_by_centroid(
            ["Attack on Titan"],
            top_k=10,
            negative_anchors=["This Negative Does Not Exist 99999"],
        )
        assert len(response["results"]) > 0, "Positive centroid should still run"
        assert response["meta"]["negative_anchor_count"] == 0

    def test_centroid_none_negative_anchors_matches_omitted_baseline(self):
        """Passing negative_anchors=None must produce the same titles as omitting the kwarg."""
        from analysis.vector_store import search_by_centroid
        baseline = search_by_centroid(["Steins;Gate"], top_k=10)
        with_none = search_by_centroid(["Steins;Gate"], top_k=10, negative_anchors=None)
        baseline_titles = [r["title"] for r in baseline["results"]]
        with_none_titles = [r["title"] for r in with_none["results"]]
        assert baseline_titles == with_none_titles


# =====================================================================
# DNA RERANKER PROMPT — NEGATIVE ANCHORS BLOCK
# =====================================================================

class TestDNARerankerPrompt:
    """Verify DNA_RERANKER_PROMPT correctly templates the negative_anchors block."""

    def test_prompt_has_negative_anchors_placeholder(self):
        from analysis.prompts import DNA_RERANKER_PROMPT
        assert "{negative_anchors_block}" in DNA_RERANKER_PROMPT, (
            "DNA reranker prompt must have a {negative_anchors_block} placeholder "
            "so the recommendation engine can inject avoid-show context."
        )

    def test_prompt_formats_with_empty_block(self):
        """Empty negative_anchors_block must format cleanly without AVOID-RESEMBLING text."""
        from analysis.prompts import DNA_RERANKER_PROMPT
        formatted = DNA_RERANKER_PROMPT.format(
            reference_titles="Show A",
            intersection_summary="Test summary",
            negative_anchors_block="",
            candidates_block="Title: Foo\n",
        )
        assert "AVOID-RESEMBLING" not in formatted
        assert "Show A" in formatted
        assert "Test summary" in formatted

    def test_prompt_formats_with_negative_anchors_block(self):
        """When the block carries content, the AVOID-RESEMBLING section appears."""
        from analysis.prompts import DNA_RERANKER_PROMPT
        block = (
            "\nAVOID-RESEMBLING SHOWS: Bad Show, Worse Show\n"
            "The user explicitly indicated these shows do NOT match what they want.\n"
        )
        formatted = DNA_RERANKER_PROMPT.format(
            reference_titles="Show A",
            intersection_summary="Test summary",
            negative_anchors_block=block,
            candidates_block="Title: Foo\n",
        )
        assert "AVOID-RESEMBLING SHOWS: Bad Show, Worse Show" in formatted
        assert "do NOT match" in formatted


# =====================================================================
# TRIANGULATION PROMPT — NEGATIVE ANCHORS BLOCK
# =====================================================================

class TestTriangulationPrompt:
    """Verify TRIANGULATION_PROMPT correctly templates the negative_anchors block.

    This is a separate concern from DNA_RERANKER_PROMPT: TRIANGULATION_PROMPT
    produces the user-visible 'intersection_summary' (the 'DNA Synthesis
    Complete' banner). Without the negative-anchors block here, the summary
    has no way to acknowledge the user's avoid-list — even when the reranker
    correctly factors it into per-show scoring.
    """

    def test_prompt_has_negative_anchors_placeholder(self):
        from analysis.prompts import TRIANGULATION_PROMPT
        assert "{negative_anchors_block}" in TRIANGULATION_PROMPT, (
            "TRIANGULATION_PROMPT must template {negative_anchors_block} so the "
            "engine can inject avoid-show context into the user-visible summary."
        )

    def test_prompt_formats_with_empty_block(self):
        """Empty block must format cleanly — no AVOID-RESEMBLING text leaks through."""
        from analysis.prompts import TRIANGULATION_PROMPT
        formatted = TRIANGULATION_PROMPT.format(
            trinity_data="SHOW: Foo\nSYNOPSIS: bar\n",
            task_directive="Analyze the core thematic DNA of this show.",
            negative_anchors_block="",
        )
        assert "AVOID-RESEMBLING" not in formatted
        assert "intersection_summary" in formatted

    def test_prompt_formats_with_negative_anchors_block(self):
        """When the block carries content, the summary instruction acknowledges the contrast."""
        from analysis.prompts import TRIANGULATION_PROMPT
        block = (
            "\nAVOID-RESEMBLING SHOWS: Bad Show, Worse Show\n"
            "The user explicitly wants results that do NOT resemble these shows.\n"
        )
        formatted = TRIANGULATION_PROMPT.format(
            trinity_data="SHOW: Foo\n",
            task_directive="Analyze the shared thematic DNA between these 2 shows.",
            negative_anchors_block=block,
        )
        assert "AVOID-RESEMBLING SHOWS: Bad Show, Worse Show" in formatted
        # The summary instruction itself must reference the avoid-shows so the
        # LLM is steered toward producing contrast language in the output.
        assert "avoid-shows" in formatted or "avoiding" in formatted.lower()


# =====================================================================
# CONFIDENCE FLOOR VALIDATION
# =====================================================================

@skip_no_key
class TestConfidenceFloors:
    """Verify that confidence floors filter low-quality recommendations."""

    @api
    def test_find_a_show_floor_50(self):
        """All results from fetch_vault_pool must have match_confidence >= 50."""
        from analysis.recommendation_engine import RecommendationEngine
        from dotenv import load_dotenv
        load_dotenv(os.path.join(ROOT_DIR, "env_variables.env"))
        api_key = os.environ.get("GOOGLE_API_KEY")

        engine = RecommendationEngine(api_key=api_key)
        result = engine.fetch_vault_pool("gritty mecha drama", "Intelligent Search")

        # Assert an actual pool exists — the floor can't be validated on
        # an empty pool. If this fails, either the engine returned empty
        # (real signal worth investigating) or the query needs to be
        # swapped for one that reliably returns results.
        assert result["success"] is True, (
            f"Search failed: {result.get('error', 'no error message')}"
        )
        assert result["pool"], (
            "Pool empty — cannot validate the confidence floor. "
            "If the engine is legitimately returning no results for "
            "'gritty mecha drama', swap in a more reliable query."
        )
        for item in result["pool"]:
            assert item["match_confidence"] >= 50, (
                f"Show '{item['profile']['title']}' has confidence "
                f"{item['match_confidence']} (below 50% floor)"
            )


# =====================================================================
# INTENT CLASSIFICATION
# =====================================================================

class TestIntentClassification:
    """Verify that _classify_intent correctly categorizes queries."""

    def test_vibe_query(self):
        from analysis.vector_store import _classify_intent
        # "atmospheric" is now in _HIDDEN_GEM_SIGNALS; a freeform query
        # with no signal words keeps this a pure vibe test.
        intent, concepts = _classify_intent("something dark and moody")
        assert intent == "vibe"

    def test_single_genre_is_vibe(self):
        from analysis.vector_store import _classify_intent
        intent, concepts = _classify_intent("best romance anime")
        assert intent == "vibe"
        assert "romance" in concepts

    def test_genre_combo_detected(self):
        from analysis.vector_store import _classify_intent
        intent, concepts = _classify_intent("mecha anime with romance aspects")
        assert intent == "genre_combo"
        assert "mecha" in concepts
        assert "romance" in concepts

    def test_hidden_gems_detected(self):
        from analysis.vector_store import _classify_intent
        for signal in ["hidden gem", "underrated anime", "overlooked shows", "obscure sci-fi"]:
            intent, _ = _classify_intent(signal)
            assert intent == "hidden_gems", f"Failed for: '{signal}'"

    def test_hidden_gems_latent_signals_detected(self):
        """Queries with distinctive niche-taste vocabulary should route to
        hidden_gems even without explicit 'hidden gem' / 'underrated'
        language. Real users write queries like the ones below, not
        'hidden gem philosophical anime'."""
        from analysis.vector_store import _classify_intent
        latent_signals = [
            "quiet, contemplative anime with beautiful visuals",
            "experimental animation with unconventional storytelling",
            "short-form series with philosophical depth",
            "atmospheric drama",
            "avant-garde animation",
            "arthouse anime",
            "meditative slice of life",
            "cerebral sci-fi",
            "understated coming-of-age",
            "niche seinen",
        ]
        for q in latent_signals:
            intent, _ = _classify_intent(q)
            assert intent == "hidden_gems", f"Failed for: '{q}'"

    def test_crowd_pleasers_detected(self):
        from analysis.vector_store import _classify_intent
        for signal in ["crowd pleaser anime", "universally loved", "most popular romance"]:
            intent, _ = _classify_intent(signal)
            assert intent == "crowd_pleasers", f"Failed for: '{signal}'"

    def test_polarizing_detected(self):
        from analysis.vector_store import _classify_intent
        for signal in ["polarizing anime", "controversial shows", "love it or hate it"]:
            intent, _ = _classify_intent(signal)
            assert intent == "polarizing", f"Failed for: '{signal}'"

    def test_intent_priority_hidden_over_genre(self):
        """Hidden gems intent should take priority over genre combo detection."""
        from analysis.vector_store import _classify_intent
        intent, _ = _classify_intent("underrated mecha romance anime")
        assert intent == "hidden_gems"

    def test_freeform_query_is_vibe(self):
        """Queries without recognizable keywords should default to vibe."""
        from analysis.vector_store import _classify_intent
        intent, _ = _classify_intent("anime that makes you think about life")
        assert intent == "vibe"

    def test_no_false_trigger_on_substring(self):
        """Word boundaries should prevent 'transport' from triggering 'sport'."""
        from analysis.vector_store import _classify_intent, _detect_concepts
        # "transport" contains "sport" as substring but is not sports
        concepts = _detect_concepts("transport anime")
        assert "sports" not in concepts, "'transport' should not trigger 'sport'"

        # "dramatic" contains "drama" — but "dramatic" IS a drama synonym so this should match
        concepts2 = _detect_concepts("dramatic anime")
        assert "drama" in concepts2

        # "combative" is not in vocabulary, should not trigger "combat" → "action"
        concepts3 = _detect_concepts("combative personality anime")
        assert "action" not in concepts3, "'combative' should not trigger 'combat'"

    def test_since_vs_after_year_parsing(self):
        """'since 2020' should be inclusive (>=2020), 'after 2020' exclusive (>=2021)."""
        from analysis.vector_store import _parse_objective_query
        after = _parse_objective_query("top anime after 2020")
        assert after["year_min"] == 2021, f"'after 2020' should give year_min=2021, got {after['year_min']}"

        since = _parse_objective_query("top anime since 2020")
        assert since["year_min"] == 2020, f"'since 2020' should give year_min=2020, got {since['year_min']}"


# =====================================================================
# ADAPTIVE COMPOSITE WEIGHTS
# =====================================================================

class TestAdaptiveWeights:
    """Verify weight profiles exist and are applied correctly."""

    def test_all_profiles_sum_to_one(self):
        from analysis.vector_store import _WEIGHT_PROFILES
        for name, weights in _WEIGHT_PROFILES.items():
            total = sum(weights.values())
            assert abs(total - 1.0) < 0.01, (
                f"Weight profile '{name}' sums to {total}, expected 1.0"
            )

    def test_all_intents_have_profiles(self):
        from analysis.vector_store import _WEIGHT_PROFILES
        expected = {"vibe", "genre_combo", "hidden_gems", "crowd_pleasers", "polarizing", "narrative"}
        assert set(_WEIGHT_PROFILES.keys()) == expected

    def test_hidden_gems_negative_popularity(self):
        """Hidden gems profile should penalize popularity — mildly. A
        more aggressive weight surfaces low-vote / near-quality-floor
        shows instead of canonical cult classics; see
        _WEIGHT_PROFILES['hidden_gems'] docstring for the tuning."""
        from analysis.vector_store import _WEIGHT_PROFILES
        assert _WEIGHT_PROFILES["hidden_gems"]["popularity"] <= -0.03
        assert _WEIGHT_PROFILES["hidden_gems"]["popularity"] < 0.0

    def test_vibe_popularity_neutral(self):
        """The default vibe profile must not add a positive popularity
        bias — it compounded with recency to produce a 4× mainstream
        skew in the diagnostic slice."""
        from analysis.vector_store import _WEIGHT_PROFILES
        assert _WEIGHT_PROFILES["vibe"]["popularity"] == 0.0

    def test_vibe_recency_neutral(self):
        """Recency correlates with popularity in this dataset and would
        compound the mainstream skew. Zeroed to keep the default
        popularity-neutral, not anti-popular."""
        from analysis.vector_store import _WEIGHT_PROFILES
        assert _WEIGHT_PROFILES["vibe"]["recency"] == 0.0

    def test_crowd_pleasers_high_popularity(self):
        """Crowd pleasers should weight popularity much higher than default."""
        from analysis.vector_store import _WEIGHT_PROFILES
        assert _WEIGHT_PROFILES["crowd_pleasers"]["popularity"] > 0.10

    def test_genre_combo_high_input_rank(self):
        """Genre combo should weight input_rank heavily (carries AND-semantics)."""
        from analysis.vector_store import _WEIGHT_PROFILES
        assert _WEIGHT_PROFILES["genre_combo"]["input_rank"] >= 0.25

    def test_composite_rerank_accepts_weights(self):
        """_composite_rerank should work with explicit weight override."""
        from analysis.vector_store import _composite_rerank, _WEIGHT_PROFILES
        candidates = [
            {"title": "Attack on Titan", "similarity": 0.8},
            {"title": "Death Note", "similarity": 0.75},
        ]
        # Should not raise with custom weights
        result = _composite_rerank(candidates, weights=_WEIGHT_PROFILES["hidden_gems"])
        assert len(result) > 0
        assert "title" in result[0] and "similarity" in result[0]


# =====================================================================
# INTENT-AWARE SEARCH INTEGRATION (end-to-end)
# =====================================================================

@skip_no_key
class TestIntentAwareSearch:
    """Integration tests verifying intent flows through search to results."""

    @api
    def test_search_meta_includes_intent(self):
        from analysis.vector_store import search
        response = search("dark psychological thriller", top_k=5)
        assert "intent" in response["meta"], "meta must include intent field"
        assert response["meta"]["intent"] in {"vibe", "genre_combo", "hidden_gems", "crowd_pleasers", "polarizing", "narrative"}

    @api
    def test_hidden_gems_surfaces_low_popularity(self):
        """Hidden gems query should include less popular shows in top results."""
        from analysis.vector_store import search
        response = search("underrated hidden gem romance anime", top_k=15)
        assert response["meta"]["intent"] == "hidden_gems"
        assert len(response["results"]) > 0

    @api
    def test_genre_combo_intent_detected(self):
        from analysis.vector_store import search
        response = search("mecha anime with romance", top_k=10)
        assert response["meta"]["intent"] == "genre_combo"


# =====================================================================
# NARRATIVE INTENT CLASSIFICATION
# =====================================================================

class TestNarrativeIntentClassification:
    """Tests for narrative query detection in _classify_intent."""

    def test_mc_rejected_is_narrative(self):
        from analysis.vector_store import _classify_intent
        intent, _ = _classify_intent("anime where the mc is rejected but the girl becomes obsessed")
        assert intent == "narrative"

    def test_protagonist_betrayed_is_narrative(self):
        from analysis.vector_store import _classify_intent
        intent, _ = _classify_intent("MC is betrayed by their best friend")
        assert intent == "narrative"

    def test_villain_transforms_is_narrative(self):
        from analysis.vector_store import _classify_intent
        intent, _ = _classify_intent("anime where the villain discovers the truth and transforms")
        assert intent == "narrative"

    def test_looking_for_anime_where_is_narrative(self):
        from analysis.vector_store import _classify_intent
        intent, _ = _classify_intent("looking for anime where the hero is falsely accused")
        assert intent == "narrative"

    def test_vibe_query_not_narrative(self):
        """Pure vibe queries must NOT trigger narrative intent."""
        from analysis.vector_store import _classify_intent
        intent, _ = _classify_intent("dark psychological thriller")
        assert intent != "narrative"

    def test_genre_combo_not_narrative(self):
        """Multi-genre queries must NOT trigger narrative intent."""
        from analysis.vector_store import _classify_intent
        intent, _ = _classify_intent("mecha anime with romance")
        assert intent != "narrative"

    def test_cozy_vibe_not_narrative(self):
        from analysis.vector_store import _classify_intent
        intent, _ = _classify_intent("cozy slice of life with good vibes")
        assert intent != "narrative"

    def test_narrative_weight_profile_high_similarity(self):
        """Narrative profile should weight semantic similarity highest."""
        from analysis.vector_store import _WEIGHT_PROFILES
        narrative = _WEIGHT_PROFILES["narrative"]
        assert narrative["similarity"] >= 0.55
        assert narrative["similarity"] == max(narrative.values())


# =====================================================================
# NARRATIVE SEARCH INTEGRATION
# =====================================================================

@skip_no_key
class TestNarrativeSearchIntegration:
    """End-to-end tests for narrative search path."""

    @api
    def test_narrative_intent_flows_through_search(self):
        from analysis.vector_store import search
        response = search("anime where the mc is betrayed by someone they trust", top_k=10)
        assert response["meta"]["intent"] == "narrative"
        assert len(response["results"]) > 0

    @api
    def test_narrative_search_returns_results(self):
        from analysis.vector_store import search
        response = search("protagonist hides their true power at school", top_k=10)
        assert len(response["results"]) > 0

    @api
    def test_narrative_profiles_include_narrative_data(self):
        """Fusion profiles for narrative candidates should include narrative_data."""
        from analysis.vector_store import search
        from analysis.queries import fetch_fusion_profiles
        response = search("MC rejected and moves on", top_k=5)
        titles = [r["title"] for r in response["results"]]
        profiles = fetch_fusion_profiles(titles)
        # At least some profiles should have narrative data (not all shows have it)
        has_narrative = sum(1 for p in profiles if p.get("narrative_data"))
        assert has_narrative > 0, "At least one profile should have narrative_data"


class TestRatingProfileField:
    """fetch_fusion_profiles() must include mal_rating in every returned dict."""

    def test_profile_includes_mal_rating_when_populated(self):
        """Attack on Titan has a known rating post-backfill (Phase A captured)."""
        from analysis.queries import fetch_fusion_profiles
        profiles = fetch_fusion_profiles(["Attack on Titan"])
        assert len(profiles) == 1
        assert profiles[0]["mal_rating"] == "R - 17+ (violence & profanity)"

    def test_profile_mal_rating_present_in_all_profiles(self):
        """The dict key always exists — value is the string, "", or None."""
        from analysis.queries import fetch_fusion_profiles
        profiles = fetch_fusion_profiles([
            "Attack on Titan", "Cardcaptor Sakura", "Gakuen Alice"
        ])
        assert len(profiles) == 3
        for p in profiles:
            assert "mal_rating" in p, f"mal_rating key missing from profile {p['title']}"


# =====================================================================
# MAL RATING FILTER
# =====================================================================

class TestRatingBucketHelper:
    """Pure-function tests for the bucket hierarchy helper."""

    def test_none_returns_none(self):
        from analysis.vector_store import _allowed_rating_buckets
        assert _allowed_rating_buckets(None) is None

    def test_g_returns_only_g(self):
        from analysis.vector_store import _allowed_rating_buckets
        assert _allowed_rating_buckets("G") == ["G"]

    def test_pg_includes_g(self):
        from analysis.vector_store import _allowed_rating_buckets
        assert _allowed_rating_buckets("PG") == ["G", "PG"]

    def test_pg13_includes_g_pg(self):
        from analysis.vector_store import _allowed_rating_buckets
        assert _allowed_rating_buckets("PG-13") == ["G", "PG", "PG-13"]

    def test_r_includes_g_pg_pg13(self):
        from analysis.vector_store import _allowed_rating_buckets
        assert _allowed_rating_buckets("R") == ["G", "PG", "PG-13", "R"]

    def test_unknown_returns_none(self):
        """Defense-in-depth — schema validation should reject this upstream."""
        from analysis.vector_store import _allowed_rating_buckets
        assert _allowed_rating_buckets("XYZ") is None
        assert _allowed_rating_buckets("") is None


class TestRatingFilterSQL:
    """DB-level tests for the WHERE clause behavior. No embedding API needed —
    these execute the exact SQL pattern that `_filtered_vector_search`
    builds, against the live SQLite vault."""

    def _query_with_ceiling(self, db_conn, rating_max):
        """Mirrors the WHERE-clause construction in _filtered_vector_search.
        Returns the set of english_titles allowed under the ceiling."""
        from analysis.vector_store import _allowed_rating_buckets
        buckets = _allowed_rating_buckets(rating_max)
        clauses = " OR ".join(["mal_rating LIKE ?"] * len(buckets))
        sql = f"SELECT english_title FROM anime_info WHERE consensus_json IS NOT NULL AND ({clauses})"
        params = [f"{b} - %" for b in buckets]
        rows = db_conn.execute(sql, params).fetchall()
        return {r[0] for r in rows}

    # IDs 999999901-906 reserved for the synthetic rating-bucket fixtures
    # inserted by _insert_synthetic_rating_rows (one row per bucket in
    # _RATING_BUCKET_HIERARCHY). Chosen outside any plausible real MAL ID
    # range. The siblings 999999991 / 999999992 are used by
    # test_strict_null_handling / test_strict_empty_string_handling.
    _SYNTHETIC_RATING_ROWS = [
        (999999901, "SYNTHETIC_G_FIXTURE", "G - All Ages"),
        (999999902, "SYNTHETIC_PG_FIXTURE", "PG - Children"),
        (999999903, "SYNTHETIC_PG13_FIXTURE", "PG-13 - Teens 13 or older"),
        (999999904, "SYNTHETIC_R_FIXTURE", "R - 17+ (violence & profanity)"),
        (999999905, "SYNTHETIC_RPLUS_FIXTURE", "R+ - Mild Nudity"),
        (999999906, "SYNTHETIC_RX_FIXTURE", "Rx - Hentai"),
    ]

    def _insert_synthetic_rating_rows(self, db_conn):
        """Insert one synthetic anime_info row per rating bucket, with
        consensus_json='{}' so the ceiling queries' `WHERE consensus_json
        IS NOT NULL` guard still admits them. Returns a {bucket_prefix:
        english_title} mapping (e.g. {'G': 'SYNTHETIC_G_FIXTURE', ...}).

        Caller is responsible for `db_conn.rollback()` on teardown so
        the synthetic rows never persist. Follows the same pattern
        established by test_strict_null_handling (PR #188)."""
        for id_, title, rating in self._SYNTHETIC_RATING_ROWS:
            db_conn.execute(
                "INSERT INTO anime_info (id, english_title, mal_rating, consensus_json) "
                "VALUES (?, ?, ?, '{}')",
                (id_, title, rating),
            )
        return {rating.split(" - ")[0]: title for _, title, rating in self._SYNTHETIC_RATING_ROWS}

    def test_g_ceiling_yields_only_g_shows(self, db_conn):
        """G ceiling admits G-rated rows and excludes everything above.

        Uses synthetic rows across all 6 buckets so the test is
        deterministic regardless of real vault composition (PR #188 /
        this PR's pattern — see _insert_synthetic_rating_rows docstring
        for context on why real-vault assertions are brittle)."""
        synthetics = self._insert_synthetic_rating_rows(db_conn)
        try:
            allowed = self._query_with_ceiling(db_conn, "G")
            assert synthetics["G"] in allowed, "G synthetic missing under G ceiling"
            for bucket in ("PG", "PG-13", "R", "R+", "Rx"):
                assert synthetics[bucket] not in allowed, (
                    f"{bucket}-rated synthetic leaked under G ceiling"
                )
        finally:
            db_conn.rollback()

    def test_pg_ceiling_excludes_pg13(self, db_conn):
        """PG ceiling admits G + PG, excludes PG-13 and above.
        Distinct prefix matching — `PG - %` must not accidentally
        match `PG-13 - %` due to a loose LIKE pattern."""
        synthetics = self._insert_synthetic_rating_rows(db_conn)
        try:
            allowed = self._query_with_ceiling(db_conn, "PG")
            for bucket in ("G", "PG"):
                assert synthetics[bucket] in allowed, (
                    f"{bucket} synthetic missing under PG ceiling"
                )
            for bucket in ("PG-13", "R", "R+", "Rx"):
                assert synthetics[bucket] not in allowed, (
                    f"{bucket}-rated synthetic leaked under PG ceiling"
                )
        finally:
            db_conn.rollback()

    def test_r_ceiling_excludes_rplus_and_rx(self, db_conn):
        """R ceiling admits G/PG/PG-13/R, excludes R+ and Rx.
        R is the strictest user-selectable ceiling — R+ and Rx are
        intentionally not exposed as options (see
        `_allowed_rating_buckets` docstring)."""
        synthetics = self._insert_synthetic_rating_rows(db_conn)
        try:
            allowed = self._query_with_ceiling(db_conn, "R")
            for bucket in ("G", "PG", "PG-13", "R"):
                assert synthetics[bucket] in allowed, (
                    f"{bucket} synthetic missing under R ceiling"
                )
            for bucket in ("R+", "Rx"):
                assert synthetics[bucket] not in allowed, (
                    f"{bucket}-rated synthetic leaked under R ceiling"
                )
        finally:
            db_conn.rollback()

    def test_strict_null_handling(self, db_conn):
        """NULL mal_rating shows must not appear in any ceiling result —
        the LIKE clause implicitly excludes them (NULL never matches LIKE).

        Uses a synthetic row inserted in a transaction and rolled back at
        teardown so the test is deterministic regardless of whether the
        production vault currently has any NULL-rating rows (which
        fluctuates as MAL rates and un-rates airing shows — pre-this-fix,
        the test asserted `len(null_titles) > 0` as a sanity check and
        failed whenever MAL happened to have rated all airing shows)."""
        synthetic_id = 999999991
        synthetic_title = "SYNTHETIC_NULL_RATING_FIXTURE"
        db_conn.execute(
            "INSERT INTO anime_info (id, english_title, mal_rating, consensus_json) "
            "VALUES (?, ?, NULL, '{}')",
            (synthetic_id, synthetic_title),
        )
        try:
            null_titles = {row[0] for row in db_conn.execute(
                "SELECT english_title FROM anime_info WHERE mal_rating IS NULL"
            ).fetchall()}
            assert synthetic_title in null_titles, "Synthetic row not visible mid-transaction"

            # Most permissive ceiling — still must exclude NULL rows
            allowed = self._query_with_ceiling(db_conn, "R")
            leak = null_titles & allowed
            assert not leak, f"NULL-rating titles leaked under R ceiling: {leak}"
        finally:
            db_conn.rollback()  # discard synthetic row — no persistence

    def test_strict_empty_string_handling(self, db_conn):
        """Empty-string mal_rating shows (Jikan-confirmed-no-rating) must
        also be excluded — empty string fails `LIKE '<bucket> - %'`.

        Same synthetic-row + rollback pattern as test_strict_null_handling
        — removes the brittle dependency on the production vault happening
        to contain empty-string rating rows at test time."""
        synthetic_id = 999999992
        synthetic_title = "SYNTHETIC_EMPTY_RATING_FIXTURE"
        db_conn.execute(
            "INSERT INTO anime_info (id, english_title, mal_rating, consensus_json) "
            "VALUES (?, ?, '', '{}')",
            (synthetic_id, synthetic_title),
        )
        try:
            empty_titles = {row[0] for row in db_conn.execute(
                "SELECT english_title FROM anime_info WHERE mal_rating = ''"
            ).fetchall()}
            assert synthetic_title in empty_titles, "Synthetic row not visible mid-transaction"

            allowed = self._query_with_ceiling(db_conn, "R")
            leak = empty_titles & allowed
            assert not leak, f"Empty-string rating titles leaked under R ceiling: {leak}"
        finally:
            db_conn.rollback()  # discard synthetic row — no persistence

    def test_ceiling_monotonicity(self, db_conn):
        """A stricter ceiling must be a strict subset of a looser one, AND
        each ceiling must add at least one show over the previous tier.

        Uses synthetic rows so the strict-less-than assertion holds
        deterministically regardless of whether the vault happens to
        contain shows at every bucket — pre-this-PR the test depended
        on real vault composition, which can drift if MAL re-classifies
        or if a bucket's one representative show gets pruned."""
        synthetics = self._insert_synthetic_rating_rows(db_conn)
        try:
            g = self._query_with_ceiling(db_conn, "G")
            pg = self._query_with_ceiling(db_conn, "PG")
            pg13 = self._query_with_ceiling(db_conn, "PG-13")
            r = self._query_with_ceiling(db_conn, "R")
            # Nested-subset invariant (code correctness)
            assert g <= pg <= pg13 <= r, "Ceilings must be nested subsets"
            # Each tier adds at least its own synthetic — guarantees the
            # strict less-than without depending on real vault composition
            assert synthetics["G"] in g and synthetics["PG"] not in g
            assert synthetics["PG"] in pg and synthetics["PG-13"] not in pg
            assert synthetics["PG-13"] in pg13 and synthetics["R"] not in pg13
            assert synthetics["R"] in r and synthetics["R+"] not in r
            # Strict less-than at the synthetic cohort level — each ceiling
            # admits strictly more buckets than the previous one.
            synthetic_titles = set(synthetics.values())
            g_syn = g & synthetic_titles
            pg_syn = pg & synthetic_titles
            pg13_syn = pg13 & synthetic_titles
            r_syn = r & synthetic_titles
            assert len(g_syn) < len(pg_syn) < len(pg13_syn) < len(r_syn), (
                "Each ceiling must admit strictly more synthetic buckets"
            )
        finally:
            db_conn.rollback()


class TestEmptyPoolStrictTrust:
    """Unit tests for `_empty_pool_or_fallback`.

    The function's job is to pick between returning `[]` (strict trust —
    show nothing rather than violate the user's filter) and `None` (soft
    fallback — the natural-language genre/studio filter zeroed the pool,
    try unfiltered FAISS so we don't ship nothing on a reasonable query).

    Regression: 2026-10-12 production bug. "military drama" + Pre-1990
    UI filter fell through to unfiltered FAISS because pre-1990 shows
    had NULL `genres` (data gap), surfacing 2021-era shows under a
    Pre-1990 label. Fix: UI year filters joined mal_rating_max +
    exclude_mal_ids in the strict-trust list.
    """

    def test_no_filters_falls_back(self):
        from analysis.vector_store import _empty_pool_or_fallback
        assert _empty_pool_or_fallback({}, "test") is None

    def test_natural_language_genre_falls_back(self):
        """A detected-from-prose genre is soft — if the SQL zeroes,
        unfiltered FAISS is a reasonable recovery."""
        from analysis.vector_store import _empty_pool_or_fallback
        filters = {"genre_db_terms": ["Drama"]}
        assert _empty_pool_or_fallback(filters, "test") is None

    def test_rating_ceiling_is_strict_trust(self):
        from analysis.vector_store import _empty_pool_or_fallback
        filters = {"mal_rating_max": "PG"}
        assert _empty_pool_or_fallback(filters, "test") == []

    def test_exclude_mal_ids_is_strict_trust(self):
        from analysis.vector_store import _empty_pool_or_fallback
        filters = {"exclude_mal_ids": {12345}}
        assert _empty_pool_or_fallback(filters, "test") == []

    def test_year_min_only_is_strict_trust(self):
        """UI 'after 2010' bound (year_min) must not fall through to pre-2010 FAISS."""
        from analysis.vector_store import _empty_pool_or_fallback
        filters = {"year_min": 2010}
        assert _empty_pool_or_fallback(filters, "test") == []

    def test_year_max_only_is_strict_trust(self):
        """UI 'Pre-1990' bound (year_max=1989) must not fall through to post-1990 FAISS.
        This is the exact bug the fix addresses."""
        from analysis.vector_store import _empty_pool_or_fallback
        filters = {"year_max": 1989}
        assert _empty_pool_or_fallback(filters, "test") == []

    def test_year_range_both_bounds_is_strict_trust(self):
        from analysis.vector_store import _empty_pool_or_fallback
        filters = {"year_min": 1990, "year_max": 1999}
        assert _empty_pool_or_fallback(filters, "test") == []

    def test_multiple_strict_filters_still_return_empty(self):
        """Combining strict filters stays strict — no accidental fallback."""
        from analysis.vector_store import _empty_pool_or_fallback
        filters = {"year_max": 1989, "mal_rating_max": "PG"}
        assert _empty_pool_or_fallback(filters, "test") == []

    def test_year_filter_overrides_natural_language_genre_softness(self):
        """Even when a soft genre is present alongside a strict year filter,
        the overall result is strict — year wins the trust contract."""
        from analysis.vector_store import _empty_pool_or_fallback
        filters = {"year_max": 1989, "genre_db_terms": ["Military", "Drama"]}
        assert _empty_pool_or_fallback(filters, "test") == []


class TestRatingFilterIntegration:
    """End-to-end search() integration — requires GOOGLE_API_KEY."""

    @api
    @skip_no_key
    def test_search_with_pg_ceiling_returns_only_g_pg(self, db_conn):
        """Real search() call with rating_max='PG' — every result must be G or PG."""
        from analysis.vector_store import search
        response = search("wholesome adventure", top_k=15, rating_max="PG")
        results = response["results"]
        assert len(results) > 0, "Expected some results under PG ceiling"
        assert response["meta"]["filters_applied"]["mal_rating_max"] == "PG"

        for r in results:
            row = db_conn.execute(
                "SELECT mal_rating FROM anime_info WHERE english_title = ?",
                (r["title"],),
            ).fetchone()
            rating = row[0] or ""
            assert rating.startswith("G - ") or rating.startswith("PG - "), (
                f"'{r['title']}' has rating '{rating}' — leaked under PG ceiling"
            )

    @api
    @skip_no_key
    def test_search_without_ceiling_unchanged(self):
        """Regression — when rating_max is None, behavior must match the
        existing unfiltered path. The same query should return >0 results."""
        from analysis.vector_store import search
        baseline = search("dark psychological thriller", top_k=15)
        assert len(baseline["results"]) > 0
        assert baseline["meta"].get("filters_applied") is None or \
            "mal_rating_max" not in (baseline["meta"].get("filters_applied") or {})


# =====================================================================
# "I DON'T KNOW" MODE — low_confidence flag computation
# =====================================================================

class TestLowConfidenceThreshold:
    """The threshold constant is the single source of truth for the
    banner's in-band trigger. Pinning it here catches accidental tuning
    changes that would silently shift production behavior."""

    def test_threshold_value(self):
        from analysis.recommendation_engine import LOW_CONFIDENCE_THRESHOLD
        assert LOW_CONFIDENCE_THRESHOLD == 75, (
            "Threshold changed — if this was intentional, update this "
            "test alongside the constant; if not, revert."
        )


class TestLowConfidenceFlagFASEngine:
    """Test the low_confidence computation in fetch_vault_pool. Mocks the
    vector_store and queries layers so the test runs offline against the
    actual engine method — verifying both empty-pool and in-band triggers."""

    def test_flag_true_when_search_returns_no_candidates(self, engine):
        """The earliest empty-result exit (no FAISS hits at all) must
        signal low_confidence."""
        from unittest.mock import patch
        with patch("analysis.vector_store.search", return_value={
            "results": [],
            "meta": {"retrieval_path": "unfiltered", "intent": "vibe",
                     "concepts_detected": None, "candidates_retrieved": 0,
                     "filters_applied": None},
        }):
            result = engine.fetch_vault_pool("nonsense query xyzzy")
        assert result["success"] is False
        assert result["low_confidence"] is True

    def test_flag_true_when_pool_empty_after_rerank_floor(self, engine):
        """When FAISS returns candidates but every reranker pick is
        below the 65 drop floor, the resulting scored_pool is empty —
        and that's a low_confidence case."""
        from unittest.mock import patch
        from analysis.recommendation_engine import RerankedShow

        # All picks below the 65 drop floor — every one gets filtered.
        weak_picks = [RerankedShow(title=f"Show {i}", match_confidence=50,
                                    reasoning="weak match")
                      for i in range(3)]

        with patch("analysis.vector_store.search", return_value={
            "results": [{"title": f"Show {i}", "similarity": 0.7} for i in range(3)],
            "meta": {"retrieval_path": "unfiltered", "intent": "vibe",
                     "concepts_detected": None, "candidates_retrieved": 3,
                     "filters_applied": None},
        }), patch("analysis.queries.fetch_fusion_profiles", return_value=[
            {"title": f"Show {i}", "synopsis": "x", "audience_consensus": "x",
             "controversy_score": 0, "quality_score": 7.0, "scored_by": 1000,
             "audience_sentiment": 0.0}
            for i in range(3)
        ]), patch.object(engine, "_rerank_candidates", return_value=weak_picks):
            result = engine.fetch_vault_pool("weak query")
        assert result["success"] is True
        assert result["pool"] == []
        assert result["low_confidence"] is True

    def test_flag_true_when_top_pick_below_threshold(self, engine):
        """When the top pick clears 65 but stays below the 75 banner
        threshold, the flag must fire (the in-band case)."""
        from unittest.mock import patch
        from analysis.recommendation_engine import RerankedShow
        picks = [
            RerankedShow(title="Show 1", match_confidence=70, reasoning="okayish"),
            RerankedShow(title="Show 2", match_confidence=68, reasoning="okayish"),
        ]
        with patch("analysis.vector_store.search", return_value={
            "results": [{"title": "Show 1", "similarity": 0.8},
                        {"title": "Show 2", "similarity": 0.7}],
            "meta": {"retrieval_path": "unfiltered", "intent": "vibe",
                     "concepts_detected": None, "candidates_retrieved": 2,
                     "filters_applied": None},
        }), patch("analysis.queries.fetch_fusion_profiles", return_value=[
            {"title": "Show 1", "synopsis": "x", "audience_consensus": "x",
             "controversy_score": 0, "quality_score": 7.0, "scored_by": 1000,
             "audience_sentiment": 0.0},
            {"title": "Show 2", "synopsis": "x", "audience_consensus": "x",
             "controversy_score": 0, "quality_score": 7.0, "scored_by": 1000,
             "audience_sentiment": 0.0},
        ]), patch.object(engine, "_rerank_candidates", return_value=picks):
            result = engine.fetch_vault_pool("middling query")
        assert result["success"] is True
        assert len(result["pool"]) == 2
        assert result["pool"][0]["match_confidence"] == 70
        assert result["low_confidence"] is True

    def test_flag_false_when_top_pick_at_or_above_threshold(self, engine):
        """Threshold is strict less-than. A top pick at exactly 75
        should NOT fire the banner — defensive boundary test."""
        from unittest.mock import patch
        from analysis.recommendation_engine import RerankedShow
        picks = [
            RerankedShow(title="Show 1", match_confidence=75, reasoning="solid"),
            RerankedShow(title="Show 2", match_confidence=72, reasoning="okay"),
        ]
        with patch("analysis.vector_store.search", return_value={
            "results": [{"title": "Show 1", "similarity": 0.9},
                        {"title": "Show 2", "similarity": 0.8}],
            "meta": {"retrieval_path": "unfiltered", "intent": "vibe",
                     "concepts_detected": None, "candidates_retrieved": 2,
                     "filters_applied": None},
        }), patch("analysis.queries.fetch_fusion_profiles", return_value=[
            {"title": "Show 1", "synopsis": "x", "audience_consensus": "x",
             "controversy_score": 0, "quality_score": 7.0, "scored_by": 1000,
             "audience_sentiment": 0.0},
            {"title": "Show 2", "synopsis": "x", "audience_consensus": "x",
             "controversy_score": 0, "quality_score": 7.0, "scored_by": 1000,
             "audience_sentiment": 0.0},
        ]), patch.object(engine, "_rerank_candidates", return_value=picks):
            result = engine.fetch_vault_pool("boundary query")
        assert result["success"] is True
        assert result["pool"][0]["match_confidence"] == 75
        assert result["low_confidence"] is False

    def test_flag_false_on_high_confidence_results(self, engine):
        """Standard high-confidence path — flag must be False."""
        from unittest.mock import patch
        from analysis.recommendation_engine import RerankedShow
        picks = [RerankedShow(title="Show 1", match_confidence=98,
                              reasoning="excellent match")]
        with patch("analysis.vector_store.search", return_value={
            "results": [{"title": "Show 1", "similarity": 0.95}],
            "meta": {"retrieval_path": "unfiltered", "intent": "vibe",
                     "concepts_detected": None, "candidates_retrieved": 1,
                     "filters_applied": None},
        }), patch("analysis.queries.fetch_fusion_profiles", return_value=[
            {"title": "Show 1", "synopsis": "x", "audience_consensus": "x",
             "controversy_score": 0, "quality_score": 8.5, "scored_by": 100000,
             "audience_sentiment": 0.5},
        ]), patch.object(engine, "_rerank_candidates", return_value=picks):
            result = engine.fetch_vault_pool("strong query")
        assert result["low_confidence"] is False


class TestLowConfidenceFlagDNAEngine:
    """DNA-side analogue. The in-band case has been historically inert
    (top results cluster at 100% above 90) but the empty-centroid case
    is the largest source of banner activations across the whole engine
    (~9.6% of historical DNA searches)."""

    def test_flag_true_when_centroid_returns_no_candidates(self, engine):
        """The DNA equivalent of fetch_vault_pool's empty-search branch.
        Title resolution succeeds (real DB query), Triangulation LLM
        succeeds (mocked), centroid returns nothing — low_confidence
        must fire."""
        from unittest.mock import patch, MagicMock
        from analysis.recommendation_engine import TriangulationPlan
        # Mock Triangulation LLM response — we don't care what the
        # intersection summary says, only that synthesis "succeeds"
        # so we reach the centroid step.
        mock_response = MagicMock()
        mock_response.parsed = TriangulationPlan(intersection_summary="test summary")
        with patch.object(engine.client.models, "generate_content",
                          return_value=mock_response), \
             patch("analysis.vector_store.search_by_centroid", return_value={
                "results": [],
                "meta": {"retrieval_path": "centroid", "reference_count": 1,
                         "negative_anchor_count": 0, "excluded_count": 0,
                         "prefiltered_count": 0, "candidates_retrieved": 0},
            }):
            result = engine.execute_dna_triangulation(["Attack on Titan"])
        assert result["success"] is False
        assert result["low_confidence"] is True
        assert "No matching targets" in result["error"]

    def test_flag_not_set_on_target_mia_error(self, engine):
        """Title-resolution failure is a different kind of failure than
        weak confidence — Target MIA means the user typed a show name
        we can't even find. low_confidence should NOT fire for this; it
        reads as 'we don't know what show you mean,' not 'we don't know
        any good matches.' The frontend may still render an error block
        for these but the trust-positioning banner is wrong here."""
        result = engine.execute_dna_triangulation(["ZZZZQQQQ_NEVER_EXISTS"])
        assert result["success"] is False
        assert "Target MIA" in result["error"]
        # Flag must be absent or False — never True for this kind of error
        assert not result.get("low_confidence", False)


# =====================================================================
# QUERY-RELEVANT CON
# =====================================================================

class TestRerankedShowSchema:
    """The Pydantic schema gained a new optional field. Defaults and
    serialization must behave for older reranker outputs that don't
    populate it."""

    def test_query_relevant_con_defaults_none(self):
        from analysis.recommendation_engine import RerankedShow
        s = RerankedShow(title="X", match_confidence=85, reasoning="r")
        assert s.query_relevant_con is None

    def test_query_relevant_con_accepts_string(self):
        from analysis.recommendation_engine import RerankedShow
        s = RerankedShow(
            title="X", match_confidence=85, reasoning="r",
            query_relevant_con="Slow first 4 episodes.",
        )
        assert s.query_relevant_con == "Slow first 4 episodes."

    def test_query_relevant_con_accepts_explicit_null(self):
        from analysis.recommendation_engine import RerankedShow
        s = RerankedShow(
            title="X", match_confidence=85, reasoning="r",
            query_relevant_con=None,
        )
        assert s.query_relevant_con is None


class TestCandidatesBlockConsFormatting:
    """The candidates_block fed to each reranker prompt must include the
    cons list when present, and omit the Cons section cleanly when the
    candidate has no cons. The cap at 8 keeps prompt size sane on the
    rare candidate with a longer cons list."""

    def _capture_prompt_for(self, method_name, profile_overrides=None):
        """Return the ``contents`` (prompt) sent to Gemini for the given
        rerank method + profile overrides. Thin adapter over the shared
        _capture_rerank_gemini_call helper."""
        return _capture_rerank_gemini_call(method_name, profile_overrides)["contents"]

    def test_fusion_block_includes_cons_when_present(self):
        prompt = self._capture_prompt_for("_rerank_candidates", {
            "cons": ["Slow first 4 episodes.", "Ending divisive."]
        })
        assert "Cons:" in prompt
        assert "- Slow first 4 episodes." in prompt
        assert "- Ending divisive." in prompt

    def test_fusion_block_omits_cons_when_empty(self):
        prompt = self._capture_prompt_for("_rerank_candidates", {"cons": []})
        assert "Cons:" not in prompt
        # The surrounding markers should still be present so we know we hit
        # the right code path.
        assert "Audience Consensus:" in prompt
        assert "Controversy Score:" in prompt

    def test_fusion_block_omits_cons_when_key_missing(self):
        prompt = self._capture_prompt_for("_rerank_candidates", {})
        assert "Cons:" not in prompt

    def test_constrained_block_includes_cons_when_present(self):
        prompt = self._capture_prompt_for("_rerank_constrained_candidates", {
            "cons": ["Plot drags in act 2."]
        })
        assert "Cons:" in prompt
        assert "- Plot drags in act 2." in prompt

    def test_narrative_block_includes_cons_when_present(self):
        prompt = self._capture_prompt_for("_rerank_narrative_candidates", {
            "cons": ["Superficial exploration of friendship concepts."]
        })
        assert "Cons:" in prompt
        assert "- Superficial exploration of friendship concepts." in prompt

    def test_dna_block_includes_cons_when_present(self):
        prompt = self._capture_prompt_for("_rerank_dna_candidates", {
            "cons": ["Generic shonen tropes and cliches."]
        })
        assert "Cons:" in prompt
        assert "- Generic shonen tropes and cliches." in prompt

    def test_cons_capped_at_eight(self):
        many = [f"Con {i}." for i in range(1, 13)]
        prompt = self._capture_prompt_for("_rerank_candidates", {"cons": many})
        for i in range(1, 9):
            assert f"- Con {i}." in prompt
        # 9-12 should be truncated
        assert "- Con 9." not in prompt
        assert "- Con 12." not in prompt


class TestRerankerTemperatureIsZero:
    """Every rerank call site must pass temperature=0 to Gemini's
    generate_content. The SDK default (1.0) produces non-deterministic
    ranking; explicit 0 pins the output so eval measurement is stable.

    Guards against future config-dict edits accidentally dropping the
    temperature field. Does not verify Gemini honors the setting — only
    that our call passes it."""

    def _capture_config(self, method_name, profile_overrides=None):
        """Return the ``config`` kwarg passed to Gemini for the given
        rerank method. Thin adapter over the shared
        _capture_rerank_gemini_call helper."""
        return _capture_rerank_gemini_call(method_name, profile_overrides)["config"]

    def test_fusion_reranker_passes_temperature_zero(self):
        config = self._capture_config("_rerank_candidates")
        assert config.get("temperature") == 0, (
            "FUSION reranker must pass temperature=0 (see comment in "
            "recommendation_engine.py _rerank_candidates)"
        )

    def test_constraint_reranker_passes_temperature_zero(self):
        config = self._capture_config("_rerank_constrained_candidates")
        assert config.get("temperature") == 0, (
            "CONSTRAINT reranker must pass temperature=0"
        )

    def test_narrative_reranker_passes_temperature_zero(self):
        config = self._capture_config("_rerank_narrative_candidates")
        assert config.get("temperature") == 0, (
            "NARRATIVE reranker must pass temperature=0"
        )

    def test_dna_reranker_passes_temperature_zero(self):
        config = self._capture_config("_rerank_dna_candidates")
        assert config.get("temperature") == 0, (
            "DNA reranker must pass temperature=0"
        )

    def test_dna_triangulation_passes_temperature_zero(self, engine):
        """The triangulation intersection-summary LLM call — separate
        from the four candidate rerank helpers but runs the same
        Gemini generate_content path."""
        from unittest.mock import patch, MagicMock
        from analysis.recommendation_engine import TriangulationPlan
        mock_response = MagicMock()
        mock_response.parsed = TriangulationPlan(intersection_summary="test")
        with patch.object(engine.client.models, "generate_content",
                          return_value=mock_response) as mock_gen, \
             patch("analysis.vector_store.search_by_centroid", return_value={
                "results": [],
                "meta": {"retrieval_path": "centroid", "reference_count": 1,
                         "negative_anchor_count": 0, "excluded_count": 0,
                         "prefiltered_count": 0, "candidates_retrieved": 0},
             }):
            engine.execute_dna_triangulation(["Attack on Titan"])
        config = mock_gen.call_args.kwargs["config"]
        assert config.get("temperature") == 0, (
            "DNA triangulation intersection-summary call must pass temperature=0"
        )


class TestQueryRelevantConThreading:
    """The new field must thread from a reranker pick through
    scored_pool / final_results into the engine's return dict on every
    intent path. Mocks Gemini so we can pin the reranker's emitted
    value precisely."""

    def _profile(self, title, cons=None):
        return {
            "title": title, "synopsis": "X", "audience_consensus": "Y",
            "controversy_score": 0, "quality_score": 8.0, "scored_by": 100_000,
            "audience_sentiment": 0.3, "cons": cons or [],
        }

    def test_fusion_path_threads_con(self, engine):
        from unittest.mock import patch
        from analysis.recommendation_engine import RerankedShow
        pick = RerankedShow(
            title="Show A", match_confidence=92,
            reasoning="solid match",
            query_relevant_con="Slow first 4 episodes.",
        )
        with patch("analysis.vector_store.search", return_value={
            "results": [{"title": "Show A", "similarity": 0.9}],
            "meta": {"retrieval_path": "unfiltered", "intent": "vibe",
                     "concepts_detected": None, "candidates_retrieved": 1,
                     "filters_applied": None},
        }), patch("analysis.queries.fetch_fusion_profiles",
                  return_value=[self._profile("Show A", ["Slow first 4 episodes."])]
        ), patch.object(engine, "_rerank_candidates", return_value=[pick]):
            result = engine.fetch_vault_pool("test query")
        assert result["success"] is True
        assert result["pool"][0]["query_relevant_con"] == "Slow first 4 episodes."

    def test_fusion_path_threads_null_con(self, engine):
        from unittest.mock import patch
        from analysis.recommendation_engine import RerankedShow
        pick = RerankedShow(
            title="Show A", match_confidence=92, reasoning="solid match",
            query_relevant_con=None,
        )
        with patch("analysis.vector_store.search", return_value={
            "results": [{"title": "Show A", "similarity": 0.9}],
            "meta": {"retrieval_path": "unfiltered", "intent": "vibe",
                     "concepts_detected": None, "candidates_retrieved": 1,
                     "filters_applied": None},
        }), patch("analysis.queries.fetch_fusion_profiles",
                  return_value=[self._profile("Show A")]
        ), patch.object(engine, "_rerank_candidates", return_value=[pick]):
            result = engine.fetch_vault_pool("test query")
        assert result["pool"][0]["query_relevant_con"] is None

    def test_constrained_path_threads_con(self, engine):
        from unittest.mock import patch
        from analysis.recommendation_engine import RerankedShow
        pick = RerankedShow(
            title="Show A", match_confidence=92, reasoning="solid match",
            query_relevant_con="Pacing drags in act 2.",
        )
        with patch("analysis.vector_store.search", return_value={
            "results": [{"title": "Show A", "similarity": 0.9}],
            "meta": {"retrieval_path": "filtered:quality_min=7", "intent": "vibe",
                     "concepts_detected": None, "candidates_retrieved": 1,
                     "filters_applied": None, "has_numeric_constraints": True,
                     "constraint_summary": "Quality Score above 7"},
        }), patch("analysis.queries.fetch_fusion_profiles",
                  return_value=[self._profile("Show A", ["Pacing drags in act 2."])]
        ), patch.object(engine, "_rerank_constrained_candidates", return_value=[pick]):
            result = engine.fetch_vault_pool("anime with quality above 7")
        assert result["pool"][0]["query_relevant_con"] == "Pacing drags in act 2."

    def test_narrative_path_threads_con(self, engine):
        from unittest.mock import patch
        from analysis.recommendation_engine import RerankedShow
        pick = RerankedShow(
            title="Show A", match_confidence=92, reasoning="plot match",
            query_relevant_con="Superficial exploration of friendship concepts.",
        )
        with patch("analysis.vector_store.search", return_value={
            "results": [{"title": "Show A", "similarity": 0.9}],
            "meta": {"retrieval_path": "narrative", "intent": "narrative",
                     "concepts_detected": None, "candidates_retrieved": 1,
                     "filters_applied": None},
        }), patch("analysis.queries.fetch_fusion_profiles",
                  return_value=[self._profile("Show A", ["Superficial exploration of friendship concepts."])]
        ), patch.object(engine, "_rerank_narrative_candidates", return_value=[pick]):
            result = engine.fetch_vault_pool("MC betrayed by a friend")
        assert result["pool"][0]["query_relevant_con"] == "Superficial exploration of friendship concepts."

    def test_dna_path_threads_con(self, engine):
        from unittest.mock import patch, MagicMock
        from analysis.recommendation_engine import RerankedShow, TriangulationPlan
        pick = RerankedShow(
            title="Black Clover", match_confidence=85, reasoning="shonen DNA match",
            query_relevant_con="Generic shonen tropes and cliches.",
        )
        mock_triangulation = MagicMock()
        mock_triangulation.parsed = TriangulationPlan(intersection_summary="shonen DNA")
        with patch.object(engine.client.models, "generate_content",
                          return_value=mock_triangulation), \
             patch("analysis.vector_store.search_by_centroid", return_value={
                "results": [{"title": "Black Clover", "similarity": 0.85}],
                "meta": {"retrieval_path": "centroid", "reference_count": 1,
                         "negative_anchor_count": 0, "excluded_count": 0,
                         "prefiltered_count": 0, "candidates_retrieved": 1},
            }), patch("analysis.queries.fetch_fusion_profiles",
                      return_value=[self._profile("Black Clover",
                                                  ["Generic shonen tropes and cliches."])]
            ), patch.object(engine, "_rerank_dna_candidates", return_value=[pick]):
            result = engine.execute_dna_triangulation(["Naruto"])
        assert result["success"] is True
        assert result["data"][0]["query_relevant_con"] == "Generic shonen tropes and cliches."

    def test_objective_rankings_always_null(self, engine):
        """Objective Rankings bypasses the LLM reranker — query_relevant_con
        should be present-and-null on every scored item, never missing."""
        from unittest.mock import patch
        with patch("analysis.vector_store.objective_rankings_search",
                   return_value=[{"title": "Show A", "similarity": 1.0}]
        ), patch("analysis.queries.fetch_fusion_profiles",
                 return_value=[self._profile("Show A", ["Some con."])]):
            result = engine.fetch_vault_pool("top anime", lens_name="Objective Rankings")
        assert result["success"] is True
        assert "query_relevant_con" in result["pool"][0]
        assert result["pool"][0]["query_relevant_con"] is None


class TestPromptUpdatesIncludeConInstruction:
    """Each reranker prompt must mention query_relevant_con — sanity guard
    so a partial edit can't break the contract on one path while leaving
    the others working."""

    def test_fusion_prompt_mentions_query_relevant_con(self):
        from analysis.prompts import FUSION_RERANKER_PROMPT
        assert "query_relevant_con" in FUSION_RERANKER_PROMPT
        assert "VERBATIM" in FUSION_RERANKER_PROMPT
        assert "emit null" in FUSION_RERANKER_PROMPT

    def test_constraint_prompt_mentions_query_relevant_con(self):
        from analysis.prompts import CONSTRAINT_RERANKER_PROMPT
        assert "query_relevant_con" in CONSTRAINT_RERANKER_PROMPT
        assert "VERBATIM" in CONSTRAINT_RERANKER_PROMPT
        assert "emit null" in CONSTRAINT_RERANKER_PROMPT

    def test_dna_prompt_mentions_query_relevant_con(self):
        from analysis.prompts import DNA_RERANKER_PROMPT
        assert "query_relevant_con" in DNA_RERANKER_PROMPT
        assert "VERBATIM" in DNA_RERANKER_PROMPT
        assert "emit null" in DNA_RERANKER_PROMPT
        # DNA-specific: should mention contrast with references
        assert "CONTRAST" in DNA_RERANKER_PROMPT or "contrast" in DNA_RERANKER_PROMPT

    def test_narrative_prompt_mentions_query_relevant_con(self):
        from analysis.prompts import NARRATIVE_RERANKER_PROMPT
        assert "query_relevant_con" in NARRATIVE_RERANKER_PROMPT
        assert "VERBATIM" in NARRATIVE_RERANKER_PROMPT
        assert "emit null" in NARRATIVE_RERANKER_PROMPT

    def test_all_prompts_extend_spoiler_directive(self):
        """contains_spoiler must now cover BOTH reasoning AND query_relevant_con.
        Otherwise a spoilery con could leak past the spoiler wall."""
        from analysis.prompts import (
            FUSION_RERANKER_PROMPT, CONSTRAINT_RERANKER_PROMPT,
            DNA_RERANKER_PROMPT, NARRATIVE_RERANKER_PROMPT,
        )
        for p in (FUSION_RERANKER_PROMPT, CONSTRAINT_RERANKER_PROMPT,
                  DNA_RERANKER_PROMPT, NARRATIVE_RERANKER_PROMPT):
            assert "reasoning OR your query_relevant_con" in p, (
                "Spoiler directive must explicitly cover both fields"
            )


class TestExtractRerankerResult:
    """Unit tests for _extract_reranker_result. The extractor turns a
    Gemini SDK response into one of three signals: parsed list on success,
    RERANK_BLOCKED sentinel on policy block, empty list on the rare
    no-block/no-parsed case."""

    def _make_response(self, parsed=None, block_reason=None):
        """Build a mock response object shaped like the SDK returns."""
        from unittest.mock import MagicMock
        r = MagicMock()
        r.parsed = parsed
        if block_reason is None:
            r.prompt_feedback = None
        else:
            pf = MagicMock()
            pf.block_reason = block_reason
            pf.safety_ratings = None
            r.prompt_feedback = pf
        r.candidates = None
        return r

    def test_success_returns_parsed_unchanged(self):
        from analysis.recommendation_engine import _extract_reranker_result, RerankedShow
        picks = [RerankedShow(title="X", match_confidence=85, reasoning="good")]
        r = self._make_response(parsed=picks)
        assert _extract_reranker_result(r) is picks

    def test_prohibited_content_returns_blocked_sentinel(self):
        """The failure mode that motivated this whole change — Gemini's
        non-configurable PROHIBITED_CONTENT policy blocks the response."""
        from analysis.recommendation_engine import _extract_reranker_result
        r = self._make_response(parsed=None, block_reason="PROHIBITED_CONTENT")
        assert _extract_reranker_result(r) == ["RERANK_BLOCKED"]

    def test_any_block_reason_returns_blocked_sentinel(self):
        """Fallback fires for ANY block_reason, not just PROHIBITED_CONTENT
        — future-proofs against Google adding new block categories."""
        from analysis.recommendation_engine import _extract_reranker_result
        for reason in ["SAFETY", "OTHER", "BLOCKLIST", "SPII"]:
            r = self._make_response(parsed=None, block_reason=reason)
            assert _extract_reranker_result(r) == ["RERANK_BLOCKED"], (
                f"block_reason={reason} should trigger fallback"
            )

    def test_empty_parsed_no_block_returns_empty_list(self):
        """Rare edge case: parsed is None but no block_reason. Not a
        fallback trigger — treated as genuine empty ranking result."""
        from analysis.recommendation_engine import _extract_reranker_result
        r = self._make_response(parsed=None, block_reason=None)
        assert _extract_reranker_result(r) == []


class TestBuildCompositeFallback:
    """Unit tests for _build_composite_fallback. Verifies the synthesized
    scored_pool shape matches what the normal-path pick loop produces —
    downstream code (series-collapse, series-context hydration, frontend
    consumers) must not need special-casing for fallback entries."""

    def test_empty_pool_returns_empty_list(self):
        from analysis.recommendation_engine import _build_composite_fallback
        assert _build_composite_fallback([]) == []

    def test_all_entries_get_fixed_confidence_and_reasoning(self):
        from analysis.recommendation_engine import (
            _build_composite_fallback,
            _FALLBACK_CONFIDENCE,
            _FALLBACK_REASONING,
        )
        pool = [{"title": "A"}, {"title": "B"}, {"title": "C"}]
        result = _build_composite_fallback(pool)
        assert len(result) == 3
        for entry in result:
            assert entry["match_confidence"] == _FALLBACK_CONFIDENCE
            assert entry["ai_reasoning"] == _FALLBACK_REASONING
            assert entry["controversy_warning"] is None
            assert entry["contains_spoiler"] is False
            assert entry["query_relevant_con"] is None

    def test_preserves_input_order_and_profile_references(self):
        """Composite ordering from vector_store.search is already tuned.
        The fallback must not resort — it just wraps each profile in the
        scored_pool envelope."""
        from analysis.recommendation_engine import _build_composite_fallback
        pool = [{"title": "First"}, {"title": "Second"}, {"title": "Third"}]
        result = _build_composite_fallback(pool)
        assert [r["profile"]["title"] for r in result] == ["First", "Second", "Third"]
        # Profile references identical (not deep-copied) — cheap and safe
        # since the caller doesn't mutate them.
        for original, wrapped in zip(pool, result):
            assert wrapped["profile"] is original

    def test_fallback_confidence_below_low_confidence_threshold(self):
        """The banner MUST fire on fallback pools so users know analysis
        wasn't available. Pinning this via cross-check on the two
        constants — if either shifts, the invariant must hold."""
        from analysis.recommendation_engine import (
            _FALLBACK_CONFIDENCE,
            LOW_CONFIDENCE_THRESHOLD,
        )
        assert _FALLBACK_CONFIDENCE < LOW_CONFIDENCE_THRESHOLD, (
            "Fallback confidence must be below the banner threshold so "
            "users see the low-confidence advisory on fallback results."
        )


class TestRerankerBlockedFallbackFAS:
    """Integration tests: fetch_vault_pool with a mocked reranker returning
    the RERANK_BLOCKED sentinel must fall back to composite scoring rather
    than returning an empty pool."""

    def _search_and_hydration_mocks(self, n=3, intent="vibe",
                                     has_numeric_constraints=False):
        """Build a matching (search_result, fusion_profile) pair for n shows."""
        results = [{"title": f"Show {i}", "similarity": 0.75} for i in range(n)]
        meta = {
            "retrieval_path": "unfiltered",
            "intent": intent,
            "concepts_detected": None,
            "candidates_retrieved": n,
            "filters_applied": None,
            "has_numeric_constraints": has_numeric_constraints,
            "constraint_summary": None,
        }
        profiles = [
            {"title": f"Show {i}", "synopsis": f"synopsis {i}",
             "audience_consensus": f"consensus {i}", "controversy_score": 0,
             "quality_score": 7.0, "scored_by": 1000, "audience_sentiment": 0.0}
            for i in range(n)
        ]
        return results, meta, profiles

    def test_fusion_path_falls_back_on_blocked_reranker(self, engine):
        from unittest.mock import patch
        results, meta, profiles = self._search_and_hydration_mocks(n=5)

        with patch("analysis.vector_store.search",
                   return_value={"results": results, "meta": meta}), \
             patch("analysis.queries.fetch_fusion_profiles", return_value=profiles), \
             patch.object(engine, "_rerank_candidates",
                          return_value=["RERANK_BLOCKED"]):
            resp = engine.fetch_vault_pool("prohibited-triggering query")

        assert resp["success"] is True
        assert resp["fallback_mode"] == "composite"
        assert len(resp["pool"]) == 5
        assert resp["low_confidence"] is True  # 70 < 75 threshold
        for entry in resp["pool"]:
            assert entry["match_confidence"] == 70
            assert "Semantic match only" in entry["ai_reasoning"]

    def test_narrative_path_falls_back_on_blocked_reranker(self, engine):
        from unittest.mock import patch
        results, meta, profiles = self._search_and_hydration_mocks(
            n=3, intent="narrative"
        )

        with patch("analysis.vector_store.search",
                   return_value={"results": results, "meta": meta}), \
             patch("analysis.queries.fetch_fusion_profiles", return_value=profiles), \
             patch.object(engine, "_rerank_narrative_candidates",
                          return_value=["RERANK_BLOCKED"]):
            resp = engine.fetch_vault_pool("MC betrayed by friend twist")

        assert resp["success"] is True
        assert resp["fallback_mode"] == "composite"
        assert len(resp["pool"]) == 3

    def test_constrained_path_falls_back_on_blocked_reranker(self, engine):
        from unittest.mock import patch
        results, meta, profiles = self._search_and_hydration_mocks(
            n=4, has_numeric_constraints=True
        )

        with patch("analysis.vector_store.search",
                   return_value={"results": results, "meta": meta}), \
             patch("analysis.queries.fetch_fusion_profiles", return_value=profiles), \
             patch.object(engine, "_rerank_constrained_candidates",
                          return_value=["RERANK_BLOCKED"]):
            resp = engine.fetch_vault_pool("anime with quality above 7")

        assert resp["success"] is True
        assert resp["fallback_mode"] == "composite"
        assert len(resp["pool"]) == 4

    def test_normal_success_does_not_set_fallback_mode(self, engine):
        """Guard: fallback_mode must be None on the happy path so
        analytics can cleanly split real reranks from fallbacks."""
        from unittest.mock import patch
        from analysis.recommendation_engine import RerankedShow
        results, meta, profiles = self._search_and_hydration_mocks(n=2)
        picks = [
            RerankedShow(title="Show 0", match_confidence=90, reasoning="great"),
            RerankedShow(title="Show 1", match_confidence=85, reasoning="good"),
        ]

        with patch("analysis.vector_store.search",
                   return_value={"results": results, "meta": meta}), \
             patch("analysis.queries.fetch_fusion_profiles", return_value=profiles), \
             patch.object(engine, "_rerank_candidates", return_value=picks):
            resp = engine.fetch_vault_pool("normal query")

        assert resp["fallback_mode"] is None
        assert len(resp["pool"]) == 2


class TestRerankerBlockedFallbackDNA:
    """DNA-side integration for the fallback path — execute_dna_triangulation
    handles the sentinel identically to fetch_vault_pool but with the
    DNA-specific 40 confidence floor being bypassed on fallback."""

    def test_dna_path_falls_back_on_blocked_reranker(self, engine):
        """When the DNA reranker returns the RERANK_BLOCKED sentinel, the
        centroid pool is wrapped in fallback envelopes at fixed 70
        confidence. Real title resolution, real reference hydration,
        mocked triangulation LLM and centroid + candidate hydration."""
        from unittest.mock import patch, MagicMock
        from analysis.recommendation_engine import TriangulationPlan

        # Triangulation LLM stub — the plan content doesn't matter beyond
        # being parseable, we just need synthesis to "succeed" so we reach
        # the centroid + rerank step.
        mock_response = MagicMock()
        mock_response.parsed = TriangulationPlan(intersection_summary="sci-fi thriller")

        centroid_results = [
            {"title": f"Similar Show {i}", "similarity": 0.7} for i in range(4)
        ]
        centroid_meta = {
            "retrieval_path": "centroid",
            "reference_count": 1,
            "negative_anchor_count": 0,
            "excluded_count": 0,
            "prefiltered_count": 0,
            "candidates_retrieved": 4,
        }

        # fetch_fusion_profiles is called twice — once for reference
        # hydration (Attack on Titan, real title) and once for candidate
        # hydration (Similar Show N, synthetic). Return matching-shape
        # profiles for either input so both calls succeed.
        def _fp_side_effect(titles):
            return [
                {"title": t, "synopsis": "s", "audience_consensus": "c",
                 "controversy_score": 0, "quality_score": 7.0,
                 "scored_by": 1000, "audience_sentiment": 0.0,
                 "series_id": None}
                for t in titles
            ]

        with patch.object(engine.client.models, "generate_content",
                          return_value=mock_response), \
             patch("analysis.queries.fetch_fusion_profiles",
                   side_effect=_fp_side_effect), \
             patch("analysis.recommendation_engine._compute_sibling_exclude_titles",
                   return_value=set()), \
             patch("analysis.vector_store.search_by_centroid", return_value={
                "results": centroid_results, "meta": centroid_meta,
             }), \
             patch.object(engine, "_rerank_dna_candidates",
                          return_value=["RERANK_BLOCKED"]):
            resp = engine.execute_dna_triangulation(["Attack on Titan"])

        assert resp["success"] is True
        assert resp["fallback_mode"] == "composite"
        assert len(resp["data"]) == 4
        for entry in resp["data"]:
            assert entry["match_confidence"] == 70
            assert "Semantic match only" in entry["ai_reasoning"]
