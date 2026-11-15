"""
Unit tests for tools/backfill_ratings.py.

Validates the dual-mode contract:
  - --initial mode skips rows that already have a rating (idempotency)
  - --refresh mode is a no-op when the live Jikan value matches stored
  - --refresh mode updates the row AND appends to data/rating_drift_log.csv
    when Jikan returns a different value (drift case)

Tests use an in-memory SQLite DB and a fake JikanClient so they run
fully offline without hitting the real API.
"""

import csv
import sqlite3
from unittest.mock import MagicMock

from src.jikan_client import JikanFetchResult
from tools.backfill_ratings import (
    DRIFT_LOG_HEADER,
    run_initial,
    run_refresh,
)


def _make_conn(rows):
    """In-memory SQLite seeded with anime_info rows.

    `rows` is a list of (id, english_title, mal_rating) tuples.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE anime_info ("
        " id INTEGER PRIMARY KEY,"
        " english_title TEXT,"
        " mal_rating TEXT"
        ")"
    )
    conn.executemany(
        "INSERT INTO anime_info (id, english_title, mal_rating) VALUES (?, ?, ?)",
        rows,
    )
    conn.commit()
    return conn


def _fake_client(rating_by_id):
    """Mock JikanClient. rating_by_id is {mal_id: rating_string_or_None}.

    IDs mapped to None produce a JikanFetchResult with is_error=True
    (mimics HTTP failure); mapped strings produce success results.
    """
    def _lookup(mid):
        rating = rating_by_id.get(mid)
        if rating is None:
            return JikanFetchResult(value="", error_kind="unexpected", error_detail="test-fake failure")
        return JikanFetchResult(value=rating, status_code=200)
    client = MagicMock()
    client.get_anime_rating.side_effect = _lookup
    return client


class TestInitialMode:
    """--initial mode targets NULL rows only and skips populated ones."""

    def test_skips_populated_rows(self):
        conn = _make_conn([
            (1, "Already Rated Show", "PG-13 - Teens 13 or older"),
            (2, "Needs Rating", None),
        ])
        # Fake client only knows about ID 2 — if initial mode incorrectly
        # processes ID 1, the lambda returns None and the test would still
        # pass deceptively. Spy on the call set instead.
        client = _fake_client({2: "R - 17+ (violence & profanity)"})

        run_initial(conn, test_mode=False, client=client)

        # Verify only ID 2 was touched
        called_ids = [call.args[0] for call in client.get_anime_rating.call_args_list]
        assert called_ids == [2], f"Initial mode should only fetch NULL rows, got {called_ids}"

        # Verify ID 1's rating is unchanged
        row1 = conn.execute("SELECT mal_rating FROM anime_info WHERE id = 1").fetchone()
        assert row1[0] == "PG-13 - Teens 13 or older"

        # Verify ID 2 got populated
        row2 = conn.execute("SELECT mal_rating FROM anime_info WHERE id = 2").fetchone()
        assert row2[0] == "R - 17+ (violence & profanity)"


class TestRefreshMode:
    """--refresh mode re-checks every row, updates + logs only on diff."""

    def test_no_diff_means_no_update_and_no_csv(self, tmp_path):
        """Stored rating matches live Jikan -> zero-op (no UPDATE, no CSV)."""
        conn = _make_conn([
            (1, "Attack on Titan", "R - 17+ (violence & profanity)"),
        ])
        client = _fake_client({1: "R - 17+ (violence & profanity)"})
        log_path = tmp_path / "drift.csv"

        run_refresh(conn, test_mode=False, client=client, log_path=str(log_path))

        # Row still has the same rating
        row = conn.execute("SELECT mal_rating FROM anime_info WHERE id = 1").fetchone()
        assert row[0] == "R - 17+ (violence & profanity)"
        # CSV file should not have been created — no drift to log
        assert not log_path.exists(), "Drift CSV should not be created when nothing drifted"

    def test_drift_updates_row_and_appends_csv(self, tmp_path):
        """Stored != live -> UPDATE row, append one CSV row with correct shape."""
        conn = _make_conn([
            (16498, "Attack on Titan", "R - 17+ (violence & profanity)"),
        ])
        # Simulate MAL re-rating AoT to R+ (synthetic; would never happen)
        client = _fake_client({16498: "R+ - Mild Nudity"})
        log_path = tmp_path / "drift.csv"

        run_refresh(conn, test_mode=False, client=client, log_path=str(log_path))

        # Row updated to the new value
        row = conn.execute("SELECT mal_rating FROM anime_info WHERE id = 16498").fetchone()
        assert row[0] == "R+ - Mild Nudity"

        # CSV exists, has header + exactly one drift row, columns correct
        assert log_path.exists(), "Drift CSV should be created on first diff"
        with open(log_path, newline="", encoding="utf-8") as f:
            reader = list(csv.reader(f))
        assert reader[0] == DRIFT_LOG_HEADER
        assert len(reader) == 2, f"Expected header + 1 drift row, got {len(reader)} rows"
        ts, mal_id, title, old, new = reader[1]
        assert mal_id == "16498"
        assert title == "Attack on Titan"
        assert old == "R - 17+ (violence & profanity)"
        assert new == "R+ - Mild Nudity"
        # Timestamp should be a non-empty ISO-8601-ish string (don't pin format too tight)
        assert ts and "T" in ts
