-- PR #84 Phase B: anon_usage composite-key migration.
--
-- Changes the primary key from (ip_hash) to (ip_hash, usage_date) so
-- prior-day rows are preserved instead of overwritten on day rollover.
-- See IMPLEMENTATION_PLAN.md PR #84 for full rationale.
--
-- This script is idempotent: safe to re-run if any step fails partway.
-- The full migration runs inside one transaction; either everything
-- commits or nothing changes.
--
-- Steps:
--   1. Rename existing `anon_usage` -> `anon_usage_v1_backup` (preserves
--      current data untouched for the 48-hour stability window).
--   2. Create new `anon_usage` with composite PK (ip_hash, usage_date).
--   3. Copy rows from the backup table into the new table.
--   4. Recreate `idx_anon_usage_date` for analytics range queries.
--      (The composite PK auto-creates the hot-path lookup index on
--      (ip_hash, usage_date) — no separate index needed.)
--   5. Enable RLS with no policies. Service role bypasses; matches
--      the existing security posture.
--
-- After running this script, redeploy the backend with the Phase C
-- anon_quota.py changes. The 48-hour backup-drop step is a separate
-- manual action; see below for the DROP command.

BEGIN;

-- Step 1: Conditional rename. Skips if already renamed (i.e. the
-- migration was previously run successfully).
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'anon_usage'
  ) AND NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'anon_usage_v1_backup'
  ) THEN
    ALTER TABLE anon_usage RENAME TO anon_usage_v1_backup;
  END IF;
END $$;

-- Step 2: New table with composite PK. CREATE IF NOT EXISTS makes
-- this re-run safe.
CREATE TABLE IF NOT EXISTS anon_usage (
  ip_hash       TEXT        NOT NULL,
  usage_date    DATE        NOT NULL,
  search_count  INT         NOT NULL DEFAULT 0,
  dna_count     INT         NOT NULL DEFAULT 0,
  last_seen     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (ip_hash, usage_date)
);

-- Step 3: Copy existing rows. ON CONFLICT handles re-runs cleanly.
-- Guarded so re-running after the backup is dropped is safe.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'anon_usage_v1_backup'
  ) THEN
    INSERT INTO anon_usage (ip_hash, usage_date, search_count, dna_count, last_seen)
    SELECT ip_hash, usage_date, search_count, dna_count, last_seen
    FROM anon_usage_v1_backup
    ON CONFLICT (ip_hash, usage_date) DO NOTHING;
  END IF;
END $$;

-- Step 4: Date-range index for analytics queries (7d/30d unique-IP
-- aggregates land here). The composite PK already covers the hot-path
-- ip_hash lookup.
CREATE INDEX IF NOT EXISTS idx_anon_usage_date ON anon_usage(usage_date);

-- Step 5: RLS enabled, no policies. Service role bypasses.
ALTER TABLE anon_usage ENABLE ROW LEVEL SECURITY;

COMMIT;

-- Reload PostgREST schema cache so the new table shape is visible
-- to the API layer immediately (without this, the API may return
-- stale 400s for ~10 minutes while the cache refreshes naturally).
NOTIFY pgrst, 'reload schema';

-- ---------------------------------------------------------------
-- 48-hour stability window cleanup (run manually after verification):
--
--   DROP TABLE IF EXISTS anon_usage_v1_backup;
--
-- Do NOT run the DROP until:
--   - Phase C (anon_quota.py refactor) has been deployed
--   - At least 48 hours of production traffic has hit the new schema
--   - The admin dashboard's 7d/30d unique-IP series shows
--     reasonable counts (not zero, not catastrophically wrong)
-- ---------------------------------------------------------------
