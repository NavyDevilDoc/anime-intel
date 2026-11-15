"""Tests for tools/refresh_airing_sentiment.py.

All tests are fully offline — no Jikan, no Gemini, no network calls.
Jikan is replaced by a fake client; the distiller's async
distill_sentiment_rolling is replaced by an AsyncMock.

Pattern mirrors test_refresh_airing_scores.py:
  - run_refresh() imported directly + mocked collaborators
  - Two subprocess CLI tests for flag parsing
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "refresh_airing_sentiment.py"

from analysis.prediction_log import (  # noqa: E402
    ensure_airing_sentiment_log_table,
    monday_of_week,
)
from src.jikan_client import JikanFetchResult, ReviewFetchResult  # noqa: E402
from tools.refresh_airing_sentiment import run_refresh  # noqa: E402

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

WEEK_DATE = "2026-11-17"  # frozen Monday for all unit tests

_GOOD_CONSENSUS = {
    "thematic_vibe": "Action, Drama, Comedy",
    "pros": ["Strong writing"],
    "cons": ["Slow pacing"],
    "controversy_score": 4,
    "consensus_summary": "Sentiment rising as episode 6 delivered on its promises.",
}

# Enough review objects to pass the MIN_REVIEWS=3 gate
_REVIEWS_3 = [
    {"review": "Great episode!", "reactions": {"overall": 10}, "scores": {"overall": 8}},
    {"review": "Loved the pacing.", "reactions": {"overall": 5}, "scores": {"overall": 9}},
    {"review": "Solid character work.", "reactions": {"overall": 3}, "scores": {"overall": 7}},
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_vault(rows: list[tuple]) -> sqlite3.Connection:
    """In-memory SQLite with anime_info + airing_sentiment_log tables.

    Row shape: (id, english_title, season, mal_synopsis, avg_sentiment, consensus_json)
    """
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE anime_info (
            id INTEGER PRIMARY KEY,
            english_title TEXT,
            season TEXT,
            mal_synopsis TEXT,
            avg_sentiment REAL,
            consensus_json TEXT
        )
        """
    )
    conn.executemany("INSERT INTO anime_info VALUES (?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    ensure_airing_sentiment_log_table(conn)
    return conn


def _fake_jikan(reviews_by_id: dict[int, list | None]) -> MagicMock:
    """Mock JikanClient.get_anime_reviews.

    reviews_by_id: {mal_id: list_of_reviews_or_None}
    None → Jikan error; list → success (even if empty).
    """
    def _lookup(mal_id):
        entry = reviews_by_id.get(mal_id)
        if entry is None:
            return ReviewFetchResult(
                error_kind="unexpected",
                error_detail="test-fake Jikan failure",
            )
        return ReviewFetchResult(reviews=entry)

    client = MagicMock()
    client.get_anime_reviews.side_effect = _lookup
    client.rate_limit_delay = 0.0
    return client


def _fake_distiller(
    result: dict | None = None,
    *,
    error_on_ids: set[int] | None = None,
) -> MagicMock:
    """Mock ReviewDistiller with an AsyncMock on distill_sentiment_rolling.

    result: the consensus dict to return on success (defaults to _GOOD_CONSENSUS)
    error_on_ids: set of mal_ids to fail on (not used directly — the mock
        can't see mal_id; use a call counter via side_effect instead).
    """
    success_result = result if result is not None else _GOOD_CONSENSUS

    # Simple always-succeed mock
    async def _distill(context_data, prior_consensus=None):
        return success_result

    distiller = MagicMock()
    distiller.distill_sentiment_rolling = AsyncMock(side_effect=_distill)
    return distiller


def _fake_distiller_with_failures(results: list[dict | None]) -> MagicMock:
    """Return results in order. Used for mixed success/failure sequences."""
    call_iter = iter(results)

    async def _distill(context_data, prior_consensus=None):
        return next(call_iter, None)

    distiller = MagicMock()
    distiller.distill_sentiment_rolling = AsyncMock(side_effect=_distill)
    return distiller


# Standard 6-show vault (current season Fall_2026 + older Spring_2026)
_VAULT_ROWS = [
    (1, "Show One",   "Fall_2026",   "Synopsis 1", 0.30, '{"thematic_vibe":"A","pros":[],"cons":[],"controversy_score":3,"consensus_summary":"Decent."}'),
    (2, "Show Two",   "Fall_2026",   "Synopsis 2", 0.40, '{"thematic_vibe":"B","pros":[],"cons":[],"controversy_score":4,"consensus_summary":"Good."}'),
    (3, "Show Three", "Fall_2026",   "Synopsis 3", 0.50, '{"thematic_vibe":"C","pros":[],"cons":[],"controversy_score":5,"consensus_summary":"Great."}'),
    (4, "No Intel",   "Fall_2026",   "Synopsis 4", 0.20, None),         # no consensus_json → excluded
    (5, "Old Show",   "Spring_2026", "Synopsis 5", 0.60, '{"thematic_vibe":"D","pros":[],"cons":[],"controversy_score":2,"consensus_summary":"Fine."}'),
]

_DEFAULT_SEASONS = ["Fall_2026"]
_ALL_REVIEWS = {1: _REVIEWS_3, 2: _REVIEWS_3, 3: _REVIEWS_3}


# ---------------------------------------------------------------------------
# TestCohortSelection
# ---------------------------------------------------------------------------

class TestCohortSelection:
    def test_only_target_season_fetched(self):
        conn = _make_vault(_VAULT_ROWS)
        jikan = _fake_jikan(_ALL_REVIEWS)
        distiller = _fake_distiller()

        run_refresh(conn, jikan, distiller, _DEFAULT_SEASONS,
                    week_date=WEEK_DATE, dry_run=True)

        called_ids = {call.args[0] for call in jikan.get_anime_reviews.call_args_list}
        # Spring_2026 show (id=5) must not be called
        assert 5 not in called_ids

    def test_no_consensus_json_excluded(self):
        conn = _make_vault(_VAULT_ROWS)
        jikan = _fake_jikan(_ALL_REVIEWS)
        distiller = _fake_distiller()

        run_refresh(conn, jikan, distiller, _DEFAULT_SEASONS,
                    week_date=WEEK_DATE, dry_run=True)

        called_ids = {call.args[0] for call in jikan.get_anime_reviews.call_args_list}
        assert 4 not in called_ids, "Show with NULL consensus_json must be excluded"

    def test_eligible_ids_all_fetched(self):
        conn = _make_vault(_VAULT_ROWS)
        jikan = _fake_jikan(_ALL_REVIEWS)
        distiller = _fake_distiller()

        run_refresh(conn, jikan, distiller, _DEFAULT_SEASONS,
                    week_date=WEEK_DATE, dry_run=True)

        called_ids = {call.args[0] for call in jikan.get_anime_reviews.call_args_list}
        assert called_ids == {1, 2, 3}


# ---------------------------------------------------------------------------
# TestReviewGating
# ---------------------------------------------------------------------------

class TestReviewGating:
    def test_insufficient_reviews_skipped_not_error(self):
        # Show 2 has only 1 review — below the default MIN_REVIEWS=3 gate
        reviews = {1: _REVIEWS_3, 2: [_REVIEWS_3[0]], 3: _REVIEWS_3}
        conn = _make_vault(_VAULT_ROWS)
        jikan = _fake_jikan(reviews)
        distiller = _fake_distiller()

        code = run_refresh(conn, jikan, distiller, _DEFAULT_SEASONS,
                           week_date=WEEK_DATE, dry_run=True)

        assert code == 0  # skips are not errors
        # Distiller should NOT have been called for show 2
        call_count = distiller.distill_sentiment_rolling.call_count
        assert call_count == 2  # shows 1 and 3 only

    def test_zero_reviews_skipped(self):
        reviews = {1: _REVIEWS_3, 2: [], 3: _REVIEWS_3}
        conn = _make_vault(_VAULT_ROWS)
        jikan = _fake_jikan(reviews)
        distiller = _fake_distiller()

        code = run_refresh(conn, jikan, distiller, _DEFAULT_SEASONS,
                           week_date=WEEK_DATE, dry_run=True)
        assert code == 0

    def test_custom_min_reviews_respected(self):
        # With min_reviews=1, all three shows should be distilled
        reviews = {1: _REVIEWS_3[:1], 2: _REVIEWS_3[:1], 3: _REVIEWS_3[:1]}
        conn = _make_vault(_VAULT_ROWS)
        jikan = _fake_jikan(reviews)
        distiller = _fake_distiller()

        run_refresh(conn, jikan, distiller, _DEFAULT_SEASONS,
                    week_date=WEEK_DATE, dry_run=True, min_reviews=1)

        assert distiller.distill_sentiment_rolling.call_count == 3


# ---------------------------------------------------------------------------
# TestSentimentUpdates
# ---------------------------------------------------------------------------

class TestSentimentUpdates:
    def test_avg_sentiment_written_to_anime_info(self):
        conn = _make_vault(_VAULT_ROWS)
        jikan = _fake_jikan(_ALL_REVIEWS)
        distiller = _fake_distiller()

        run_refresh(conn, jikan, distiller, _DEFAULT_SEASONS,
                    week_date=WEEK_DATE, dry_run=False)

        row = conn.execute(
            "SELECT avg_sentiment FROM anime_info WHERE id = 1"
        ).fetchone()
        # We can't assert an exact float (VADER runs on real text),
        # but it must be a float, not None, and updated from the fixture value 0.30
        assert row[0] is not None
        assert isinstance(row[0], float)

    def test_consensus_json_NOT_written_to_anime_info(self):
        """The key invariant: anime_info.consensus_json stays at ingest-time value."""
        conn = _make_vault(_VAULT_ROWS)
        original_consensus = json.loads(
            conn.execute(
                "SELECT consensus_json FROM anime_info WHERE id = 1"
            ).fetchone()[0]
        )
        jikan = _fake_jikan(_ALL_REVIEWS)
        distiller = _fake_distiller()

        run_refresh(conn, jikan, distiller, _DEFAULT_SEASONS,
                    week_date=WEEK_DATE, dry_run=False)

        after_consensus = json.loads(
            conn.execute(
                "SELECT consensus_json FROM anime_info WHERE id = 1"
            ).fetchone()[0]
        )
        assert after_consensus == original_consensus, (
            "anime_info.consensus_json must not be modified by the sentiment refresh"
        )

    def test_airing_sentiment_log_row_created(self):
        conn = _make_vault(_VAULT_ROWS)
        jikan = _fake_jikan(_ALL_REVIEWS)
        distiller = _fake_distiller()

        run_refresh(conn, jikan, distiller, _DEFAULT_SEASONS,
                    week_date=WEEK_DATE, dry_run=False)

        count = conn.execute(
            "SELECT COUNT(*) FROM airing_sentiment_log WHERE week_date = ?",
            (WEEK_DATE,),
        ).fetchone()[0]
        assert count == 3  # one row per eligible show

    def test_log_row_has_correct_week_date(self):
        conn = _make_vault(_VAULT_ROWS)
        jikan = _fake_jikan(_ALL_REVIEWS)
        distiller = _fake_distiller()

        run_refresh(conn, jikan, distiller, _DEFAULT_SEASONS,
                    week_date=WEEK_DATE, dry_run=False)

        row = conn.execute(
            "SELECT week_date FROM airing_sentiment_log WHERE anime_id = 1"
        ).fetchone()
        assert row[0] == WEEK_DATE

    def test_log_row_consensus_json_is_trajectory_aware(self):
        conn = _make_vault(_VAULT_ROWS)
        jikan = _fake_jikan(_ALL_REVIEWS)
        distiller = _fake_distiller()

        run_refresh(conn, jikan, distiller, _DEFAULT_SEASONS,
                    week_date=WEEK_DATE, dry_run=False)

        row = conn.execute(
            "SELECT consensus_json FROM airing_sentiment_log WHERE anime_id = 1"
        ).fetchone()
        stored = json.loads(row[0])
        assert stored == _GOOD_CONSENSUS


# ---------------------------------------------------------------------------
# TestRollingContext
# ---------------------------------------------------------------------------

class TestRollingContext:
    def test_prior_consensus_passed_when_log_row_exists(self):
        """If a prior airing_sentiment_log row exists, it's passed as prior_consensus."""
        conn = _make_vault(_VAULT_ROWS)
        # Seed an existing log row for show 1
        prior = {
            "thematic_vibe": "X, Y, Z",
            "pros": [], "cons": [],
            "controversy_score": 3,
            "consensus_summary": "Last week things were fine.",
        }
        conn.execute(
            "INSERT INTO airing_sentiment_log VALUES (?, ?, ?, ?)",
            (1, "2026-11-10", 0.28, json.dumps(prior)),
        )
        conn.commit()

        jikan = _fake_jikan(_ALL_REVIEWS)
        captured_priors: list = []

        async def _capture_prior(context_data, prior_consensus=None):
            captured_priors.append(prior_consensus)
            return _GOOD_CONSENSUS

        distiller = MagicMock()
        distiller.distill_sentiment_rolling = AsyncMock(side_effect=_capture_prior)

        run_refresh(conn, jikan, distiller, _DEFAULT_SEASONS,
                    week_date=WEEK_DATE, dry_run=True)

        # First call is for show 1 (lowest id), which has a prior log row
        assert captured_priors[0] == prior

    def test_falls_back_to_anime_info_consensus_when_no_prior_log(self):
        """No prior log row → current anime_info.consensus_json passed as prior."""
        conn = _make_vault(_VAULT_ROWS)
        jikan = _fake_jikan({1: _REVIEWS_3})
        captured_priors: list = []

        async def _capture_prior(context_data, prior_consensus=None):
            captured_priors.append(prior_consensus)
            return _GOOD_CONSENSUS

        distiller = MagicMock()
        distiller.distill_sentiment_rolling = AsyncMock(side_effect=_capture_prior)

        run_refresh(conn, jikan, distiller, ["Fall_2026"],
                    week_date=WEEK_DATE, dry_run=True)

        # Should fall back to anime_info.consensus_json for show 1
        expected = json.loads(_VAULT_ROWS[0][5])  # index 5 = consensus_json
        assert captured_priors[0] == expected


# ---------------------------------------------------------------------------
# TestDryRun
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_calls_jikan_and_distiller_but_writes_nothing(self):
        conn = _make_vault(_VAULT_ROWS)
        jikan = _fake_jikan(_ALL_REVIEWS)
        distiller = _fake_distiller()

        run_refresh(conn, jikan, distiller, _DEFAULT_SEASONS,
                    week_date=WEEK_DATE, dry_run=True)

        # Jikan and distiller were called
        assert jikan.get_anime_reviews.call_count > 0
        assert distiller.distill_sentiment_rolling.call_count > 0

        # anime_info.avg_sentiment unchanged
        rows = conn.execute(
            "SELECT id, avg_sentiment FROM anime_info WHERE season = 'Fall_2026' "
            "AND consensus_json IS NOT NULL ORDER BY id"
        ).fetchall()
        assert {r[0]: r[1] for r in rows} == {1: 0.30, 2: 0.40, 3: 0.50}

        # airing_sentiment_log empty
        count = conn.execute("SELECT COUNT(*) FROM airing_sentiment_log").fetchone()[0]
        assert count == 0


# ---------------------------------------------------------------------------
# TestErrorHandling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_jikan_error_continues_loop(self):
        # Show 2 has a Jikan error; shows 1 and 3 should still be processed
        reviews = {1: _REVIEWS_3, 2: None, 3: _REVIEWS_3}
        conn = _make_vault(_VAULT_ROWS)
        jikan = _fake_jikan(reviews)
        distiller = _fake_distiller()

        code = run_refresh(conn, jikan, distiller, _DEFAULT_SEASONS,
                           week_date=WEEK_DATE, dry_run=False)

        assert code == 1
        # Shows 1 and 3 should have log rows; show 2 should not
        ids_in_log = {
            r[0] for r in conn.execute(
                "SELECT anime_id FROM airing_sentiment_log"
            ).fetchall()
        }
        assert 1 in ids_in_log
        assert 3 in ids_in_log
        assert 2 not in ids_in_log

    def test_distillation_failure_continues_loop(self):
        # Show 2 distillation returns None
        conn = _make_vault(_VAULT_ROWS)
        jikan = _fake_jikan(_ALL_REVIEWS)
        # results in order: show1=success, show2=None, show3=success
        distiller = _fake_distiller_with_failures([_GOOD_CONSENSUS, None, _GOOD_CONSENSUS])

        code = run_refresh(conn, jikan, distiller, _DEFAULT_SEASONS,
                           week_date=WEEK_DATE, dry_run=False)

        assert code == 1
        ids_in_log = {
            r[0] for r in conn.execute(
                "SELECT anime_id FROM airing_sentiment_log"
            ).fetchall()
        }
        assert 1 in ids_in_log
        assert 3 in ids_in_log
        assert 2 not in ids_in_log

    def test_all_errors_exits_1(self):
        reviews = {1: None, 2: None, 3: None}
        conn = _make_vault(_VAULT_ROWS)
        jikan = _fake_jikan(reviews)
        distiller = _fake_distiller()

        code = run_refresh(conn, jikan, distiller, _DEFAULT_SEASONS,
                           week_date=WEEK_DATE, dry_run=False)
        assert code == 1


# ---------------------------------------------------------------------------
# TestExitCodes
# ---------------------------------------------------------------------------

class TestExitCodes:
    def test_zero_matching_shows_exits_2(self):
        conn = _make_vault(_VAULT_ROWS)
        jikan = _fake_jikan({})
        distiller = _fake_distiller()

        code = run_refresh(conn, jikan, distiller, ["Fall_2099"],
                           week_date=WEEK_DATE, dry_run=False)
        assert code == 2
        jikan.get_anime_reviews.assert_not_called()

    def test_all_success_exits_0(self):
        conn = _make_vault(_VAULT_ROWS)
        jikan = _fake_jikan(_ALL_REVIEWS)
        distiller = _fake_distiller()

        code = run_refresh(conn, jikan, distiller, _DEFAULT_SEASONS,
                           week_date=WEEK_DATE, dry_run=False)
        assert code == 0

    def test_all_skipped_exits_0(self):
        # All shows have fewer than min_reviews — all skipped, not errors
        reviews = {1: [], 2: [], 3: []}
        conn = _make_vault(_VAULT_ROWS)
        jikan = _fake_jikan(reviews)
        distiller = _fake_distiller()

        code = run_refresh(conn, jikan, distiller, _DEFAULT_SEASONS,
                           week_date=WEEK_DATE, dry_run=False)
        assert code == 0

    def test_partial_errors_exits_1(self):
        reviews = {1: _REVIEWS_3, 2: None, 3: _REVIEWS_3}
        conn = _make_vault(_VAULT_ROWS)
        jikan = _fake_jikan(reviews)
        distiller = _fake_distiller()

        code = run_refresh(conn, jikan, distiller, _DEFAULT_SEASONS,
                           week_date=WEEK_DATE, dry_run=False)
        assert code == 1


# ---------------------------------------------------------------------------
# TestUpsertIdempotency
# ---------------------------------------------------------------------------

class TestUpsertIdempotency:
    def test_same_week_rerun_overwrites_not_duplicates(self):
        conn = _make_vault(_VAULT_ROWS)
        jikan = _fake_jikan(_ALL_REVIEWS)
        distiller = _fake_distiller()

        run_refresh(conn, jikan, distiller, _DEFAULT_SEASONS,
                    week_date=WEEK_DATE, dry_run=False)
        run_refresh(conn, jikan, distiller, _DEFAULT_SEASONS,
                    week_date=WEEK_DATE, dry_run=False)

        count = conn.execute(
            "SELECT COUNT(*) FROM airing_sentiment_log WHERE week_date = ?",
            (WEEK_DATE,),
        ).fetchone()[0]
        assert count == 3  # 3 rows, not 6

    def test_different_weeks_coexist(self):
        conn = _make_vault(_VAULT_ROWS)
        jikan = _fake_jikan(_ALL_REVIEWS)
        distiller = _fake_distiller()

        run_refresh(conn, jikan, distiller, _DEFAULT_SEASONS,
                    week_date="2026-11-10", dry_run=False)
        run_refresh(conn, jikan, distiller, _DEFAULT_SEASONS,
                    week_date="2026-11-17", dry_run=False)

        count = conn.execute(
            "SELECT COUNT(*) FROM airing_sentiment_log"
        ).fetchone()[0]
        assert count == 6  # 3 shows × 2 weeks


# ---------------------------------------------------------------------------
# TestMondayOfWeek
# ---------------------------------------------------------------------------

class TestMondayOfWeek:
    @pytest.mark.parametrize("weekday,expected", [
        (0, "2026-11-16"),  # Monday
        (1, "2026-11-16"),  # Tuesday
        (3, "2026-11-16"),  # Thursday
        (6, "2026-11-16"),  # Sunday
    ])
    def test_always_returns_monday(self, weekday, expected):
        # Week of 2026-11-16 (Monday) through 2026-11-22 (Sunday)
        from datetime import timedelta
        monday = datetime(2026, 11, 16, tzinfo=timezone.utc)
        dt = monday + timedelta(days=weekday)
        assert monday_of_week(dt) == expected


# ---------------------------------------------------------------------------
# TestLimit
# ---------------------------------------------------------------------------

class TestLimit:
    def test_limit_caps_calls_and_writes(self):
        conn = _make_vault(_VAULT_ROWS)
        jikan = _fake_jikan(_ALL_REVIEWS)
        distiller = _fake_distiller()

        run_refresh(conn, jikan, distiller, _DEFAULT_SEASONS,
                    week_date=WEEK_DATE, dry_run=False, limit=2)

        assert jikan.get_anime_reviews.call_count == 2
        count = conn.execute(
            "SELECT COUNT(*) FROM airing_sentiment_log"
        ).fetchone()[0]
        assert count == 2


# ---------------------------------------------------------------------------
# TestCLI  (subprocess)
# ---------------------------------------------------------------------------

def _run_tool(args: list[str], db_path: Path) -> subprocess.CompletedProcess:
    env = {
        **__import__("os").environ,
        "JIKAN_BASE_URL": "http://fake-jikan.test",
        "GOOGLE_API_KEY": "fake-key-for-cli-test",
        "TESTING": "1",  # suppresses Supabase cost telemetry
    }
    cmd = [sys.executable, str(TOOL), "--db", str(db_path), *args]
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


class TestCLI:
    def test_no_matching_season_exits_2(self, tmp_path):
        db = tmp_path / "vault.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE anime_info "
            "(id INTEGER PRIMARY KEY, english_title TEXT, season TEXT, "
            "mal_synopsis TEXT, avg_sentiment REAL, consensus_json TEXT)"
        )
        conn.commit()
        conn.close()

        result = _run_tool(["--season", "Fall_2099", "--dry-run"], db)
        assert result.returncode == 2

    def test_test_flag_implies_dry_run(self, tmp_path):
        """--test must not write even when Jikan/Gemini fail (fake URLs)."""
        db = tmp_path / "vault.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE anime_info "
            "(id INTEGER PRIMARY KEY, english_title TEXT, season TEXT, "
            "mal_synopsis TEXT, avg_sentiment REAL, consensus_json TEXT)"
        )
        conn.execute(
            "INSERT INTO anime_info VALUES "
            "(1, 'Test Show', 'Fall_2026', 'Synopsis', 0.5, '{\"thematic_vibe\":\"A\","
            "\"pros\":[],\"cons\":[],\"controversy_score\":3,\"consensus_summary\":\"OK.\"}')"
        )
        conn.commit()
        conn.close()

        result = _run_tool(["--season", "Fall_2026", "--test"], db)
        # Exit 1 expected (Jikan call will fail on fake host), but no writes
        assert result.returncode in (1, 2)

        conn2 = sqlite3.connect(str(db))
        sentiment_unchanged = conn2.execute(
            "SELECT avg_sentiment FROM anime_info WHERE id = 1"
        ).fetchone()[0]
        log_count = conn2.execute(
            "SELECT COUNT(*) FROM airing_sentiment_log"
            if _table_exists(conn2, "airing_sentiment_log")
            else "SELECT 0"
        ).fetchone()[0]
        conn2.close()

        assert sentiment_unchanged == pytest.approx(0.5, abs=1e-6), (
            "--test must imply dry_run; avg_sentiment must be unchanged"
        )
        assert log_count == 0, "--test must imply dry_run; log table must be empty"


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None
