-- =============================================================================
-- Migration 017: Vestlus (Chat) UX polish sweep — pin, archive, feedback
-- =============================================================================
--
-- Adds schema required by the Vestlus chat polish sweep:
--
--   1. conversations — pin/archive support and custom-title guard
--      is_pinned, pinned_at, is_archived  allow users to pin and archive
--      individual conversations without deleting them.
--      title_is_custom prevents the auto-title background job from
--      overwriting a name the user has manually set.
--
--   2. messages — pin and truncation flags
--      is_pinned   lets users bookmark individual messages inside a thread.
--      is_truncated marks partial assistant turns produced by "stop generation"
--      so the UI can render a visible indicator and the context builder can
--      handle them correctly.
--
--   3. message_feedback — thumbs-up / thumbs-down quality signal
--      One feedback row per (message, user) pair.  rating is constrained to
--      {-1, 1} (-1 = thumbs down, 1 = thumbs up).  An optional free-text
--      comment allows reviewers / admins to surface qualitative issues.
--
-- Migration is idempotent:
--   ADD COLUMN IF NOT EXISTS is used throughout.
--   CREATE TABLE IF NOT EXISTS guards the new table.
--   CREATE INDEX IF NOT EXISTS guards all new indexes.
--
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. conversations — pin / archive / custom-title columns
-- ---------------------------------------------------------------------------

ALTER TABLE conversations
    ADD COLUMN IF NOT EXISTS is_pinned       BOOLEAN    NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS is_archived     BOOLEAN    NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS pinned_at       TIMESTAMPTZ         NULL,
    ADD COLUMN IF NOT EXISTS title_is_custom BOOLEAN    NOT NULL DEFAULT FALSE;

-- Partial index: only pinned rows — kept small and used by the "pinned
-- conversations" section that sorts by pin recency.
CREATE INDEX IF NOT EXISTS idx_conversations_pinned
    ON conversations (user_id, pinned_at DESC)
    WHERE is_pinned = TRUE;

-- Partial index: active (non-archived) conversations sorted by recency —
-- the default conversation list query.
CREATE INDEX IF NOT EXISTS idx_conversations_active
    ON conversations (user_id, updated_at DESC)
    WHERE is_archived = FALSE;

-- ---------------------------------------------------------------------------
-- 2. messages — pin and truncation flags
-- ---------------------------------------------------------------------------

ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS is_pinned    BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS is_truncated BOOLEAN NOT NULL DEFAULT FALSE;

-- ---------------------------------------------------------------------------
-- 3. message_feedback — per-user thumbs-up / thumbs-down
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS message_feedback (
    id         UUID      PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id UUID      NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    user_id    UUID      NOT NULL REFERENCES users(id)    ON DELETE CASCADE,
    -- 1 = thumbs up, -1 = thumbs down
    rating     SMALLINT  NOT NULL CHECK (rating IN (-1, 1)),
    comment    TEXT                  NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (message_id, user_id)
);

-- Supports fetching all feedback for a given message (admin / analytics view).
CREATE INDEX IF NOT EXISTS idx_message_feedback_message
    ON message_feedback (message_id);

-- Foreign-key-backing index on user_id (convention: always index FK columns).
CREATE INDEX IF NOT EXISTS idx_message_feedback_user
    ON message_feedback (user_id);

-- =============================================================================
-- Down migration (non-executable reference — manual use only)
-- =============================================================================
--
-- DROP TABLE IF EXISTS message_feedback;
--
-- ALTER TABLE messages
--     DROP COLUMN IF EXISTS is_truncated,
--     DROP COLUMN IF EXISTS is_pinned;
--
-- ALTER TABLE conversations
--     DROP COLUMN IF EXISTS title_is_custom,
--     DROP COLUMN IF EXISTS pinned_at,
--     DROP COLUMN IF EXISTS is_archived,
--     DROP COLUMN IF EXISTS is_pinned;
--
-- (Indexes are dropped automatically when their table/column is dropped.)
-- =============================================================================
