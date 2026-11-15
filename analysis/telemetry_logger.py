"""
MODULE: analysis/telemetry_logger.py
FUNCTION: Silent observer for the Recommendation Engine. Logs prompts,
         pipeline-stage timings, and outcomes to Supabase for offline
         analysis and admin-dashboard surfacing. Also maintains a local
         JSONL search journal for engine tuning that survives only on the
         box that wrote it (Railway's filesystem is ephemeral).

Telemetry destination:
  - Supabase `telemetry_logs` table (see tools/supabase_setup_telemetry.sql)
  - Writes are fire-and-forget on a daemon thread so the search path is
    never blocked by network latency or transient Supabase errors.
"""
import json
import os
import sys
import threading
from datetime import datetime, timezone

# --- BULLETPROOF PATH ANCHORING ---
SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
if os.path.basename(SCRIPT_DIR) in ['tools', 'analysis', 'src', 'tests']:
    ROOT_DIR = os.path.dirname(SCRIPT_DIR)
else:
    ROOT_DIR = SCRIPT_DIR

if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from api.supabase_client import supabase_post

JOURNAL_PATH = os.path.join(ROOT_DIR, "data", "search_journal.jsonl")


def log_engine_execution(prompt, lens, sql, candidate_count, success, error_msg="",
                         recommendations="", mode=None, timings=None):
    """
    Insert a row into Supabase `telemetry_logs` via a background thread.

    Failures are swallowed — the search path must never block on telemetry.
    Errors are printed to stderr for diagnostics.

    Args:
        mode: "find_a_show" | "shows_like_this" | None — drives admin grouping.
        timings: dict with keys retrieval_ms, hydration_ms, rerank_ms,
                 rerank_type, total_ms. Any missing keys write as NULL.
    """
    timings = timings or {}

    payload = {
        "user_prompt": prompt,
        "lens_used": lens,
        "generated_sql": sql,
        "candidate_count": candidate_count,
        "success": success,
        "error_message": error_msg,
        "recommended_titles": recommendations,
        "mode": mode,
        "retrieval_ms": timings.get("retrieval_ms"),
        "hydration_ms": timings.get("hydration_ms"),
        "rerank_ms": timings.get("rerank_ms"),
        "rerank_type": timings.get("rerank_type"),
        "total_ms": timings.get("total_ms"),
    }

    def _send():
        try:
            supabase_post("telemetry_logs", payload)
        except Exception as e:
            # Never let telemetry crash the search path. Print for ops visibility.
            print(f"[telemetry] Supabase insert failed: {type(e).__name__}: {e}", flush=True)

    bg_thread = threading.Thread(target=_send)
    bg_thread.daemon = True
    bg_thread.start()


# Pre-PR-#62 entries lack timings.rerank_type; map intent → rerank_type
# the same way Initiative 1's regression harness does.
_INTENT_TO_RERANK_TYPE = {
    "vibe": "fusion",
    "genre_combo": "fusion",
    "narrative": "narrative",
    "hidden_gems": "constrained",
    "crowd_pleasers": "constrained",
    "polarizing": "constrained",
    "objective": "objective",
}


def _strip_null_bytes(value):
    """
    Recursively strip `\\u0000` from strings within a payload. PostgreSQL
    TEXT and JSONB columns reject null bytes (22P05 error); ~0.8% of
    historical journal entries contained them, surfaced during Phase C's
    backfill. Sanitizing in the payload builder keeps both the dual-write
    and any future backfill safe.
    """
    if isinstance(value, str):
        return value.replace("\x00", "") if "\x00" in value else value
    if isinstance(value, list):
        return [_strip_null_bytes(v) for v in value]
    if isinstance(value, dict):
        return {k: _strip_null_bytes(v) for k, v in value.items()}
    return value


def _build_search_journal_payload(entry: dict) -> dict:
    """
    Translate a raw journal entry into a `search_journal` row payload.

    Materializes the derived columns (rerank_type, mal_rating_max,
    results_count, top_match_confidence) and unpacks the always-present
    scalars into dedicated columns. The remaining semi-structured fields
    land in JSONB.

    Reads `entry["timestamp"]` for `event_at`, which the caller has
    already populated.
    """
    results = entry.get("results") or []
    timings = entry.get("timings") or {}
    filters_applied = entry.get("filters_applied") or {}

    rerank_type = timings.get("rerank_type") if isinstance(timings, dict) else None
    if not rerank_type:
        if entry.get("mode") == "shows_like_this":
            rerank_type = "dna"
        else:
            rerank_type = _INTENT_TO_RERANK_TYPE.get(entry.get("intent"))

    mal_rating_max = None
    if isinstance(filters_applied, dict):
        mal_rating_max = filters_applied.get("mal_rating_max")

    top_confidence = None
    if results and isinstance(results[0], dict):
        # Journal entries use `confidence`; API responses use `match_confidence`.
        # log_search_journal sees the journal-shape entry, so we read `confidence`.
        top_confidence = results[0].get("confidence")

    payload = {
        "event_at": entry.get("timestamp"),
        "mode": entry.get("mode"),
        "success": bool(entry.get("success", True)),
        "error": entry.get("error"),
        "search_id": entry.get("search_id"),
        "retrieval_path": entry.get("retrieval_path"),
        "candidates_retrieved": entry.get("candidates_retrieved"),
        "candidates_after_rerank": entry.get("candidates_after_rerank"),
        "query": entry.get("query"),
        "lens": entry.get("lens"),
        "era": entry.get("era"),
        "intent": entry.get("intent"),
        "intersection_summary": entry.get("intersection_summary"),
        "reference_count": entry.get("reference_count"),
        "excluded_count": entry.get("excluded_count"),
        "negative_anchor_count": entry.get("negative_anchor_count"),
        "prefiltered_count": entry.get("prefiltered_count"),
        # Materialized columns
        "rerank_type": rerank_type,
        "mal_rating_max": mal_rating_max,
        "results_count": len(results),
        "top_match_confidence": top_confidence,
        # JSONB payloads — pass Python lists/dicts; PostgREST handles JSONB.
        "results": results or None,
        "timings": timings or None,
        "filters_applied": filters_applied or None,
        "concepts_detected": entry.get("concepts_detected"),
        "reference_titles": entry.get("reference_titles"),
        "negative_anchors": entry.get("negative_anchors"),
        "title_resolution_notes": entry.get("title_resolution_notes"),
    }
    return _strip_null_bytes(payload)


def log_search_journal(entry):
    """
    Dual-write a search event:
      - Local JSONL file (`data/search_journal.jsonl`) — fast, no network,
        good for in-session inspection during local dev. Ephemeral on
        Railway (container writable layer wiped each deploy).
      - Supabase `search_journal` table — persistent across deploys,
        queryable from the admin dashboard, and the join target for
        Initiative 4's SearchFeedback Analytics.

    Both writes run on independent daemon threads so neither blocks the
    search response path. Failures are printed for ops visibility.

    Supabase write is skipped when TESTING=1 (avoid polluting prod
    analytics from CI/test runs) or when Supabase env vars aren't set
    (e.g., CI without secrets). The file write always runs.
    """
    entry["timestamp"] = datetime.now(timezone.utc).isoformat()

    def _write_file():
        try:
            os.makedirs(os.path.dirname(JOURNAL_PATH), exist_ok=True)
            with open(JOURNAL_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[search_journal] file write failed: {e}", flush=True)

    def _write_supabase():
        # Skip in test mode to keep production analytics clean of test traffic.
        if os.environ.get("TESTING") == "1":
            return
        # Skip silently when Supabase isn't configured (e.g., CI without
        # secrets). supabase_post would raise RuntimeError otherwise.
        if not os.environ.get("SUPABASE_URL") or not os.environ.get("SUPABASE_SERVICE_KEY"):
            return
        try:
            payload = _build_search_journal_payload(entry)
            supabase_post("search_journal", payload)
        except Exception as e:
            print(
                f"[search_journal] Supabase insert failed: "
                f"{type(e).__name__}: {e}",
                flush=True,
            )

    threading.Thread(target=_write_file, daemon=True).start()
    threading.Thread(target=_write_supabase, daemon=True).start()
