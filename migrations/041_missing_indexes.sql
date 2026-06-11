-- =============================================================================
-- Migration 041: Missing indexes for foreign-key and query-pattern coverage
-- =============================================================================
--
-- Adds four indexes that were absent from earlier migrations and cause
-- avoidable sequential scans in common application paths.  All statements
-- use IF NOT EXISTS so the migration is safe to re-apply (idempotent).
--
-- INDEX CHOICES
-- -------------
--
-- 1. draft_versions(created_by)
--    The ``created_by`` column is an FK to ``users(id)`` (migration 030).
--    Without an index, deleting or suspending a user requires a seqscan of
--    draft_versions; the FK-constraint enforcement path also benefits.
--
-- 2. pending_chat_seed(org_id)
--    Migration 033 added ``idx_pending_chat_seed_user`` (user_id lookup) but
--    omitted org_id, which is an FK column.  The ON DELETE CASCADE from
--    organizations also benefits from this index.
--
-- 3. pending_chat_seed(draft_id)
--    draft_id is a NULLABLE FK to drafts(id) ON DELETE CASCADE.  When a draft
--    is deleted any orphan seed rows must be found; without an index this
--    is a seqscan.  A partial index on non-NULL rows keeps it lean (ad-hoc
--    analyses with draft_id IS NULL are never joined this way).
--
-- 4. llm_usage(org_id, created_at) — composite
--    The cost dashboard (app/admin/cost_dashboard.py) issues several queries
--    of the shape:
--
--        SELECT … FROM llm_usage
--        WHERE created_at >= %s [AND org_id = %s]
--        GROUP BY feature | model | date_trunc('day', created_at)
--
--    Org-scoped calls (the common case — org_admins, org filter in admin view)
--    filter on BOTH org_id AND created_at.  A composite (org_id, created_at)
--    index satisfies both predicates with a single index scan; Postgres can
--    also use the leading org_id column for the FK constraint lookup on
--    organizations.  For the system-admin "all-orgs" path (created_at only),
--    Postgres will use this index via a skip-scan if the cardinality of org_id
--    is low enough, or fall back to a seqscan on the small table — acceptable.
--
--    A plain (created_at) index was considered but rejected: the composite
--    covers all org-scoped dashboard queries *and* the FK cascade path in a
--    single B-tree, versus maintaining two separate indexes.
--
-- HNSW INDEX REVIEW (rag_chunks, migration 009)
-- -----------------------------------------------
-- Migration 009 created the HNSW index with default pgvector parameters
-- (m=16, ef_construction=64).  For 1024-dimensional vectors at 90k+ scale
-- the recommended values from the pgvector documentation are:
--
--   m               = 16   (default — reasonable for 1024d; 32 would increase
--                            recall marginally but roughly doubles index size)
--   ef_construction = 128  (or higher: 64 underbuilds recall at this scale;
--                            128 is the pgvector-recommended starting point
--                            for production workloads)
--   ef_search       = 64   (set at query time via SET hnsw.ef_search = 64)
--
-- Rebuilding the HNSW index with ef_construction=128 requires a full index
-- rebuild (there is no in-place ALTER for HNSW parameters):
--
--   DROP INDEX CONCURRENTLY idx_rag_chunks_embedding;
--   CREATE INDEX CONCURRENTLY idx_rag_chunks_embedding
--       ON rag_chunks USING hnsw (embedding vector_cosine_ops)
--       WITH (m = 16, ef_construction = 128);
--
-- This is an ops event (builds in the background but holds a ShareUpdateExclusiveLock
-- on the table during the CREATE phase; on 90k rows with 1024d vectors this
-- takes several minutes).  It is NOT included in this migration to avoid
-- making a routine schema migration an ops disruption.  The operator should
-- schedule the rebuild during a low-traffic window via the CONCURRENTLY path
-- above.  Until rebuilt, recall at k=10 will be slightly below optimal but
-- functional.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. draft_versions(created_by) — FK to users(id)
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_draft_versions_created_by
    ON draft_versions (created_by);

-- ---------------------------------------------------------------------------
-- 2. pending_chat_seed(org_id) — FK to organizations(id)
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_pending_chat_seed_org
    ON pending_chat_seed (org_id);

-- ---------------------------------------------------------------------------
-- 3. pending_chat_seed(draft_id) — partial, non-NULL only
-- ---------------------------------------------------------------------------
-- Ad-hoc seeds (draft_id IS NULL) are never looked up by draft; the partial
-- index keeps the structure lean and the planner honest.
CREATE INDEX IF NOT EXISTS idx_pending_chat_seed_draft
    ON pending_chat_seed (draft_id)
    WHERE draft_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 4. llm_usage(org_id, created_at) — composite for cost dashboard
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_llm_usage_org_created
    ON llm_usage (org_id, created_at);
