"""Unit tests for tools/backfill_from_coverage_report.

Phase E of the Vault Coverage Investigation. Tests cover the pure
filter / extract logic — MAL API + ingestor side-effects are not
exercised in-process (the --execute path is validated by the live
Phase E backfill run itself).
"""
import json
import os
import sys

import pytest

TESTS_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

TOOLS_DIR = os.path.join(ROOT_DIR, "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

from backfill_from_coverage_report import (  # noqa: E402
    extract_missing_from_source,
    filter_candidates,
    find_latest_report,
    load_report,
)


def _make_entry(mal_id: int, scored_by: int, year: int = 2020, title: str = "Test") -> dict:
    return {
        "mal_id": mal_id,
        "title": title,
        "media_type": "tv",
        "scored_by": scored_by,
        "mean": 8.0,
        "mal_start_season": "spring",
        "mal_start_year": year,
    }


class TestFindLatestReport:
    """Newest-report discovery — sorts by filename (ISO date embedded
    in the name), returns None on empty dir."""

    def test_returns_none_when_no_reports(self, tmp_path):
        assert find_latest_report(str(tmp_path)) is None

    def test_returns_newest_by_lexical_sort(self, tmp_path):
        (tmp_path / "vault_coverage_report_2026-09-25.json").write_text("{}")
        (tmp_path / "vault_coverage_report_2026-09-26.json").write_text("{}")
        (tmp_path / "vault_coverage_report_2026-09-24.json").write_text("{}")
        latest = find_latest_report(str(tmp_path))
        assert latest is not None
        assert "2026-09-26" in latest

    def test_ignores_non_matching_files(self, tmp_path):
        (tmp_path / "something_else.json").write_text("{}")
        (tmp_path / "vault_coverage_report_2026-09-26.json").write_text("{}")
        latest = find_latest_report(str(tmp_path))
        assert "vault_coverage_report_2026-09-26" in latest


class TestLoadReport:
    def test_loads_json_correctly(self, tmp_path):
        content = {"report_id": "test", "modes_run": ["popularity"]}
        path = tmp_path / "report.json"
        path.write_text(json.dumps(content), encoding="utf-8")
        loaded = load_report(str(path))
        assert loaded == content


class TestExtractMissingFromSource:
    """Extract MISSING entries from the report's nested audit blocks."""

    def _report_with_popularity_missing(self, entries: list[dict]) -> dict:
        return {
            "modes_run": ["popularity"],
            "popularity_audit": {
                "gaps": {"MISSING": entries, "QUARANTINED": []},
            },
        }

    def _report_with_season_missing(self, per_season: list[list[dict]]) -> dict:
        return {
            "modes_run": ["season"],
            "season_audit": {
                "seasons": [
                    {"season_tag": f"Season_{i}", "gaps": {"MISSING": entries, "QUARANTINED": [], "MISCLASSIFIED": []}}
                    for i, entries in enumerate(per_season)
                ],
            },
        }

    def test_popularity_source_returns_missing(self):
        report = self._report_with_popularity_missing([
            _make_entry(1, 100_000), _make_entry(2, 50_000),
        ])
        entries = extract_missing_from_source(report, "popularity")
        assert len(entries) == 2
        assert entries[0]["mal_id"] == 1

    def test_popularity_source_missing_returns_empty_if_no_pop_audit(self):
        report = {"modes_run": ["season"]}
        assert extract_missing_from_source(report, "popularity") == []

    def test_season_source_flattens_across_seasons(self):
        report = self._report_with_season_missing([
            [_make_entry(10, 5000)],
            [_make_entry(20, 3000)],
            [_make_entry(30, 1000)],
        ])
        entries = extract_missing_from_source(report, "season")
        ids = {e["mal_id"] for e in entries}
        assert ids == {10, 20, 30}

    def test_season_source_deduplicates_long_running_shows(self):
        """A show returned by MAL in Spring + Summer + Fall of the same
        year appears in three season MISSING lists — dedupe to one entry."""
        entry = _make_entry(100, 50_000, title="Long Runner")
        report = self._report_with_season_missing([
            [entry], [entry], [entry],
        ])
        entries = extract_missing_from_source(report, "season")
        assert len(entries) == 1
        assert entries[0]["mal_id"] == 100

    def test_invalid_source_raises(self):
        with pytest.raises(ValueError, match="Unknown source"):
            extract_missing_from_source({}, "not_a_mode")


class TestFilterCandidates:
    """The filter that decides which MISSING entries go into the backfill."""

    def test_scored_by_threshold_applied(self):
        entries = [
            _make_entry(1, 200_000),
            _make_entry(2, 100_000),
            _make_entry(3, 50_000),
            _make_entry(4, 10_000),
        ]
        kept, excluded = filter_candidates(entries, min_scored_by=100_000, min_year=1990, allow_unknown_year=False)
        assert {e["mal_id"] for e in kept} == {1, 2}
        assert {e["mal_id"] for e in excluded} == {3, 4}

    def test_year_floor_excludes_pre_1990(self):
        """Dragon Ball case — top popularity but before vault floor."""
        entries = [
            _make_entry(223, 734_000, year=1986, title="Dragon Ball"),
            _make_entry(1, 200_000, year=2020, title="Modern Show"),
        ]
        kept, excluded = filter_candidates(entries, min_scored_by=100_000, min_year=1990, allow_unknown_year=False)
        kept_ids = {e["mal_id"] for e in kept}
        excluded_ids = {e["mal_id"] for e in excluded}
        assert 1 in kept_ids
        assert 223 in excluded_ids
        # Excluded entries carry a reason field so operators know why
        db_entry = next(e for e in excluded if e["mal_id"] == 223)
        assert "vault floor" in db_entry["_exclusion"]

    def test_unknown_year_excluded_by_default(self):
        entries = [_make_entry(1, 200_000)]
        entries[0]["mal_start_year"] = None
        kept, excluded = filter_candidates(entries, min_scored_by=100_000, min_year=1990, allow_unknown_year=False)
        assert len(kept) == 0
        assert len(excluded) == 1
        assert "unknown" in excluded[0]["_exclusion"]

    def test_unknown_year_included_when_flag_set(self):
        entries = [_make_entry(1, 200_000)]
        entries[0]["mal_start_year"] = None
        kept, excluded = filter_candidates(entries, min_scored_by=100_000, min_year=1990, allow_unknown_year=True)
        assert len(kept) == 1
        assert len(excluded) == 0

    def test_exclusion_reason_is_specific(self):
        entries = [
            _make_entry(1, 5_000),          # below scored_by threshold
            _make_entry(2, 200_000, year=1985),  # below year floor
        ]
        _, excluded = filter_candidates(entries, min_scored_by=100_000, min_year=1990, allow_unknown_year=False)
        by_id = {e["mal_id"]: e["_exclusion"] for e in excluded}
        assert "scored_by" in by_id[1]
        assert "vault floor" in by_id[2]

    def test_empty_input_returns_empty_lists(self):
        kept, excluded = filter_candidates([], 100_000, 1990, False)
        assert kept == []
        assert excluded == []
