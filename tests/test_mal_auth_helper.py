"""
Tests for src/mal_auth_helper.refresh_mal_token — Initiative 3 Phase C.

Covers the Supabase-first read chain and the dual-write (Supabase +
disk) success path. The disk-write best-effort behavior is what makes
Railway's ephemeral writable layer non-fatal in production.

Patch targets reflect the deferred import inside refresh_mal_token —
the helpers come in via `from analysis.mal_token_store import ...`
each call, so we patch at `analysis.mal_token_store.read_token` rather
than at `src.mal_auth_helper.read_token` (which doesn't exist as a
module attribute).
"""
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

TESTS_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from src import mal_auth_helper
from src.mal_auth_helper import refresh_mal_token


def _mock_mal_response(status_code: int, body: dict | None = None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body or {}
    resp.text = json.dumps(body) if body else ""
    return resp


SUCCESS_RESPONSE = {
    "access_token": "new-access-token",
    "refresh_token": "new-refresh-token",
    "token_type": "Bearer",
    "expires_in": 2592000,
}

SUPABASE_ROW = {
    "id": 1,
    "access_token": "supabase-access",
    "refresh_token": "supabase-refresh",
    "token_type": "Bearer",
    "expires_in": 2592000,
    "refreshed_at": "2026-06-06T20:00:00+00:00",
}


# ---------------------------------------------------------------------
# Read chain — refresh_token source
# ---------------------------------------------------------------------

class TestRefreshTokenSource:

    def test_reads_refresh_token_from_supabase_when_present(self, tmp_path):
        """Supabase row → refresh request carries the Supabase refresh_token."""
        with patch(
            "analysis.mal_token_store.read_token", return_value=SUPABASE_ROW
        ), patch(
            "analysis.mal_token_store.write_token", return_value=True
        ), patch.object(
            mal_auth_helper.requests, "post", return_value=_mock_mal_response(200, SUCCESS_RESPONSE)
        ) as mock_post:
            ok = refresh_mal_token(token_path=str(tmp_path / "token_data.json"))

        assert ok is True
        # The POST payload should carry SUPABASE_ROW's refresh_token,
        # not anything else.
        _, kwargs = mock_post.call_args
        assert kwargs["data"]["refresh_token"] == SUPABASE_ROW["refresh_token"]
        assert kwargs["data"]["grant_type"] == "refresh_token"

    def test_falls_back_to_disk_when_supabase_returns_none(self, tmp_path):
        """Supabase miss → reads refresh_token from token_data.json."""
        disk_path = tmp_path / "token_data.json"
        disk_path.write_text(
            json.dumps({"access_token": "old", "refresh_token": "disk-refresh"})
        )

        with patch(
            "analysis.mal_token_store.read_token", return_value=None
        ), patch(
            "analysis.mal_token_store.write_token", return_value=True
        ), patch.object(
            mal_auth_helper.requests, "post", return_value=_mock_mal_response(200, SUCCESS_RESPONSE)
        ) as mock_post:
            ok = refresh_mal_token(token_path=str(disk_path))

        assert ok is True
        _, kwargs = mock_post.call_args
        assert kwargs["data"]["refresh_token"] == "disk-refresh"

    def test_returns_false_when_no_refresh_token_available(self, tmp_path):
        """Supabase miss + no disk file → False, no POST attempt."""
        with patch(
            "analysis.mal_token_store.read_token", return_value=None
        ), patch.object(mal_auth_helper.requests, "post") as mock_post:
            ok = refresh_mal_token(token_path=str(tmp_path / "missing.json"))

        assert ok is False
        mock_post.assert_not_called()


# ---------------------------------------------------------------------
# Dual-write — Supabase primary, disk best-effort
# ---------------------------------------------------------------------

class TestDualWriteOnSuccess:

    def test_writes_to_both_supabase_and_disk_on_success(self, tmp_path):
        """200 OK → write_token called AND disk file updated."""
        disk_path = tmp_path / "token_data.json"
        disk_path.write_text(
            json.dumps({"access_token": "old", "refresh_token": "old-refresh"})
        )

        with patch(
            "analysis.mal_token_store.read_token", return_value=SUPABASE_ROW
        ), patch(
            "analysis.mal_token_store.write_token", return_value=True
        ) as mock_write, patch.object(
            mal_auth_helper.requests, "post", return_value=_mock_mal_response(200, SUCCESS_RESPONSE)
        ):
            refresh_mal_token(token_path=str(disk_path))

        # Supabase write happened with the full MAL response.
        mock_write.assert_called_once_with(SUCCESS_RESPONSE)
        # Disk write also happened (file content reflects the rotation).
        on_disk = json.loads(disk_path.read_text())
        assert on_disk == SUCCESS_RESPONSE

    def test_disk_write_failure_does_not_break_success(self, tmp_path):
        """Railway case: disk OSError on write → still returns True
        because Supabase already holds the new tokens."""
        # Pass a path whose parent doesn't exist so open() raises OSError.
        bad_disk_path = tmp_path / "no_such_dir" / "token_data.json"

        with patch(
            "analysis.mal_token_store.read_token", return_value=SUPABASE_ROW
        ), patch(
            "analysis.mal_token_store.write_token", return_value=True
        ) as mock_write, patch.object(
            mal_auth_helper.requests, "post", return_value=_mock_mal_response(200, SUCCESS_RESPONSE)
        ):
            ok = refresh_mal_token(token_path=str(bad_disk_path))

        # Supabase persisted; disk swallowed the OSError; function reports True.
        assert ok is True
        mock_write.assert_called_once_with(SUCCESS_RESPONSE)
        assert not bad_disk_path.exists()

    def test_supabase_write_failure_still_writes_disk(self, tmp_path):
        """Local-dev / transient-Supabase case: write_token returns False;
        disk write still happens so token_data.json stays usable as the
        fallback source for the next refresh."""
        disk_path = tmp_path / "token_data.json"
        disk_path.write_text(
            json.dumps({"access_token": "old", "refresh_token": "old-refresh"})
        )

        with patch(
            "analysis.mal_token_store.read_token", return_value=SUPABASE_ROW
        ), patch(
            "analysis.mal_token_store.write_token", return_value=False
        ), patch.object(
            mal_auth_helper.requests, "post", return_value=_mock_mal_response(200, SUCCESS_RESPONSE)
        ):
            ok = refresh_mal_token(token_path=str(disk_path))

        # Reported success because at least one durability layer landed.
        assert ok is True
        on_disk = json.loads(disk_path.read_text())
        assert on_disk == SUCCESS_RESPONSE


# ---------------------------------------------------------------------
# Return shape + MAL error handling
# ---------------------------------------------------------------------

class TestReturnShapeAndErrors:

    def test_return_data_true_returns_dict_on_success(self, tmp_path):
        """return_data=True → caller gets the full MAL response dict
        (used by the GitHub Actions self-healing pulse check)."""
        with patch(
            "analysis.mal_token_store.read_token", return_value=SUPABASE_ROW
        ), patch(
            "analysis.mal_token_store.write_token", return_value=True
        ), patch.object(
            mal_auth_helper.requests, "post", return_value=_mock_mal_response(200, SUCCESS_RESPONSE)
        ):
            result = refresh_mal_token(
                token_path=str(tmp_path / "token_data.json"),
                return_data=True,
            )

        assert result == SUCCESS_RESPONSE

    def test_mal_refresh_failure_returns_false(self, tmp_path):
        """Non-200 from MAL → False (or None for return_data=True), no writes."""
        with patch(
            "analysis.mal_token_store.read_token", return_value=SUPABASE_ROW
        ), patch(
            "analysis.mal_token_store.write_token"
        ) as mock_write, patch.object(
            mal_auth_helper.requests,
            "post",
            return_value=_mock_mal_response(401, {"error": "invalid_grant"}),
        ):
            ok = refresh_mal_token(token_path=str(tmp_path / "token_data.json"))
            data = refresh_mal_token(
                token_path=str(tmp_path / "token_data.json"),
                return_data=True,
            )

        assert ok is False
        assert data is None
        mock_write.assert_not_called()
