-- =============================================================================
-- Migration 035: draft_reviews — reviewer-only outcome record per draft
-- =============================================================================
--
-- Issue #817 (docs/2026-05-19-usability-fixes-plan.md §4.4):
--   A user with the ``reviewer`` role can open same-org drafts but had no
--   way to persist a review outcome ("no issue" / "issue found" / "needs
--   discussion"). This migration adds the ``draft_reviews`` table so each
--   reviewer-driven conclusion can be stored, audited, and surfaced on the
--   reviewer's Töölaud as a work queue.
--
-- Design decisions:
--   - Separate table (not a column on ``drafts``) so a single draft can
--     carry the FULL HISTORY of reviews — a reviewer can update their
--     conclusion (e.g., from "needs_discussion" to "no_issue" after a
--     follow-up conversation) and both rows persist for the audit trail.
--   - ``reviewer_id`` is NULLABLE with ``ON DELETE SET NULL``. NOT NULL
--     would contradict SET NULL (rejected write at user deletion time),
--     so we follow the same pattern as ``annotations.user_id`` and
--     ``annotation_replies.user_id`` — see
--     ``migrations/012_fix_cascade_and_constraints.sql:31-34`` for the
--     documented rule. Preserves the review record when the reviewer's
--     user account is later deleted.
--   - ``reviewer_name_snapshot`` captures the reviewer's display name at
--     review time. When ``reviewer_id`` is later nulled by a user delete,
--     the UI can still render "Anne Tamm (kustutatud kasutaja)" instead
--     of a bare "—" placeholder.
--   - ``outcome`` is constrained via CHECK to the three legal values so a
--     typo at the app layer cannot reach durable state. Matches the
--     pattern used by ``notifications.type`` (migration 012).
--   - ``comment`` is optional — a "no issue" review needs no narrative;
--     a "needs discussion" usually does.
--   - ``draft_id`` cascades on draft delete so removing a draft removes
--     the review history with it (consistent with ``impact_reports`` and
--     ``draft_versions``).
--
-- Indexes:
--   - ``idx_draft_reviews_draft_id`` (draft_id, created_at DESC) — the
--     dominant query pattern is "list every review for a draft, newest
--     first" (renderer on the detail page) and "latest review for a
--     draft" (dashboard widget).
--   - ``idx_draft_reviews_reviewer`` partial index on reviewer_id WHERE
--     NOT NULL — powers the reviewer dashboard query "drafts I have not
--     yet reviewed" (anti-join via NOT EXISTS). Partial because a NULL
--     reviewer_id is never the join target.
--
-- Idempotency:
--   - ``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS`` make
--     the migration safe to re-run.
--
-- ROLLBACK procedure (manual; requires app on pre-#817 code):
--   DROP TABLE IF EXISTS draft_reviews;
--   DELETE FROM schema_migrations WHERE version = '035_draft_reviews';
-- =============================================================================

CREATE TABLE IF NOT EXISTS draft_reviews (
    id                     UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    draft_id               UUID        NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    -- reviewer_id is NULLABLE: ON DELETE SET NULL preserves the review
    -- record when the reviewer's user account is later deleted. NOT NULL
    -- would contradict SET NULL — same pattern as annotations.user_id
    -- (migrations/012_fix_cascade_and_constraints.sql:31-34).
    reviewer_id            UUID        REFERENCES users(id) ON DELETE SET NULL,
    -- Snapshot of the reviewer's display name at review time so the UI
    -- can render "Anne Tamm (kustutatud kasutaja)" when reviewer_id has
    -- been nulled by a user delete.
    reviewer_name_snapshot TEXT,
    outcome                TEXT        NOT NULL
                                       CHECK (outcome IN ('no_issue', 'issue_found', 'needs_discussion')),
    comment                TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Dominant query: list every review for a draft, newest first.
CREATE INDEX IF NOT EXISTS idx_draft_reviews_draft_id
    ON draft_reviews(draft_id, created_at DESC);

-- Reviewer-Töölaud anti-join (NOT EXISTS) — partial because a NULL
-- reviewer_id is never the join target.
CREATE INDEX IF NOT EXISTS idx_draft_reviews_reviewer
    ON draft_reviews(reviewer_id) WHERE reviewer_id IS NOT NULL;
