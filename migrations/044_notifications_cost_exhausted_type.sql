-- =============================================================================
-- Migration 044: Allow cost_exhausted notification type (#861 B)
-- =============================================================================
--
-- The per-org budget guard (app/chat/rate_limiter.py::check_org_cost_budget)
-- now emits a one-time 'cost_exhausted' alert at 100% of the monthly LLM
-- budget, distinct from the 80% advisory 'cost_alert' (see
-- app/notifications/wire.py::notify_cost_exhausted). The notifications.type
-- CHECK constraint did not permit 'cost_exhausted', so every such insert
-- would have been silently dropped: notify() swallows DB errors by design,
-- so the CHECK violation would never surface — the admins simply never get
-- the "budget exhausted" notification. (Same silent-drop class that
-- migrations 015/036/038 each fixed for a newly-emitted type; pinned by
-- tests/test_export_lifecycle_sessions_sweep.py's superset regression test.)
--
-- Recreates the canonical notifications.type CHECK as the UNION of every
-- type literal the codebase emits (grep of type="..." across
-- notify()/create_notification() callers: app/notifications/wire.py +
-- app/jobs/archive_warning.py) plus the constraint history (012/015/036/038),
-- adding 'cost_exhausted'. This becomes the new canonical list; future
-- additions only need to update one constraint here.
--
-- Mirrors migration 036/038's cleanup pattern: drop BOTH historical
-- constraint names (chk_notifications_type from 012, notifications_type_check
-- from 015/036/038) so the final state is independent of which prior
-- migrations ran, then re-add a single canonical constraint guarded by a
-- pg_constraint DO block (the 012/019 idempotency shape — Postgres has no
-- ADD CONSTRAINT IF NOT EXISTS).
--
-- Idempotent: the unconditional DROP ... IF EXISTS pair removes any prior
-- form of the constraint (including 038's stale list that lacks
-- 'cost_exhausted'), and the DO block only re-adds when absent — so
-- re-running is a no-op and a DB already carrying 038 is correctly upgraded
-- rather than left with the stale list.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Expand notifications.type CHECK constraint to include 'cost_exhausted'
-- ---------------------------------------------------------------------------

-- Drop both legacy constraint names unconditionally. This is what upgrades a
-- DB that already ran migration 038 (whose notifications_type_check omits
-- 'cost_exhausted') — a DO-block guard alone would see the existing
-- constraint and skip, leaving the stale list in place. Either or both may
-- be present depending on which historical migrations ran; the IF EXISTS
-- guards make this safe in all cases.
alter table notifications
    drop constraint if exists chk_notifications_type;

alter table notifications
    drop constraint if exists notifications_type_check;

-- Re-add the single canonical constraint. Wrapped in a pg_constraint-guarded
-- DO block (same idempotency pattern as migrations 012/019) so re-running the
-- migration when the constraint already exists is a no-op.
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'notifications_type_check'
          AND conrelid = 'notifications'::regclass
    ) THEN
        ALTER TABLE notifications
            ADD CONSTRAINT notifications_type_check
            CHECK (type IN (
                'annotation_reply',
                'annotation_mention',
                'analysis_done',
                'drafter_complete',
                'sync_failed',
                'cost_alert',
                'cost_exhausted',
                'draft_archive_warning',
                'draft_shared',
                'drafting_session_archive_warning'
            ));
    END IF;
END $$;
