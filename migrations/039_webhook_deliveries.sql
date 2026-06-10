-- =============================================================================
-- Migration 039: webhook_deliveries — GitHub webhook replay protection (#853)
-- =============================================================================
--
-- Issue #853 (H5): the GitHub webhook receiver (``app/sync/webhook.py``) had
-- no replay protection. A captured-and-replayed push payload carries a valid
-- HMAC signature forever, so an attacker (or a GitHub redelivery / proxy
-- retry) could re-trigger a full ontology reload at will. GitHub stamps every
-- delivery with a unique ``X-GitHub-Delivery`` UUID; persisting the ones we
-- have already processed lets us reject duplicates even when the signature
-- checks out.
--
-- Design decisions:
--   - ``delivery_id`` is the PRIMARY KEY (the GitHub delivery UUID, stored as
--     TEXT — GitHub documents it as a UUID but we keep it TEXT to be robust to
--     any future format change and to avoid a cast on the hot insert path).
--     The PK doubles as the dedupe uniqueness guard: the webhook does an
--     ``INSERT ... ON CONFLICT (delivery_id) DO NOTHING`` and treats a
--     zero-rowcount result as "already seen → reject as replay".
--   - ``event`` records the ``X-GitHub-Event`` header (push / ping / …) purely
--     for operational forensics ("what did we dedupe?"). NULLABLE — a malformed
--     request may omit it, and we still want the delivery id recorded.
--   - ``received_at`` drives the retention sweep. Rows older than 7 days are
--     pruned opportunistically on each insert (see
--     ``app.sync.webhook_deliveries.record_delivery``), so the table stays
--     small (GitHub delivery ids are unique and low-volume) without needing a
--     scheduled job. 7 days comfortably exceeds GitHub's redelivery window.
--   - ``idx_webhook_deliveries_received_at`` keeps the retention DELETE
--     (``WHERE received_at < now() - interval '7 days'``) index-backed.
--
-- Idempotency:
--   - ``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS`` make the
--     migration safe to re-run.
--
-- ROLLBACK procedure (manual; requires app on pre-#853 code):
--   DROP TABLE IF EXISTS webhook_deliveries;
--   DELETE FROM schema_migrations WHERE version = '039_webhook_deliveries';
--   Then redeploy the previous app image. No durable state is lost — the table
--   only holds transient replay-dedupe records.
-- =============================================================================

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    delivery_id  TEXT        PRIMARY KEY,                     -- GitHub X-GitHub-Delivery UUID
    event        TEXT,                                        -- X-GitHub-Event (push / ping / …); nullable
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_received_at
    ON webhook_deliveries (received_at);
