"""
Phase 5.1 tests: Seasonal automation (COA 2 — manual dispatch workflow).

Covers:
  - Season auto-detection logic (regression + functionality)
  - MAL token pulse_check (functionality)
  - auto_seasonal_sweep orchestration (integration)
  - vault_manager helpers (regression)

Run: pytest tests/test_seasonal_automation.py -v
"""

import os
import sys
import json
import sqlite3
import asyncio
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime

TESTS_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

DB_PATH = os.path.join(ROOT_DIR, "data", "anime_intelligence_v2.db")


# =====================================================================
# SEASON AUTO-DETECTION (Functionality)
# =====================================================================

class TestSeasonDetection:
    """
    Verify that detect_last_season() maps each month to the correct
    most-recently-completed anime season.

    Anime seasons:
      Winter (Jan-Mar), Spring (Apr-Jun), Summer (Jul-Sep), Fall (Oct-Dec)

    Detection logic: "what season JUST ended?"
      Jan-Mar -> Fall of previous year
      Apr-Jun -> Winter of current year
      Jul-Sep -> Spring of current year
      Oct-Dec -> Summer of current year
    """

    def test_january_detects_fall_previous_year(self):
        """January is after Fall ends -> Fall of previous year."""
        from vault_manager import detect_last_season
        season, year = detect_last_season(_now=datetime(2026, 1, 15))
        assert season == "fall"
        assert year == 2025

    def test_february_detects_fall_previous_year(self):
        from vault_manager import detect_last_season
        season, year = detect_last_season(_now=datetime(2026, 2, 10))
        assert season == "fall"
        assert year == 2025

    def test_march_detects_fall_previous_year(self):
        from vault_manager import detect_last_season
        season, year = detect_last_season(_now=datetime(2026, 3, 31))
        assert season == "fall"
        assert year == 2025

    def test_april_detects_winter_current_year(self):
        """April is after Winter ends -> Winter of current year."""
        from vault_manager import detect_last_season
        season, year = detect_last_season(_now=datetime(2026, 4, 5))
        assert season == "winter"
        assert year == 2026

    def test_june_detects_winter_current_year(self):
        from vault_manager import detect_last_season
        season, year = detect_last_season(_now=datetime(2026, 6, 30))
        assert season == "winter"
        assert year == 2026

    def test_july_detects_spring_current_year(self):
        """July is after Spring ends -> Spring of current year."""
        from vault_manager import detect_last_season
        season, year = detect_last_season(_now=datetime(2026, 7, 5))
        assert season == "spring"
        assert year == 2026

    def test_september_detects_spring_current_year(self):
        from vault_manager import detect_last_season
        season, year = detect_last_season(_now=datetime(2026, 9, 15))
        assert season == "spring"
        assert year == 2026

    def test_october_detects_summer_current_year(self):
        """October is after Summer ends -> Summer of current year."""
        from vault_manager import detect_last_season
        season, year = detect_last_season(_now=datetime(2026, 10, 5))
        assert season == "summer"
        assert year == 2026

    def test_december_detects_summer_current_year(self):
        from vault_manager import detect_last_season
        season, year = detect_last_season(_now=datetime(2026, 12, 25))
        assert season == "summer"
        assert year == 2026

    def test_returns_tuple_of_str_and_int(self):
        """Return type must be (str, int) for downstream compatibility."""
        from vault_manager import detect_last_season
        result = detect_last_season()
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], str)
        assert isinstance(result[1], int)

    def test_season_is_valid_value(self):
        """Season must be one of the four valid MAL season names."""
        from vault_manager import detect_last_season
        season, _ = detect_last_season()
        assert season in ("winter", "spring", "summer", "fall")

    def test_year_is_reasonable(self):
        """Year must be within a reasonable range (not a future year, not ancient)."""
        from vault_manager import detect_last_season
        _, year = detect_last_season()
        current_year = datetime.now().year
        assert current_year - 1 <= year <= current_year


# =====================================================================
# MAL TOKEN MANAGEMENT (Functionality)
# =====================================================================

class TestPulseCheck:
    """
    Verify pulse_check() — a lightweight MAL API call to validate
    that the current access token is still valid.
    """

    @patch("src.mal_auth_helper.requests.get")
    def test_pulse_check_returns_true_on_200(self, mock_get):
        """A 200 response from MAL means the token is valid."""
        from src.mal_auth_helper import pulse_check

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": 12345, "name": "testuser"}
        mock_get.return_value = mock_response

        assert pulse_check("fake_access_token") is True

    @patch("src.mal_auth_helper.requests.get")
    def test_pulse_check_returns_false_on_401(self, mock_get):
        """A 401 response means the token is expired/invalid."""
        from src.mal_auth_helper import pulse_check

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_get.return_value = mock_response

        assert pulse_check("expired_token") is False

    @patch("src.mal_auth_helper.requests.get")
    def test_pulse_check_returns_false_on_403(self, mock_get):
        """A 403 response means the token is invalid."""
        from src.mal_auth_helper import pulse_check

        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_get.return_value = mock_response

        assert pulse_check("bad_token") is False

    @patch("src.mal_auth_helper.requests.get")
    def test_pulse_check_returns_false_on_network_error(self, mock_get):
        """Network errors should return False, not raise."""
        from src.mal_auth_helper import pulse_check

        mock_get.side_effect = Exception("Connection timeout")

        assert pulse_check("any_token") is False

    @patch("src.mal_auth_helper.requests.get")
    def test_pulse_check_hits_correct_endpoint(self, mock_get):
        """pulse_check should call the MAL users/@me endpoint."""
        from src.mal_auth_helper import pulse_check

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": 1}
        mock_get.return_value = mock_response

        pulse_check("test_token")

        mock_get.assert_called_once()
        call_url = mock_get.call_args[0][0]
        assert "myanimelist.net" in call_url
        assert "users" in call_url or "@me" in call_url


class TestRefreshTokenEnhanced:
    """
    Verify the enhanced refresh_mal_token() that can return token data
    for GitHub Actions secret write-back.

    Note: Initiative 3 (PR #82) added a Supabase dual-write inside
    refresh_mal_token. These tests patch analysis.mal_token_store.read_token
    (force disk-fallback path) and analysis.mal_token_store.write_token
    (prevent test-mocked MAL responses from polluting the real Supabase
    mal_oauth_token singleton row) so they exercise the disk-write
    behavior in isolation.
    """

    @patch("analysis.mal_token_store.write_token", return_value=True)
    @patch("analysis.mal_token_store.read_token", return_value=None)
    @patch("src.mal_auth_helper.requests.post")
    def test_refresh_returns_token_data_when_requested(
        self, mock_post, mock_read, mock_write
    ):
        """When return_data=True, refresh should return the new token dict."""
        from src.mal_auth_helper import refresh_mal_token

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "new_access",
            "refresh_token": "new_refresh",
            "token_type": "Bearer",
            "expires_in": 2415600,
        }
        mock_post.return_value = mock_response

        # Create a temporary token file
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump({"refresh_token": "old_refresh"}, f)
            temp_path = f.name

        try:
            result = refresh_mal_token(token_path=temp_path, return_data=True)
            assert result is not None
            assert result["access_token"] == "new_access"
            assert result["refresh_token"] == "new_refresh"
            # Phase C dual-write: confirm the mocked MAL response would
            # have been written to the (mocked) Supabase store.
            mock_write.assert_called_once_with(mock_response.json.return_value)
        finally:
            os.unlink(temp_path)

    @patch("analysis.mal_token_store.write_token")
    @patch("analysis.mal_token_store.read_token", return_value=None)
    @patch("src.mal_auth_helper.requests.post")
    def test_refresh_returns_none_on_failure(
        self, mock_post, mock_read, mock_write
    ):
        """When the refresh API call fails, return None (not raise)."""
        from src.mal_auth_helper import refresh_mal_token

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "invalid_grant"
        mock_post.return_value = mock_response

        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump({"refresh_token": "expired_refresh"}, f)
            temp_path = f.name

        try:
            result = refresh_mal_token(token_path=temp_path, return_data=True)
            assert result is None
            # Non-200 path bails before the Supabase write fires.
            mock_write.assert_not_called()
        finally:
            os.unlink(temp_path)


# =====================================================================
# AUTO SEASONAL SWEEP — ORCHESTRATION (Integration)
# =====================================================================

class TestAutoSeasonalSweep:
    """
    Integration tests for auto_seasonal_sweep() — the non-interactive
    entry point that GitHub Actions will call.
    """

    def test_function_exists_and_is_async(self):
        """auto_seasonal_sweep must be an async function."""
        from vault_manager import auto_seasonal_sweep
        import asyncio
        assert asyncio.iscoroutinefunction(auto_seasonal_sweep)

    def test_accepts_override_params(self):
        """auto_seasonal_sweep must accept optional season/year overrides."""
        from vault_manager import auto_seasonal_sweep
        import inspect
        sig = inspect.signature(auto_seasonal_sweep)
        params = list(sig.parameters.keys())
        assert "season" in params, "Must accept 'season' parameter for workflow override"
        assert "year" in params, "Must accept 'year' parameter for workflow override"

    def test_override_params_are_optional(self):
        """season and year params must have defaults (for auto-detection mode)."""
        from vault_manager import auto_seasonal_sweep
        import inspect
        sig = inspect.signature(auto_seasonal_sweep)
        for name in ("season", "year"):
            param = sig.parameters[name]
            assert param.default is not inspect.Parameter.empty, (
                f"'{name}' must have a default value for auto-detection mode"
            )

    @patch("vault_manager.sync_faiss_index")
    @patch("vault_manager.execute_self_healing_campaign", new_callable=AsyncMock)
    @patch("vault_manager.detect_last_season", return_value=("winter", 2026))
    def test_auto_detect_calls_campaign_with_detected_season(
        self, mock_detect, mock_campaign, mock_faiss
    ):
        """When no overrides given, uses detect_last_season() result."""
        from vault_manager import auto_seasonal_sweep
        asyncio.run(auto_seasonal_sweep())
        mock_detect.assert_called_once()
        # Campaign should be called with the detected season
        mock_campaign.assert_called_once()
        call_args = mock_campaign.call_args
        targets = call_args[0][0]  # First positional arg
        assert targets[0]["season"] == "winter"
        assert targets[0]["year"] == 2026

    @patch("vault_manager.sync_faiss_index")
    @patch("vault_manager.execute_self_healing_campaign", new_callable=AsyncMock)
    def test_override_skips_auto_detection(self, mock_campaign, mock_faiss):
        """When season/year explicitly provided, skip auto-detection."""
        from vault_manager import auto_seasonal_sweep
        asyncio.run(auto_seasonal_sweep(season="fall", year=2025))
        mock_campaign.assert_called_once()
        targets = mock_campaign.call_args[0][0]
        assert targets[0]["season"] == "fall"
        assert targets[0]["year"] == 2025

    @patch("vault_manager.sync_faiss_index")
    @patch("vault_manager.execute_self_healing_campaign", new_callable=AsyncMock)
    def test_returns_ingested_season_info(self, mock_campaign, mock_faiss):
        """auto_seasonal_sweep should return what was ingested for logging."""
        from vault_manager import auto_seasonal_sweep
        result = asyncio.run(auto_seasonal_sweep(season="spring", year=2026))
        assert result is not None
        assert result["season"] == "spring"
        assert result["year"] == 2026


# =====================================================================
# REGRESSION: EXISTING VAULT MANAGER FUNCTIONS
# =====================================================================

class TestVaultManagerRegression:
    """
    Ensure existing functions still work after adding automation.
    These guard against breaking the manual workflow.
    """

    def test_run_seasonal_audit_returns_list(self):
        """run_seasonal_audit must return a list (possibly empty)."""
        from vault_manager import run_seasonal_audit
        result = run_seasonal_audit(2025, "winter")
        assert isinstance(result, list)

    def test_sync_faiss_index_handles_empty_list(self):
        """sync_faiss_index with empty list should exit cleanly."""
        from vault_manager import sync_faiss_index
        # Should not raise
        sync_faiss_index([])

    def test_db_path_is_correct(self):
        """DB_PATH in vault_manager must point to the real database."""
        from vault_manager import DB_PATH as vm_db_path
        assert os.path.exists(vm_db_path), f"DB not found at {vm_db_path}"

    def test_execute_self_healing_campaign_is_async(self):
        """execute_self_healing_campaign must remain async."""
        from vault_manager import execute_self_healing_campaign
        import asyncio
        assert asyncio.iscoroutinefunction(execute_self_healing_campaign)

    def test_database_has_expected_schema(self):
        """Core columns must exist — guards against schema drift."""
        required_columns = {
            "id", "english_title", "mal_score", "season", "scored_by",
            "studio", "avg_sentiment", "consensus_json", "mal_synopsis",
            "release_year",
        }
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(anime_info)")
            actual_columns = {row[1] for row in cursor.fetchall()}

        missing = required_columns - actual_columns
        assert not missing, f"Missing columns: {missing}"

    def test_database_has_data(self):
        """Database should not be empty — sanity check."""
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM anime_info")
            count = cursor.fetchone()[0]
        assert count > 5000, f"Expected 5000+ rows, got {count}"


