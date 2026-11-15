"""
MODULE: analysis/journal_reader.py
FUNCTION: Source-agnostic reader for the search journal. Returns dicts in
         the JSONL-file shape regardless of whether the underlying data
         came from `data/search_journal.jsonl` or the Supabase
         `search_journal` table.

The point of the adapter is so downstream tools
(analyze_confidence_distribution.py, future Initiative 4 analytics
panels) don't care which source the data comes from. Same field names,
same nesting, same types.

Source selection:
  - Explicit `source=` param overrides everything.
  - Else: env var JOURNAL_SOURCE = 'file' | 'supabase'.
  - Default 'file' for backward compat with dev sessions that don't
    have Supabase env vars set.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

# --- BULLETPROOF PATH ANCHORING ---
SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
if os.path.basename(SCRIPT_DIR) in ("tools", "analysis", "src", "tests"):
    ROOT_DIR = os.path.dirname(SCRIPT_DIR)
else:
    ROOT_DIR = SCRIPT_DIR

if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from api.supabase_client import supabase_get  # noqa: E402

DEFAULT_JOURNAL_PATH = Path(ROOT_DIR) / "data" / "search_journal.jsonl"
TABLE = "search_journal"
PAGE_SIZE = 1000  # Supabase default max per GET


def _resolve_source(source: str | None) -> str:
    """Explicit param > env var > 'file' default."""
    if source:
        return source
    env = os.environ.get("JOURNAL_SOURCE", "").strip().lower()
    if env in ("file", "supabase"):
        return env
    return "file"


def _translate_supabase_row(row: dict) -> dict:
    """
    Map a column-shape Supabase row back to the JSONL `timestamp` shape
    downstream tools expect. Materialized columns (rerank_type,
    mal_rating_max, results_count, top_match_confidence) are left in
    place — they don't conflict with the nested JSONB fields and let
    new code read them directly when the materialization is more
    convenient than digging into JSONB.
    """
    out = dict(row)
    out.pop("created_at", None)
    out.pop("id", None)
    if "event_at" in out:
        out["timestamp"] = out.pop("event_at")
    return out


def _read_from_file(
    file_path: Path,
    since: datetime | None,
    mode: str | None,
    intent: str | None,
    limit: int | None,
) -> list[dict]:
    if not file_path.exists():
        return []
    entries: list[dict] = []
    with file_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if mode and entry.get("mode") != mode:
                continue
            if intent and entry.get("intent") != intent:
                continue
            if since:
                ts = entry.get("timestamp", "")
                try:
                    entry_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except Exception:
                    continue
                if entry_dt < since:
                    continue
            entries.append(entry)
            if limit and len(entries) >= limit:
                break
    return entries


def _read_from_supabase(
    since: datetime | None,
    mode: str | None,
    intent: str | None,
    limit: int | None,
) -> list[dict]:
    entries: list[dict] = []
    offset = 0
    remaining = limit
    while True:
        page = PAGE_SIZE if remaining is None else min(PAGE_SIZE, remaining)
        if page <= 0:
            break
        params = {
            "select": "*",
            "order": "event_at.asc",
            "limit": str(page),
            "offset": str(offset),
        }
        if since:
            params["event_at"] = f"gte.{since.isoformat()}"
        if mode:
            params["mode"] = f"eq.{mode}"
        if intent:
            params["intent"] = f"eq.{intent}"
        rows = supabase_get(TABLE, params=params)
        if not rows:
            break
        for r in rows:
            entries.append(_translate_supabase_row(r))
        if remaining is not None:
            remaining -= len(rows)
            if remaining <= 0:
                break
        if len(rows) < page:
            break
        offset += page
    # Defensive: if Supabase returned more than the requested `limit`
    # (paginated overshoot, or a backend that ignores limit), enforce it
    # at the boundary.
    if limit is not None and len(entries) > limit:
        entries = entries[:limit]
    return entries


def read_entries(
    since: datetime | None = None,
    mode: str | None = None,
    intent: str | None = None,
    limit: int | None = None,
    source: str | None = None,
    file_path: Path | None = None,
) -> list[dict]:
    """
    Source-agnostic journal read. Returns a list of dicts matching the
    JSONL file shape regardless of underlying source.

    Args:
        since: Filter to entries at or after this datetime.
        mode: 'find_a_show' or 'shows_like_this' (server-side filter).
        intent: 'vibe', 'genre_combo', 'narrative', 'hidden_gems', etc.
        limit: Maximum number of entries to return.
        source: 'file' | 'supabase'. Defaults to JOURNAL_SOURCE env var,
            then 'file'.
        file_path: Only used when source='file'; defaults to
            data/search_journal.jsonl at the project root.
    """
    resolved = _resolve_source(source)
    if resolved == "supabase":
        return _read_from_supabase(since=since, mode=mode, intent=intent, limit=limit)
    return _read_from_file(
        file_path=file_path or DEFAULT_JOURNAL_PATH,
        since=since,
        mode=mode,
        intent=intent,
        limit=limit,
    )
