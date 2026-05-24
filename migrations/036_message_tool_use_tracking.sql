-- =============================================================================
-- Migration 036: messages.tool_use_id + messages.parent_message_id (#315)
-- =============================================================================
--
-- Issue #315 ("Persist tool_use/tool_result in messages table"):
--   The messages table (migration 008) already carries tool_name / tool_input /
--   tool_output for role='tool' rows. What's MISSING is:
--
--     1. ``tool_use_id`` — Claude's API identifier (e.g. ``toolu_...``) that
--        ties a streaming ``tool_use`` block to its follow-up
--        ``tool_result`` block. Without it we cannot reconstruct the
--        Anthropic tool-use protocol from persisted history when replaying
--        a multi-turn conversation, and we cannot match orchestrator
--        emissions to their persisted rows.
--
--     2. ``parent_message_id`` — pointer back to the assistant turn that
--        triggered the tool call. A single assistant turn can request N
--        tools (see ``app/chat/orchestrator.py::_run_stream_loop``); without
--        a parent link, the persisted tool rows float free of the message
--        they belong to. The UI cannot group them under the assistant
--        bubble, and a follow-up history loader cannot rebuild the
--        ``tool_use → tool_result`` pairing required by the Anthropic
--        Messages API for multi-turn tool use.
--
-- Both columns are NULLable: a chat message that never invoked a tool
-- carries NULL in both, so the columns are additive and backwards
-- compatible. The CHECK constraint ensures a ``tool_use_id`` only ever
-- appears on a role='tool' row (defensive — the orchestrator never sets
-- it on user/assistant/system rows, but a future regression would be
-- caught at the DB layer).
--
-- Cascade semantics:
--   - parent_message_id REFERENCES messages(id) ON DELETE CASCADE.
--     Deleting the assistant turn that triggered the tool calls
--     automatically removes the tool rows. This matches the implicit
--     contract: a tool row only makes sense in the context of the
--     assistant turn that requested it.
--
-- Idempotency:
--   - ADD COLUMN IF NOT EXISTS for both columns.
--   - CREATE INDEX IF NOT EXISTS for both indexes.
--   - The CHECK constraint is added via a DO block that probes
--     pg_constraint so a re-run does not duplicate it.
--
-- ROLLBACK procedure (manual; requires app on pre-#315 code):
--   ALTER TABLE messages DROP CONSTRAINT IF EXISTS messages_tool_use_id_role_chk;
--   DROP INDEX IF EXISTS idx_messages_tool_use;
--   DROP INDEX IF EXISTS idx_messages_parent;
--   ALTER TABLE messages DROP COLUMN IF EXISTS parent_message_id;
--   ALTER TABLE messages DROP COLUMN IF EXISTS tool_use_id;
--   DELETE FROM schema_migrations WHERE version = '036_message_tool_use_tracking';
-- =============================================================================

ALTER TABLE messages ADD COLUMN IF NOT EXISTS tool_use_id TEXT NULL;

ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS parent_message_id UUID NULL
        REFERENCES messages(id) ON DELETE CASCADE;

-- Defensive CHECK: ``tool_use_id`` may only be set on a role='tool' row.
-- A regression that wrote it onto an assistant / user row would silently
-- corrupt the replay pairing logic, so we reject it at the DB layer.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'messages_tool_use_id_role_chk'
    ) THEN
        ALTER TABLE messages
            ADD CONSTRAINT messages_tool_use_id_role_chk
            CHECK (tool_use_id IS NULL OR role = 'tool');
    END IF;
END $$;

-- Index for the "load tool children of an assistant turn" query pattern
-- (renderer groups tool rows under their parent). Partial: messages
-- without a parent are the overwhelming majority and would just bloat
-- the index.
CREATE INDEX IF NOT EXISTS idx_messages_parent
    ON messages (parent_message_id)
    WHERE parent_message_id IS NOT NULL;

-- Index for the "look up a tool row by its Anthropic tool_use_id within
-- a conversation" pattern, used by the history-to-API replay code to
-- pair tool_use blocks with their tool_result blocks. Partial for the
-- same reason as above.
CREATE INDEX IF NOT EXISTS idx_messages_tool_use
    ON messages (conversation_id, tool_use_id)
    WHERE tool_use_id IS NOT NULL;
