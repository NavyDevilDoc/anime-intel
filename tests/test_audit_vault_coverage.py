"""Unit tests for tools/audit_vault_coverage.

Phase D of the Vault Coverage Investigation — coverage audit tool.
Tests focus on the pure classification / gap-building / aggregation
logic. MAL API is not exercised in-process (that's the smoke test).
"""
import json
import os
import sqlite3
import sys

import pytest

TESTS_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# tools/ isn't on sys.path by default — add it so the audit module imports.
TOOLS_DIR = os.path.join(ROOT_DIR, "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

from audit_vault_coverage import (  # noqa: E402
    CAT_MISCLASSIFIED,
    CAT_MISSING,
    CAT_QUARANTINED,
    CAT_SKIPPED_NON_TV,
    CAT_VAULTED,
    _extract_show_fields,
    build_gap_entry,
    build_report,
    build_season_audit_block,
    categorize_show,
    mal_start_season_tag,
    snapshot_vault_state,
    write_report,
)


class TestExtractShowFields:
    """Compact-shape extraction from MAL node payloads — shared by
    both fetch paths so a mid-list schema surprise is caught once."""

    def test_extracts_expected_fields(self):
        node = {
            "id": 21,
            "title": "One Piece",
            "alternative_titles": {"en": "One Piece"},
            "media_type": "tv",
            "num_scoring_users": 1558955,
            "mean": 8.72,
            "start_season": {"season": "fall", "year": 1999},
        }
        out = _extract_show_fields(node)
        assert out["mal_id"] == 21
        assert out["title"] == "One Piece"
        assert out["media_type"] == "tv"
        assert out["scored_by"] == 1558955
        assert out["mean"] == 8.72
        assert out["mal_start_season"] == "fall"
        assert out["mal_start_year"] == 1999

    def test_falls_back_to_japanese_title(self):
        node = {"id": 1, "title": "Kaubōi Bibappu", "alternative_titles": {}}
        out = _extract_show_fields(node)
        assert out["title"] == "Kaubōi Bibappu"

    def test_falls_back_to_mal_id_placeholder_when_no_title(self):
        node = {"id": 99999}
        out = _extract_show_fields(node)
        assert out["title"] == "MAL#99999"

    def test_missing_num_scoring_users_defaults_to_zero(self):
        node = {"id": 1, "title": "X", "alternative_titles": {}}
        out = _extract_show_fields(node)
        assert out["scored_by"] == 0

    def test_missing_start_season_is_none(self):
        node = {"id": 1, "title": "X", "alternative_titles": {}}
        out = _extract_show_fields(node)
        assert out["mal_start_season"] is None
        assert out["mal_start_year"] is None


class TestMalStartSeasonTag:
    """The 'Season_Year' string builder — must fall back cleanly for
    shows without start_season metadata."""

    def test_builds_capitalized_tag(self):
        show = {"mal_start_season": "fall", "mal_start_year": 1999}
        assert mal_start_season_tag(show) == "Fall_1999"

    def test_falls_back_when_season_missing(self):
        show = {"mal_start_season": None, "mal_start_year": 1999}
        assert mal_start_season_tag(show, fallback="Winter_2026") == "Winter_2026"

    def test_falls_back_when_year_missing(self):
        show = {"mal_start_season": "fall", "mal_start_year": None}
        assert mal_start_season_tag(show, fallback="X") == "X"

    def test_default_fallback_is_empty(self):
        show = {"mal_start_season": None, "mal_start_year": None}
        assert mal_start_season_tag(show) == ""


class TestCategorizeShow:
    """The pure classification function — all 5 outcomes must be
    correctly resolved based on media_type, vault state, and quarantine
    state. Note: after the 2026-09-26 refactor, the tag passed in is
    MAL's authoritative start_season, not the queried season."""

    def test_non_tv_is_skipped_regardless_of_vault_state(self):
        cat = categorize_show(
            mal_id=1, mal_start_season_tag="Fall_2026", media_type="movie",
            vaulted={1: "Fall_2026"}, quarantined={},
        )
        assert cat == CAT_SKIPPED_NON_TV

    def test_ova_is_skipped(self):
        cat = categorize_show(
            mal_id=1, mal_start_season_tag="Fall_2026", media_type="ova",
            vaulted={}, quarantined={},
        )
        assert cat == CAT_SKIPPED_NON_TV

    def test_tv_in_quarantine_is_quarantined(self):
        cat = categorize_show(
            mal_id=2, mal_start_season_tag="Fall_2026", media_type="tv",
            vaulted={}, quarantined={2: {"error_message": "x"}},
        )
        assert cat == CAT_QUARANTINED

    def test_tv_vaulted_matches_mal_start_season(self):
        cat = categorize_show(
            mal_id=1, mal_start_season_tag="Fall_2026", media_type="tv",
            vaulted={1: "Fall_2026"}, quarantined={},
        )
        assert cat == CAT_VAULTED

    def test_long_running_show_vaulted_under_start_season_is_still_vaulted(self):
        """Naruto scenario: start_season=Winter_2007, aired across many
        seasons. When the audit probes Summer_2007, MAL returns Naruto
        with mal_start_season_tag='Winter_2007' — vault agrees, so
        VAULTED. This was the false-positive bug pre-refactor."""
        cat = categorize_show(
            mal_id=20, mal_start_season_tag="Winter_2007", media_type="tv",
            vaulted={20: "Winter_2007"}, quarantined={},
        )
        assert cat == CAT_VAULTED

    def test_vault_disagrees_with_mal_start_season_is_misclassified(self):
        """Real MISCLASSIFIED case: vault has the show under a different
        canonical season than MAL currently claims. Rare, but real when
        MAL reclassifies a show after original ingest."""
        cat = categorize_show(
            mal_id=1, mal_start_season_tag="Fall_2026", media_type="tv",
            vaulted={1: "Summer_2025"}, quarantined={},
        )
        assert cat == CAT_MISCLASSIFIED

    # --- Option-B --media-types extension ---

    def test_movie_in_expanded_whitelist_classified_by_vault_state(self):
        """When --media-types tv movie is passed, a movie in the vault
        is VAULTED (not SKIPPED_NON_TV). This is the Option-B contract."""
        cat = categorize_show(
            mal_id=164, mal_start_season_tag="Summer_1997", media_type="movie",
            vaulted={164: "Summer_1997"}, quarantined={},
            allowed_media_types=frozenset({"tv", "movie"}),
        )
        assert cat == CAT_VAULTED

    def test_movie_missing_when_in_expanded_whitelist_but_not_vaulted(self):
        """Princess Mononoke (164) scenario — in MAL's popularity ranking,
        not in the vault, and the audit is explicitly scoping movies in.
        Should surface as MISSING so the backfill can target it."""
        cat = categorize_show(
            mal_id=164, mal_start_season_tag="Summer_1997", media_type="movie",
            vaulted={}, quarantined={},
            allowed_media_types=frozenset({"tv", "movie"}),
        )
        assert cat == CAT_MISSING

    def test_ova_in_expanded_whitelist_quarantine_wins(self):
        """Quarantine still takes precedence over MISSING even when the
        whitelist is widened — a show the ingestor tried and failed on
        shouldn't resurface in the backfill target list."""
        cat = categorize_show(
            mal_id=5, mal_start_season_tag="Fall_1995", media_type="ova",
            vaulted={}, quarantined={5: {"error_message": "distill_failed"}},
            allowed_media_types=frozenset({"tv", "ova"}),
        )
        assert cat == CAT_QUARANTINED

    def test_tv_not_in_whitelist_is_skipped(self):
        """Inverse scoping — if the operator passes --media-types movie
        (no tv), TV shows are categorized as SKIPPED. Confirms the
        whitelist is literal, not additive-to-default."""
        cat = categorize_show(
            mal_id=1, mal_start_season_tag="Fall_2026", media_type="tv",
            vaulted={1: "Fall_2026"}, quarantined={},
            allowed_media_types=frozenset({"movie"}),
        )
        assert cat == CAT_SKIPPED_NON_TV

    def test_unknown_media_type_skipped_regardless_of_whitelist(self):
        """MAL's media_type='unknown' fallback shouldn't accidentally land
        in a backfill target list just because the whitelist is wide."""
        cat = categorize_show(
            mal_id=99, mal_start_season_tag="Fall_2026", media_type="unknown",
            vaulted={}, quarantined={},
            allowed_media_types=frozenset({"tv", "movie", "ova", "ona", "special"}),
        )
        assert cat == CAT_SKIPPED_NON_TV

    def test_empty_whitelist_skips_everything(self):
        """Edge case: operator passes empty --media-types. Degenerate but
        shouldn't crash — everything becomes SKIPPED."""
        cat = categorize_show(
            mal_id=1, mal_start_season_tag="Fall_2026", media_type="tv",
            vaulted={1: "Fall_2026"}, quarantined={},
            allowed_media_types=frozenset(),
        )
        assert cat == CAT_SKIPPED_NON_TV

    def test_default_whitelist_preserves_tv_only_behavior(self):
        """Guard: if we ever change DEFAULT_ALLOWED_MEDIA_TYPES, the
        existing TV-only behavior stays the default. This catches any
        future refactor that silently expands the default scope."""
        from tools.audit_vault_coverage import DEFAULT_ALLOWED_MEDIA_TYPES
        assert DEFAULT_ALLOWED_MEDIA_TYPES == frozenset({"tv"})

    def test_tv_not_in_vault_or_quarantine_is_missing(self):
        """The Phase D core case — show MAL returned but our pipeline
        never touched. The Air Gear anchor case."""
        cat = categorize_show(
            mal_id=857, mal_start_season_tag="Spring_2006", media_type="tv",
            vaulted={}, quarantined={},
        )
        assert cat == CAT_MISSING


class TestBuildGapEntry:
    """Category-specific fields in the gap record."""

    def _base_show(self, mal_id: int = 1, title: str = "Test Show") -> dict:
        return {
            "mal_id": mal_id,
            "title": title,
            "media_type": "tv",
            "scored_by": 12345,
            "mean": 8.5,
            "mal_start_season": "spring",
            "mal_start_year": 2006,
        }

    def test_missing_entry_has_bare_fields_plus_start_season(self):
        """MISSING entries omit category-specific extras (no quarantine
        metadata, no vault_season), but always include mal_start_season /
        mal_start_year so backfill tooling can filter by year without
        re-fetching MAL."""
        entry = build_gap_entry(
            self._base_show(), CAT_MISSING, quarantine_meta=None, vault_season=None,
        )
        assert entry["mal_id"] == 1
        assert entry["title"] == "Test Show"
        assert entry["scored_by"] == 12345
        assert entry["mal_start_season"] == "spring"
        assert entry["mal_start_year"] == 2006
        assert "quarantine_error" not in entry
        assert "vault_season" not in entry

    def test_quarantined_entry_includes_error_metadata(self):
        meta = {"error_message": "Returned 0 reviews", "last_attempt": "2026-08-30"}
        entry = build_gap_entry(
            self._base_show(), CAT_QUARANTINED, quarantine_meta=meta, vault_season=None,
        )
        assert entry["quarantine_error"] == "Returned 0 reviews"
        assert entry["last_attempt"] == "2026-08-30"

    def test_misclassified_entry_includes_both_seasons(self):
        entry = build_gap_entry(
            self._base_show(), CAT_MISCLASSIFIED, quarantine_meta=None,
            vault_season="Summer_2025",
        )
        assert entry["vault_season"] == "Summer_2025"
        assert entry["mal_season"] == "Spring_2006"


class TestSnapshotVaultState:
    """Read the vault + quarantine snapshots that categorize_show
    consumes. Fixture DB isolates from the real vault."""

    def _make_db(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        with sqlite3.connect(db_path) as conn:
            conn.execute("""
                CREATE TABLE anime_info (
                    id INTEGER PRIMARY KEY,
                    season TEXT,
                    consensus_json TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE intelligence_quarantine (
                    mal_id INTEGER PRIMARY KEY,
                    title TEXT,
                    season TEXT,
                    failure_node TEXT,
                    error_message TEXT,
                    last_attempt TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        return db_path

    def test_reads_vaulted_with_season_tag(self, tmp_path):
        db_path = self._make_db(tmp_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute("INSERT INTO anime_info (id, season, consensus_json) VALUES (1, 'Fall_2026', '{}')")
            conn.execute("INSERT INTO anime_info (id, season, consensus_json) VALUES (2, 'Spring_2020', '{}')")
        vaulted, quarantined = snapshot_vault_state(db_path)
        assert vaulted == {1: "Fall_2026", 2: "Spring_2020"}
        assert quarantined == {}

    def test_null_consensus_not_in_vaulted_snapshot(self, tmp_path):
        db_path = self._make_db(tmp_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute("INSERT INTO anime_info (id, season, consensus_json) VALUES (1, 'Fall_2026', '{}')")
            conn.execute("INSERT INTO anime_info (id, season, consensus_json) VALUES (2, 'Fall_2026', NULL)")
        vaulted, _ = snapshot_vault_state(db_path)
        assert 1 in vaulted
        assert 2 not in vaulted

    def test_reads_quarantine_with_error_metadata(self, tmp_path):
        db_path = self._make_db(tmp_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO intelligence_quarantine (mal_id, title, season, failure_node, error_message) "
                "VALUES (99, 'Bad Show', 'Fall_2026', 'JIKAN_API', 'no reviews')"
            )
        _, quarantined = snapshot_vault_state(db_path)
        assert 99 in quarantined
        assert quarantined[99]["error_message"] == "no reviews"

    def test_null_season_is_empty_string_not_none(self, tmp_path):
        db_path = self._make_db(tmp_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute("INSERT INTO anime_info (id, season, consensus_json) VALUES (1, NULL, '{}')")
        vaulted, _ = snapshot_vault_state(db_path)
        assert vaulted[1] == ""


class TestBuildSeasonAuditBlock:
    """Aggregation of per-season season audits into the season_audit
    block that lands inside the top-level report."""

    def _season_report(self, cats: dict, source_count: int = 100) -> dict:
        return {
            "year": 2006,
            "season": "spring",
            "season_tag": "Spring_2006",
            "source_count": source_count,
            "categories": cats,
            "gaps": {CAT_MISSING: [], CAT_QUARANTINED: [], CAT_MISCLASSIFIED: []},
        }

    def test_aggregates_category_counts_across_seasons(self):
        seasons = [
            self._season_report({CAT_VAULTED: 40, CAT_MISSING: 5}, source_count=45),
            self._season_report({CAT_VAULTED: 30, CAT_MISSING: 3, CAT_QUARANTINED: 2}, source_count=35),
        ]
        block = build_season_audit_block(seasons, (2006, 2006), ("spring", "summer"))
        assert block["overall_categories"] == {
            CAT_VAULTED: 70, CAT_MISSING: 8, CAT_QUARANTINED: 2,
        }
        assert block["total_shows_probed"] == 80
        assert block["years_range"] == [2006, 2006]
        assert block["seasons_filter"] == ["spring", "summer"]


class TestBuildReport:
    """Top-level report assembly — nested audit blocks based on which
    modes ran."""

    def test_report_with_only_season_audit(self):
        season_block = {"years_range": [2006, 2006], "overall_categories": {}, "seasons": []}
        report = build_report(modes_run=["season"], season_audit=season_block)
        assert report["modes_run"] == ["season"]
        assert "season_audit" in report
        assert "popularity_audit" not in report

    def test_report_with_only_popularity_audit(self):
        pop_block = {"top_n_target": 5000, "categories": {}, "gaps": {}}
        report = build_report(modes_run=["popularity"], popularity_audit=pop_block)
        assert report["modes_run"] == ["popularity"]
        assert "popularity_audit" in report
        assert "season_audit" not in report

    def test_combined_report_includes_both_blocks(self):
        season_block = {"years_range": [2006, 2006], "overall_categories": {}, "seasons": []}
        pop_block = {"top_n_target": 5000, "categories": {}, "gaps": {}}
        report = build_report(
            modes_run=["season", "popularity"],
            season_audit=season_block, popularity_audit=pop_block,
        )
        assert report["modes_run"] == ["season", "popularity"]
        assert "season_audit" in report
        assert "popularity_audit" in report

    def test_report_id_present(self):
        report = build_report(modes_run=["season"], season_audit={})
        assert "report_id" in report

    def test_report_records_media_types_filter(self):
        """Option-B contract: the report includes `media_types` so
        downstream consumers (backfill_from_coverage_report.py) can
        cross-check against their own expectations."""
        report = build_report(
            modes_run=["popularity"],
            popularity_audit={},
            media_types=["tv", "movie", "ova"],
        )
        assert report["media_types"] == ["movie", "ova", "tv"]  # sorted

    def test_report_defaults_media_types_to_tv_when_not_passed(self):
        """Backward compat: existing callers that don't pass media_types
        get the TV-only default surfaced in the report."""
        report = build_report(modes_run=["season"], season_audit={})
        assert report["media_types"] == ["tv"]


class TestWriteReport:
    """JSON persistence — creates the target directory if missing."""

    def test_writes_json_at_target_path(self, tmp_path):
        report = {"report_id": "test", "total_shows_probed": 42}
        output_path = str(tmp_path / "subdir" / "report.json")
        write_report(report, output_path)
        assert os.path.exists(output_path)
        with open(output_path, encoding="utf-8") as f:
            loaded = json.load(f)
        assert loaded == report
