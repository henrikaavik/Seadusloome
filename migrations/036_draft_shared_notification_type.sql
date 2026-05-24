-- =============================================================================
-- Migration 036: Allow draft_shared notification type (#299)
-- =============================================================================
--
-- Extends the notifications.type CHECK constraints to permit the new
-- 'draft_shared' value emitted by notify_draft_shared() (see
-- app/notifications/wire.py).
--
-- Background: BOTH of the following CHECK constraints currently exist on
-- notifications.type in production:
--
--   * chk_notifications_type   -- added by migration 012, 5 base types
--                                 (annotation_reply, analysis_done,
--                                 drafter_complete, sync_failed, cost_alert)
--   * notifications_type_check -- added by migration 015, same 5 + the
--                                 draft_archive_warning type from #572
--
-- Migration 015 did NOT drop the 012 constraint (it incorrectly assumed
-- no explicit CHECK existed), which means an insert with
-- type='draft_archive_warning' would already have failed against the
-- still-present 012 constraint. notify() swallows DB errors so the
-- regression was invisible. This migration cleans up both constraints
-- and recreates a single canonical one.
--
-- Idempotent: ``DROP CONSTRAINT IF EXISTS`` covers DBs where either
-- constraint is already missing, and the recreated constraint always
-- carries the full allowed set.
-- =============================================================================

-- Drop both legacy constraints. Either or both may be present depending
-- on which historical migrations ran successfully against the target
-- DB; the IF EXISTS guards make this safe in all cases.
alter table notifications
    drop constraint if exists chk_notifications_type;

alter table notifications
    drop constraint if exists notifications_type_check;

-- Recreate as a single canonical CHECK so future additions only need to
-- update one constraint (and so this migration is self-contained — the
-- final state is independent of which prior migrations ran).
alter table notifications
    add constraint notifications_type_check
    check (type in (
        'annotation_reply',
        'analysis_done',
        'drafter_complete',
        'sync_failed',
        'cost_alert',
        'draft_archive_warning',
        'draft_shared'
    ));
