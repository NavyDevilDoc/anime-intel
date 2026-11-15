"""Unit tests for src/seasonal_ingest_manifest.

Phase C of the Vault Coverage Investigation: silent-miss visibility
for seasonal ingest runs. Tests exercise the manifest dataclass,
outcome-computation logic (pure function), and JSON persistence
independently of the ingestor so we can verify the accounting
rigor without spinning up MALClient / JikanClient / Gemini.
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

from src.seasonal_ingest_manifest import (
    MANIFEST_SUBDIR,
    SeasonalIngestManifest,
    compute_outcomes,
    snapshot_vault_and_quarantine,
    write_manifest,
)


def _make_manifest(**overrides):
    """Build a fully-populated manifest for summary_line + serialization tests."""
    defaults = {
        "run_id": "2026-09-25T15-42-00Z",
        "year": 2026,
        "season": "fall",
        "season_tag": "Fall_2026",
        "target_ids_mode": False,
    }
    defaults.update(overrides)
    return SeasonalIngestManifest(**defaults)


class TestSummaryLine:
    """The one-line summary printed at end-of-run and injected into
    the seasonal-ingest workflow's auto-PR body."""

    def test_all_zero_populated_counts(self):
        m = _make_manifest()
        line = m.summary_line
        assert "Fall_2026" in line
        assert "source=0" in line
        assert "attempted=0" in line
        assert "success=0" in line
        assert "newly_vaulted=0" in line
        assert "MISSED=0" in line

    def test_missed_gets_bolded_when_nonzero(self):
        """MISSED > 0 is the bug signal — it must visually stand out
        in the PR body, not blend in with routine counts."""
        m = _make_manifest(missed=[100, 200])
        line = m.summary_line
        assert "**MISSED=2**" in line

    def test_missed_not_bolded_when_zero(self):
        m = _make_manifest(missed=[])
        line = m.summary_line
        assert "MISSED=0" in line
        assert "**MISSED=" not in line

    def test_shows_newly_vaulted_alongside_success(self):
        """Phase B refinement — success count alone would mislead about
        product impact; newly_vaulted disambiguates."""
        m = _make_manifest(
            processed_success=[1, 2, 3, 4, 5],
            newly_vaulted=[5],
        )
        line = m.summary_line
        assert "success=5" in line
        assert "newly_vaulted=1" in line


class TestToDict:
    """JSON serialization — deterministic for cross-run diffing."""

    def test_lists_are_sorted(self):
        m = _make_manifest(
            source_ids=[3, 1, 2],
            attempted_ids=[2, 1],
            processed_success=[2],
            processed_quarantine=[1],
        )
        d = m.to_dict()
        assert d["source_ids"] == [1, 2, 3]
        assert d["attempted_ids"] == [1, 2]

    def test_skipped_non_tv_serializes_dict_keys_as_strings(self):
        """JSON only accepts string keys — int keys need coercion,
        and sorted for stability."""
        m = _make_manifest(skipped_non_tv={200: "media_type=ova", 100: "media_type=movie"})
        d = m.to_dict()
        assert list(d["skipped_non_tv"].keys()) == ["100", "200"]
        assert d["skipped_non_tv"]["100"] == "media_type=movie"

    def test_dataclass_fields_all_serialize(self):
        m = _make_manifest()
        d = m.to_dict()
        expected_keys = {
            "run_id", "year", "season", "season_tag", "target_ids_mode",
            "source_ids", "skipped_non_tv", "attempted_ids",
            "processed_success", "processed_quarantine",
            "newly_vaulted", "missed",
        }
        assert set(d.keys()) == expected_keys


class TestSnapshotVaultAndQuarantine:
    """Reads current sets from anime_info + intelligence_quarantine
    for computing newly_vaulted deltas at run finalization."""

    def _make_db(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        with sqlite3.connect(db_path) as conn:
            conn.execute("""
                CREATE TABLE anime_info (
                    id INTEGER PRIMARY KEY,
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

    def test_reads_both_tables(self, tmp_path):
        db_path = self._make_db(tmp_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute("INSERT INTO anime_info (id, consensus_json) VALUES (1, '{}'), (2, '{}')")
            conn.execute("INSERT INTO intelligence_quarantine (mal_id, title, season, failure_node, error_message) "
                         "VALUES (100, 'X', 'Fall_2026', 'JIKAN_API', 'no reviews')")

        vaulted, quarantined = snapshot_vault_and_quarantine(db_path)
        assert vaulted == {1, 2}
        assert quarantined == {100}

    def test_null_consensus_excluded_from_vault_set(self, tmp_path):
        """A row without consensus_json isn't a real vault member —
        excluding it here prevents newly_vaulted from being wrong
        when a partial-write leaves a naked id row lying around."""
        db_path = self._make_db(tmp_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute("INSERT INTO anime_info (id, consensus_json) VALUES (1, '{}')")
            conn.execute("INSERT INTO anime_info (id, consensus_json) VALUES (2, NULL)")

        vaulted, _ = snapshot_vault_and_quarantine(db_path)
        assert vaulted == {1}
        assert 2 not in vaulted

    def test_empty_tables_return_empty_sets(self, tmp_path):
        db_path = self._make_db(tmp_path)
        vaulted, quarantined = snapshot_vault_and_quarantine(db_path)
        assert vaulted == set()
        assert quarantined == set()


class TestComputeOutcomes:
    """The pure accounting function. Load-bearing correctness — every
    manifest field derives from this. Exercise all four outcome buckets
    plus edge cases."""

    def test_all_success_all_new(self):
        """First run against an empty vault — all attempts succeed
        AND all count as newly vaulted."""
        success, quarantined, newly_vaulted, missed = compute_outcomes(
            attempted=[1, 2, 3],
            current_vaulted={1, 2, 3},
            current_quarantined=set(),
            prior_vaulted=set(),
        )
        assert success == [1, 2, 3]
        assert quarantined == []
        assert newly_vaulted == [1, 2, 3]
        assert missed == []

    def test_all_success_none_new(self):
        """Refresh run — everything was already vaulted, consensus
        got updated but no newly_vaulted entries. Matches the Phase B
        pattern where 47 of 48 rescues were pre-existing vault rows."""
        success, quarantined, newly_vaulted, missed = compute_outcomes(
            attempted=[1, 2, 3],
            current_vaulted={1, 2, 3},
            current_quarantined=set(),
            prior_vaulted={1, 2, 3},
        )
        assert success == [1, 2, 3]
        assert newly_vaulted == []
        assert missed == []

    def test_mixed_success_and_quarantine(self):
        """Typical seasonal-run outcome — some shows land, some fail."""
        success, quarantined, newly_vaulted, missed = compute_outcomes(
            attempted=[1, 2, 3, 4, 5],
            current_vaulted={1, 2, 3},
            current_quarantined={4, 5},
            prior_vaulted={1},  # only #1 was pre-existing
        )
        assert success == [1, 2, 3]
        assert quarantined == [4, 5]
        assert newly_vaulted == [2, 3]
        assert missed == []

    def test_missed_detected_when_show_falls_out_of_accounting(self):
        """The whole point of the manifest: catch shows attempted
        but landing in NEITHER anime_info NOR quarantine. Should be
        empty in a healthy pipeline; non-zero signals a bug."""
        success, quarantined, newly_vaulted, missed = compute_outcomes(
            attempted=[1, 2, 3],
            current_vaulted={1},
            current_quarantined={2},
            prior_vaulted=set(),
        )
        # #3 was attempted but ended up nowhere — MISSED
        assert success == [1]
        assert quarantined == [2]
        assert missed == [3]

    def test_shows_not_in_attempted_are_ignored(self):
        """Vault + quarantine may contain shows from prior runs that
        aren't relevant to this run's attempted set. They must not
        appear in success/quarantine/newly_vaulted/missed."""
        success, quarantined, newly_vaulted, missed = compute_outcomes(
            attempted=[1, 2],
            current_vaulted={1, 2, 999},   # 999 is from a prior season
            current_quarantined={888},     # 888 too
            prior_vaulted={999},
        )
        assert success == [1, 2]
        assert 999 not in success
        assert 999 not in newly_vaulted
        assert quarantined == []
        assert missed == []

    def test_empty_attempted_yields_all_empty_buckets(self):
        """MAL fetch returned nothing (or every show was filtered out).
        All buckets empty, no MISSED — nothing to account for."""
        success, quarantined, newly_vaulted, missed = compute_outcomes(
            attempted=[],
            current_vaulted={1, 2, 3},
            current_quarantined={99},
            prior_vaulted={1},
        )
        assert success == []
        assert quarantined == []
        assert newly_vaulted == []
        assert missed == []


class TestWriteManifest:
    """JSON persistence to data/seasonal_ingest_manifests/."""

    def test_writes_json_with_correct_filename(self, tmp_path):
        m = _make_manifest(run_id="2026-09-25T15-42-00Z", season_tag="Fall_2026")
        fpath = write_manifest(m, str(tmp_path))
        assert os.path.exists(fpath)
        assert fpath.endswith("2026-09-25T15-42-00Z_Fall_2026.json")
        assert MANIFEST_SUBDIR in fpath

    def test_creates_subdir_if_missing(self, tmp_path):
        """First-run scenario — the manifest subdir doesn't exist yet.
        write_manifest must create it, not fail."""
        m = _make_manifest()
        # tmp_path is fresh — no data/seasonal_ingest_manifests/ exists
        fpath = write_manifest(m, str(tmp_path))
        assert os.path.exists(os.path.dirname(fpath))

    def test_json_content_matches_to_dict(self, tmp_path):
        m = _make_manifest(
            source_ids=[3, 1, 2],
            attempted_ids=[1, 2],
            processed_success=[1, 2],
            newly_vaulted=[2],
            skipped_non_tv={99: "media_type=movie"},
        )
        fpath = write_manifest(m, str(tmp_path))
        with open(fpath, encoding="utf-8") as f:
            loaded = json.load(f)
        assert loaded == m.to_dict()

    def test_written_json_is_diff_stable(self, tmp_path):
        """Same manifest content → same file bytes. Enables
        cross-run diffing without ordering noise."""
        m1 = _make_manifest(source_ids=[3, 1, 2], processed_success=[2, 1])
        m2 = _make_manifest(source_ids=[1, 2, 3], processed_success=[1, 2])
        f1 = write_manifest(m1, str(tmp_path / "run1"))
        f2 = write_manifest(m2, str(tmp_path / "run2"))
        with open(f1, encoding="utf-8") as f:
            content1 = f.read()
        with open(f2, encoding="utf-8") as f:
            content2 = f.read()
        assert content1 == content2
