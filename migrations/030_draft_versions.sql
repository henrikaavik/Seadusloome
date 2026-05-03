-- =============================================================================
-- Migration 030: Draft versioning — draft_versions table + idempotent backfill
-- =============================================================================
--
-- Adds the ``draft_versions`` table which tracks successive versions of a
-- draft through the legislative pipeline (VTK → reading 1 → ... → enacted).
--
-- Each row represents one discrete version of a draft document.  The first
-- version (version_number = 1) is created for every existing draft row via
-- an idempotent backfill INSERT at the bottom of this file so no draft is
-- left without at least one version record.
--
-- Design decisions (sprint plan §9.5):
--   - This is a pure DB migration.  No Fuseki graphs are copied here because
--     coupling container-boot migrations to triplestore reachability would
--     break rollback.  A separate optional post-deploy script
--     (scripts/migrate_jena_graphs_to_versioned.py) handles Fuseki copies.
--   - v1 rows inherit the legacy per-draft graph_uri verbatim so no Jena
--     touch is needed; the URI already exists.
--   - parsed_text_encrypted is NULLABLE to match drafts.parsed_text_encrypted:
--     an upload that has not yet been parsed through the Tika pipeline has no
--     encrypted text yet.
--   - No application code reads from draft_versions yet; the cutover from
--     drafts.* columns happens in PR-B (#618 PR-B).
--
-- ROLLBACK procedure (manual; requires app to be on pre-PR-A code):
--   DROP TABLE IF EXISTS draft_versions;
--   DELETE FROM schema_migrations WHERE version = '030_draft_versions';
--   Then redeploy previous app image.  No data loss because the source of
--   truth (drafts table) is untouched by this migration.
--
-- FORWARD-FIX (partial backfill):
--   Re-running this migration is safe — the CREATE TABLE uses IF NOT EXISTS
--   and the INSERT uses ON CONFLICT DO NOTHING, so already-backfilled rows
--   are skipped and only newly-created drafts receive a v1 row.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Table: draft_versions
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS draft_versions (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    draft_id        uuid        NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    version_number  int         NOT NULL,
    -- reading_stage tracks the legislative pipeline step this version was
    -- submitted at.  'vtk' is the default for the initial backfill because
    -- the existing drafts table has no reading-stage metadata; callers
    -- uploading a 1st-reading version explicitly will create version 2+.
    reading_stage   text        NOT NULL CHECK (reading_stage IN (
                                    'vtk',
                                    'reading_1',
                                    'reading_2',
                                    'reading_3',
                                    'enacted'
                                )),
    -- NULLABLE: matches drafts.parsed_text_encrypted behaviour.  Fernet
    -- ciphertext (STORAGE_ENCRYPTION_KEY).  Copy verbatim from the source
    -- draft row — do NOT re-encrypt, the bytes are already encrypted.
    parsed_text_encrypted bytea,
    -- mirrors drafts.storage_path naming: path to Fernet-encrypted file
    storage_path    text        NOT NULL,
    -- per-version Jena named graph URI.  For v1 rows this is the legacy
    -- per-draft URI (urn:draft:{draft_id}); future versions allocate a new
    -- URI per upload.
    graph_uri       text        NOT NULL,
    -- mirrors the pipeline status from the source drafts row at backfill
    -- time.  The status column follows the same state machine as
    -- drafts.status.
    status          text        NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      uuid        NOT NULL REFERENCES users(id),
    -- Natural key: one version_number per draft.
    UNIQUE (draft_id, version_number)
);

-- Covering index for the common query: "list all versions for a draft,
-- newest version first".  Descending version_number is the natural sort
-- order because higher numbers are later in the legislative pipeline.
CREATE INDEX IF NOT EXISTS idx_draft_versions_draft
    ON draft_versions (draft_id, version_number DESC);

-- ---------------------------------------------------------------------------
-- Idempotent backfill: every existing draft → one version row at v1
-- ---------------------------------------------------------------------------
-- reading_stage is 'vtk' because that is the legislative starting point;
-- callers who upload a 1st-reading version later will create v2.
-- ON CONFLICT DO NOTHING makes re-running safe after a partial failure.

INSERT INTO draft_versions (
    draft_id,
    version_number,
    reading_stage,
    parsed_text_encrypted,
    storage_path,
    graph_uri,
    status,
    created_by,
    created_at
)
SELECT
    id,
    1,
    'vtk',
    parsed_text_encrypted,
    storage_path,
    graph_uri,
    status,
    user_id,
    created_at
FROM drafts
ON CONFLICT (draft_id, version_number) DO NOTHING;
