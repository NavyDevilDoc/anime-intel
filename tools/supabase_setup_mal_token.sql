-- =====================================================================
-- tools/supabase_setup_mal_token.sql
--
-- One-time setup: mal_oauth_token singleton table for persistent MAL
-- OAuth credential storage. Paste into Supabase SQL Editor and run.
-- Idempotent — safe to re-run.
--
-- Replaces the ephemeral disk-based token_data.json on Railway, which
-- gets wiped on every container restart. Initiative 3 (see
-- IMPLEMENTATION_PLAN.md) — closes the staleness bug where Railway's
-- env vars hold an already-invalidated refresh_token after the first
-- post-deploy rotation.
--
-- Singleton pattern: one project = one MAL app = one token row.
-- CHECK (id = 1) enforces this at the schema level so any UNIQUE-key
-- mistake by the application is rejected by the database.
--
-- Service-role only — RLS enabled with no policies. Matches the
-- system_flags / telemetry_logs / search_feedback / search_journal
-- pattern.
-- =====================================================================


-- ---------------------------------------------------------------------
-- Table: mal_oauth_token
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mal_oauth_token (
    id            SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    access_token  TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    -- token_type is "Bearer" in MAL's response today. Persisted verbatim
    -- so a future change in OAuth server behavior doesn't silently break.
    token_type    TEXT NOT NULL DEFAULT 'Bearer',
    -- Seconds until access_token expiry. MAL returns ~2,678,400 (~31d).
    -- Nullable to tolerate OAuth servers that omit it (RFC 6749 says
    -- recommended-not-required).
    expires_in    INT,
    -- Reflects the actual most-recent write — set on INSERT via DEFAULT,
    -- maintained on UPDATE via trigger below.
    refreshed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ---------------------------------------------------------------------
-- Trigger: keep refreshed_at server-side on UPDATE.
-- DEFAULT NOW() only fires on INSERT; without this trigger, PATCH
-- requests that don't pass refreshed_at would leave the column stale.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION update_mal_oauth_token_refreshed_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.refreshed_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS mal_oauth_token_refreshed_at_trg ON mal_oauth_token;
CREATE TRIGGER mal_oauth_token_refreshed_at_trg
    BEFORE UPDATE ON mal_oauth_token
    FOR EACH ROW
    EXECUTE FUNCTION update_mal_oauth_token_refreshed_at();


-- ---------------------------------------------------------------------
-- RLS: enabled, no policies = service-role only. Anon/authenticated
-- have no access. Tokens are project-internal credentials.
-- ---------------------------------------------------------------------
ALTER TABLE mal_oauth_token ENABLE ROW LEVEL SECURITY;
