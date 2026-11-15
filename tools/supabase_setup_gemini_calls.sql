-- PR #85 Phase C: Create gemini_calls per-call cost ledger.
--
-- Each call through analysis/gemini_client.py writes one row here, via a
-- best-effort daemon thread (same write semantics as PR #76's search
-- journal). The admin dashboard reads aggregated cost from this table
-- grouped by `origin` over a 7-day window for the new "Cost Attribution"
-- panel.
--
-- This script is idempotent: safe to re-run if any step fails partway.
-- The full migration runs inside one transaction; either everything
-- commits or nothing changes.
--
-- Schema rationale:
--   - id BIGSERIAL: auto-increment 8-byte int handles high-volume well.
--   - event_at: when the wrapper logged the call (server-side default).
--   - model: e.g. 'gemini-2.5-flash' or 'gemini-embedding-001' — what
--     the call actually billed against.
--   - caller: qualified function name from stack inspection
--     (e.g. 'RecommendationEngine._rerank_candidates').
--   - origin: one of production / ingestion / dev / test / probe /
--     regression. Drives the dashboard's attribution panel.
--   - input_tokens / output_tokens: real counts from usage_metadata
--     (or estimated for embed_content which has no usage_metadata).
--     Output INCLUDES thoughts_token_count — billed at output rate per
--     Phase A finding.
--   - cost_usd_estimate NUMERIC(10,6): six decimal places — per-call
--     costs are tiny (single cents at most). 9999.999999 max — generous
--     headroom even if a future batch call grows by orders of magnitude.
--   - search_id: soft FK to search_journal.search_id (TEXT match, no
--     hard constraint — distillation/embedding calls have no search_id
--     and that's fine).
--
-- Index rationale:
--   - idx_gemini_calls_event_at: time-range queries for the dashboard's
--     last-7d window.
--   - idx_gemini_calls_origin: composite (origin, event_at) so the
--     dashboard's per-origin aggregation hits a single index scan.

BEGIN;

-- Step 1: Create the table (idempotent).
CREATE TABLE IF NOT EXISTS gemini_calls (
  id                BIGSERIAL    PRIMARY KEY,
  event_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  model             TEXT         NOT NULL,
  caller            TEXT         NOT NULL,
  origin            TEXT         NOT NULL,
  input_tokens      INT          NOT NULL,
  output_tokens     INT          NOT NULL,
  cost_usd_estimate NUMERIC(10,6) NOT NULL,
  search_id         TEXT
);

-- Step 2: Indexes for the dashboard's read patterns.
CREATE INDEX IF NOT EXISTS idx_gemini_calls_event_at
  ON gemini_calls(event_at);

CREATE INDEX IF NOT EXISTS idx_gemini_calls_origin
  ON gemini_calls(origin, event_at);

-- Step 3: RLS enabled, no policies. Service role bypasses; matches the
-- existing security posture across telemetry_logs, search_journal,
-- anon_usage, etc.
ALTER TABLE gemini_calls ENABLE ROW LEVEL SECURITY;

COMMIT;

-- Reload PostgREST schema cache so the new table is visible to the API
-- layer immediately (without this, writes may fail for ~10 minutes
-- while the cache refreshes naturally).
NOTIFY pgrst, 'reload schema';
