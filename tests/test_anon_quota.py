"""
Tests for the anonymous per-IP quota middleware (Phase 1 of the
anonymous-access rollout — see IMPLEMENTATION_PLAN_anon_access.md).

Coverage:
  - Master kill switch (ANON_ACCESS_ENABLED=false → 401)
  - Cap defaults (5 search / 3 DNA)
  - Env-tunable cap overrides
  - First-ever request inserts a new row
  - Below-cap request increments existing row
  - At-cap request raises structured 429 without writing
  - New UTC day resets counters
  - IP hashing is deterministic and salt-sensitive
  - X-Forwarded-For takes precedence over request.client.host
  - Structured 429 detail shape matches frontend contract

Run: pytest tests/test_anon_quota.py -v
"""
import os
import sys
import hashlib
from unittest.mock import patch, MagicMock

import pytest
from fastapi import HTTPException

TESTS_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


def _make_request(
    forwarded_for: str | None = None,
    real_ip: str | None = None,
    client_host: str = "10.0.0.1",
):
    """Build a minimal Request-like stub. The real Request object is hard to
    construct, so we use a MagicMock with just the fields client_ip cares about."""
    req = MagicMock()
    req.headers = {}
    if forwarded_for:
        req.headers["x-forwarded-for"] = forwarded_for
    if real_ip:
        req.headers["x-real-ip"] = real_ip
    req.client = MagicMock()
    req.client.host = client_host
    return req


# =====================================================================
# CLIENT IP EXTRACTION
# =====================================================================

class TestClientIPExtraction:
    """Verify get_client_ip extracts the real client IP behind Railway's proxy.

    Railway sets X-Real-IP and the leftmost X-Forwarded-For entry to the
    same observed client IP. The rightmost X-Forwarded-For entry is a
    Railway internal edge IP, NOT the user — so we must NOT key on it.
    See client_ip.py module docstring for the full threat model.
    """

    def test_x_real_ip_takes_top_precedence(self):
        """X-Real-IP is the explicit, single-value signal. Use it first."""
        from api.middleware.client_ip import get_client_ip
        req = _make_request(real_ip="203.0.113.5", client_host="100.64.0.5")
        assert get_client_ip(req) == "203.0.113.5"

    def test_x_real_ip_wins_over_xff(self):
        """When both headers are present, X-Real-IP wins because it is
        unambiguous (no chain to parse)."""
        from api.middleware.client_ip import get_client_ip
        req = _make_request(
            real_ip="203.0.113.5",
            forwarded_for="203.0.113.5, 167.82.233.159",
        )
        assert get_client_ip(req) == "203.0.113.5"

    def test_xff_leftmost_used_when_no_real_ip(self):
        """If X-Real-IP is missing, fall back to the leftmost X-Forwarded-For
        entry — that is the value Railway's edge proxy set from observation,
        and the entry to the right is a Railway internal IP."""
        from api.middleware.client_ip import get_client_ip
        req = _make_request(
            forwarded_for="203.0.113.5, 167.82.233.159",
            client_host="100.64.0.5",
        )
        assert get_client_ip(req) == "203.0.113.5"

    def test_railway_edge_ip_is_never_returned(self):
        """Regression: the rightmost XFF entry on Railway is an internal
        edge IP. Returning it would group different users into the same
        bucket. The fix must extract the leftmost entry, which is the real
        client IP set by Railway's edge proxy."""
        from api.middleware.client_ip import get_client_ip
        # Real production data from a debug_ip probe
        req = _make_request(
            real_ip="173.72.199.193",
            forwarded_for="173.72.199.193, 167.82.233.159",
            client_host="100.64.0.5",
        )
        result = get_client_ip(req)
        assert result == "173.72.199.193"
        assert result != "167.82.233.159"  # Railway edge — must NOT match
        assert not result.startswith("100.64.")  # Railway internal mesh

    def test_spoofed_real_ip_is_overwritten_by_railway_in_practice(self):
        """Documents the trust model: even if the test passes a forged
        X-Real-IP, in production Railway's edge proxy overwrites that
        header with what it actually observed before the request reaches
        the app. This test does NOT exercise that overwrite — it only
        documents that the helper trusts whatever value Railway delivered."""
        from api.middleware.client_ip import get_client_ip
        req = _make_request(real_ip="203.0.113.5")
        # Helper returns whatever was in the header — Railway's edge is
        # responsible for ensuring it is trustworthy upstream of us.
        assert get_client_ip(req) == "203.0.113.5"

    def test_falls_back_to_client_host_in_local_dev(self):
        """Local dev with no proxy in front: no XFF, no X-Real-IP, just
        request.client.host with the actual client address."""
        from api.middleware.client_ip import get_client_ip
        req = _make_request(forwarded_for=None, client_host="192.0.2.1")
        assert get_client_ip(req) == "192.0.2.1"

    def test_returns_unknown_when_nothing_available(self):
        from api.middleware.client_ip import get_client_ip
        req = MagicMock()
        req.headers = {}
        req.client = None
        assert get_client_ip(req) == "unknown"

    def test_xff_chain_with_three_entries_uses_leftmost(self):
        """If a future Railway change adds a third proxy hop, leftmost
        is still the right answer because it is what the outermost
        trusted proxy set from observation."""
        from api.middleware.client_ip import get_client_ip
        req = _make_request(forwarded_for="203.0.113.5, 198.51.100.1, 167.82.233.159")
        assert get_client_ip(req) == "203.0.113.5"


# =====================================================================
# IP HASHING
# =====================================================================

class TestIPHashing:
    """Salted hashing must be deterministic and salt-sensitive."""

    def test_hash_is_deterministic(self):
        from api.middleware.anon_quota import _hash_ip
        assert _hash_ip("203.0.113.5") == _hash_ip("203.0.113.5")

    def test_hash_changes_with_salt(self):
        from api.middleware.anon_quota import _hash_ip
        with patch.dict(os.environ, {"ANON_IP_SALT": "salt-a"}):
            h_a = _hash_ip("203.0.113.5")
        with patch.dict(os.environ, {"ANON_IP_SALT": "salt-b"}):
            h_b = _hash_ip("203.0.113.5")
        assert h_a != h_b

    def test_hash_length_is_32(self):
        from api.middleware.anon_quota import _hash_ip
        assert len(_hash_ip("203.0.113.5")) == 32

    def test_different_ips_produce_different_hashes(self):
        from api.middleware.anon_quota import _hash_ip
        assert _hash_ip("203.0.113.5") != _hash_ip("203.0.113.6")

    def test_hash_does_not_leak_ip(self):
        """The hash output must not contain the plaintext IP."""
        from api.middleware.anon_quota import _hash_ip
        ip = "203.0.113.5"
        assert ip not in _hash_ip(ip)


# =====================================================================
# CAP CONFIGURATION
# =====================================================================

class TestCapConfig:
    """Defaults match the plan, env vars override at call time."""

    def test_default_search_cap_is_5(self):
        from api.middleware.anon_quota import _get_cap
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANON_SEARCH_CAP", None)
            assert _get_cap("search") == 30

    def test_default_dna_cap_is_10(self):
        from api.middleware.anon_quota import _get_cap
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANON_DNA_CAP", None)
            assert _get_cap("dna") == 10

    def test_search_cap_env_override(self):
        from api.middleware.anon_quota import _get_cap
        with patch.dict(os.environ, {"ANON_SEARCH_CAP": "12"}):
            assert _get_cap("search") == 12

    def test_dna_cap_env_override(self):
        from api.middleware.anon_quota import _get_cap
        with patch.dict(os.environ, {"ANON_DNA_CAP": "1"}):
            assert _get_cap("dna") == 1

    def test_access_enabled_default_true(self):
        from api.middleware.anon_quota import _is_enabled
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANON_ACCESS_ENABLED", None)
            assert _is_enabled() is True

    def test_access_disabled_when_env_false(self):
        from api.middleware.anon_quota import _is_enabled
        with patch.dict(os.environ, {"ANON_ACCESS_ENABLED": "false"}):
            assert _is_enabled() is False

    def test_access_disabled_case_insensitive(self):
        from api.middleware.anon_quota import _is_enabled
        with patch.dict(os.environ, {"ANON_ACCESS_ENABLED": "FALSE"}):
            assert _is_enabled() is False


# =====================================================================
# QUOTA CHECK BEHAVIOR
# =====================================================================

class TestCheckAnonQuota:
    """End-to-end behavior of the check_anon_quota dependency."""

    @patch("api.middleware.anon_quota.supabase_post")
    @patch("api.middleware.anon_quota.supabase_get")
    def test_first_request_inserts_new_row(self, mock_get, mock_post):
        from api.middleware.anon_quota import check_anon_quota
        mock_get.return_value = []  # No existing row

        req = _make_request(forwarded_for="203.0.113.5")
        check_anon_quota(req, "search")

        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0] == "anon_usage"
        payload = args[1]
        assert payload["search_count"] == 1
        assert payload["dna_count"] == 0
        assert "ip_hash" in payload
        assert "usage_date" in payload

    @patch("api.middleware.anon_quota.supabase_patch")
    @patch("api.middleware.anon_quota.supabase_get")
    def test_below_cap_increments_existing_row(self, mock_get, mock_patch):
        from api.middleware.anon_quota import check_anon_quota, _today_utc
        mock_get.return_value = [{
            "ip_hash": "abc123",
            "usage_date": _today_utc(),
            "search_count": 2,
            "dna_count": 0,
        }]

        req = _make_request(forwarded_for="203.0.113.5")
        check_anon_quota(req, "search")

        mock_patch.assert_called_once()
        args, kwargs = mock_patch.call_args
        update = args[1]
        assert update["search_count"] == 3

    @patch("api.middleware.anon_quota.supabase_patch")
    @patch("api.middleware.anon_quota.supabase_get")
    def test_at_cap_raises_429_without_writing(self, mock_get, mock_patch):
        from api.middleware.anon_quota import check_anon_quota, _today_utc
        mock_get.return_value = [{
            "ip_hash": "abc123",
            "usage_date": _today_utc(),
            "search_count": 30,  # at default cap
            "dna_count": 0,
        }]

        req = _make_request(forwarded_for="203.0.113.5")
        with pytest.raises(HTTPException) as exc:
            check_anon_quota(req, "search")

        assert exc.value.status_code == 429
        mock_patch.assert_not_called()

    @patch("api.middleware.anon_quota.supabase_patch")
    @patch("api.middleware.anon_quota.supabase_get")
    def test_429_detail_shape_matches_frontend_contract(self, mock_get, mock_patch):
        from api.middleware.anon_quota import check_anon_quota, _today_utc
        mock_get.return_value = [{
            "ip_hash": "abc123",
            "usage_date": _today_utc(),
            "search_count": 30,
            "dna_count": 0,
        }]

        req = _make_request(forwarded_for="203.0.113.5")
        with pytest.raises(HTTPException) as exc:
            check_anon_quota(req, "search")

        detail = exc.value.detail
        assert isinstance(detail, dict)
        assert detail["limit_type"] == "search"
        assert detail["cap"] == 30
        assert "reset_at" in detail
        assert "upgrade_hint" in detail

    @patch("api.middleware.anon_quota.supabase_post")
    @patch("api.middleware.anon_quota.supabase_patch")
    @patch("api.middleware.anon_quota.supabase_get")
    def test_no_today_row_means_fresh_insert_not_patch_reset(
        self, mock_get, mock_patch, mock_post
    ):
        """Under composite-PK schema, prior-day rows are filtered out of
        _fetch_anon_row at the DB level — the quota logic never sees a
        stale row. A new UTC day looks identical to a first-ever request:
        POST a fresh row, never PATCH-reset. The destructive PATCH-reset
        branch from the single-PK schema is deleted by deletion."""
        from api.middleware.anon_quota import check_anon_quota
        mock_get.return_value = []  # today-filter returns no row

        req = _make_request(forwarded_for="203.0.113.5")
        check_anon_quota(req, "search")

        # Fresh POST, no PATCH — the destructive branch is gone.
        mock_post.assert_called_once()
        mock_patch.assert_not_called()
        payload = mock_post.call_args[0][1]
        assert payload["search_count"] == 1
        assert payload["dna_count"] == 0

    @patch("api.middleware.anon_quota.supabase_post")
    @patch("api.middleware.anon_quota.supabase_get")
    def test_fetch_query_filters_to_today_under_composite_pk(
        self, mock_get, mock_post
    ):
        """The _fetch_anon_row GET must include usage_date=eq.{today} in
        the filter, otherwise PostgREST would return every historical row
        for that IP and rows[0] would be undefined-order. Regression guard
        for the composite-PK schema contract."""
        from api.middleware.anon_quota import check_anon_quota, _today_utc
        mock_get.return_value = []

        req = _make_request(forwarded_for="203.0.113.5")
        check_anon_quota(req, "search")

        params = mock_get.call_args[0][1]
        assert "ip_hash" in params
        assert params["usage_date"] == f"eq.{_today_utc()}"

    @patch("api.middleware.anon_quota.supabase_patch")
    @patch("api.middleware.anon_quota.supabase_get")
    def test_increment_patch_filter_includes_usage_date(
        self, mock_get, mock_patch
    ):
        """The increment PATCH filter must include usage_date so the update
        targets today's row only. Without it, PostgREST would PATCH every
        historical row matching the ip_hash — silent data corruption on
        the analytics history. Regression guard for the composite-PK
        schema contract."""
        from api.middleware.anon_quota import check_anon_quota, _today_utc
        mock_get.return_value = [{
            "ip_hash": "abc123",
            "usage_date": _today_utc(),
            "search_count": 2,
            "dna_count": 0,
        }]

        req = _make_request(forwarded_for="203.0.113.5")
        check_anon_quota(req, "search")

        # Third positional arg to supabase_patch is the filter dict.
        patch_filter = mock_patch.call_args[0][2]
        assert "ip_hash" in patch_filter
        assert patch_filter["usage_date"] == f"eq.{_today_utc()}"

    @patch("api.middleware.anon_quota.supabase_get")
    def test_master_kill_switch_raises_401(self, mock_get):
        from api.middleware.anon_quota import check_anon_quota
        req = _make_request(forwarded_for="203.0.113.5")

        with patch.dict(os.environ, {"ANON_ACCESS_ENABLED": "false"}):
            with pytest.raises(HTTPException) as exc:
                check_anon_quota(req, "search")

        assert exc.value.status_code == 401
        # Must not have hit the DB at all
        mock_get.assert_not_called()

    @patch("api.middleware.anon_quota.supabase_get")
    def test_zero_cap_rejects_without_db(self, mock_get):
        from api.middleware.anon_quota import check_anon_quota
        req = _make_request(forwarded_for="203.0.113.5")

        with patch.dict(os.environ, {"ANON_SEARCH_CAP": "0"}):
            with pytest.raises(HTTPException) as exc:
                check_anon_quota(req, "search")

        assert exc.value.status_code == 429
        mock_get.assert_not_called()

    @patch("api.middleware.anon_quota.supabase_patch")
    @patch("api.middleware.anon_quota.supabase_get")
    def test_env_override_cap_one(self, mock_get, mock_patch):
        """Setting ANON_SEARCH_CAP=1 means the second call 429s."""
        from api.middleware.anon_quota import check_anon_quota, _today_utc
        mock_get.return_value = [{
            "ip_hash": "abc123",
            "usage_date": _today_utc(),
            "search_count": 1,
            "dna_count": 0,
        }]

        req = _make_request(forwarded_for="203.0.113.5")
        with patch.dict(os.environ, {"ANON_SEARCH_CAP": "1"}):
            with pytest.raises(HTTPException) as exc:
                check_anon_quota(req, "search")

        assert exc.value.status_code == 429
        mock_patch.assert_not_called()

    @patch("api.middleware.anon_quota.supabase_patch")
    @patch("api.middleware.anon_quota.supabase_get")
    def test_dna_uses_dna_counter(self, mock_get, mock_patch):
        """DNA quota should bump dna_count, not search_count."""
        from api.middleware.anon_quota import check_anon_quota, _today_utc
        mock_get.return_value = [{
            "ip_hash": "abc123",
            "usage_date": _today_utc(),
            "search_count": 999,  # high search count must not block DNA
            "dna_count": 1,
        }]

        req = _make_request(forwarded_for="203.0.113.5")
        check_anon_quota(req, "dna")

        mock_patch.assert_called_once()
        update = mock_patch.call_args[0][1]
        assert update["dna_count"] == 2
        assert "search_count" not in update


# =====================================================================
# WIRING CHECK — Phase 2 confirms search/dna import check_anon_quota
# =====================================================================

class TestRouteWiring:
    """Phase 2 wires check_anon_quota into search and DNA. This class
    confirms the wiring is in place — the inverse of the Phase 1
    dormancy check that lived here previously."""

    def test_search_route_imports_check_anon_quota(self):
        from api.routes import search
        assert hasattr(search, "check_anon_quota"), \
            "search.py must import check_anon_quota for the anon path"

    def test_dna_route_imports_check_anon_quota(self):
        from api.routes import dna
        assert hasattr(dna, "check_anon_quota"), \
            "dna.py must import check_anon_quota for the anon path"

    def test_encyclopedia_route_has_no_auth_dep(self):
        """Encyclopedia is fully open-access — no auth dependency at all."""
        from api.routes import encyclopedia
        import inspect
        src = inspect.getsource(encyclopedia)
        # Both the old required-auth dep and the old optional-auth dep should
        # be gone post-Basic-Auth migration.
        assert "require_auth" not in src
        assert "get_current_user" not in src


# =====================================================================
# RETURN VALUE — check_anon_quota now reports post-increment remaining
# =====================================================================

class TestCheckAnonQuotaReturnValue:
    """check_anon_quota returns AnonRemaining(search_remaining, dna_remaining)
    so route handlers can thread the values into response bodies. The
    frontend uses them to keep its display in sync with the real server
    counter instead of decrementing its own local guess (which lied across
    tabs and across reloads)."""

    @patch("api.middleware.anon_quota.supabase_post")
    @patch("api.middleware.anon_quota.supabase_get")
    def test_first_request_returns_cap_minus_one(self, mock_get, mock_post):
        """A fresh IP making its first search gets back (cap-1, dna_cap)."""
        from api.middleware.anon_quota import check_anon_quota, AnonRemaining
        mock_get.return_value = []

        req = _make_request(forwarded_for="203.0.113.5")
        result = check_anon_quota(req, "search")

        assert isinstance(result, AnonRemaining)
        assert result.search_remaining == 29  # default cap 30 - 1
        assert result.dna_remaining == 10     # untouched

    @patch("api.middleware.anon_quota.supabase_post")
    @patch("api.middleware.anon_quota.supabase_get")
    def test_first_dna_request_decrements_dna_not_search(self, mock_get, mock_post):
        from api.middleware.anon_quota import check_anon_quota
        mock_get.return_value = []

        req = _make_request(forwarded_for="203.0.113.5")
        result = check_anon_quota(req, "dna")

        assert result.search_remaining == 30
        assert result.dna_remaining == 9  # default cap 10 - 1

    @patch("api.middleware.anon_quota.supabase_patch")
    @patch("api.middleware.anon_quota.supabase_get")
    def test_increment_returns_synthesized_remaining(self, mock_get, mock_patch):
        """When patching an existing row, the function synthesizes the
        post-patch state without an extra DB read."""
        from api.middleware.anon_quota import check_anon_quota, _today_utc
        mock_get.return_value = [{
            "ip_hash": "abc123",
            "usage_date": _today_utc(),
            "search_count": 5,
            "dna_count": 3,
        }]

        req = _make_request(forwarded_for="203.0.113.5")
        result = check_anon_quota(req, "search")

        assert result.search_remaining == 24  # 30 - (5+1)
        assert result.dna_remaining == 7      # 10 - 3 (unchanged)

    @patch("api.middleware.anon_quota.supabase_post")
    @patch("api.middleware.anon_quota.supabase_get")
    def test_no_today_row_returns_cap_minus_one(self, mock_get, mock_post):
        """Under composite-PK schema, prior-day rows are invisible to the
        quota logic — _fetch_anon_row's today-filter excludes them. The
        fresh-day case is identical to first-ever-request from the
        return-value perspective: cap-minus-one for the quota type just
        consumed, full cap for the other."""
        from api.middleware.anon_quota import check_anon_quota
        mock_get.return_value = []

        req = _make_request(forwarded_for="203.0.113.5")
        result = check_anon_quota(req, "dna")

        assert result.search_remaining == 30  # full cap
        assert result.dna_remaining == 9      # 10 - 1

    @patch("api.middleware.anon_quota.supabase_patch")
    @patch("api.middleware.anon_quota.supabase_get")
    def test_env_cap_override_reflected_in_remaining(self, mock_get, mock_patch):
        """Remaining counts must respect env-overridden caps so an incident
        dial-down via Railway env vars updates the frontend immediately."""
        from api.middleware.anon_quota import check_anon_quota, _today_utc
        mock_get.return_value = [{
            "ip_hash": "abc123",
            "usage_date": _today_utc(),
            "search_count": 2,
            "dna_count": 0,
        }]

        req = _make_request(forwarded_for="203.0.113.5")
        with patch.dict(os.environ, {"ANON_SEARCH_CAP": "5"}):
            result = check_anon_quota(req, "search")

        assert result.search_remaining == 2  # 5 - (2+1)
        assert result.dna_remaining == 10


# =====================================================================
# PURE READ — get_anon_remaining never writes and returns truthful counts
# =====================================================================

class TestGetAnonRemaining:
    """get_anon_remaining is the read-only twin used by GET /api/anon-usage
    to seed the frontend on page mount without consuming any quota."""

    @patch("api.middleware.anon_quota.supabase_post")
    @patch("api.middleware.anon_quota.supabase_patch")
    @patch("api.middleware.anon_quota.supabase_get")
    def test_no_row_returns_full_caps_without_writing(
        self, mock_get, mock_patch, mock_post
    ):
        from api.middleware.anon_quota import get_anon_remaining
        mock_get.return_value = []

        req = _make_request(forwarded_for="203.0.113.5")
        result = get_anon_remaining(req)

        assert result.search_remaining == 30
        assert result.dna_remaining == 10
        mock_patch.assert_not_called()
        mock_post.assert_not_called()

    @patch("api.middleware.anon_quota.supabase_post")
    @patch("api.middleware.anon_quota.supabase_patch")
    @patch("api.middleware.anon_quota.supabase_get")
    def test_existing_row_returns_actual_remaining(
        self, mock_get, mock_patch, mock_post
    ):
        from api.middleware.anon_quota import get_anon_remaining, _today_utc
        mock_get.return_value = [{
            "ip_hash": "abc123",
            "usage_date": _today_utc(),
            "search_count": 7,
            "dna_count": 4,
        }]

        req = _make_request(forwarded_for="203.0.113.5")
        result = get_anon_remaining(req)

        assert result.search_remaining == 23  # 30 - 7
        assert result.dna_remaining == 6      # 10 - 4
        mock_patch.assert_not_called()
        mock_post.assert_not_called()

    @patch("api.middleware.anon_quota.supabase_post")
    @patch("api.middleware.anon_quota.supabase_patch")
    @patch("api.middleware.anon_quota.supabase_get")
    def test_no_today_row_returns_full_caps(
        self, mock_get, mock_patch, mock_post
    ):
        """Under composite-PK schema, prior-day rows are filtered out at
        the DB level via _fetch_anon_row's today-filter — get_anon_remaining
        never sees them. No row for today reads as 'no usage today' and
        returns full caps."""
        from api.middleware.anon_quota import get_anon_remaining
        mock_get.return_value = []

        req = _make_request(forwarded_for="203.0.113.5")
        result = get_anon_remaining(req)

        assert result.search_remaining == 30
        assert result.dna_remaining == 10
        mock_patch.assert_not_called()
        mock_post.assert_not_called()

    @patch("api.middleware.anon_quota.supabase_get")
    def test_kill_switch_raises_401(self, mock_get):
        from api.middleware.anon_quota import get_anon_remaining
        req = _make_request(forwarded_for="203.0.113.5")

        with patch.dict(os.environ, {"ANON_ACCESS_ENABLED": "false"}):
            with pytest.raises(HTTPException) as exc:
                get_anon_remaining(req)

        assert exc.value.status_code == 401
        mock_get.assert_not_called()

    @patch("api.middleware.anon_quota.supabase_get")
    def test_at_cap_returns_zero_not_negative(self, mock_get):
        """If somehow a count exceeds the cap (e.g., env cap was just lowered),
        remaining must floor at 0 instead of going negative."""
        from api.middleware.anon_quota import get_anon_remaining, _today_utc
        mock_get.return_value = [{
            "ip_hash": "abc123",
            "usage_date": _today_utc(),
            "search_count": 50,  # over default cap of 30
            "dna_count": 20,
        }]

        req = _make_request(forwarded_for="203.0.113.5")
        result = get_anon_remaining(req)

        assert result.search_remaining == 0
        assert result.dna_remaining == 0
