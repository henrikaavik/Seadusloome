-- =============================================================================
-- Migration 013: Live sync progress tracking
-- =============================================================================
--
-- Issue #567: admins had no visible indication that a sync was running.
-- The orchestrator only wrote a sync_log row at the end, so the admin UI
-- stayed unchanged for the minutes-long sync and looked broken.
--
-- Changes:
--   1. Add `current_step` TEXT column so the orchestrator can record which
--      phase it is in (cloning / converting / validating / uploading /
--      reingesting). The admin card polls and reads this column to drive
--      a progress-pill UI.
--   2. Drop the existing CHECK constraint on `status`. We still only
--      write 'running' / 'success' / 'failed', but keeping the check in
--      place is unnecessary enum-style rigidity for a log table and makes
--      future status additions (e.g. 'cancelled') require a migration.
--      Application code (app/admin/sync.py::_SYNC_STATUS_MAP) already
--      treats unknown values gracefully.
-- =============================================================================

ALTER TABLE sync_log
    ADD COLUMN IF NOT EXISTS current_step TEXT;

ALTER TABLE sync_log
    DROP CONSTRAINT IF EXISTS sync_log_status_check;
