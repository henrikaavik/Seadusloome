-- =============================================================================
-- Migration 019: draft doc_type discriminator + VTK lineage (#639)
-- =============================================================================
--
-- Implements §2.1 of the Eelnõud Metadata & Discovery design (Sub-project A,
-- epic #597). Two new columns on ``drafts`` plus a family of indexes to
-- support the filtered listing page and trigram search.
--
-- Changes:
--
--   1. drafts.doc_type TEXT NOT NULL DEFAULT 'eelnou'
--      CHECK (doc_type IN ('eelnou', 'vtk'))
--      Discriminates regular eelnõud from VTKd (väljatöötamiskavatsused).
--      Defaults to 'eelnou' so every existing row is valid without backfill.
--
--   2. drafts.parent_vtk_id UUID REFERENCES drafts(id) ON DELETE SET NULL
--      Optional FK from an eelnõu back to the VTK it originates from.
--      ON DELETE SET NULL preserves the eelnõu if its parent VTK is deleted.
--
--   3. CHECK chk_vtk_has_no_parent
--      Prevents VTK->VTK chains: a row with doc_type='vtk' must have
--      parent_vtk_id IS NULL.
--
--   4. Composite index idx_drafts_org_doctype_status_created
--      Covers the default filtered listing query:
--        WHERE org_id = ? AND doc_type = ? AND status = ?
--        ORDER BY created_at DESC
--
--   5. Partial index idx_drafts_parent_vtk
--      Hot path for "child eelnoud of this VTK" on the VTK detail page.
--
--   6. pg_trgm extension + trigram indexes on title, filename, and
--      draft_entities.label to power ILIKE %q% search without seqscans.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. doc_type discriminator
-- ---------------------------------------------------------------------------

ALTER TABLE drafts
    ADD COLUMN doc_type TEXT NOT NULL DEFAULT 'eelnou'
        CHECK (doc_type IN ('eelnou', 'vtk')),
    ADD COLUMN parent_vtk_id UUID REFERENCES drafts(id) ON DELETE SET NULL;

-- ---------------------------------------------------------------------------
-- 2. VTK->VTK chain prevention
-- ---------------------------------------------------------------------------
-- A VTK cannot itself have a parent VTK; only eelnoud may carry a VTK link.

ALTER TABLE drafts
    ADD CONSTRAINT chk_vtk_has_no_parent
        CHECK (doc_type = 'eelnou' OR parent_vtk_id IS NULL);

-- ---------------------------------------------------------------------------
-- 3. Composite index for the filtered listing page
-- ---------------------------------------------------------------------------
-- Covers: WHERE org_id = ? [AND doc_type = ?] [AND status = ?]
--         ORDER BY created_at DESC

CREATE INDEX idx_drafts_org_doctype_status_created
    ON drafts (org_id, doc_type, status, created_at DESC);

-- ---------------------------------------------------------------------------
-- 4. Partial index for VTK child-eelnou query
-- ---------------------------------------------------------------------------

CREATE INDEX idx_drafts_parent_vtk
    ON drafts (parent_vtk_id) WHERE parent_vtk_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 5. Trigram search indexes
-- ---------------------------------------------------------------------------
-- pg_trgm is a stable in-tree PostgreSQL extension; safe to add without
-- downtime. The three GIN indexes below make ILIKE '%q%' index-supported
-- on title, filename, and draft_entities.label respectively.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX idx_drafts_title_trgm
    ON drafts USING gin (title gin_trgm_ops);

CREATE INDEX idx_drafts_filename_trgm
    ON drafts USING gin (filename gin_trgm_ops);

CREATE INDEX idx_draft_entities_label_trgm
    ON draft_entities USING gin (label gin_trgm_ops);
