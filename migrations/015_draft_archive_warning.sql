-- =============================================================================
-- Migration 015: 90-day Draft Auto-Archive Warning (#572)
-- =============================================================================
--
-- Adds infrastructure for the 90-day draft auto-archive warning feature:
--
--   1. drafts.last_accessed_at -- TIMESTAMPTZ NOT NULL DEFAULT now()
--      tracks when the draft was last surfaced to a user. Backfilled to
--      now() for existing rows so they get a fresh 90-day clock starting
--      at the migration date rather than retroactively triggering warnings
--      for long-lived drafts whose owners have been actively using them.
--
--   2. Partial index on drafts(last_accessed_at) WHERE status != 'archived'
--      to make the daily stale-drafts scan cheap even once the table grows.
--
--   3. Expands the notifications.type CHECK constraint to include
--      'draft_archive_warning'. Existing allowed values: annotation_reply,
--      analysis_done, drafter_complete, sync_failed, cost_alert.
--
-- Migration is idempotent: guards ADD COLUMN and index creation with
-- IF NOT EXISTS, and drops the old CHECK constraint before recreating
-- the expanded version.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. drafts.last_accessed_at
-- ---------------------------------------------------------------------------

alter table drafts
    add column if not exists last_accessed_at timestamptz not null default now();

-- ---------------------------------------------------------------------------
-- 2. Partial index for the stale-draft scan
-- ---------------------------------------------------------------------------
-- The archive warning job runs once per day and queries:
--   SELECT ... FROM drafts
--   WHERE last_accessed_at < now() - interval '90 days'
--     AND status != 'archived'
-- The partial predicate keeps the index small by excluding already-archived
-- rows, which by definition can never produce a new warning.

create index if not exists idx_drafts_last_accessed_at
    on drafts(last_accessed_at)
    where status != 'archived';

-- ---------------------------------------------------------------------------
-- 3. Expand notifications.type CHECK constraint
-- ---------------------------------------------------------------------------
-- PostgreSQL does not allow ALTER CHECK CONSTRAINT in place; drop + recreate
-- is the supported pattern. The constraint name `notifications_type_check`
-- is the default PostgreSQL-generated name for the original CHECK (type in
-- (...)) clause on the notifications table.
--
-- NOTE: migration 011 created notifications WITHOUT an explicit CHECK on
-- `type` (the column is plain `text`). We add one here so callers cannot
-- write arbitrary notification types. The set of allowed values mirrors
-- the factories in app/notifications/wire.py.

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
        'draft_archive_warning'
    ));
