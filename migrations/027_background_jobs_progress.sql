-- =============================================================================
-- Migration 027: background_jobs.progress JSONB — export progress indicator (#610)
-- =============================================================================
--
-- Sprint 1 Day 3 of the Eelnõud sprint plan. The .docx export job
-- (handler in ``app/docs/export_handler.py``) currently has no in-flight
-- progress channel: the UI shows an indeterminate "Eksport käimas..."
-- spinner that polls ``GET /drafts/{id}/export-status/{job}`` every
-- 2-10 seconds. For larger reports (50+ affected entities, dozens of
-- conflicts) this leaves the user staring at a blank spinner for a
-- minute or more with no signal that anything is happening.
--
-- This migration adds an optional ``progress`` JSONB column to
-- ``background_jobs``. The export handler writes a small payload like
-- ``{"current": 5, "total": 12}`` every few rendered sections; a new
-- WebSocket endpoint (``/ws/drafts/export-progress``) reads from this
-- column and pushes updates to the browser so the spinner can be
-- replaced with a real ``<progress>`` bar showing actual completion.
--
-- The column is intentionally generic (a JSONB blob, not a fixed
-- ``current/total`` int pair) so future job handlers can publish their
-- own progress shape — e.g. analyze_impact could emit
-- ``{"phase": "extracting", "items": 30, "total": 100}`` without
-- another schema migration.
--
-- Idempotent: ``ADD COLUMN IF NOT EXISTS``. Safe to apply on prod with
-- live workers running because the existing handlers don't read or
-- write the column at all — the absence of a value is the same as the
-- pre-migration state.
-- =============================================================================

ALTER TABLE background_jobs
    ADD COLUMN IF NOT EXISTS progress JSONB;

COMMENT ON COLUMN background_jobs.progress IS
    'Optional handler-defined progress payload, e.g. {"current": N, "total": M}. '
    'Written by long-running handlers (export_report — see app/docs/export_handler.py) '
    'and read by the /ws/drafts/export-progress WebSocket so the UI can render a '
    'real progress bar instead of an indeterminate spinner. See migration 027 '
    'and issue #610.';
