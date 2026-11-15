"""
Unit tests for analysis/journal_reader.py — the Initiative 2 Phase D
source-agnostic read adapter.

Covers:
  - _resolve_source priority: explicit param > JOURNAL_SOURCE env var
    > 'file' default.
  - _translate_supabase_row column-shape → JSONL-shape translation.
  - _read_from_file filtering (mode, intent, since, limit) against a
    temp JSONL fixture.
  - _read_from_supabase mocked: query params, pagination loop, limit
    short-circuit.
  - read_entries dispatch on source.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from analysis.journal_reader import (
    PAGE_SIZE,
    _read_from_file,
    _read_from_supabase,
    _resolve_source,
    _translate_supabase_row,
    read_entries,
)


# ---------------------------------------------------------------------
# _resolve_source
# ---------------------------------------------------------------------

class TestResolveSource:

    def setup_method(self):
        self._orig = os.environ.get("JOURNAL_SOURCE")

    def teardown_method(self):
        if self._orig is None:
            os.environ.pop("JOURNAL_SOURCE", None)
        else:
            os.environ["JOURNAL_SOURCE"] = self._orig

    def test_explicit_param_wins_over_env(self):
        os.environ["JOURNAL_SOURCE"] = "supabase"
        assert _resolve_source("file") == "file"

    def test_env_var_wins_over_default(self):
        os.environ["JOURNAL_SOURCE"] = "supabase"
        assert _resolve_source(None) == "supabase"

    def test_default_is_file(self):
        os.environ.pop("JOURNAL_SOURCE", None)
        assert _resolve_source(None) == "file"

    def test_unknown_env_value_falls_back_to_default(self):
        os.environ["JOURNAL_SOURCE"] = "kafka"
        assert _resolve_source(None) == "file"

    def test_env_var_is_case_insensitive(self):
        os.environ["JOURNAL_SOURCE"] = "SUPABASE"
        assert _resolve_source(None) == "supabase"


# ---------------------------------------------------------------------
# _translate_supabase_row
# ---------------------------------------------------------------------

class TestTranslateSupabaseRow:

    def test_event_at_renamed_to_timestamp(self):
        row = {"event_at": "2026-06-02T00:00:00+00:00", "mode": "find_a_show"}
        out = _translate_supabase_row(row)
        assert out["timestamp"] == "2026-06-02T00:00:00+00:00"
        assert "event_at" not in out

    def test_storage_columns_dropped(self):
        row = {"id": 42, "created_at": "...", "event_at": "...", "mode": "x"}
        out = _translate_supabase_row(row)
        assert "id" not in out
        assert "created_at" not in out

    def test_materialized_columns_kept_in_place(self):
        """Materialized rerank_type and friends survive the translation."""
        row = {
            "event_at": "2026-06-02T00:00:00+00:00",
            "mode": "find_a_show",
            "rerank_type": "fusion",
            "mal_rating_max": "PG",
            "results_count": 10,
            "top_match_confidence": 95,
        }
        out = _translate_supabase_row(row)
        for k in ("rerank_type", "mal_rating_max", "results_count", "top_match_confidence"):
            assert k in out

    def test_jsonb_fields_preserved_unchanged(self):
        row = {
            "event_at": "2026-06-02T00:00:00+00:00",
            "mode": "find_a_show",
            "results": [{"title": "X", "confidence": 95}],
            "timings": {"rerank_type": "fusion", "rerank_ms": 1200},
        }
        out = _translate_supabase_row(row)
        assert out["results"][0]["title"] == "X"
        assert out["timings"]["rerank_ms"] == 1200


# ---------------------------------------------------------------------
# _read_from_file
# ---------------------------------------------------------------------

def _write_journal(path: Path, entries: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


@pytest.fixture
def temp_journal(tmp_path):
    path = tmp_path / "journal.jsonl"
    entries = [
        {"mode": "find_a_show", "intent": "vibe",
         "timestamp": "2026-01-01T00:00:00+00:00", "query": "a"},
        {"mode": "find_a_show", "intent": "narrative",
         "timestamp": "2026-02-01T00:00:00+00:00", "query": "b"},
        {"mode": "shows_like_this", "intent": None,
         "timestamp": "2026-03-01T00:00:00+00:00", "reference_titles": ["X"]},
        {"mode": "find_a_show", "intent": "vibe",
         "timestamp": "2026-04-01T00:00:00+00:00", "query": "c"},
    ]
    _write_journal(path, entries)
    return path


class TestReadFromFile:

    def test_returns_all_entries_with_no_filter(self, temp_journal):
        rows = _read_from_file(temp_journal, since=None, mode=None,
                               intent=None, limit=None)
        assert len(rows) == 4

    def test_filters_by_mode(self, temp_journal):
        rows = _read_from_file(temp_journal, since=None, mode="find_a_show",
                               intent=None, limit=None)
        assert len(rows) == 3
        assert all(r["mode"] == "find_a_show" for r in rows)

    def test_filters_by_intent(self, temp_journal):
        rows = _read_from_file(temp_journal, since=None, mode=None,
                               intent="vibe", limit=None)
        assert len(rows) == 2

    def test_filters_by_since(self, temp_journal):
        rows = _read_from_file(
            temp_journal,
            since=datetime(2026, 2, 15, tzinfo=timezone.utc),
            mode=None, intent=None, limit=None,
        )
        assert len(rows) == 2
        assert rows[0]["timestamp"].startswith("2026-03")

    def test_limit_caps_result_count(self, temp_journal):
        rows = _read_from_file(temp_journal, since=None, mode=None,
                               intent=None, limit=2)
        assert len(rows) == 2

    def test_missing_file_returns_empty_list(self, tmp_path):
        rows = _read_from_file(tmp_path / "nope.jsonl", since=None,
                               mode=None, intent=None, limit=None)
        assert rows == []

    def test_malformed_line_skipped(self, tmp_path):
        path = tmp_path / "journal.jsonl"
        with path.open("w", encoding="utf-8") as f:
            f.write('{"mode": "find_a_show", "timestamp": "2026-01-01T00:00:00+00:00"}\n')
            f.write("not valid json\n")
            f.write('{"mode": "find_a_show", "timestamp": "2026-01-02T00:00:00+00:00"}\n')
        rows = _read_from_file(path, since=None, mode=None, intent=None, limit=None)
        assert len(rows) == 2


# ---------------------------------------------------------------------
# _read_from_supabase — pagination + filters
# ---------------------------------------------------------------------

class TestReadFromSupabase:

    @patch("analysis.journal_reader.supabase_get")
    def test_passes_filter_params_to_supabase_get(self, mock_get):
        mock_get.return_value = []
        _read_from_supabase(
            since=datetime(2026, 6, 1, tzinfo=timezone.utc),
            mode="find_a_show",
            intent="vibe",
            limit=None,
        )
        params = mock_get.call_args.kwargs["params"]
        assert params["mode"] == "eq.find_a_show"
        assert params["intent"] == "eq.vibe"
        assert params["event_at"] == "gte.2026-06-01T00:00:00+00:00"
        assert params["order"] == "event_at.asc"

    @patch("analysis.journal_reader.supabase_get")
    def test_paginates_until_short_page(self, mock_get):
        """
        Walks multiple pages, stops when a page comes back shorter than
        PAGE_SIZE. The short-page short-circuit avoids one redundant
        empty request — only two calls happen here, not three.
        """
        page1 = [{"event_at": f"2026-01-{i:02d}T00:00:00+00:00", "mode": "find_a_show"}
                 for i in range(1, PAGE_SIZE + 1)]
        page2 = [{"event_at": "2026-02-01T00:00:00+00:00", "mode": "find_a_show"}]
        mock_get.side_effect = [page1, page2, []]
        rows = _read_from_supabase(since=None, mode=None, intent=None, limit=None)
        assert len(rows) == PAGE_SIZE + 1
        assert mock_get.call_count == 2

    @patch("analysis.journal_reader.supabase_get")
    def test_limit_short_circuits_before_full_pagination(self, mock_get):
        mock_get.return_value = [
            {"event_at": f"2026-01-{i:02d}T00:00:00+00:00", "mode": "find_a_show"}
            for i in range(1, 11)
        ]
        rows = _read_from_supabase(since=None, mode=None, intent=None, limit=5)
        assert len(rows) <= 5
        # First call should request only 5 entries via limit param
        first_call = mock_get.call_args_list[0]
        assert first_call.kwargs["params"]["limit"] == "5"

    @patch("analysis.journal_reader.supabase_get")
    def test_event_at_renamed_to_timestamp_in_output(self, mock_get):
        mock_get.side_effect = [
            [{"event_at": "2026-06-02T00:00:00+00:00", "mode": "find_a_show",
              "id": 7, "created_at": "junk"}],
            [],
        ]
        rows = _read_from_supabase(since=None, mode=None, intent=None, limit=None)
        assert rows[0]["timestamp"] == "2026-06-02T00:00:00+00:00"
        assert "event_at" not in rows[0]
        assert "id" not in rows[0]


# ---------------------------------------------------------------------
# read_entries dispatch
# ---------------------------------------------------------------------

class TestReadEntriesDispatch:

    def test_dispatches_to_file_by_default(self, temp_journal):
        rows = read_entries(file_path=temp_journal, source="file")
        assert len(rows) == 4

    @patch("analysis.journal_reader._read_from_supabase")
    def test_dispatches_to_supabase_when_source_says_so(self, mock_supa):
        mock_supa.return_value = []
        read_entries(source="supabase")
        assert mock_supa.called

    @patch("analysis.journal_reader._read_from_supabase")
    def test_env_var_drives_default(self, mock_supa, monkeypatch):
        mock_supa.return_value = []
        monkeypatch.setenv("JOURNAL_SOURCE", "supabase")
        read_entries(source=None)
        assert mock_supa.called

    def test_explicit_file_path_used_when_provided(self, temp_journal):
        rows = read_entries(source="file", file_path=temp_journal, mode="find_a_show")
        assert len(rows) == 3
