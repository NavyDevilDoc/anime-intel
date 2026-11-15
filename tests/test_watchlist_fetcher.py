"""
PR C / Initiative 2 Phase B — watchlist fetcher tests.

Mocked unit coverage for the MAL and AniList fetchers plus the
source-dispatching wrapper. A separate live integration test (gated
behind WATCHLIST_LIVE_TEST=1) hits real APIs with the project lead's
public username from tests/data/watchlist_test_fixtures.json — run
opt-in only, gated by env to keep CI runs from hitting MAL/AniList.
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from analysis.watchlist_fetcher import (  # noqa: E402
    DEFAULT_STATUSES,
    WatchlistFetchError,
    WatchlistResult,
    fetch_anilist,
    fetch_mal,
    fetch_watchlist,
)


# ---------------------------------------------------------------------
# Helpers — build canned HTTP responses
# ---------------------------------------------------------------------

def _mal_response(entries: list[int], next_url: str | None = None, status_code: int = 200):
    """Build a MAL-shaped JSON response."""
    payload = {
        "data": [{"node": {"id": mid, "title": f"Show {mid}"}, "list_status": {}} for mid in entries],
        "paging": {"next": next_url} if next_url else {},
    }
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.headers = {}
    return resp


def _mal_error(status_code: int, body: str = ""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = body
    resp.headers = {"Retry-After": "60"} if status_code == 429 else {}
    return resp


def _anilist_response(lists: list[dict], status_code: int = 200):
    """Build an AniList GraphQL response."""
    payload = {
        "data": {
            "MediaListCollection": {
                "user": {"id": 1, "name": "test_user"},
                "lists": lists,
            }
        }
    }
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.headers = {}
    return resp


def _anilist_list(status: str, mal_ids: list[int | None], anilist_ids: list[int] | None = None) -> dict:
    """Build one AniList sublist."""
    if anilist_ids is None:
        anilist_ids = list(range(1000, 1000 + len(mal_ids)))
    entries = []
    for ali_id, mid in zip(anilist_ids, mal_ids):
        entries.append({
            "status": status,
            "media": {
                "id": ali_id,
                "idMal": mid,
                "title": {"romaji": f"Show {ali_id}", "english": None},
            },
        })
    return {"name": status.title(), "status": status, "entries": entries}


# =====================================================================
# MAL fetcher — token lifecycle
# =====================================================================

class TestMALTokenLifecycle:

    def test_missing_token_raises_503(self, monkeypatch):
        """No token in env, no token_data.json → WatchlistFetchError(503).

        The user shouldn't see a 401 — that's an upstream auth issue,
        not a user mistake. 503 conveys "server side problem."
        """
        monkeypatch.delenv("MAL_ACCESS_TOKEN", raising=False)
        with patch("analysis.watchlist_fetcher._read_mal_token", return_value=None):
            with pytest.raises(WatchlistFetchError) as exc:
                fetch_mal("anyone", statuses=["completed"])
        assert exc.value.status_code == 503

    def test_401_triggers_refresh_then_retries(self, monkeypatch):
        """Expired token on first call → refresh → success on retry."""
        monkeypatch.setenv("MAL_ACCESS_TOKEN", "stale")

        with patch("analysis.watchlist_fetcher._read_mal_token") as mock_read, \
             patch("analysis.watchlist_fetcher._refresh_mal_token") as mock_refresh, \
             patch("requests.Session") as mock_session:

            mock_read.return_value = "stale"
            mock_refresh.return_value = "fresh"

            sess = MagicMock()
            mock_session.return_value = sess
            # First page: 401, then 200 on retry
            sess.get.side_effect = [_mal_error(401), _mal_response([100, 101])]

            result = fetch_mal("user1", statuses=["completed"])

            assert mock_refresh.called, "Refresh helper should have been called"
            assert result.mal_ids == [100, 101]
            assert result.errors == []

    def test_double_401_raises_503(self, monkeypatch):
        """Refresh ran but the new token is also rejected → 503."""
        monkeypatch.setenv("MAL_ACCESS_TOKEN", "stale")
        with patch("analysis.watchlist_fetcher._read_mal_token", return_value="stale"), \
             patch("analysis.watchlist_fetcher._refresh_mal_token", return_value="still-bad"), \
             patch("requests.Session") as mock_session:

            sess = MagicMock()
            mock_session.return_value = sess
            sess.get.side_effect = [_mal_error(401), _mal_error(401)]

            with pytest.raises(WatchlistFetchError) as exc:
                fetch_mal("user1", statuses=["completed"])
            assert exc.value.status_code == 503

    def test_refresh_failure_raises_503(self, monkeypatch):
        """401 + refresh helper returned None → 503, no retry attempted."""
        monkeypatch.setenv("MAL_ACCESS_TOKEN", "stale")
        with patch("analysis.watchlist_fetcher._read_mal_token", return_value="stale"), \
             patch("analysis.watchlist_fetcher._refresh_mal_token", return_value=None), \
             patch("requests.Session") as mock_session:

            sess = MagicMock()
            mock_session.return_value = sess
            sess.get.return_value = _mal_error(401)

            with pytest.raises(WatchlistFetchError) as exc:
                fetch_mal("user1", statuses=["completed"])
            assert exc.value.status_code == 503


# =====================================================================
# MAL fetcher — happy path + error surfaces
# =====================================================================

class TestMALFetcher:

    def _make_session(self, responses: list):
        sess = MagicMock()
        sess.get.side_effect = responses
        return sess

    def test_single_status_single_page(self, monkeypatch):
        monkeypatch.setenv("MAL_ACCESS_TOKEN", "ok")
        with patch("requests.Session") as mock_sess_cls:
            mock_sess_cls.return_value = self._make_session([
                _mal_response([1, 2, 3]),
            ])
            result = fetch_mal("user1", statuses=["completed"])
            assert result.source == "mal"
            assert result.mal_ids == [1, 2, 3]
            assert result.total_entries == 3
            assert result.unmappable_entries == 0
            assert result.errors == []

    def test_multi_status_dedupes_ids(self, monkeypatch):
        """Same MAL ID across two statuses returns once in mal_ids but
        bumps total_entries twice — the raw count reflects what the
        source returned, the dedup runs in the set."""
        monkeypatch.setenv("MAL_ACCESS_TOKEN", "ok")
        with patch("requests.Session") as mock_sess_cls:
            mock_sess_cls.return_value = self._make_session([
                _mal_response([1, 2]),         # completed
                _mal_response([2, 3]),         # dropped
            ])
            result = fetch_mal("user1", statuses=["completed", "dropped"])
            assert result.mal_ids == [1, 2, 3]
            assert result.total_entries == 4  # 2 + 2 raw

    def test_pagination_via_paging_next(self, monkeypatch):
        monkeypatch.setenv("MAL_ACCESS_TOKEN", "ok")
        with patch("requests.Session") as mock_sess_cls:
            mock_sess_cls.return_value = self._make_session([
                _mal_response([1, 2], next_url="https://api.myanimelist.net/v2/users/u/animelist?offset=1000"),
                _mal_response([3, 4]),
            ])
            result = fetch_mal("user1", statuses=["completed"])
            assert result.mal_ids == [1, 2, 3, 4]

    def test_404_first_page_raises(self, monkeypatch):
        """User-not-found / private → WatchlistFetchError(404) immediately."""
        monkeypatch.setenv("MAL_ACCESS_TOKEN", "ok")
        with patch("requests.Session") as mock_sess_cls:
            mock_sess_cls.return_value = self._make_session([
                _mal_error(404, body='{"error":"not found"}'),
            ])
            with pytest.raises(WatchlistFetchError) as exc:
                fetch_mal("nobody", statuses=["completed"])
            assert exc.value.status_code == 404

    def test_429_records_error_but_continues_other_statuses(self, monkeypatch):
        """Partial success: rate-limit on one status, success on the next."""
        monkeypatch.setenv("MAL_ACCESS_TOKEN", "ok")
        with patch("requests.Session") as mock_sess_cls:
            mock_sess_cls.return_value = self._make_session([
                _mal_error(429),
                _mal_response([5, 6]),
            ])
            result = fetch_mal("user1", statuses=["completed", "dropped"])
            assert result.mal_ids == [5, 6]
            assert any("rate-limited" in e.lower() for e in result.errors)

    def test_empty_statuses_returns_empty_result(self, monkeypatch):
        """No statuses requested → empty result, no API calls."""
        monkeypatch.setenv("MAL_ACCESS_TOKEN", "ok")
        with patch("requests.Session") as mock_sess_cls:
            result = fetch_mal("user1", statuses=[])
            assert result.mal_ids == []
            assert result.total_entries == 0
            assert not mock_sess_cls.called or not mock_sess_cls.return_value.get.called

    def test_default_statuses_applied_when_none(self, monkeypatch):
        """statuses=None falls back to completed + dropped."""
        monkeypatch.setenv("MAL_ACCESS_TOKEN", "ok")
        with patch("requests.Session") as mock_sess_cls:
            sess = self._make_session([_mal_response([]), _mal_response([])])
            mock_sess_cls.return_value = sess
            fetch_mal("user1", statuses=None)
            # 2 calls = 2 default statuses
            assert sess.get.call_count == 2


# =====================================================================
# AniList fetcher
# =====================================================================

class TestAniListFetcher:

    def test_single_status_filter(self):
        """Only COMPLETED entries returned when statuses=['completed']."""
        with patch("requests.post") as mock_post:
            mock_post.return_value = _anilist_response([
                _anilist_list("COMPLETED", [10, 20]),
                _anilist_list("DROPPED", [30]),
                _anilist_list("CURRENT", [40, 50]),
            ])
            result = fetch_anilist("user1", statuses=["completed"])
            assert result.source == "anilist"
            assert result.mal_ids == [10, 20]

    def test_watching_includes_current_and_repeating(self):
        """AniList REPEATING (rewatching) folds into canonical 'watching'."""
        with patch("requests.post") as mock_post:
            mock_post.return_value = _anilist_response([
                _anilist_list("CURRENT", [100]),
                _anilist_list("REPEATING", [200]),
                _anilist_list("COMPLETED", [300]),  # excluded
            ])
            result = fetch_anilist("user1", statuses=["watching"])
            assert result.mal_ids == [100, 200]

    def test_unmappable_entries_counted(self):
        """AniList entries with idMal=None bump unmappable_entries but
        don't appear in mal_ids."""
        with patch("requests.post") as mock_post:
            mock_post.return_value = _anilist_response([
                _anilist_list("COMPLETED", [11, None, 13, None]),
            ])
            result = fetch_anilist("user1", statuses=["completed"])
            assert result.mal_ids == [11, 13]
            assert result.unmappable_entries == 2
            assert result.total_entries == 4

    def test_404_raises_explicitly(self):
        with patch("requests.post") as mock_post:
            resp = MagicMock()
            resp.status_code = 404
            resp.headers = {}
            mock_post.return_value = resp
            with pytest.raises(WatchlistFetchError) as exc:
                fetch_anilist("nobody", statuses=["completed"])
            assert exc.value.status_code == 404

    def test_429_raises_explicitly(self):
        with patch("requests.post") as mock_post:
            resp = MagicMock()
            resp.status_code = 429
            resp.headers = {"Retry-After": "30"}
            mock_post.return_value = resp
            with pytest.raises(WatchlistFetchError) as exc:
                fetch_anilist("user1", statuses=["completed"])
            assert exc.value.status_code == 429

    def test_graphql_errors_treated_as_404(self):
        """GraphQL error body usually = private user OR malformed query.
        Surface as 404 so the frontend's not-found copy renders."""
        with patch("requests.post") as mock_post:
            resp = MagicMock()
            resp.status_code = 200
            resp.headers = {}
            resp.json.return_value = {"errors": [{"message": "User not found"}]}
            mock_post.return_value = resp
            with pytest.raises(WatchlistFetchError) as exc:
                fetch_anilist("nobody", statuses=["completed"])
            assert exc.value.status_code == 404

    def test_status_mapping_completeness(self):
        """Every canonical status maps to at least one AniList enum."""
        with patch("requests.post") as mock_post:
            mock_post.return_value = _anilist_response([
                _anilist_list("COMPLETED", [1]),
                _anilist_list("DROPPED", [2]),
                _anilist_list("CURRENT", [3]),
                _anilist_list("REPEATING", [4]),
                _anilist_list("PAUSED", [5]),
                _anilist_list("PLANNING", [6]),
            ])
            result = fetch_anilist(
                "user1",
                statuses=["completed", "dropped", "watching", "on_hold", "plan_to_watch"],
            )
            assert sorted(result.mal_ids) == [1, 2, 3, 4, 5, 6]


# =====================================================================
# Dispatch wrapper
# =====================================================================

class TestFetchWatchlistDispatch:

    def test_mal_dispatch(self, monkeypatch):
        monkeypatch.setenv("MAL_ACCESS_TOKEN", "ok")
        with patch("requests.Session") as mock_sess_cls:
            sess = MagicMock()
            sess.get.return_value = _mal_response([1])
            mock_sess_cls.return_value = sess
            result = fetch_watchlist("mal", "u", ["completed"])
            assert result.source == "mal"

    def test_anilist_dispatch(self):
        with patch("requests.post") as mock_post:
            mock_post.return_value = _anilist_response([_anilist_list("COMPLETED", [1])])
            result = fetch_watchlist("anilist", "u", ["completed"])
            assert result.source == "anilist"

    def test_unsupported_source_raises_400(self):
        with pytest.raises(WatchlistFetchError) as exc:
            fetch_watchlist("kitsu", "u", ["completed"])  # type: ignore[arg-type]
        assert exc.value.status_code == 400


# =====================================================================
# Live integration test — opt-in only via env
# =====================================================================

@pytest.mark.skipif(
    os.environ.get("WATCHLIST_LIVE_TEST") != "1",
    reason="Set WATCHLIST_LIVE_TEST=1 to hit real MAL+AniList APIs",
)
class TestLiveIntegration:
    """Hits real upstream APIs against the project lead's public
    AnimeMathNerd account. Gated behind WATCHLIST_LIVE_TEST=1 so CI
    doesn't burn rate limit budget on every push."""

    @pytest.fixture(scope="class")
    def fixtures(self) -> dict:
        path = os.path.join(ROOT_DIR, "tests", "data", "watchlist_test_fixtures.json")
        if not os.path.exists(path):
            pytest.skip(f"No test fixtures at {path}")
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_mal_live_returns_real_ids(self, fixtures):
        username = fixtures.get("mal_username")
        if not username:
            pytest.skip("No mal_username in fixtures")
        result = fetch_mal(username, statuses=["completed"])
        assert result.source == "mal"
        assert result.mal_ids, "completed list should not be empty for the fixture user"
        assert all(isinstance(i, int) and i > 0 for i in result.mal_ids)

    def test_anilist_live_returns_real_ids(self, fixtures):
        username = fixtures.get("anilist_username")
        if not username:
            pytest.skip("No anilist_username in fixtures")
        result = fetch_anilist(username, statuses=["completed", "dropped"])
        assert result.source == "anilist"
        assert all(isinstance(i, int) and i > 0 for i in result.mal_ids)


# =====================================================================
# Initiative 3: _read_mal_token Supabase-first fallback chain
# =====================================================================

class TestMALTokenFallbackChain:
    """Verify the three-link chain in analysis.watchlist_fetcher._read_mal_token:
    Supabase → env → disk. Initiative 3 Phase C."""

    def test_reads_from_supabase_first(self, monkeypatch, tmp_path):
        """Supabase row wins even when env var and disk both have values."""
        monkeypatch.setenv("MAL_ACCESS_TOKEN", "env-value")
        # Provide a disk file too, to prove neither lower link is consulted.
        disk_path = tmp_path / "token_data.json"
        disk_path.write_text(json.dumps({"access_token": "disk-value"}))
        monkeypatch.setattr(
            "analysis.watchlist_fetcher.ROOT_DIR", str(tmp_path)
        )

        from analysis.watchlist_fetcher import _read_mal_token
        with patch(
            "analysis.mal_token_store.read_token",
            return_value={"access_token": "supabase-value"},
        ):
            assert _read_mal_token() == "supabase-value"

    def test_falls_back_to_env_when_supabase_returns_none(
        self, monkeypatch, tmp_path
    ):
        """Supabase miss → env var wins over disk."""
        monkeypatch.setenv("MAL_ACCESS_TOKEN", "env-value")
        disk_path = tmp_path / "token_data.json"
        disk_path.write_text(json.dumps({"access_token": "disk-value"}))
        monkeypatch.setattr(
            "analysis.watchlist_fetcher.ROOT_DIR", str(tmp_path)
        )

        from analysis.watchlist_fetcher import _read_mal_token
        with patch("analysis.mal_token_store.read_token", return_value=None):
            assert _read_mal_token() == "env-value"

    def test_falls_back_to_disk_when_supabase_and_env_miss(
        self, monkeypatch, tmp_path
    ):
        """Supabase miss + env unset → disk file wins (local-dev path)."""
        monkeypatch.delenv("MAL_ACCESS_TOKEN", raising=False)
        disk_path = tmp_path / "token_data.json"
        disk_path.write_text(json.dumps({"access_token": "disk-value"}))
        monkeypatch.setattr(
            "analysis.watchlist_fetcher.ROOT_DIR", str(tmp_path)
        )

        from analysis.watchlist_fetcher import _read_mal_token
        with patch("analysis.mal_token_store.read_token", return_value=None):
            assert _read_mal_token() == "disk-value"

    def test_returns_none_when_all_three_miss(self, monkeypatch, tmp_path):
        """No Supabase row + no env var + no disk file → None.
        Caller raises 503 (token-broken-at-source)."""
        monkeypatch.delenv("MAL_ACCESS_TOKEN", raising=False)
        monkeypatch.setattr(
            "analysis.watchlist_fetcher.ROOT_DIR", str(tmp_path)
        )
        # No token_data.json file in tmp_path.

        from analysis.watchlist_fetcher import _read_mal_token
        with patch("analysis.mal_token_store.read_token", return_value=None):
            assert _read_mal_token() is None

    def test_supabase_row_without_access_token_falls_through(
        self, monkeypatch, tmp_path
    ):
        """Malformed Supabase row (missing access_token) → fall through
        to env. Defensive — partial schema migration shouldn't break
        production."""
        monkeypatch.setenv("MAL_ACCESS_TOKEN", "env-value")
        monkeypatch.setattr(
            "analysis.watchlist_fetcher.ROOT_DIR", str(tmp_path)
        )

        from analysis.watchlist_fetcher import _read_mal_token
        with patch(
            "analysis.mal_token_store.read_token",
            return_value={"refresh_token": "only-refresh"},
        ):
            assert _read_mal_token() == "env-value"
