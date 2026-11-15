-- =====================================================================
-- tools/supabase_setup_telemetry.sql
--
-- One-time setup: telemetry_logs table + get_pipeline_timings() RPC.
-- Paste into Supabase SQL Editor and run. Idempotent — safe to re-run.
--
-- Replaces the GCP Cloud SQL telemetry pipe. Schema mirrors the columns
-- the application writes via supabase_post() in analysis/telemetry_logger.py
-- and the percentile shape the admin dashboard reads via supabase_rpc().
-- =====================================================================


-- ---------------------------------------------------------------------
-- Table: telemetry_logs
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS telemetry_logs (
    id                 BIGSERIAL PRIMARY KEY,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_prompt        TEXT,
    lens_used          TEXT,
    generated_sql      TEXT,
    candidate_count    INTEGER,
    success            BOOLEAN,
    error_message      TEXT,
    recommended_titles TEXT,
    mode               TEXT,
    retrieval_ms       REAL,
    hydration_ms       REAL,
    rerank_ms          REAL,
    rerank_type        TEXT,
    total_ms           REAL
);

-- Time-window queries on the dashboard hit (created_at, total_ms) — index helps.
CREATE INDEX IF NOT EXISTS idx_telemetry_logs_created_at
    ON telemetry_logs(created_at);

-- RLS on, no policies = service key only. Anon/authenticated have no access.
-- (Matches the system_flags / search_feedback pattern.)
ALTER TABLE telemetry_logs ENABLE ROW LEVEL SECURITY;


-- ---------------------------------------------------------------------
-- RPC: get_pipeline_timings(p_window_hours INT)
--
-- Returns one row per rerank_type with P50/P95/P99 per pipeline stage
-- over the last `p_window_hours` hours. Called by the admin dashboard.
--
-- Default window: 24 hours. Pass any positive integer to widen/narrow.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION get_pipeline_timings(p_window_hours INT DEFAULT 24)
RETURNS TABLE(
    rerank_type    TEXT,
    sample_count   BIGINT,
    retrieval_p50  NUMERIC,
    retrieval_p95  NUMERIC,
    retrieval_p99  NUMERIC,
    hydration_p50  NUMERIC,
    hydration_p95  NUMERIC,
    hydration_p99  NUMERIC,
    rerank_p50     NUMERIC,
    rerank_p95     NUMERIC,
    rerank_p99     NUMERIC,
    total_p50      NUMERIC,
    total_p95      NUMERIC,
    total_p99      NUMERIC
)
LANGUAGE SQL
STABLE
AS $$
    SELECT
        COALESCE(t.rerank_type, 'unknown')                                      AS rerank_type,
        COUNT(*)                                                                AS sample_count,
        percentile_cont(0.5)  WITHIN GROUP (ORDER BY t.retrieval_ms)            AS retrieval_p50,
        percentile_cont(0.95) WITHIN GROUP (ORDER BY t.retrieval_ms)            AS retrieval_p95,
        percentile_cont(0.99) WITHIN GROUP (ORDER BY t.retrieval_ms)            AS retrieval_p99,
        percentile_cont(0.5)  WITHIN GROUP (ORDER BY t.hydration_ms)            AS hydration_p50,
        percentile_cont(0.95) WITHIN GROUP (ORDER BY t.hydration_ms)            AS hydration_p95,
        percentile_cont(0.99) WITHIN GROUP (ORDER BY t.hydration_ms)            AS hydration_p99,
        percentile_cont(0.5)  WITHIN GROUP (ORDER BY t.rerank_ms)               AS rerank_p50,
        percentile_cont(0.95) WITHIN GROUP (ORDER BY t.rerank_ms)               AS rerank_p95,
        percentile_cont(0.99) WITHIN GROUP (ORDER BY t.rerank_ms)               AS rerank_p99,
        percentile_cont(0.5)  WITHIN GROUP (ORDER BY t.total_ms)                AS total_p50,
        percentile_cont(0.95) WITHIN GROUP (ORDER BY t.total_ms)                AS total_p95,
        percentile_cont(0.99) WITHIN GROUP (ORDER BY t.total_ms)                AS total_p99
    FROM telemetry_logs t
    WHERE t.created_at >= NOW() - (p_window_hours || ' hours')::INTERVAL
      AND t.total_ms IS NOT NULL
    GROUP BY COALESCE(t.rerank_type, 'unknown')
    ORDER BY rerank_type;
$$;

-- Allow PostgREST to call the function with the service role key.
GRANT EXECUTE ON FUNCTION get_pipeline_timings(INT) TO service_role;
