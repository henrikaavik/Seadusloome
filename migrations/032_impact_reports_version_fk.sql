-- =============================================================================
-- Migration 032: Per-version impact reports — draft_version_id FK on impact_reports
-- =============================================================================
--
-- Adds the ``draft_version_id`` column to ``impact_reports`` so each impact
-- analysis run is bound to a specific :class:`DraftVersion` rather than the
-- bare ``drafts.id``.  This is the analyze-pipeline half of the §4.2 cutover
-- shipped in #618 PR-B (the upload + read/write cutover lives in the same PR
-- but is application code).
--
-- Design decisions (sprint plan §6 Days 5-7):
--   - Column is NULLABLE so the migration is non-blocking against any
--     in-flight analyze job.  A subsequent backfill UPDATE links every
--     existing row to v1 of its draft (the only version that exists post
--     migration 030 backfill).  Future PR-D may flip this to NOT NULL.
--   - ``ON DELETE CASCADE`` so deleting a draft_version row (e.g. user
--     hard-deletes a specific reading) propagates to its impact report,
--     mirroring the existing ``draft_id`` FK behaviour.
--   - Plain B-tree index on ``draft_version_id`` for the
--     ``WHERE draft_version_id = $1`` lookup the report routes will issue
--     once they pivot to per-version queries (#618 PR-C).
--
-- Idempotency:
--   - ``ADD COLUMN IF NOT EXISTS`` and ``CREATE INDEX IF NOT EXISTS`` make
--     the schema mutations safe to re-run.
--   - The backfill UPDATE only touches rows where the FK is still NULL so a
--     partial-failure replay does not double-write.
--
-- ROLLBACK procedure (manual; requires app on pre-PR-B code):
--   ALTER TABLE impact_reports DROP COLUMN IF EXISTS draft_version_id;
--   DROP INDEX IF EXISTS idx_impact_reports_draft_version;
--   DELETE FROM schema_migrations WHERE version = '032_impact_reports_version_fk';
--   Then redeploy previous app image.  No data loss because the source of
--   truth (drafts + draft_versions) is untouched.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Schema mutation
-- ---------------------------------------------------------------------------

ALTER TABLE impact_reports
    ADD COLUMN IF NOT EXISTS draft_version_id uuid
    REFERENCES draft_versions(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_impact_reports_draft_version
    ON impact_reports(draft_version_id);

-- ---------------------------------------------------------------------------
-- Backfill: link every existing impact_reports row to the latest version of
-- its parent draft.  Post migration 030 this is always v1 because no caller
-- has produced a v2 yet, but the LIMIT 1 + ORDER BY DESC stays correct
-- when re-run after #618 PR-B has actually shipped v2 uploads.
-- ---------------------------------------------------------------------------

UPDATE impact_reports ir
SET draft_version_id = (
    SELECT id FROM draft_versions
    WHERE draft_id = ir.draft_id
    ORDER BY version_number DESC
    LIMIT 1
)
WHERE ir.draft_version_id IS NULL;
