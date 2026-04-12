-- =============================================================================
-- Migration 014: Encrypt chat message transcripts at rest (#570)
-- =============================================================================
--
-- NFR §6.1 compliance: chat transcripts may contain sensitive pre-publication
-- legislative content (quoted draft sections pasted by users, LLM analyses
-- that reference unpublished provisions, RAG snippets sourced from encrypted
-- drafts). These must be encrypted at rest via application-layer Fernet,
-- matching the pattern used for ``drafts.parsed_text_encrypted`` (migration
-- 006) and ``drafting_sessions.draft_content_encrypted``.
--
-- Scope: the ``messages`` table's payload-bearing columns —
--   - ``content``       (TEXT)  — the user/assistant/tool message text
--   - ``tool_input``    (JSONB) — SPARQL queries, RAG lookup params, etc.
--   - ``tool_output``   (JSONB) — query results, may surface draft text
--   - ``rag_context``   (JSONB) — chunks retrieved from the RAG index
--
-- Key: reuses ``STORAGE_ENCRYPTION_KEY`` (the application-wide Fernet key
-- also used by ``app/storage/encrypted.py::encrypt_text``). The NFR §6 key
-- matrix names this key "draft encryption key"; there is a single Fernet
-- key for all pre-publication at-rest encryption and no separate chat key.
--
-- =============================================================================
-- Two-phase rollout (IMPORTANT — read before squashing / cleaning up)
-- =============================================================================
--
-- Phase A (this migration): ADD the new ``*_encrypted`` BYTEA columns
-- alongside the existing plaintext columns. New writes go to the encrypted
-- columns; SELECTs prefer the encrypted column and fall back to plaintext
-- when the encrypted column is NULL (backward compatibility for rows that
-- existed before this rollout).
--
-- Phase B (separate follow-up ticket): a backfill script
-- (``scripts/migrate_chat_encryption.py``) reads every row and populates
-- the new ``*_encrypted`` columns from the plaintext. Idempotent — skips
-- rows where the encrypted column is already set. The backfill MUST run
-- and be verified in production before Phase C.
--
-- Phase C (separate follow-up migration): once the backfill is verified
-- and no rows have ``content_encrypted IS NULL``, a follow-up migration
-- drops the ``content``, ``tool_input``, ``tool_output`` and ``rag_context``
-- plaintext columns and makes ``content_encrypted`` NOT NULL.
--
-- We explicitly do NOT drop the plaintext columns in this migration because
-- production has live chat data; a single migration that both renames and
-- re-encrypts would lose data if the backfill had any bug. Keeping both
-- columns for one release cycle lets us roll back by simply pointing
-- ``_row_to_message`` back at the plaintext column.
-- =============================================================================

ALTER TABLE messages ADD COLUMN IF NOT EXISTS content_encrypted BYTEA;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS tool_input_encrypted BYTEA;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS tool_output_encrypted BYTEA;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS rag_context_encrypted BYTEA;

-- Relax the old NOT NULL constraint on ``content`` so new writes are free to
-- leave the plaintext column NULL. Without this, every INSERT would still
-- need to write the plaintext (defeating the purpose of the migration) or
-- hit a NOT NULL violation. The ``content_encrypted`` column carries the
-- authoritative payload going forward; fallback reads still work because
-- ``_row_to_message`` decrypts the encrypted column when present.
ALTER TABLE messages ALTER COLUMN content DROP NOT NULL;
