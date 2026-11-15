"""Unit + integration tests for tools/backfill_poster_urls.py and
tools/setup_poster_schema.py.

Uses per-test SQLite files in tmp_path so tests run fully offline. The
MAL HTTP layer is never touched — we exercise the parser + writer
directly, or use a hand-crafted stub for run_backfill.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest
import requests

from tools.backfill_poster_urls import (
    FAILURE_BUCKETS,
    Manifest,
    PosterUrls,
    _extract_poster_urls,
    compute_coverage,
    enumerate_shows,
    fetch_poster_urls,
    run_backfill,
    write_manifest,
    write_poster_urls,
)
from tools.setup_poster_schema import apply_schema, verify_schema


# ------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------

def _seed_db(tmp_path: Path, rows: list[tuple] | None = None) -> Path:
    """Fresh SQLite file with a minimal anime_info + poster schema applied."""
    db_path = tmp_path / "vault.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE anime_info ("
        " id INTEGER PRIMARY KEY,"
        " english_title TEXT,"
        " scored_by INTEGER,"
        " consensus_json TEXT"
        ")"
    )
    if rows is None:
        rows = [
            (1, "Cowboy Bebop", 900_000, "{}"),
            (21, "One Piece", 1_400_000, "{}"),
            (30, "Neon Genesis Evangelion", 800_000, "{}"),
        ]
    conn.executemany(
        "INSERT INTO anime_info (id, english_title, scored_by, consensus_json) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    apply_schema(db_path)
    return db_path


@dataclass
class _StubResponse:
    status_code: int
    payload: dict | None = None
    raise_on_json: bool = False

    def json(self):
        if self.raise_on_json:
            raise ValueError("bad json")
        return self.payload


class _StubMAL:
    """Test double for MALClient.get_with_auto_refresh. Keyed on
    anime_id parsed from the URL tail. Supports a `raise_exception`
    per-id override for exercising the try/except paths."""

    def __init__(self, responses: dict[int, _StubResponse],
                 raise_exception: dict[int, Exception] | None = None):
        self._responses = responses
        self._raise_exception = raise_exception or {}
        self.calls: list[tuple[str, dict]] = []

    def get_with_auto_refresh(self, url, params=None, timeout=30):
        self.calls.append((url, params or {}))
        anime_id = int(url.rsplit("/", 1)[-1])
        if anime_id in self._raise_exception:
            raise self._raise_exception[anime_id]
        return self._responses[anime_id]


# ------------------------------------------------------------------------
# Schema
# ------------------------------------------------------------------------

class TestSchema:
    def test_apply_schema_adds_columns_and_verifies(self, tmp_path: Path):
        db_path = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE anime_info (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        existed = apply_schema(db_path)
        assert existed == {"poster_url_large": False, "poster_url_medium": False}

        diag = verify_schema(db_path)
        assert diag["all_present"] is True
        assert diag["columns_missing"] == []
        assert diag["columns_present"] == ["poster_url_large", "poster_url_medium"]

    def test_apply_schema_idempotent(self, tmp_path: Path):
        db_path = _seed_db(tmp_path)  # already applies schema once
        existed = apply_schema(db_path)
        assert existed == {"poster_url_large": True, "poster_url_medium": True}


# ------------------------------------------------------------------------
# _extract_poster_urls
# ------------------------------------------------------------------------

class TestExtractPosterUrls:
    def test_both_urls_present(self):
        urls = _extract_poster_urls({"main_picture": {
            "large": "https://cdn.mal/large.jpg",
            "medium": "https://cdn.mal/medium.jpg",
        }})
        assert urls.large == "https://cdn.mal/large.jpg"
        assert urls.medium == "https://cdn.mal/medium.jpg"
        assert urls.any_present

    def test_only_large_present(self):
        urls = _extract_poster_urls({"main_picture": {"large": "https://cdn.mal/large.jpg"}})
        assert urls.large is not None
        assert urls.medium is None
        assert urls.any_present

    def test_only_medium_present(self):
        urls = _extract_poster_urls({"main_picture": {"medium": "https://cdn.mal/medium.jpg"}})
        assert urls.large is None
        assert urls.medium is not None
        assert urls.any_present

    def test_main_picture_absent_or_empty(self):
        # No main_picture field at all
        assert not _extract_poster_urls({"id": 1, "title": "Show"}).any_present
        # main_picture present but empty
        assert not _extract_poster_urls({"main_picture": {}}).any_present
        # Payload isn't even a dict (defensive against upstream drift)
        assert not _extract_poster_urls("not a dict").any_present


# ------------------------------------------------------------------------
# DB enumerate / write
# ------------------------------------------------------------------------

class TestDBReadWrite:
    def test_enumerate_default_skips_populated(self, tmp_path: Path):
        db_path = _seed_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        write_poster_urls(conn, 1, PosterUrls("https://x/L.jpg", "https://x/M.jpg"))
        shows = enumerate_shows(conn, force=False)
        # And confirm the write itself round-trips
        row = conn.execute(
            "SELECT poster_url_large, poster_url_medium FROM anime_info WHERE id = 1"
        ).fetchone()
        conn.close()
        assert {r[0] for r in shows} == {21, 30}
        assert row == ("https://x/L.jpg", "https://x/M.jpg")

    def test_enumerate_force_returns_all(self, tmp_path: Path):
        db_path = _seed_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        write_poster_urls(conn, 1, PosterUrls("https://x/L.jpg", "https://x/M.jpg"))
        shows = enumerate_shows(conn, force=True)
        conn.close()
        assert {r[0] for r in shows} == {1, 21, 30}


# ------------------------------------------------------------------------
# fetch_poster_urls — bucketing contract for the manifest
# ------------------------------------------------------------------------

class TestFetchBucketing:
    def test_success(self):
        stub = _StubMAL({1: _StubResponse(
            status_code=200,
            payload={"main_picture": {"large": "L", "medium": "M"}},
        )})
        outcome = fetch_poster_urls(stub, 1)
        assert outcome.bucket is None
        assert outcome.urls.large == "L" and outcome.urls.medium == "M"
        # And it hit the right URL + params
        assert stub.calls == [("https://api.myanimelist.net/v2/anime/1",
                               {"fields": "main_picture"})]

    def test_404_bucketed_no_mal_entry(self):
        stub = _StubMAL({999: _StubResponse(status_code=404)})
        assert fetch_poster_urls(stub, 999).bucket == "no_mal_entry"

    def test_5xx_bucketed_network_or_api(self):
        stub = _StubMAL({1: _StubResponse(status_code=500)})
        assert fetch_poster_urls(stub, 1).bucket == "network_or_api"

    def test_network_exception_bucketed_network_or_api(self):
        stub = _StubMAL(
            responses={},
            raise_exception={1: requests.ConnectionError("boom")},
        )
        assert fetch_poster_urls(stub, 1).bucket == "network_or_api"

    def test_200_no_main_picture_bucketed_no_poster_url(self):
        stub = _StubMAL({1: _StubResponse(status_code=200, payload={"id": 1})})
        assert fetch_poster_urls(stub, 1).bucket == "no_poster_url"

    def test_json_decode_failure_bucketed_other(self):
        stub = _StubMAL({1: _StubResponse(status_code=200, raise_on_json=True)})
        assert fetch_poster_urls(stub, 1).bucket == "other"


# ------------------------------------------------------------------------
# run_backfill integration
# ------------------------------------------------------------------------

class TestRunBackfill:
    def test_dry_run_probes_but_does_not_write(self, tmp_path: Path):
        db_path = _seed_db(tmp_path)
        stub = _StubMAL({
            1: _StubResponse(200, {"main_picture": {"large": "L1", "medium": "M1"}}),
            21: _StubResponse(200, {"main_picture": {"large": "L21", "medium": "M21"}}),
            30: _StubResponse(200, {"main_picture": {"large": "L30", "medium": "M30"}}),
        })
        manifest = run_backfill(
            db_path, dry_run=True, limit=None, force=False,
            sleep_seconds=0.0, mal_client=stub,
        )
        assert manifest.total_probed == 3
        assert manifest.success_count == 3
        assert manifest.failed_count == 0

        conn = sqlite3.connect(str(db_path))
        cnt = conn.execute(
            "SELECT COUNT(*) FROM anime_info WHERE poster_url_large IS NOT NULL"
        ).fetchone()[0]
        conn.close()
        assert cnt == 0

    def test_mixed_success_and_failure_writes_only_successes(self, tmp_path: Path):
        db_path = _seed_db(tmp_path)
        stub = _StubMAL({
            1: _StubResponse(200, {"main_picture": {"large": "L1", "medium": "M1"}}),
            21: _StubResponse(404),
            30: _StubResponse(200, {"id": 30}),  # no main_picture
        })
        manifest = run_backfill(
            db_path, dry_run=False, limit=None, force=False,
            sleep_seconds=0.0, mal_client=stub,
        )
        assert manifest.success_count == 1
        assert manifest.failed_count == 2
        assert manifest.failure_details["no_mal_entry"] == [21]
        assert manifest.failure_details["no_poster_url"] == [30]

        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT id, poster_url_large FROM anime_info "
            "WHERE poster_url_large IS NOT NULL"
        ).fetchall()
        conn.close()
        assert rows == [(1, "L1")]


# ------------------------------------------------------------------------
# Manifest serialization contract
# ------------------------------------------------------------------------

class TestManifest:
    def test_serialization_shape_and_file_path(self, tmp_path: Path):
        m = Manifest(run_id="2026-09-29T12-00-00Z")
        m.total_probed = 4
        m.record_success(1)
        m.record_failure(2, "no_mal_entry")
        m.record_failure(3, "no_poster_url")
        m.record_failure(4, "network_or_api")
        m.duration_seconds = 12.34

        path = write_manifest(m, tmp_path)
        assert path.name == "poster_backfill_manifest_2026-09-29T12-00-00Z.json"

        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["run_id"] == "2026-09-29T12-00-00Z"
        assert data["total_probed"] == 4
        assert data["success_count"] == 1
        assert data["failed_count"] == 3
        assert data["failure_details"]["no_mal_entry"] == [2]
        assert data["failure_details"]["no_poster_url"] == [3]
        assert data["failure_details"]["network_or_api"] == [4]
        assert data["failure_details"]["other"] == []
        assert set(data["failure_details"].keys()) == set(FAILURE_BUCKETS)
        assert data["duration_seconds"] == 12.34


# ------------------------------------------------------------------------
# Coverage stats
# ------------------------------------------------------------------------

class TestComputeCoverage:
    def test_partial_coverage(self, tmp_path: Path):
        db_path = _seed_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        write_poster_urls(conn, 1, PosterUrls("L1", "M1"))
        write_poster_urls(conn, 21, PosterUrls("L21", None))  # large only
        cov = compute_coverage(conn)
        conn.close()
        assert cov["total"] == 3
        assert cov["with_large"] == 2
        assert cov["with_medium"] == 1
        assert cov["missing"] == 1
