-- =============================================================================
-- Migration 016: RAG chunks tenant scoping (#576)
-- =============================================================================
--
-- Today `rag_chunks` holds only public corpus (ontology, court decisions, EU
-- acts) so retrieval without tenant filtering is safe. Before any private
-- draft text or org-scoped content is ingested, the schema must support
-- per-org isolation. This migration adds that machinery; actual private-draft
-- ingestion is deliberately out of scope here.
--
-- Columns:
--   - `org_id`    UUID NULL  — NULL = public corpus visible to every tenant.
--                              Non-NULL = chunk is private to that org.
--   - `source_id` UUID NULL  — polymorphic soft reference. When
--                              `source_type = 'draft'` this points at
--                              `drafts.id`. For public corpus rows it is NULL.
--
-- Polymorphic FK note:
--   PostgreSQL does not support polymorphic foreign keys natively. We deliberately
--   do NOT add a `REFERENCES drafts(id)` constraint here because `source_id`
--   also covers future source_types (e.g. ontology URIs → no drafts row exists).
--   Cascade is handled in application code: `app/rag/retriever.py::delete_chunks_for_draft`
--   is invoked by `app/docs/routes.py::delete_draft_handler` when a draft row
--   is removed. If you add a new private source_type, add a matching
--   `delete_chunks_for_<source_type>` helper and wire it into that source
--   type's delete path.

ALTER TABLE rag_chunks
    ADD COLUMN IF NOT EXISTS org_id UUID NULL,
    ADD COLUMN IF NOT EXISTS source_id UUID NULL;

COMMENT ON COLUMN rag_chunks.org_id IS
    'Tenant owner. NULL = public corpus visible to every org; non-NULL = private to that org.';
COMMENT ON COLUMN rag_chunks.source_id IS
    'Polymorphic soft reference to the owning domain row (e.g. drafts.id when source_type=''draft''). No FK — cascade is handled in application code via delete_chunks_for_draft.';

-- Compound index supports the common retrieval predicate
-- `(org_id IS NULL OR org_id = $1) AND source_type = $2` and the cascade
-- predicate `source_type = 'draft' AND source_id = $1`.
CREATE INDEX IF NOT EXISTS idx_rag_chunks_org_source
    ON rag_chunks (org_id, source_type);
