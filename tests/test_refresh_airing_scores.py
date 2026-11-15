"""Tests for tools/refresh_airing_scores.py.

Unit tests use a fake JikanClient + in-memory SQLite — fully offline,
no network calls. The pattern mirrors test_backfill_ratings.py: import
the core function (`run_refresh`) directly and pass a mock client.

Two subprocess tests cover CLI flag parsing (--test, --season) where
the entry-point behaviour is the thing under test.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "refresh_airing_scores.py"

from src.jikan_client import JikanFetchResult, ScoreSnapshot  # noqa: E402
from tools.refresh_airing_scores import run_refresh  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_vault(rows: list[tuple]) -> sqlite3.Connection:
    """In-memory SQLite with anime_info pre-seeded.

    Each row: (id, english_title, season, mal_score, scored_by, consensus_json)
    """
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE anime_info (
            id INTEGER PRIMARY KEY,
            english_title TEXT,
            season TEXT,
            mal_score REAL,
            scored_by INTEGER,
            consensus_json TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO anime_info VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return conn


def _fake_client(
    score_by_id: dict[int, tuple[float | None, int | None] | None],
) -> MagicMock:
    """Mock JikanClient.get_anime_score.

    score_by_id maps mal_id ->
        (score, scored_by)  — success result
        None                — Jikan error
    """
    def _lookup(mal_id):
        entry = score_by_id.get(mal_id)
        if entry is None:
            return JikanFetchResult(
                value=ScoreSnapshot(),
                error_kind="unexpected",
                error_detail="test-fake failure",
            )
        score, scored_by = entry
        return JikanFetchResult(
            value=ScoreSnapshot(score=score, scored_by=scored_by),
            status_code=200,
        )

    client = MagicMock()
    client.get_anime_score.side_effect = _lookup
    # run_refresh sets this; mock must accept attribute assignment
    client.rate_limit_delay = 0.0
    return client


# Standard 8-show vault:
#   4 × Fall_2026 (IDs 1-4), 2 × Summer_2026 (IDs 5-6),
#   2 × Spring_2026 (IDs 7-8), 1 × Winter_2024 (ID 99, old — not in cohort)
# ID 4 has consensus_json=NULL → ineligible (gate test)
_VAULT_ROWS = [
    (1, "Show One",   "Fall_2026",   7.10, 10000, "{}"),
    (2, "Show Two",   "Fall_2026",   7.20, 20000, "{}"),
    (3, "Show Three", "Fall_2026",   7.30, 30000, "{}"),
    (4, "No Intel",   "Fall_2026",   6.00,  5000, None),   # no consensus_json
    (5, "Show Five",  "Summer_2026", 7.50, 50000, "{}"),
    (6, "Show Six",   "Summer_2026", 7.60, 60000, "{}"),
    (7, "Show Seven", "Spring_2026", 8.00, 70000, "{}"),
    (8, "Show Eight", "Spring_2026", 8.10, 80000, "{}"),
    (99, "Old Show",  "Winter_2024", 6.50, 90000, "{}"),
]

_DEFAULT_SEASONS = ["Fall_2026", "Summer_2026"]

# Fresh Jikan scores for all eligible IDs.
_FRESH_SCORES: dict[int, tuple[float | None, int | None] | None] = {
    1: (7.15, 11000),
    2: (7.20, 21000),  # score unchanged, scored_by ticked up
    3: (7.35, 32000),
    5: (7.55, 52000),
    6: (7.65, 61000),
}


# ---------------------------------------------------------------------------
# TestCohortSelection
# ---------------------------------------------------------------------------

class TestCohortSelection:
    def test_only_target_seasons_fetched(self):
        conn = _make_vault(_VAULT_ROWS)
        client = _fake_client(_FRESH_SCORES)

        run_refresh(conn, client, _DEFAULT_SEASONS, dry_run=True, delay_ms=0)

        called_ids = {call.args[0] for call in client.get_anime_score.call_args_list}
        # Fall_2026 eligible: 1,2,3 (not 4 — no consensus_json)
        # Summer_2026 eligible: 5,6
        # Spring_2026 and Winter_2024 must NOT be called
        assert called_ids == {1, 2, 3, 5, 6}

    def test_no_consensus_json_excluded(self):
        conn = _make_vault(_VAULT_ROWS)
        client = _fake_client(_FRESH_SCORES)

        run_refresh(conn, client, ["Fall_2026"], dry_run=True, delay_ms=0)

        called_ids = {call.args[0] for call in client.get_anime_score.call_args_list}
        assert 4 not in called_ids, "Show with NULL consensus_json must be excluded"

    def test_explicit_single_season(self):
        conn = _make_vault(_VAULT_ROWS)
        client = _fake_client(_FRESH_SCORES)

        run_refresh(conn, client, ["Summer_2026"], dry_run=True, delay_ms=0)

        called_ids = {call.args[0] for call in client.get_anime_score.call_args_list}
        assert called_ids == {5, 6}


# ---------------------------------------------------------------------------
# TestScoreUpdates
# ---------------------------------------------------------------------------

class TestScoreUpdates:
    def test_score_and_scored_by_written_to_vault(self):
        conn = _make_vault(_VAULT_ROWS)
        client = _fake_client(_FRESH_SCORES)

        code = run_refresh(conn, client, _DEFAULT_SEASONS, dry_run=False, delay_ms=0)

        assert code == 0
        row = conn.execute(
            "SELECT mal_score, scored_by FROM anime_info WHERE id = 1"
        ).fetchone()
        assert row[0] == pytest.approx(7.15, abs=1e-6)
        assert row[1] == 11000

    def test_unchanged_score_still_overwrites(self):
        # Show 2: Jikan returns same score (7.20) but different scored_by (21000).
        # The tool writes unconditionally — no "skip if unchanged" optimisation.
        conn = _make_vault(_VAULT_ROWS)
        client = _fake_client(_FRESH_SCORES)

        run_refresh(conn, client, ["Fall_2026"], dry_run=False, delay_ms=0)

        row = conn.execute(
            "SELECT mal_score, scored_by FROM anime_info WHERE id = 2"
        ).fetchone()
        assert row[0] == pytest.approx(7.20, abs=1e-6)
        assert row[1] == 21000  # updated from 20000

    def test_unscored_show_writes_none(self):
        # Jikan returns score=None, scored_by=None for a pre-airing show.
        rows = [(10, "Pre-Air Show", "Fall_2026", None, None, "{}")]
        conn = _make_vault(rows)
        client = _fake_client({10: (None, None)})

        run_refresh(conn, client, ["Fall_2026"], dry_run=False, delay_ms=0)

        row = conn.execute(
            "SELECT mal_score, scored_by FROM anime_info WHERE id = 10"
        ).fetchone()
        assert row[0] is None
        assert row[1] is None

    def test_multiple_shows_all_updated(self):
        conn = _make_vault(_VAULT_ROWS)
        client = _fake_client(_FRESH_SCORES)

        run_refresh(conn, client, _DEFAULT_SEASONS, dry_run=False, delay_ms=0)

        for mal_id, (expected_score, expected_voted) in _FRESH_SCORES.items():
            row = conn.execute(
                "SELECT mal_score, scored_by FROM anime_info WHERE id = ?",
                (mal_id,),
            ).fetchone()
            assert row[0] == pytest.approx(expected_score, abs=1e-6), f"MAL {mal_id} score"
            assert row[1] == expected_voted, f"MAL {mal_id} scored_by"


# ---------------------------------------------------------------------------
# TestDryRun
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_calls_jikan_but_writes_nothing(self):
        conn = _make_vault(_VAULT_ROWS)
        client = _fake_client(_FRESH_SCORES)

        run_refresh(conn, client, _DEFAULT_SEASONS, dry_run=True, delay_ms=0)

        # Jikan was called
        assert client.get_anime_score.call_count > 0

        # DB rows are unchanged
        rows = conn.execute(
            "SELECT id, mal_score, scored_by FROM anime_info "
            "WHERE season IN ('Fall_2026', 'Summer_2026') AND consensus_json IS NOT NULL "
            "ORDER BY id"
        ).fetchall()
        original = {r[0]: (r[1], r[2]) for r in rows}
        expected = {
            1: (7.10, 10000),
            2: (7.20, 20000),
            3: (7.30, 30000),
            5: (7.50, 50000),
            6: (7.60, 60000),
        }
        assert original == expected


# ---------------------------------------------------------------------------
# TestErrorHandling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_one_jikan_error_does_not_abort_loop(self):
        # Show 2 errors; shows 1 and 3 should still be updated.
        scores = {
            1: (7.15, 11000),
            2: None,          # error
            3: (7.35, 32000),
        }
        conn = _make_vault(_VAULT_ROWS)
        client = _fake_client(scores)

        code = run_refresh(conn, client, ["Fall_2026"], dry_run=False, delay_ms=0)

        # Exit code 1 because of the error
        assert code == 1

        # Show 1 updated
        row1 = conn.execute(
            "SELECT mal_score FROM anime_info WHERE id = 1"
        ).fetchone()
        assert row1[0] == pytest.approx(7.15, abs=1e-6)

        # Show 2 unchanged (error — no write)
        row2 = conn.execute(
            "SELECT mal_score FROM anime_info WHERE id = 2"
        ).fetchone()
        assert row2[0] == pytest.approx(7.20, abs=1e-6)

        # Show 3 updated
        row3 = conn.execute(
            "SELECT mal_score FROM anime_info WHERE id = 3"
        ).fetchone()
        assert row3[0] == pytest.approx(7.35, abs=1e-6)

    def test_all_jikan_errors_returns_exit_1(self):
        scores = {1: None, 2: None, 3: None}
        conn = _make_vault(_VAULT_ROWS)
        client = _fake_client(scores)

        code = run_refresh(conn, client, ["Fall_2026"], dry_run=False, delay_ms=0)
        assert code == 1

    def test_all_errors_leaves_db_unchanged(self):
        scores = {1: None, 2: None, 3: None}
        conn = _make_vault(_VAULT_ROWS)
        client = _fake_client(scores)

        run_refresh(conn, client, ["Fall_2026"], dry_run=False, delay_ms=0)

        row = conn.execute(
            "SELECT mal_score FROM anime_info WHERE id = 1"
        ).fetchone()
        assert row[0] == pytest.approx(7.10, abs=1e-6)


# ---------------------------------------------------------------------------
# TestExitCodes
# ---------------------------------------------------------------------------

class TestExitCodes:
    def test_zero_matching_shows_returns_exit_2(self):
        conn = _make_vault(_VAULT_ROWS)
        client = _fake_client({})

        code = run_refresh(conn, client, ["Fall_2099"], dry_run=False, delay_ms=0)
        assert code == 2
        # Jikan should not have been called at all
        client.get_anime_score.assert_not_called()

    def test_all_success_returns_exit_0(self):
        conn = _make_vault(_VAULT_ROWS)
        client = _fake_client(_FRESH_SCORES)

        code = run_refresh(conn, client, _DEFAULT_SEASONS, dry_run=False, delay_ms=0)
        assert code == 0

    def test_partial_errors_returns_exit_1(self):
        # One success, one error
        scores = {1: (7.15, 11000), 2: None, 3: (7.35, 32000)}
        conn = _make_vault(_VAULT_ROWS)
        client = _fake_client(scores)

        code = run_refresh(conn, client, ["Fall_2026"], dry_run=False, delay_ms=0)
        assert code == 1

    def test_dry_run_all_success_returns_exit_0(self):
        conn = _make_vault(_VAULT_ROWS)
        client = _fake_client(_FRESH_SCORES)

        code = run_refresh(conn, client, _DEFAULT_SEASONS, dry_run=True, delay_ms=0)
        assert code == 0


# ---------------------------------------------------------------------------
# TestLimit
# ---------------------------------------------------------------------------

class TestLimit:
    def test_limit_caps_jikan_calls(self):
        conn = _make_vault(_VAULT_ROWS)
        client = _fake_client(_FRESH_SCORES)

        run_refresh(conn, client, _DEFAULT_SEASONS, dry_run=True, limit=2, delay_ms=0)

        assert client.get_anime_score.call_count == 2

    def test_limit_with_writes(self):
        # limit=2 with dry_run=False → only first 2 shows written
        conn = _make_vault(_VAULT_ROWS)
        client = _fake_client(_FRESH_SCORES)

        run_refresh(conn, client, ["Fall_2026"], dry_run=False, limit=2, delay_ms=0)

        # Shows 1 and 2 updated (first 2 by id order)
        row1 = conn.execute("SELECT mal_score FROM anime_info WHERE id = 1").fetchone()
        assert row1[0] == pytest.approx(7.15, abs=1e-6)

        # Show 3 not touched
        row3 = conn.execute("SELECT mal_score FROM anime_info WHERE id = 3").fetchone()
        assert row3[0] == pytest.approx(7.30, abs=1e-6)


# ---------------------------------------------------------------------------
# TestCLI  (subprocess — CLI flag parsing)
# ---------------------------------------------------------------------------

def _run_tool(args: list[str], db_path: Path) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(TOOL), "--db", str(db_path), *args]
    return subprocess.run(cmd, capture_output=True, text=True,
                          env={**__import__("os").environ, "JIKAN_BASE_URL": "http://fake-jikan.test"})


class TestCLI:
    def test_no_matching_season_exits_2(self, tmp_path):
        db = tmp_path / "vault.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE anime_info "
            "(id INTEGER PRIMARY KEY, english_title TEXT, season TEXT, "
            "mal_score REAL, scored_by INTEGER, consensus_json TEXT)"
        )
        conn.commit()
        conn.close()

        result = _run_tool(["--season", "Fall_2099", "--dry-run"], db)
        assert result.returncode == 2

    def test_test_flag_implies_dry_run(self, tmp_path):
        """--test must not write to DB even without an explicit --dry-run flag.

        We seed the DB with one Fall_2026 show, run with --test, and verify
        the row is unchanged. The tool will attempt to contact Jikan but
        since JIKAN_BASE_URL points to a fake host it will error on the
        network call. The test checks exit code 1 (errors tolerated) and
        confirms no DB write happened.
        """
        db = tmp_path / "vault.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE anime_info "
            "(id INTEGER PRIMARY KEY, english_title TEXT, season TEXT, "
            "mal_score REAL, scored_by INTEGER, consensus_json TEXT)"
        )
        conn.execute(
            "INSERT INTO anime_info VALUES (1, 'Test Show', 'Fall_2026', 7.0, 1000, '{}')"
        )
        conn.commit()
        conn.close()

        result = _run_tool(["--season", "Fall_2026", "--test"], db)
        # Exit 1 expected: Jikan call fails (fake host), but --test implies dry_run
        # so no write should happen regardless
        assert result.returncode in (1, 2)

        conn2 = sqlite3.connect(str(db))
        row = conn2.execute(
            "SELECT mal_score FROM anime_info WHERE id = 1"
        ).fetchone()
        conn2.close()
        assert row[0] == pytest.approx(7.0, abs=1e-6), (
            "--test must imply dry_run; DB row must be unchanged"
        )
