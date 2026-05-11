-- =============================================================================
-- Migration 033: pending_chat_seed — server-side single-use chat-seed tokens
-- =============================================================================
--
-- Epic #714 PR-J (#724) — cross-links between Eelnõud / Analüüsikeskus /
-- Nõustaja. The "Küsi nõustajalt selle leiu kohta" affordance pre-fills the
-- chat input with a finding phrased as a question. A finding can quote draft
-- content (politically sensitive pre-publication text, see CLAUDE.md §"Draft
-- sensitivity"), so the seed text MUST NOT travel through the URL as plain
-- text. Instead the POST /chat/seed handler stashes the (Fernet-encrypted)
-- seed in this table and redirects with an opaque single-use ``token`` UUID;
-- the chat view consumes the token once and renders the textarea pre-filled.
--
-- Design decisions:
--   - ``token`` is the primary key (a random UUID v4) — it is the only thing
--     that travels through the URL, so it must be unguessable and opaque.
--   - ``seed_encrypted`` holds the Fernet ciphertext of the seed text
--     (``app.storage.encrypt_text``), mirroring drafts.parsed_text_encrypted
--     and messages.content_encrypted. The plaintext is never persisted.
--   - ``draft_id`` is NULLABLE — ad-hoc Analüüsikeskus analyses (Normi
--     mõjuahel against a free-text reference) have no backing draft. When set,
--     the chat-new handler uses it to bind the new conversation's
--     ``context_draft_id`` (same as ``?draft=`` does today).
--   - ``user_id`` / ``org_id`` cascade-delete with their owners so a deleted
--     user / org never leaves orphaned (encrypted) seed rows behind.
--   - Tokens are single-use AND short-lived: the consume path DELETEs the row
--     and the model layer opportunistically garbage-collects rows older than
--     one hour, so this table stays tiny.
--   - ``idx_pending_chat_seed_user`` supports the per-user lookup the consume
--     path issues (``WHERE token = $1 AND user_id = $2``).
--
-- Idempotency:
--   - ``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS`` make the
--     migration safe to re-run.
--
-- ROLLBACK procedure (manual; requires app on pre-PR-J code):
--   DROP TABLE IF EXISTS pending_chat_seed;
--   DELETE FROM schema_migrations WHERE version = '033_pending_chat_seed';
--   Then redeploy the previous app image. No data loss — the table only ever
--   holds transient single-use tokens, not durable state.
-- =============================================================================

CREATE TABLE IF NOT EXISTS pending_chat_seed (
    token          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    org_id         uuid        REFERENCES organizations(id) ON DELETE CASCADE,
    draft_id       uuid        REFERENCES drafts(id) ON DELETE CASCADE,   -- nullable: ad-hoc analyses have no draft
    seed_encrypted bytea       NOT NULL,                                  -- Fernet-encrypted seed text (may quote draft/finding content)
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pending_chat_seed_user ON pending_chat_seed(user_id);
