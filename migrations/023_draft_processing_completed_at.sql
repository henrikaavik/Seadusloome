-- =============================================================================
-- Migration 023: drafts.processing_completed_at -- frozen completion timestamp (#670)
-- =============================================================================
--
-- ``app/docs/routes.py::_processing_duration_seconds`` historically computed
-- the final "Analüüsitud N h M min S s" label on a terminal draft as
-- ``updated_at - created_at``. ``updated_at`` is bumped by every write
-- (rename, link-VTK, ``touch_draft_access`` is not — but any write that
-- goes through ``update_draft_status`` or the per-draft edit helpers is),
-- which meant renaming a ready draft twelve hours after upload
-- retroactively inflated the label to "Analüüsitud 12 h 0 min 42 s"
-- even though the actual pipeline took 42 seconds.
--
-- This migration introduces a frozen ``processing_completed_at`` timestamp
-- that is written exactly once per draft -- the moment the pipeline
-- transitions into ``status='ready'`` or ``status='failed'``. Every
-- subsequent edit leaves the column untouched so the label stays stable.
--
-- Semantics:
--   - NULL when the draft has never finished processing (still in
--     ``uploaded`` / ``parsing`` / ``extracting`` / ``analyzing``).
--   - Set to ``now()`` when the pipeline flips into ``ready`` or
--     ``failed``.
--   - Cleared back to NULL by the retry path (#656 / retry_handler.py)
--     so a re-run writes a fresh completion time.
--
-- Backfill: existing terminal drafts get ``processing_completed_at =
-- updated_at`` -- this is the same proxy the old code was using, so the
-- label they render is unchanged by the migration. Drafts whose
-- ``updated_at`` has already been inflated by a later edit will keep
-- showing the inflated duration; we can't recover the real pipeline
-- finish time for historical rows, but every NEW terminal transition
-- starting now writes an accurate value.
--
-- Idempotent: ``ADD COLUMN IF NOT EXISTS`` + the backfill skips rows
-- where ``processing_completed_at`` is already set.
-- =============================================================================

ALTER TABLE drafts
    ADD COLUMN IF NOT EXISTS processing_completed_at TIMESTAMPTZ NULL;

UPDATE drafts
   SET processing_completed_at = updated_at
 WHERE status IN ('ready', 'failed')
   AND processing_completed_at IS NULL;

COMMENT ON COLUMN drafts.processing_completed_at IS
    'Wall-clock timestamp of the last pipeline completion (ready or failed). '
    'Written once per terminal transition; cleared on retry. Decoupled '
    'from updated_at so downstream edits do not inflate the final '
    'Analüüsitud duration label. See migration 023 and issue #670.';
