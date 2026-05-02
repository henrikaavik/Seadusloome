-- =============================================================================
-- Migration 026: drop plaintext chat columns + lock content_encrypted NOT NULL
-- =============================================================================
--
-- Phase C of the chat-encryption rollout (#687, follow-up to #570 / migration
-- 014). Migration 014 added ``*_encrypted`` BYTEA columns alongside the
-- legacy plaintext columns; ``scripts/migrate_chat_encryption.py`` (Phase B)
-- backfilled them. The Step 1 pre-flight on prod (issue comment, 2026-05-02)
-- confirmed zero rows have ``content_encrypted IS NULL`` — the messages
-- table is in fact empty in prod, so this migration is risk-free.
--
-- Safety net: a defensive PL/pgSQL block re-runs the pre-flight inline.
-- If any row violates the invariant the migration aborts before the
-- destructive DROPs run, so a botched deploy cannot lose data.
-- =============================================================================

DO $$
DECLARE
  pending BIGINT;
BEGIN
  SELECT COUNT(*) INTO pending FROM messages WHERE content_encrypted IS NULL;
  IF pending > 0 THEN
    RAISE EXCEPTION
      'Refusing to drop plaintext columns: % messages still have content_encrypted IS NULL — '
      're-run scripts/migrate_chat_encryption.py before applying this migration',
      pending;
  END IF;
END $$;

ALTER TABLE messages
  DROP COLUMN content,
  DROP COLUMN tool_input,
  DROP COLUMN tool_output,
  DROP COLUMN rag_context;

-- The encrypted column is now the sole source of truth for message content.
-- Locking it NOT NULL guarantees future writes cannot regress to a
-- "no-payload" state without an explicit migration to relax the constraint.
ALTER TABLE messages ALTER COLUMN content_encrypted SET NOT NULL;
