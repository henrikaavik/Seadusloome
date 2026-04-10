-- Migration 010: FK ON DELETE fixes and missing indexes
--
-- H3: users.org_id FK missing ON DELETE clause (should be SET NULL).
-- M9: Self-contained vector extension guard for rag_chunks.
-- M10: Composite index for llm_usage cost-budget queries.
-- M11: Composite index for conversations list-by-user queries.

-- H3: Fix users.org_id FK to include ON DELETE SET NULL.
-- Cannot modify 001_initial.sql since it is already applied in prod.
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_org_id_fkey;
ALTER TABLE users ADD CONSTRAINT users_org_id_fkey
    FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE SET NULL;

-- M9: Ensure pgvector extension exists (self-containment for rag_chunks).
CREATE EXTENSION IF NOT EXISTS vector;

-- M10: Composite index for per-org monthly cost budget queries.
CREATE INDEX IF NOT EXISTS idx_llm_usage_org_created
    ON llm_usage(org_id, created_at);

-- M11: Composite index for listing conversations ordered by recency.
CREATE INDEX IF NOT EXISTS idx_conversations_user_updated
    ON conversations(user_id, updated_at DESC);
