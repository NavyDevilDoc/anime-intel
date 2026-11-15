-- =====================================================================
-- tools/supabase_setup_search_journal.sql
--
-- One-time setup: search_journal table for persistent journal storage.
-- Paste into Supabase SQL Editor and run. Idempotent — safe to re-run.
--
-- Replaces the ephemeral local JSONL journal in data/search_journal.jsonl
-- which gets wiped on every Railway redeploy. Production traffic since
-- PR #62 has been writing to that ephemeral path; this table captures it
-- persistently for cross-deploy analytics and Initiative 4 (SearchFeedback
-- Analytics) joins.
--
-- Schema mirrors the columns the application writes via supabase_post()
-- in analysis/telemetry_logger.py. Column choices documented in
-- IMPLEMENTATION_PLAN.md Initiative 2 Phase A.
-- =====================================================================


-- ---------------------------------------------------------------------
-- Table: search_journal
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS search_journal (
    id                       BIGSERIAL PRIMARY KEY,
    -- Supabase row-insert time, for audit and replication-lag diagnostics.
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Engine-event time, from the journal entry's timestamp field.
    -- Analysis always queries on event_at, not created_at.
    event_at                 TIMESTAMPTZ NOT NULL,

    -- Always-present anchors
    mode                     TEXT NOT NULL CHECK (mode IN ('find_a_show', 'shows_like_this')),
    success                  BOOLEAN NOT NULL,
    error                    TEXT,

    -- Join target for search_feedback (introduced mid-2026; ~50% fill).
    -- TEXT (not UUID) to match search_feedback.search_id's declared shape
    -- in api/schemas/requests.py, so joins use both columns' indexes
    -- without a cast.
    search_id                TEXT,

    -- Retrieval pipeline scalars (96.9% fill rate; NULL on early-error rows).
    retrieval_path           TEXT,
    candidates_retrieved     INTEGER,
    candidates_after_rerank  INTEGER,

    -- Find a Show fields (NULL for shows_like_this rows).
    query                    TEXT,
    lens                     TEXT CHECK (lens IS NULL OR lens IN
        ('Intelligent Search', 'Objective Rankings')),
    era                      TEXT,
    intent                   TEXT,

    -- Shows Like This fields (NULL for find_a_show rows).
    intersection_summary     TEXT,
    reference_count          INTEGER,
    excluded_count           INTEGER,
    negative_anchor_count    INTEGER,
    prefiltered_count        INTEGER,

    -- Materialized at write time for query convenience.
    -- rerank_type: from timings.rerank_type post-PR-#62; derived from
    -- intent for older shapes. Five-value enum across all reranker
    -- prompts plus the no-rerank objective path.
    rerank_type              TEXT CHECK (rerank_type IS NULL OR rerank_type IN
        ('fusion', 'narrative', 'constrained', 'dna', 'objective')),
    -- mal_rating_max: from filters_applied.mal_rating_max (PR #71+).
    -- Closed enum matches the API request validator. R+/Rx included for
    -- forward-compat even though they aren't user-selectable.
    mal_rating_max           TEXT CHECK (mal_rating_max IS NULL OR mal_rating_max IN
        ('G', 'PG', 'PG-13', 'R', 'R+', 'Rx')),
    -- Materialized so empty-pool / banner-fire analysis doesn't need to
    -- dig into the results JSONB on every query.
    results_count            INTEGER NOT NULL DEFAULT 0,
    top_match_confidence     INTEGER,

    -- Semi-structured payloads (variable shape across feature generations).
    results                  JSONB,
    timings                  JSONB,
    filters_applied          JSONB,
    concepts_detected        JSONB,
    reference_titles         JSONB,
    negative_anchors         JSONB,
    title_resolution_notes   JSONB
);


-- ---------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------

-- Primary access pattern: newest-first, optionally per-mode.
CREATE INDEX IF NOT EXISTS idx_search_journal_event_at
    ON search_journal (event_at DESC);
CREATE INDEX IF NOT EXISTS idx_search_journal_mode_event_at
    ON search_journal (mode, event_at DESC);

-- Filter/group columns (partial indexes — column is NULL for the other mode).
CREATE INDEX IF NOT EXISTS idx_search_journal_intent
    ON search_journal (intent) WHERE intent IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_search_journal_rerank_type
    ON search_journal (rerank_type) WHERE rerank_type IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_search_journal_mal_rating_max
    ON search_journal (mal_rating_max) WHERE mal_rating_max IS NOT NULL;

-- Cross-table join target for Initiative 4 (SearchFeedback Analytics).
CREATE INDEX IF NOT EXISTS idx_search_journal_search_id
    ON search_journal (search_id) WHERE search_id IS NOT NULL;

-- Search within results — "how often does Frieren land top-10?" etc.
CREATE INDEX IF NOT EXISTS idx_search_journal_results_gin
    ON search_journal USING GIN (results);


-- ---------------------------------------------------------------------
-- RLS: enabled, no policies = service-role only. Anon/authenticated have
-- no access. Matches the pattern of telemetry_logs / search_feedback /
-- contact_messages.
-- ---------------------------------------------------------------------
ALTER TABLE search_journal ENABLE ROW LEVEL SECURITY;
