"""
Unit tests for analysis/telemetry_logger.py.

Covers the Phase B / Phase C surface added in Initiative 2:
  - _build_search_journal_payload: column-shape translation for all
    journal entry variants, including the materialized columns and
    the intent → rerank_type derivation for pre-PR-#62 shapes.
  - _strip_null_bytes: recursive sanitization that Phase C added after
    Supabase rejected ~0.8% of historical entries with 22P05 errors.
  - log_search_journal: dual-write semantics — file write always runs,
    Supabase write skipped under TESTING=1 or missing env vars,
    Supabase failures don't break the file write.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from analysis.telemetry_logger import (
    _INTENT_TO_RERANK_TYPE,
    _build_search_journal_payload,
    _strip_null_bytes,
    log_search_journal,
)


class _SyncThread:
    """Thread shim that runs target() inline so tests can assert post-state."""

    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        if self._target:
            self._target()


# ---------------------------------------------------------------------
# _build_search_journal_payload
# ---------------------------------------------------------------------

class TestBuildPayload:

    def test_fas_with_timings_materializes_rerank_type_from_timings(self):
        entry = {
            "timestamp": "2026-05-15T12:00:00+00:00",
            "mode": "find_a_show",
            "success": True,
            "query": "dark psychological thriller",
            "lens": "Intelligent Search",
            "intent": "vibe",
            "era": "All Eras",
            "search_id": "abcd-1234",
            "results": [
                {"title": "Psycho-Pass", "confidence": 95},
                {"title": "Monster", "confidence": 92},
            ],
            "timings": {"rerank_type": "fusion", "rerank_ms": 1200, "total_ms": 1500},
            "filters_applied": {"year_min": 2010},
        }
        p = _build_search_journal_payload(entry)
        assert p["mode"] == "find_a_show"
        assert p["event_at"] == "2026-05-15T12:00:00+00:00"
        assert p["query"] == "dark psychological thriller"
        assert p["intent"] == "vibe"
        # timings.rerank_type takes precedence over intent derivation
        assert p["rerank_type"] == "fusion"
        assert p["results_count"] == 2
        assert p["top_match_confidence"] == 95
        # JSONB round-trips unchanged
        assert p["results"][0]["title"] == "Psycho-Pass"
        assert p["timings"]["rerank_ms"] == 1200

    def test_fas_pre_pr62_derives_rerank_type_from_intent(self):
        """Older entries lack timings; intent maps to rerank_type."""
        entry = {
            "timestamp": "2026-03-20T00:00:00+00:00",
            "mode": "find_a_show",
            "success": True,
            "query": "underrated mecha",
            "intent": "hidden_gems",
            "results": [{"title": "Zegapain", "confidence": 88}],
        }
        p = _build_search_journal_payload(entry)
        assert p["rerank_type"] == _INTENT_TO_RERANK_TYPE["hidden_gems"] == "constrained"
        assert p["timings"] is None

    def test_fas_pre_pr62_no_intent_yields_null_rerank_type(self):
        """Pre-intent-classifier entries have no intent; rerank_type stays NULL."""
        entry = {
            "timestamp": "2026-03-19T00:00:00+00:00",
            "mode": "find_a_show",
            "success": True,
            "query": "gritty mecha drama",
            "results": [{"title": "NGE", "confidence": 95}],
        }
        p = _build_search_journal_payload(entry)
        assert p["rerank_type"] is None
        assert p["intent"] is None

    def test_slt_mode_always_derives_rerank_type_dna(self):
        entry = {
            "timestamp": "2026-05-26T00:00:00+00:00",
            "mode": "shows_like_this",
            "success": True,
            "reference_titles": ["Attack on Titan"],
            "negative_anchors": ["Naruto"],
            "title_resolution_notes": ["Substituted: AoT → Attack on Titan"],
            "results": [{"title": "Berserk", "confidence": 95}],
        }
        p = _build_search_journal_payload(entry)
        assert p["rerank_type"] == "dna"
        assert p["reference_titles"] == ["Attack on Titan"]
        assert p["negative_anchors"] == ["Naruto"]
        assert p["title_resolution_notes"][0].startswith("Substituted:")
        # FaS-only fields are NULL for SLT rows
        assert p["query"] is None
        assert p["intent"] is None

    def test_error_entry_marks_success_false_and_empty_results(self):
        entry = {
            "timestamp": "2026-04-01T00:00:00+00:00",
            "mode": "shows_like_this",
            "success": False,
            "error": "Title not found in vault",
            "reference_titles": ["xxxnotashow"],
        }
        p = _build_search_journal_payload(entry)
        assert p["success"] is False
        assert p["error"] == "Title not found in vault"
        assert p["results_count"] == 0
        assert p["top_match_confidence"] is None

    def test_mal_rating_max_materialized_from_filters(self):
        entry = {
            "timestamp": "2026-05-27T00:00:00+00:00",
            "mode": "find_a_show",
            "success": True,
            "query": "cozy slice of life",
            "intent": "vibe",
            "results": [{"title": "Barakamon", "confidence": 96}],
            "filters_applied": {"mal_rating_max": "PG"},
        }
        p = _build_search_journal_payload(entry)
        assert p["mal_rating_max"] == "PG"


# ---------------------------------------------------------------------
# _strip_null_bytes
# ---------------------------------------------------------------------

class TestStripNullBytes:

    def test_strips_from_top_level_string(self):
        assert _strip_null_bytes("hello\x00world") == "helloworld"

    def test_passes_clean_strings_unchanged(self):
        assert _strip_null_bytes("plain text") == "plain text"

    def test_recurses_into_nested_dict(self):
        out = _strip_null_bytes({"query": "foo\x00bar", "n": 5})
        assert out == {"query": "foobar", "n": 5}

    def test_recurses_into_nested_list(self):
        out = _strip_null_bytes(["clean", "dir\x00ty", 42])
        assert out == ["clean", "dirty", 42]

    def test_handles_payload_with_null_bytes_in_results_jsonb(self):
        """The actual failure mode from Phase C — null bytes inside results JSONB."""
        entry = {
            "timestamp": "2026-05-26T04:30:00+00:00",
            "mode": "find_a_show",
            "success": True,
            "query": "anime\x00with\x00null",
            "intent": "vibe",
            "results": [
                {"title": "Show A\x00", "confidence": 95, "reasoning": "foo\x00bar"},
            ],
        }
        p = _build_search_journal_payload(entry)
        assert "\x00" not in p["query"]
        assert "\x00" not in p["results"][0]["title"]
        assert "\x00" not in p["results"][0]["reasoning"]

    def test_preserves_non_string_types(self):
        assert _strip_null_bytes(None) is None
        assert _strip_null_bytes(42) == 42
        assert _strip_null_bytes(True) is True


# ---------------------------------------------------------------------
# log_search_journal — dual-write
# ---------------------------------------------------------------------

class TestLogSearchJournalDualWrite:
    """Verify the file/Supabase dual-write contract under various env shapes."""

    def setup_method(self):
        # Capture original env so we can restore in teardown
        self._orig_testing = os.environ.get("TESTING")
        self._orig_url = os.environ.get("SUPABASE_URL")
        self._orig_key = os.environ.get("SUPABASE_SERVICE_KEY")

    def teardown_method(self):
        for k, v in (
            ("TESTING", self._orig_testing),
            ("SUPABASE_URL", self._orig_url),
            ("SUPABASE_SERVICE_KEY", self._orig_key),
        ):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _entry(self):
        return {
            "mode": "find_a_show",
            "success": True,
            "query": "test",
            "intent": "vibe",
            "results": [{"title": "X", "confidence": 90}],
        }

    @patch("analysis.telemetry_logger.threading.Thread", _SyncThread)
    @patch("analysis.telemetry_logger.supabase_post")
    @patch("analysis.telemetry_logger.open")
    def test_both_writes_happen_when_configured(self, mock_open, mock_post):
        os.environ.pop("TESTING", None)
        os.environ["SUPABASE_URL"] = "https://test.supabase.co"
        os.environ["SUPABASE_SERVICE_KEY"] = "test-key"
        log_search_journal(self._entry())
        assert mock_open.called, "File write must always run"
        assert mock_post.called, "Supabase write should run when configured"
        assert mock_post.call_args[0][0] == "search_journal"

    @patch("analysis.telemetry_logger.threading.Thread", _SyncThread)
    @patch("analysis.telemetry_logger.supabase_post")
    @patch("analysis.telemetry_logger.open")
    def test_supabase_skipped_under_testing_env(self, mock_open, mock_post):
        os.environ["TESTING"] = "1"
        os.environ["SUPABASE_URL"] = "https://test.supabase.co"
        os.environ["SUPABASE_SERVICE_KEY"] = "test-key"
        log_search_journal(self._entry())
        assert mock_open.called, "File write still runs under TESTING=1"
        assert not mock_post.called, "Supabase write must skip under TESTING=1"

    @patch("analysis.telemetry_logger.threading.Thread", _SyncThread)
    @patch("analysis.telemetry_logger.supabase_post")
    @patch("analysis.telemetry_logger.open")
    def test_supabase_skipped_when_env_vars_missing(self, mock_open, mock_post):
        os.environ.pop("TESTING", None)
        os.environ.pop("SUPABASE_URL", None)
        os.environ.pop("SUPABASE_SERVICE_KEY", None)
        log_search_journal(self._entry())
        assert mock_open.called
        assert not mock_post.called, (
            "Supabase write should skip silently when env vars are not set"
        )

    @patch("analysis.telemetry_logger.threading.Thread", _SyncThread)
    @patch("analysis.telemetry_logger.supabase_post", side_effect=RuntimeError("boom"))
    @patch("analysis.telemetry_logger.open")
    def test_file_write_succeeds_when_supabase_raises(self, mock_open, mock_post):
        """Supabase failure must not break the file write."""
        os.environ.pop("TESTING", None)
        os.environ["SUPABASE_URL"] = "https://test.supabase.co"
        os.environ["SUPABASE_SERVICE_KEY"] = "test-key"
        log_search_journal(self._entry())
        assert mock_open.called, "File write must run even when Supabase raises"
        assert mock_post.called, "Supabase call was attempted"

    @patch("analysis.telemetry_logger.threading.Thread", _SyncThread)
    @patch("analysis.telemetry_logger.supabase_post")
    @patch("analysis.telemetry_logger.open")
    def test_entry_gets_timestamp_stamped_in_place(self, mock_open, mock_post):
        """log_search_journal mutates the entry to add a UTC ISO timestamp."""
        entry = self._entry()
        assert "timestamp" not in entry
        log_search_journal(entry)
        assert "timestamp" in entry
        # Stamp is ISO format with timezone
        assert "T" in entry["timestamp"]
        assert "+00:00" in entry["timestamp"] or entry["timestamp"].endswith("Z")
