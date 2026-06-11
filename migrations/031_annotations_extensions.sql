-- Migration 031: Extend annotations schema for impact-report row annotations
-- (Phase A of #619 row-annotations rollout).
--
-- PURPOSE
--   Adds the columns the service layer needs for impact-report row annotations
--   (#619 PR-B writes) and encryption-at-rest (#9.5) without breaking any
--   existing reader.  Prod has 0 rows in annotations / annotation_replies so
--   the encryption columns ship directly with no Phase B backfill script.
--
-- DESIGN DECISION
--   We extend the existing ``annotations`` / ``annotation_replies`` tables
--   from migration 011 rather than creating new annotation_threads /
--   annotation_messages tables.  The sprint plan §9.4 predated an audit of the
--   existing Phase 4 schema; new tables would have duplicated functionality.
--
-- §9.4 target mapping
--   target_type = 'impact_report_item'   (already in the CHECK constraint)
--   target_id   = '{row_kind}:{row_key}' (colon-delimited; existing index covers it)
--   draft_version_id = FK to draft_versions(id) for per-version isolation
--
-- ROLLBACK (manual; requires app on pre-PR-A code):
--   ALTER TABLE annotations
--     DROP COLUMN IF EXISTS content_encrypted,
--     DROP COLUMN IF EXISTS draft_version_id,
--     DROP COLUMN IF EXISTS mentions,
--     DROP COLUMN IF EXISTS stale,
--     ALTER COLUMN content SET NOT NULL;
--   ALTER TABLE annotation_replies
--     DROP COLUMN IF EXISTS content_encrypted,
--     DROP COLUMN IF EXISTS mentions,
--     ALTER COLUMN content SET NOT NULL;
--   DROP INDEX IF EXISTS idx_annotations_version_target;
--   DROP INDEX IF EXISTS idx_annotations_stale;
--   DELETE FROM schema_migrations WHERE version = '031_annotations_extensions';
-- No data loss — the content column is preserved throughout.
--
-- This migration is fully idempotent via IF NOT EXISTS / conditional ALTER.

-- ---------------------------------------------------------------------------
-- annotations table extensions
-- ---------------------------------------------------------------------------

-- §9.5 encryption-at-rest: the legacy plaintext content column stays for one
-- release cycle so PR-B's read fallback can handle any rollback scenario.
-- We relax NOT NULL here so new encrypted-only writes do not need to
-- populate a redundant plaintext copy.
ALTER TABLE annotations ALTER COLUMN content DROP NOT NULL;

-- New BYTEA column that will hold Fernet-encrypted content in PR-B onwards.
ALTER TABLE annotations ADD COLUMN IF NOT EXISTS content_encrypted bytea;

-- §9.4 per-version isolation: an annotation may be scoped to a specific
-- draft_version (impact_report_item case) or remain version-agnostic (other
-- target_types).  NULLABLE for full back-compatibility.
ALTER TABLE annotations
    ADD COLUMN IF NOT EXISTS draft_version_id uuid
    REFERENCES draft_versions(id) ON DELETE CASCADE;

-- §9.4 @mentions: array of in-org user UUIDs resolved at write time from
-- @kasutaja-style parsing.  Default empty so existing rows satisfy NOT NULL.
ALTER TABLE annotations
    ADD COLUMN IF NOT EXISTS mentions uuid[] NOT NULL DEFAULT '{}';

-- §9.4 stale flag: rows whose (row_kind, row_key) no longer exist after a
-- re-analyze are marked stale=TRUE rather than deleted.  Default FALSE so
-- existing rows are not surprised by the new column.
ALTER TABLE annotations
    ADD COLUMN IF NOT EXISTS stale boolean NOT NULL DEFAULT false;

-- Hot path: load all impact-report annotations for a given draft version
-- (the primary query in list_annotations_for_version_row).
CREATE INDEX IF NOT EXISTS idx_annotations_version_target
    ON annotations(draft_version_id, target_type, target_id)
    WHERE draft_version_id IS NOT NULL;

-- Partial index for the "show stale only" UI section (§9.4).
-- Keys on draft_version_id because annotations has no direct draft_id column.
CREATE INDEX IF NOT EXISTS idx_annotations_stale
    ON annotations(draft_version_id) WHERE stale = true;

-- ---------------------------------------------------------------------------
-- annotation_replies table extensions
-- ---------------------------------------------------------------------------
-- Replies are message-level: no version scope, no stale flag (those are
-- thread-level concerns on the parent annotation).  Only encryption + mentions.

ALTER TABLE annotation_replies ALTER COLUMN content DROP NOT NULL;

ALTER TABLE annotation_replies
    ADD COLUMN IF NOT EXISTS content_encrypted bytea;

ALTER TABLE annotation_replies
    ADD COLUMN IF NOT EXISTS mentions uuid[] NOT NULL DEFAULT '{}';
