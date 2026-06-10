-- =============================================================================
-- Migration 038: Drafting-session archive warning (#845 B4b)
-- =============================================================================
--
-- ``drafting_sessions`` rows carry encrypted draft clauses + legislative
-- intent — the same class of politically sensitive content as uploaded
-- drafts — but were excluded from the 90-day archive-warning lifecycle
-- (the daily scan in app/jobs/archive_warning.py only covered ``drafts``).
-- The scan now also sweeps stale *active* drafting sessions and emits a
-- new ``drafting_session_archive_warning`` notification type.
--
--   1. Extends the notifications.type CHECK constraint with
--      'drafting_session_archive_warning'. Mirrors migration 036's
--      cleanup pattern: drop BOTH historical constraint names
--      (chk_notifications_type from 012, notifications_type_check from
--      015/036) and recreate a single canonical constraint, so the
--      final state is independent of which prior migrations ran.
--
--   2. Partial index on drafting_sessions(updated_at) WHERE
--      status = 'active' so the daily stale-session scan stays cheap.
--      Mirrors migration 015's idx_drafts_last_accessed_at: the scan
--      filters on status = 'active' (completed/abandoned sessions are
--      terminal and never warned about), so the partial predicate keeps
--      the index small.
--
-- Idempotent: DROP CONSTRAINT IF EXISTS + CREATE INDEX IF NOT EXISTS;
-- the recreated CHECK always carries the full allowed set.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. Expand notifications.type CHECK constraint
-- ---------------------------------------------------------------------------

alter table notifications
    drop constraint if exists chk_notifications_type;

alter table notifications
    drop constraint if exists notifications_type_check;

alter table notifications
    add constraint notifications_type_check
    check (type in (
        'annotation_reply',
        'analysis_done',
        'drafter_complete',
        'sync_failed',
        'cost_alert',
        'draft_archive_warning',
        'draft_shared',
        'drafting_session_archive_warning'
    ));

-- ---------------------------------------------------------------------------
-- 2. Partial index for the stale-session scan
-- ---------------------------------------------------------------------------
-- The archive-warning job runs once per day and queries:
--   SELECT ... FROM drafting_sessions
--   WHERE updated_at < now() - make_interval(days => 90)
--     AND status = 'active'

create index if not exists idx_drafting_sessions_updated_at_active
    on drafting_sessions(updated_at)
    where status = 'active';
