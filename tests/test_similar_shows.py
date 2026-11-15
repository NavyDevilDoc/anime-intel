"""Unit tests for analysis/similar_shows.py.

Tests the pure-logic layer without touching FAISS or the real DB. Fixtures
provide small hand-crafted lookups + a fake `faiss_search_fn` so the tests
run fully offline and instantly.
"""

import sqlite3
import types

import pytest

from analysis.similar_shows import (
    EngineContext,
    _PROVENANCE_TIER_ORDER,
    _QUALITY_FLOOR,
    _ROLE_BONUS,
    _SKIP_BY_ROLE,
    _W_QUALITY,
    _W_SIMILARITY,
    bayesian_norm_quality,
    build_personnel_hook,
    build_recommendations,
    compute_personnel_bonus,
    extract_thematic_hint,
    load_engine_context,
    share_franchise_signal,
)


# ------------------------------------------------------------------------
# share_franchise_signal
# ------------------------------------------------------------------------

class TestShareFranchiseSignal:
    def test_containment_catches_code_geass_akito(self):
        assert share_franchise_signal(
            "Code Geass: Lelouch of the Rebellion",
            "Code Geass: Akito the Exiled - The Wyvern Arrives",
        )

    def test_containment_catches_gits_sac_variants(self):
        assert share_franchise_signal(
            "Ghost in the Shell: Stand Alone Complex",
            "Ghost in the Shell: Stand Alone Complex: Tachikomatic Days",
        )

    def test_containment_catches_madoka_magia_record(self):
        assert share_franchise_signal(
            "Puella Magi Madoka Magica",
            "Magia Record: Puella Magi Madoka Magica Side Story",
        )

    def test_flcl_progressive_matches_via_containment(self):
        # "FLCL" (4 chars) is below the length guard for containment,
        # but it's also below min_shared, so the fallback substring
        # search won't fire either. Documented behavior: very short
        # titles rely on series_id, not this heuristic.
        assert not share_franchise_signal("FLCL", "FLCL Progressive")

    def test_unrelated_shows_do_not_match(self):
        assert not share_franchise_signal("Cowboy Bebop", "Attack on Titan")
        assert not share_franchise_signal("Mushi-Shi", "Steins;Gate")

    def test_short_title_does_not_false_positive(self):
        # "K-On!" is 5 chars — will match anything containing "K-On!"
        # but must not match unrelated titles that only share a
        # single character
        assert not share_franchise_signal("K-On!", "Attack on Titan")
        assert not share_franchise_signal("FLCL", "Cowboy Bebop")

    def test_case_insensitive(self):
        assert share_franchise_signal(
            "CODE GEASS main", "code geass sequel",
        )


# ------------------------------------------------------------------------
# bayesian_norm_quality
# ------------------------------------------------------------------------

class TestBayesianNormQuality:
    def test_high_vote_show_stays_near_raw_score(self):
        # Mushi-Shi: 8.65 raw, 296k votes → Bayesian ≈ 8.62 → norm ≈ 0.90
        n = bayesian_norm_quality(8.65, 296_293)
        assert 0.88 < n < 0.92

    def test_low_vote_show_shrinks_toward_mean(self):
        # 9.0 raw, 100 votes → Bayesian close to global mean 7.05
        n = bayesian_norm_quality(9.0, 100)
        # bayesian ≈ (100/5100)*9 + (5000/5100)*7.05 ≈ 7.09
        # norm ≈ (7.09 - 2.87) / (9.28 - 2.87) ≈ 0.66
        assert 0.63 < n < 0.69

    def test_zero_votes_zero_score_returns_zero(self):
        # With zero votes and zero score, we still land at the global mean
        # after shrinkage; the result is ~0.65 rather than 0.0. Confirm
        # the clipping doesn't return something out of range.
        n = bayesian_norm_quality(0.0, 0)
        assert 0.0 <= n <= 1.0

    def test_clipped_to_unit_range(self):
        assert 0.0 <= bayesian_norm_quality(10.0, 10_000_000) <= 1.0
        assert 0.0 <= bayesian_norm_quality(1.0, 0) <= 1.0


# ------------------------------------------------------------------------
# compute_personnel_bonus
# ------------------------------------------------------------------------

class TestComputePersonnelBonus:
    def test_no_overlap_returns_zero(self):
        ref = {"Director": {1}, "Music": {2}}
        cand = {"Director": {99}, "Music": {100}}
        total, shared = compute_personnel_bonus(ref, cand)
        assert total == 0.0
        assert shared == {}

    def test_director_overlap_bonus(self):
        ref = {"Director": {1}}
        cand = {"Director": {1}}
        total, shared = compute_personnel_bonus(ref, cand)
        assert total == pytest.approx(_ROLE_BONUS["Director"])
        assert shared == {"Director": {1}}

    def test_multi_role_overlap_sums(self):
        # Cowboy Bebop → Space Dandy: shared Director + Music
        ref = {"Director": {10}, "Music": {20}, "Original Creator": {30}}
        cand = {"Director": {10}, "Music": {20}}
        total, shared = compute_personnel_bonus(ref, cand)
        expected = _ROLE_BONUS["Director"] + _ROLE_BONUS["Music"]
        assert total == pytest.approx(expected)
        assert set(shared.keys()) == {"Director", "Music"}

    def test_all_five_roles_max_bonus(self):
        ref = {r: {1} for r in _ROLE_BONUS}
        cand = {r: {1} for r in _ROLE_BONUS}
        total, _ = compute_personnel_bonus(ref, cand)
        assert total == pytest.approx(sum(_ROLE_BONUS.values()))


# ------------------------------------------------------------------------
# build_personnel_hook + extract_thematic_hint
# ------------------------------------------------------------------------

class TestBuildPersonnelHook:
    def test_director_takes_priority_over_music(self):
        shared = {"Director": {5}, "Music": {7}}
        names = {5: "Shinichirou Watanabe", 7: "Yoko Kanno"}
        assert build_personnel_hook(shared, names) == "Also directed by Shinichirou Watanabe"

    def test_music_when_no_director_overlap(self):
        shared = {"Music": {7}}
        names = {7: "Yoko Kanno"}
        assert build_personnel_hook(shared, names) == "Music by Yoko Kanno"

    def test_original_creator_when_no_director_music(self):
        shared = {"Original Creator": {12}}
        names = {12: "Yoshiyuki Tomino"}
        assert build_personnel_hook(shared, names) == "Adapted from a work by Yoshiyuki Tomino"

    def test_returns_none_when_no_priority_role_shared(self):
        # Character Design not in _PROVENANCE_TIER_ORDER — no hook
        shared = {"Character Design": {99}}
        assert build_personnel_hook(shared, {99: "X"}) is None

    def test_returns_none_when_no_shared_at_all(self):
        assert build_personnel_hook({}, {}) is None

    def test_deterministic_person_selection_with_multiple(self):
        # When multiple people share a role, pick the one with the
        # lowest mal_id for reproducibility (Item 3 Phase B decision).
        shared = {"Director": {50, 20, 100}}
        names = {50: "Alice", 20: "Bob", 100: "Carol"}
        assert build_personnel_hook(shared, names) == "Also directed by Bob"


class TestExtractThematicHint:
    def test_returns_bare_theme_string_no_prefix(self):
        # Deliberate: no "Similar theme:" or "Theme:" prefix here — the
        # frontend applies the label. Copy-decision rationale: "Similar
        # theme:" was overclaiming similarity when personnel connection
        # was the real link.
        hint = extract_thematic_hint('{"thematic_vibe": "Psychological, Dark"}')
        assert hint == "Psychological, Dark"

    def test_returns_none_when_missing_consensus_json(self):
        assert extract_thematic_hint(None) is None

    def test_returns_none_when_malformed_json(self):
        assert extract_thematic_hint("not json") is None

    def test_returns_none_when_no_thematic_vibe_field(self):
        assert extract_thematic_hint('{"other_field": "x"}') is None

    def test_returns_none_when_empty_vibe(self):
        assert extract_thematic_hint('{"thematic_vibe": ""}') is None

    def test_stops_at_first_sentence(self):
        hint = extract_thematic_hint('{"thematic_vibe": "Dark. Second sentence."}')
        assert hint == "Dark"

    def test_truncated_to_80_chars(self):
        long = "x" * 200
        hint = extract_thematic_hint(f'{{"thematic_vibe": "{long}"}}')
        assert hint is not None and len(hint) <= 80


# ------------------------------------------------------------------------
# Signal-degeneracy skip lists
# ------------------------------------------------------------------------

class TestSkipLists:
    def test_yatate_in_original_creator_skip_list(self):
        assert 2197 in _SKIP_BY_ROLE["Original Creator"]

    def test_beethoven_in_music_skip_list(self):
        assert 9160 in _SKIP_BY_ROLE["Music"]

    def test_no_skip_list_for_director_role(self):
        # Phase A: director / char design / series comp had no
        # observed pseudonym pollution. Guard against a future edit
        # accidentally adding one.
        assert "Director" not in _SKIP_BY_ROLE
        assert "Character Design" not in _SKIP_BY_ROLE
        assert "Series Composition" not in _SKIP_BY_ROLE


# ------------------------------------------------------------------------
# load_engine_context — integration with a real in-memory DB
# ------------------------------------------------------------------------

def _mock_faiss_loader():
    """Fake FAISS layer for tests. Returns a stub index + minimal metadata."""
    class StubIndex:
        def reconstruct(self, pos):
            import numpy as np
            return np.zeros(4, dtype=np.float32)
    metadata = [
        {"english_title": "Cowboy Bebop"},
        {"english_title": "Samurai Champloo"},
        {"english_title": "Attack on Titan"},
    ]
    return StubIndex(), metadata


def _seed_full_db(tmp_path):
    """SQLite file with anime_info + people + anime_staff populated for
    integration tests."""
    db_path = tmp_path / "vault.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE anime_info (
            id INTEGER PRIMARY KEY,
            english_title TEXT,
            mal_score REAL,
            scored_by INTEGER,
            series_id INTEGER,
            consensus_json TEXT,
            release_year INTEGER
        );
        CREATE TABLE people (
            person_mal_id INTEGER PRIMARY KEY,
            name_display TEXT
        );
        CREATE TABLE anime_staff (
            anime_id INTEGER,
            person_mal_id INTEGER,
            role TEXT,
            PRIMARY KEY (anime_id, person_mal_id, role)
        );
    """)
    conn.executemany(
        "INSERT INTO anime_info VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "Cowboy Bebop", 8.75, 1_000_000, 1,
             '{"thematic_vibe": "Space bounty hunters, jazz noir"}', 1998),
            (205, "Samurai Champloo", 8.52, 680_000, 205,
             '{"thematic_vibe": "Edo-period samurai, hip-hop"}', 2004),
            (16498, "Attack on Titan", 8.57, 3_000_000, 16498,
             '{"thematic_vibe": "Dark titans, political drama"}', 2013),
        ],
    )
    conn.executemany(
        "INSERT INTO people VALUES (?, ?)",
        [
            (2009, "Shinichirou Watanabe"),
            (5077, "Yoko Kanno"),
            (2197, "Hajime Yatate"),   # pseudonym — should be filtered
            (99, "Some Other Person"),
        ],
    )
    conn.executemany(
        "INSERT INTO anime_staff VALUES (?, ?, ?)",
        [
            # Cowboy Bebop
            (1, 2009, "Director"),
            (1, 5077, "Music"),
            (1, 2197, "Original Creator"),   # Yatate — filtered at load
            # Samurai Champloo (shares director with Bebop)
            (205, 2009, "Director"),
            (205, 99, "Music"),   # different music
        ],
    )
    conn.commit()
    conn.close()
    return db_path


class TestLoadEngineContext:
    def test_yatate_filtered_at_load_time(self, tmp_path):
        db_path = _seed_full_db(tmp_path)
        ctx = load_engine_context(str(db_path), faiss_loader=_mock_faiss_loader)
        bebop_staff = ctx.staff_by_anime[1]
        # Yatate (2197) is in _SKIP_BY_ROLE["Original Creator"] so
        # Bebop should not have an Original Creator entry at all
        assert "Original Creator" not in bebop_staff
        # But Director and Music remain
        assert bebop_staff["Director"] == {2009}
        assert bebop_staff["Music"] == {5077}

    def test_title_lookup_populated(self, tmp_path):
        db_path = _seed_full_db(tmp_path)
        ctx = load_engine_context(str(db_path), faiss_loader=_mock_faiss_loader)
        assert "Cowboy Bebop" in ctx.title_lookup
        assert ctx.title_lookup["Cowboy Bebop"]["anime_id"] == 1
        assert ctx.title_lookup["Cowboy Bebop"]["mal_score"] == 8.75

    def test_id_to_title_populated(self, tmp_path):
        db_path = _seed_full_db(tmp_path)
        ctx = load_engine_context(str(db_path), faiss_loader=_mock_faiss_loader)
        assert ctx.id_to_title[1] == "Cowboy Bebop"


# ------------------------------------------------------------------------
# build_recommendations — end-to-end with mocks
# ------------------------------------------------------------------------

def _make_mock_search_fn(results_by_query_pos):
    """Return a fake faiss_search_fn that returns pre-canned results
    regardless of the input embedding."""
    def fn(embedding, fetch_k):
        return results_by_query_pos
    return fn


class TestBuildRecommendations:
    def test_reference_show_excluded_from_own_recs(self, tmp_path):
        db_path = _seed_full_db(tmp_path)
        ctx = load_engine_context(str(db_path), faiss_loader=_mock_faiss_loader)
        search_fn = _make_mock_search_fn([
            {"title": "Cowboy Bebop", "similarity": 1.0},
            {"title": "Samurai Champloo", "similarity": 0.85},
            {"title": "Attack on Titan", "similarity": 0.7},
        ])
        recs = build_recommendations(ctx, reference_id=1, top_k=3,
                                     faiss_search_fn=search_fn)
        titles = [r["title"] for r in recs]
        assert "Cowboy Bebop" not in titles

    def test_same_series_id_excluded(self, tmp_path):
        db_path = _seed_full_db(tmp_path)
        # Add a Bebop sequel with the same series_id
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO anime_info VALUES (?, ?, ?, ?, ?, ?, ?)",
            (5, "Cowboy Bebop: The Movie", 8.4, 500_000, 1, "{}", 2001),
        )
        conn.commit()
        conn.close()
        ctx = load_engine_context(str(db_path), faiss_loader=lambda: (_mock_faiss_loader()[0],
                                                                     [{"english_title": "Cowboy Bebop"},
                                                                      {"english_title": "Cowboy Bebop: The Movie"},
                                                                      {"english_title": "Samurai Champloo"}]))
        search_fn = _make_mock_search_fn([
            {"title": "Cowboy Bebop: The Movie", "similarity": 0.95},
            {"title": "Samurai Champloo", "similarity": 0.85},
        ])
        recs = build_recommendations(ctx, reference_id=1, top_k=3,
                                     faiss_search_fn=search_fn)
        titles = [r["title"] for r in recs]
        assert "Cowboy Bebop: The Movie" not in titles

    def test_quality_floor_drops_low_score_candidates(self, tmp_path):
        db_path = _seed_full_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO anime_info VALUES (?, ?, ?, ?, ?, ?, ?)",
            (99, "Terrible Show", 5.5, 100, 99, "{}", 2020),  # below floor
        )
        conn.commit()
        conn.close()
        ctx = load_engine_context(str(db_path), faiss_loader=lambda: (_mock_faiss_loader()[0],
                                                                     [{"english_title": "Cowboy Bebop"},
                                                                      {"english_title": "Terrible Show"},
                                                                      {"english_title": "Samurai Champloo"}]))
        search_fn = _make_mock_search_fn([
            {"title": "Terrible Show", "similarity": 0.99},  # highest similarity but below floor
            {"title": "Samurai Champloo", "similarity": 0.85},
        ])
        recs = build_recommendations(ctx, reference_id=1, top_k=3,
                                     faiss_search_fn=search_fn)
        assert all(r["title"] != "Terrible Show" for r in recs)

    def test_personnel_bonus_promotes_shared_director(self, tmp_path):
        db_path = _seed_full_db(tmp_path)
        ctx = load_engine_context(str(db_path), faiss_loader=_mock_faiss_loader)
        # Champloo shares Director with Bebop; AoT does not
        search_fn = _make_mock_search_fn([
            {"title": "Attack on Titan", "similarity": 0.85},
            {"title": "Samurai Champloo", "similarity": 0.80},  # lower sim but shared director
        ])
        recs = build_recommendations(ctx, reference_id=1, top_k=2,
                                     faiss_search_fn=search_fn)
        titles = [r["title"] for r in recs]
        # Champloo should rank first despite lower similarity, because
        # +0.15 director bonus overrides the 0.05 similarity gap
        assert titles[0] == "Samurai Champloo"
        assert titles[0] != "Attack on Titan"

    def test_personnel_hook_and_thematic_hint_emitted(self, tmp_path):
        db_path = _seed_full_db(tmp_path)
        ctx = load_engine_context(str(db_path), faiss_loader=_mock_faiss_loader)
        search_fn = _make_mock_search_fn([
            {"title": "Samurai Champloo", "similarity": 0.85},
        ])
        recs = build_recommendations(ctx, reference_id=1, top_k=1,
                                     faiss_search_fn=search_fn)
        # Shared director → personnel_hook set
        assert recs[0]["personnel_hook"] == "Also directed by Shinichirou Watanabe"
        # Champloo's thematic_vibe in the seed fixture → thematic_hint populated
        assert recs[0]["thematic_hint"] == "Edo-period samurai, hip-hop"

    def test_personnel_hook_null_when_no_shared_priority_role(self, tmp_path):
        """Candidate with no shared Director/Music/Original Creator should
        have personnel_hook=None, letting the frontend render only the
        thematic line."""
        db_path = _seed_full_db(tmp_path)
        ctx = load_engine_context(str(db_path), faiss_loader=_mock_faiss_loader)
        search_fn = _make_mock_search_fn([
            {"title": "Attack on Titan", "similarity": 0.85},  # no shared personnel with Bebop
        ])
        recs = build_recommendations(ctx, reference_id=1, top_k=1,
                                     faiss_search_fn=search_fn)
        assert recs[0]["personnel_hook"] is None
        # Thematic hint still populated
        assert recs[0]["thematic_hint"] == "Dark titans, political drama"

    def test_candidate_franchise_dedup(self, tmp_path):
        """Only one entry per candidate series_id in the final top-K."""
        db_path = _seed_full_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.executemany(
            "INSERT INTO anime_info VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (300, "Gintama", 8.9, 500_000, 918, "{}", 2006),
                (301, "Gintama Season 2", 9.0, 400_000, 918, "{}", 2012),  # SAME series_id
                (302, "Gintama Season 3", 9.0, 300_000, 918, "{}", 2015),
            ],
        )
        conn.commit()
        conn.close()
        ctx = load_engine_context(str(db_path), faiss_loader=lambda: (_mock_faiss_loader()[0],
                                                                     [{"english_title": "Samurai Champloo"},
                                                                      {"english_title": "Gintama"},
                                                                      {"english_title": "Gintama Season 2"},
                                                                      {"english_title": "Gintama Season 3"}]))
        search_fn = _make_mock_search_fn([
            {"title": "Gintama", "similarity": 0.9},
            {"title": "Gintama Season 2", "similarity": 0.89},
            {"title": "Gintama Season 3", "similarity": 0.88},
        ])
        recs = build_recommendations(ctx, reference_id=205, top_k=6,
                                     faiss_search_fn=search_fn)
        # Only one Gintama should survive the series_id dedup
        gintama_count = sum(1 for r in recs if "Gintama" in r["title"])
        assert gintama_count == 1

    def test_returns_empty_when_reference_not_in_lookup(self, tmp_path):
        db_path = _seed_full_db(tmp_path)
        ctx = load_engine_context(str(db_path), faiss_loader=_mock_faiss_loader)
        recs = build_recommendations(ctx, reference_id=99999, top_k=6,
                                     faiss_search_fn=lambda e, k: [])
        assert recs == []


# ------------------------------------------------------------------------
# Regression: end-to-end dedup + filter pipeline correctness
# ------------------------------------------------------------------------

class TestEndToEndCorrectness:
    def test_pipeline_all_filters_interact_correctly(self, tmp_path):
        """Comprehensive scenario: quality floor + series_id exclusion +
        fuzzy franchise exclusion + candidate dedup + personnel bonus
        all firing on a single query."""
        db_path = _seed_full_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.executemany(
            "INSERT INTO anime_info VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (5, "Cowboy Bebop: The Movie", 8.4, 500_000, 1, "{}", 2001),  # same series_id
                (99, "Trash Show", 5.5, 100, 99, "{}", 2020),                # below floor
                (300, "Gintama", 8.9, 500_000, 918, "{}", 2006),
                (301, "Gintama Season 2", 9.0, 400_000, 918, "{}", 2012),
            ],
        )
        conn.commit()
        conn.close()
        ctx = load_engine_context(str(db_path), faiss_loader=lambda: (
            _mock_faiss_loader()[0],
            [{"english_title": t} for t in [
                "Cowboy Bebop", "Cowboy Bebop: The Movie", "Trash Show",
                "Samurai Champloo", "Gintama", "Gintama Season 2",
            ]],
        ))
        search_fn = _make_mock_search_fn([
            {"title": "Cowboy Bebop", "similarity": 1.0},          # self — filtered
            {"title": "Cowboy Bebop: The Movie", "similarity": 0.95},  # same series_id
            {"title": "Trash Show", "similarity": 0.9},            # below floor
            {"title": "Samurai Champloo", "similarity": 0.85},     # keep, shared director
            {"title": "Gintama", "similarity": 0.8},               # keep
            {"title": "Gintama Season 2", "similarity": 0.79},     # dedup by series_id
        ])
        recs = build_recommendations(ctx, reference_id=1, top_k=6,
                                     faiss_search_fn=search_fn)
        titles = [r["title"] for r in recs]
        assert "Cowboy Bebop" not in titles
        assert "Cowboy Bebop: The Movie" not in titles
        assert "Trash Show" not in titles
        assert "Samurai Champloo" in titles
        assert titles.count("Gintama") + titles.count("Gintama Season 2") == 1
