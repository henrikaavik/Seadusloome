-- =============================================================================
-- Migration 036: Allow draft_shared notification type (#299)
-- =============================================================================
--
-- Extends the notifications.type CHECK constraint to permit the new
-- 'draft_shared' value emitted by notify_draft_shared() (see
-- app/notifications/wire.py).
--
-- Without this, the INSERT in app/notifications/notify.notify() fails
-- the CHECK introduced by migration 015, and because notify() swallows
-- DB errors, the new fan-out silently produces zero rows.
--
-- Follows the same drop+recreate pattern as migration 015 (PostgreSQL
-- does not support ALTER CHECK CONSTRAINT in place).
-- =============================================================================

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
        'draft_shared'
    ));
