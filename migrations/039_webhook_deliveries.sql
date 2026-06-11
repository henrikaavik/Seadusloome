-- =============================================================================
-- Migration 039: webhook_deliveries + sync_rerun_request (#853)
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
-- Round-2 review (#853): a push arriving WHILE a sync is already running was
-- recorded as processed but never synced — ``trigger_sync_background`` returns
-- False (another sync in flight) and the commit was stranded until some
-- unrelated later push, because GitHub does not auto-retry push deliveries.
-- The fix is a durable, cross-process **coalescing rerun**: when a valid push
-- lands mid-sync we still record the delivery (dedupe stays correct precisely
-- because the work is now durably scheduled) AND set a single pending-rerun
-- flag. The orchestrator drains that flag at the end of every sync and runs
-- exactly one more pass, coalescing N mid-sync pushes into one rerun. That
-- flag lives in ``sync_rerun_request`` (below).
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
-- ``sync_rerun_request`` design decisions:
--   - SINGLE-ROW coalescing table. The ``id`` column is a BOOLEAN pinned to
--     TRUE with a CHECK, so there is at most one row ever: every "please rerun
--     after the current sync" request is an UPSERT onto that one row
--     (``ON CONFLICT (id) DO UPDATE``). N mid-sync pushes therefore collapse
--     into one pending rerun — exactly the coalescing the fix needs.
--   - ``requested_at`` / ``requested_by`` are forensic only (when, and which
--     delivery id last set the flag). The mere EXISTENCE of the row is the
--     flag; consuming it is a ``DELETE ... RETURNING`` so that, if two drainers
--     race, only the one that actually removes the row "wins" and triggers the
--     single rerun.
--   - No retention needed: the table holds 0 or 1 row by construction.
--   - Round-3 review (#853): for a push that arrives mid-sync, the dedupe
--     INSERT into ``webhook_deliveries`` and this rerun-flag UPSERT are done
--     in ONE transaction on ONE connection
--     (``app.sync.webhook_deliveries.record_delivery_and_request_rerun``), so a
--     flag-write failure rolls back the delivery too — the delivery id is NOT
--     consumed and a GitHub manual redelivery can retry. The two tables are
--     therefore written together for that path even though they are separate
--     relations.
--
-- Idempotency:
--   - ``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS`` make the
--     migration safe to re-run.
--
-- ROLLBACK procedure (manual; requires app on pre-#853 code):
--   DROP TABLE IF EXISTS webhook_deliveries;
--   DROP TABLE IF EXISTS sync_rerun_request;
--   DELETE FROM schema_migrations WHERE version = '039_webhook_deliveries';
--   Then redeploy the previous app image. No durable state is lost — both
--   tables only hold transient dedupe / coalescing records.
-- =============================================================================

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    delivery_id  TEXT        PRIMARY KEY,                     -- GitHub X-GitHub-Delivery UUID
    event        TEXT,                                        -- X-GitHub-Event (push / ping / …); nullable
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_received_at
    ON webhook_deliveries (received_at);

-- Single-row coalescing flag: a pending "rerun sync once the current run
-- finishes" request. At most one row exists (id pinned to TRUE).
CREATE TABLE IF NOT EXISTS sync_rerun_request (
    id            BOOLEAN     PRIMARY KEY DEFAULT TRUE CHECK (id),  -- always TRUE → single row
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    requested_by  TEXT                                              -- delivery id that last set it; forensic only
);
