"""Unit + integration tests for tools/backfill_staff.py and
tools/setup_staff_schema.py.

Uses in-memory SQLite so tests run fully offline. The Jikan HTTP layer is
never touched — we exercise the parser + writer directly with hand-crafted
payloads and StaffEntry values.
"""

import sqlite3

import pytest

from tools.backfill_staff import (
    ABORT_WARMUP_SHOWS,
    StaffEntry,
    TARGET_ROLES,
    check_tripwire,
    enumerate_shows,
    normalize_name,
    parse_staff_response,
    write_entries,
)
from tools.setup_staff_schema import apply_schema, verify_schema


# ------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------

def _seed_db(tmp_path):
    """Fresh SQLite file with a minimal anime_info + the staff schema applied."""
    db_path = tmp_path / "vault.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE anime_info ("
        " id INTEGER PRIMARY KEY,"
        " english_title TEXT,"
        " consensus_json TEXT"
        ")"
    )
    conn.executemany(
        "INSERT INTO anime_info (id, english_title, consensus_json) VALUES (?, ?, ?)",
        [
            (1, "Cowboy Bebop", "{}"),
            (2, "Samurai Champloo", "{}"),
            (3, "Terror in Resonance", "{}"),
        ],
    )
    conn.commit()
    conn.close()

    apply_schema(db_path)
    return db_path


# ------------------------------------------------------------------------
# normalize_name
# ------------------------------------------------------------------------

class TestNormalizeName:
    def test_family_first_comma(self):
        orig, disp, norm = normalize_name("Watanabe, Shinichirou")
        assert orig == "Watanabe, Shinichirou"
        assert disp == "Shinichirou Watanabe"
        assert norm == "shinichirou watanabe"

    def test_no_comma_preserves_original_as_display(self):
        orig, disp, norm = normalize_name("Yoko Kanno")
        assert orig == "Yoko Kanno"
        assert disp == "Yoko Kanno"
        assert norm == "yoko kanno"

    def test_strips_punctuation_in_normalized_only(self):
        orig, disp, norm = normalize_name("O'Brien, Sean")
        assert orig == "O'Brien, Sean"
        assert disp == "Sean O'Brien"
        # Normalization drops the apostrophe for lookup consistency
        assert norm == "sean obrien"

    def test_strips_initials_periods(self):
        _, disp, norm = normalize_name("Smith, J. R.")
        assert disp == "J. R. Smith"
        assert norm == "j r smith"

    def test_extra_whitespace_collapsed(self):
        _, _, norm = normalize_name("  Watanabe  ,   Shinichirou  ")
        assert norm == "shinichirou watanabe"

    def test_empty_string(self):
        orig, disp, norm = normalize_name("")
        assert orig == "" and disp == "" and norm == ""

    def test_whitespace_only_string(self):
        orig, disp, norm = normalize_name("   ")
        assert orig == "" and disp == "" and norm == ""


# ------------------------------------------------------------------------
# parse_staff_response
# ------------------------------------------------------------------------

class TestParseStaffResponse:
    def test_multi_role_person_yields_multiple_entries(self):
        """Watanabe on Bebop holds 4 positions; the 2 in TARGET_ROLES
        (Director + none — Music/Original Creator/Series Composition/
        Character Design not in his list) yield 1 entry."""
        payload = {
            "data": [
                {
                    "person": {"mal_id": 2009, "name": "Watanabe, Shinichirou"},
                    "positions": ["Director", "Episode Director", "Script", "Storyboard"],
                }
            ]
        }
        entries = parse_staff_response(payload)
        assert len(entries) == 1
        assert entries[0].role == "Director"
        assert entries[0].person_mal_id == 2009
        assert entries[0].name_display == "Shinichirou Watanabe"

    def test_person_with_multiple_target_roles(self):
        """A person who holds Director AND Series Composition on the
        same show yields two junction entries."""
        payload = {
            "data": [
                {
                    "person": {"mal_id": 100, "name": "Doe, Jane"},
                    "positions": ["Director", "Series Composition", "Script"],
                }
            ]
        }
        entries = parse_staff_response(payload)
        assert {e.role for e in entries} == {"Director", "Series Composition"}
        assert all(e.person_mal_id == 100 for e in entries)

    def test_non_target_positions_dropped(self):
        payload = {
            "data": [
                {
                    "person": {"mal_id": 200, "name": "Producer, Some"},
                    "positions": ["Producer", "Assistant Producer", "Planning"],
                }
            ]
        }
        assert parse_staff_response(payload) == []

    def test_missing_person_dropped(self):
        payload = {
            "data": [
                {"person": {}, "positions": ["Director"]},
                {"positions": ["Director"]},
            ]
        }
        assert parse_staff_response(payload) == []

    def test_missing_positions_treated_as_empty(self):
        payload = {"data": [{"person": {"mal_id": 1, "name": "X, Y"}}]}
        assert parse_staff_response(payload) == []

    def test_empty_data_array(self):
        assert parse_staff_response({"data": []}) == []

    def test_missing_data_key(self):
        assert parse_staff_response({}) == []


# ------------------------------------------------------------------------
# Schema setup (via apply_schema + verify)
# ------------------------------------------------------------------------

class TestSetupStaffSchema:
    def test_creates_both_tables_and_indexes(self, tmp_path):
        db_path = _seed_db(tmp_path)
        diag = verify_schema(db_path)
        assert set(diag["people_columns"]) == {
            "person_mal_id", "name_original", "name_display", "name_normalized"
        }
        assert set(diag["anime_staff_columns"]) == {
            "anime_id", "person_mal_id", "role"
        }
        assert "idx_people_name_normalized" in diag["people_indexes"]
        assert "idx_anime_staff_person" in diag["anime_staff_indexes"]
        assert "idx_anime_staff_role_person" in diag["anime_staff_indexes"]

    def test_check_constraint_present(self, tmp_path):
        db_path = _seed_db(tmp_path)
        diag = verify_schema(db_path)
        assert diag["anime_staff_check_constraint_present"] is True

    def test_check_constraint_rejects_unknown_role(self, tmp_path):
        """Fail-loudly guarantee: any INSERT with a non-target role must
        raise, not silently corrupt the table."""
        db_path = _seed_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO people (person_mal_id, name_original, name_display, name_normalized) "
            "VALUES (1, 'X, Y', 'Y X', 'y x')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO anime_staff (anime_id, person_mal_id, role) "
                "VALUES (1, 1, 'Producer')"  # Not in target set
            )
        conn.close()

    def test_check_constraint_accepts_all_target_roles(self, tmp_path):
        db_path = _seed_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO people (person_mal_id, name_original, name_display, name_normalized) "
            "VALUES (1, 'X, Y', 'Y X', 'y x')"
        )
        for role in TARGET_ROLES:
            conn.execute(
                "INSERT INTO anime_staff (anime_id, person_mal_id, role) VALUES (?, ?, ?)",
                (1, 1, role),
            )
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM anime_staff").fetchone()[0]
        assert n == len(TARGET_ROLES)
        conn.close()

    def test_idempotent_reapply(self, tmp_path):
        db_path = _seed_db(tmp_path)
        # Second apply_schema call must not raise
        existed = apply_schema(db_path)
        assert existed == {"people": True, "anime_staff": True}


# ------------------------------------------------------------------------
# write_entries (DB integration)
# ------------------------------------------------------------------------

class TestWriteEntries:
    def test_writes_person_and_junction_row(self, tmp_path):
        db_path = _seed_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        entries = [
            StaffEntry(2009, "Watanabe, Shinichirou", "Shinichirou Watanabe",
                       "shinichirou watanabe", "Director"),
        ]
        p_new, s_new = write_entries(conn, anime_id=1, entries=entries)
        assert p_new == 1 and s_new == 1

        row = conn.execute("SELECT name_display FROM people WHERE person_mal_id = 2009").fetchone()
        assert row[0] == "Shinichirou Watanabe"
        row = conn.execute(
            "SELECT role FROM anime_staff WHERE anime_id = 1 AND person_mal_id = 2009"
        ).fetchone()
        assert row[0] == "Director"
        conn.close()

    def test_dedupes_person_across_shows(self, tmp_path):
        """Watanabe as Director for Bebop AND Champloo → one people row,
        two anime_staff rows."""
        db_path = _seed_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        e_bebop = StaffEntry(2009, "Watanabe, Shinichirou", "Shinichirou Watanabe",
                             "shinichirou watanabe", "Director")
        e_champ = StaffEntry(2009, "Watanabe, Shinichirou", "Shinichirou Watanabe",
                             "shinichirou watanabe", "Director")

        write_entries(conn, anime_id=1, entries=[e_bebop])
        write_entries(conn, anime_id=2, entries=[e_champ])

        n_people = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
        n_staff = conn.execute("SELECT COUNT(*) FROM anime_staff").fetchone()[0]
        assert n_people == 1 and n_staff == 2
        conn.close()

    def test_rerun_same_show_is_noop(self, tmp_path):
        """Composite PK on (anime_id, person_mal_id, role) means a second
        write of the same entry produces no new rows."""
        db_path = _seed_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        entry = StaffEntry(2009, "Watanabe, Shinichirou", "Shinichirou Watanabe",
                           "shinichirou watanabe", "Director")

        p_new_1, s_new_1 = write_entries(conn, anime_id=1, entries=[entry])
        p_new_2, s_new_2 = write_entries(conn, anime_id=1, entries=[entry])
        assert (p_new_1, s_new_1) == (1, 1)
        assert (p_new_2, s_new_2) == (0, 0)
        conn.close()

    def test_multi_role_same_person_same_show(self, tmp_path):
        """A person with both Director + Series Composition on the same
        show gets one people row, two anime_staff rows."""
        db_path = _seed_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        entries = [
            StaffEntry(100, "Doe, Jane", "Jane Doe", "jane doe", "Director"),
            StaffEntry(100, "Doe, Jane", "Jane Doe", "jane doe", "Series Composition"),
        ]
        p_new, s_new = write_entries(conn, anime_id=1, entries=entries)
        assert p_new == 1 and s_new == 2
        conn.close()


# ------------------------------------------------------------------------
# enumerate_shows (--only-missing)
# ------------------------------------------------------------------------

class TestEnumerateShows:
    def test_returns_all_when_not_only_missing(self, tmp_path):
        db_path = _seed_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        shows = enumerate_shows(conn, only_missing=False)
        assert [s[0] for s in shows] == [1, 2, 3]
        conn.close()

    def test_only_missing_skips_already_processed(self, tmp_path):
        db_path = _seed_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        # Show #2 already has staff data
        entry = StaffEntry(999, "X, Y", "Y X", "y x", "Director")
        write_entries(conn, anime_id=2, entries=[entry])

        shows = enumerate_shows(conn, only_missing=True)
        assert [s[0] for s in shows] == [1, 3]
        conn.close()

    def test_only_missing_returns_empty_when_all_processed(self, tmp_path):
        db_path = _seed_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        entry = StaffEntry(999, "X, Y", "Y X", "y x", "Director")
        for anime_id in (1, 2, 3):
            write_entries(conn, anime_id=anime_id, entries=[entry])

        shows = enumerate_shows(conn, only_missing=True)
        assert shows == []
        conn.close()


# ------------------------------------------------------------------------
# check_tripwire (Phase D abort safeguard)
# ------------------------------------------------------------------------

class TestCheckTripwire:
    def test_no_abort_during_warmup(self):
        """Even a 100% failure rate must not fire before ABORT_WARMUP_SHOWS."""
        outcomes = ["error"] * (ABORT_WARMUP_SHOWS - 1)
        should_abort, _ = check_tripwire(outcomes, window=50, threshold=0.30)
        assert should_abort is False

    def test_no_abort_when_recent_healthy(self):
        outcomes = ["ok"] * 100
        should_abort, _ = check_tripwire(outcomes, window=50, threshold=0.30)
        assert should_abort is False

    def test_no_abort_at_threshold_boundary(self):
        """Exactly 30% bad rate does NOT fire (strict >)."""
        # 15 bad out of 50 = 30.0%
        outcomes = ["error"] * 15 + ["ok"] * 35
        # Pad with healthy entries to exit warmup, keeping window at last 50
        outcomes = ["ok"] * 50 + outcomes
        should_abort, _ = check_tripwire(outcomes, window=50, threshold=0.30)
        assert should_abort is False

    def test_abort_fires_on_high_error_rate(self):
        """Once past warmup, 40% errors in the sliding window fires."""
        outcomes = ["ok"] * 50 + ["error"] * 20 + ["ok"] * 30
        # Last-50 window: 20 error + 30 ok = 40% bad
        should_abort, diag = check_tripwire(outcomes, window=50, threshold=0.30)
        assert should_abort is True
        assert diag["recent_errors"] == 20
        assert diag["recent_empty"] == 0
        assert diag["combined_bad_rate"] == 0.4

    def test_abort_fires_on_high_empty_rate(self):
        """Spring-2026-Jikan-brownout fingerprint: mostly-empty responses.
        This is the exact failure mode the tripwire was designed for."""
        outcomes = ["ok"] * 50 + ["empty"] * 20 + ["ok"] * 30
        should_abort, diag = check_tripwire(outcomes, window=50, threshold=0.30)
        assert should_abort is True
        assert diag["recent_empty"] == 20

    def test_abort_fires_on_mixed_errors_and_empty(self):
        """Combines errors and empties into a single bad-rate signal."""
        outcomes = ["ok"] * 50 + ["error"] * 10 + ["empty"] * 10 + ["ok"] * 30
        # Last 50: 10 error + 10 empty + 30 ok = 40% bad
        should_abort, diag = check_tripwire(outcomes, window=50, threshold=0.30)
        assert should_abort is True
        assert diag["recent_errors"] == 10
        assert diag["recent_empty"] == 10

    def test_sliding_window_recovers_from_early_failures(self):
        """Once bad shows fall out of the window, the tripwire no longer
        fires — early transient issues shouldn't kill a run that recovered."""
        # 20 errors during warmup, then 100 healthy — last 50 all healthy
        outcomes = ["error"] * 20 + ["ok"] * 100
        should_abort, _ = check_tripwire(outcomes, window=50, threshold=0.30)
        assert should_abort is False

    def test_baseline_rate_does_not_fire(self):
        """Phase A empirical baseline: 1/240 errors + 6/240 empty = ~3%.
        Threshold 30% is 10× baseline — should never fire under healthy
        operation."""
        # Simulate 200 shows at Phase A baseline rate
        outcomes = ["ok"] * 185 + ["empty"] * 12 + ["error"] * 3
        should_abort, _ = check_tripwire(outcomes, window=50, threshold=0.30)
        # Random subwindow could vary, but the aggregate is healthy
        assert should_abort is False
