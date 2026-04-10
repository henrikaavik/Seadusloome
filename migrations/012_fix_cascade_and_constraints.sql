-- =============================================================================
-- Migration 012: Fix CASCADE → SET NULL on annotation user FKs, add
--                CHECK constraint on notifications.type
-- =============================================================================
--
-- Fixes three issues found in the Phase 4 code review:
--
-- M6: annotations.user_id — ON DELETE CASCADE silently destroys annotation
--     content when a user is deleted.  Changed to SET NULL so the audit trail
--     (content, timestamps, org context) is preserved.  The column is made
--     nullable to accommodate the NULL value written on user deletion.
--
-- M7: annotation_replies.user_id — same problem as M6, same fix.  Replies
--     authored by a deleted user are kept; user_id becomes NULL.
--
-- M8: notifications.type has no CHECK constraint, allowing arbitrary strings
--     to be inserted.  A constraint is added to enforce the exact set of event
--     types defined in app/notifications/wire.py.
--
-- Idempotency strategy:
--   - FK changes: DROP CONSTRAINT IF EXISTS before re-adding — safe to re-run.
--   - ALTER COLUMN … DROP NOT NULL is idempotent in PostgreSQL (no error if
--     the column is already nullable).
--   - CHECK constraint: guarded by a DO block that checks pg_constraint so
--     re-running the migration does not raise a duplicate-constraint error.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- M6: annotations.user_id — change ON DELETE CASCADE → SET NULL
-- ---------------------------------------------------------------------------
-- Drop the original FK (created in migration 011 as ON DELETE CASCADE) and
-- re-add it as ON DELETE SET NULL.  The column must be nullable first because
-- SET NULL writes a NULL into the column when the referenced user row is
-- deleted; a NOT NULL constraint would reject that write.

ALTER TABLE annotations
    DROP CONSTRAINT IF EXISTS annotations_user_id_fkey,
    ALTER COLUMN user_id DROP NOT NULL,
    ADD CONSTRAINT annotations_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL;

-- ---------------------------------------------------------------------------
-- M7: annotation_replies.user_id — change ON DELETE CASCADE → SET NULL
-- ---------------------------------------------------------------------------
-- Same rationale as M6.  The parent annotation_id FK remains ON DELETE CASCADE
-- (removing an annotation still removes all its replies, which is intentional).

ALTER TABLE annotation_replies
    DROP CONSTRAINT IF EXISTS annotation_replies_user_id_fkey,
    ALTER COLUMN user_id DROP NOT NULL,
    ADD CONSTRAINT annotation_replies_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL;

-- ---------------------------------------------------------------------------
-- M8: notifications.type — add CHECK constraint for allowed event types
-- ---------------------------------------------------------------------------
-- Constrains notifications.type to the five event types currently emitted by
-- app/notifications/wire.py.  Wrapped in a DO block so re-running the
-- migration when the constraint already exists is a no-op.

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_notifications_type'
    ) THEN
        ALTER TABLE notifications
            ADD CONSTRAINT chk_notifications_type CHECK (
                type IN (
                    'annotation_reply',
                    'analysis_done',
                    'drafter_complete',
                    'sync_failed',
                    'cost_alert'
                )
            );
    END IF;
END $$;
