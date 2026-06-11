-- =============================================================================
-- Migration 043: Index for metrics retention sweeps
-- Ticket #861
-- =============================================================================
--
-- The metrics retention sweep in app/metrics.py (_maybe_prune) issues:
--
--     DELETE FROM metrics WHERE recorded_at < now() - $1::interval;
--
-- Migration 011 created idx_metrics_name_recorded_at on (name, recorded_at),
-- whose LEADING column is `name`. A name-agnostic delete keyed solely on
-- `recorded_at` cannot use that index efficiently and falls back to a
-- sequential scan that grows with the table. This single-column index lets
-- the planner satisfy the retention predicate with an index range scan.
--
-- IF NOT EXISTS keeps the migration idempotent (re-applicable).
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_metrics_recorded_at
    ON metrics (recorded_at);
