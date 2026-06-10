"""Durable webhook-delivery state for GitHub push sync (#853).

Two concerns, both backed by ``migrations/039_webhook_deliveries.sql``:

1. **Replay protection (H5).** GitHub stamps every webhook POST with a
   unique ``X-GitHub-Delivery`` UUID. A replayed payload keeps its (valid)
   HMAC signature forever, so signature verification alone cannot stop a
   capture-and-replay attack or an accidental redelivery from re-triggering
   a full ontology reload. We persist processed delivery ids in
   ``webhook_deliveries`` and expose an atomic "have we seen this before?"
   primitive (:func:`record_delivery`). The dedupe is an
   ``INSERT ... ON CONFLICT (delivery_id) DO NOTHING`` so two concurrent
   deliveries with the same id can never both be treated as new. Retention
   is opportunistic: every insert prunes rows older than the window.

2. **Coalescing rerun (round-2 review).** A push arriving WHILE a sync is
   already running used to be recorded-but-never-synced (the running sync
   had already cloned an older tree, and GitHub does not auto-retry push
   deliveries). The fix is a durable, cross-process pending-rerun flag in
   the single-row ``sync_rerun_request`` table: the webhook sets it
   (:func:`request_rerun`) when it lands mid-sync, and the orchestrator
   drains it (:func:`consume_rerun_request`) at the end of every run,
   triggering exactly one more pass. N mid-sync pushes coalesce into one
   rerun because they all UPSERT the same single row.
"""

from __future__ import annotations

import logging

from app.db import get_connection

logger = logging.getLogger(__name__)

# GitHub redelivers within a short window; 7 days is a generous superset
# that still keeps the table tiny given delivery ids are low-volume.
_RETENTION_DAYS = 7


def record_delivery(delivery_id: str, event: str | None = None) -> bool:
    """Record a webhook delivery id, returning True only if it is new.

    Atomically inserts ``delivery_id``. A row that already exists yields
    ``rowcount == 0`` → this is a replay/duplicate and the caller must
    reject it. The same call opportunistically deletes delivery records
    older than the retention window so the table never grows unbounded.

    Args:
        delivery_id: The ``X-GitHub-Delivery`` header value. Must be a
            non-empty string — a missing/blank id is treated as "cannot
            dedupe" and the function returns ``False`` (reject) so we fail
            closed rather than process an unidentifiable delivery.
        event: The ``X-GitHub-Event`` header, stored for forensics only.

    Returns:
        ``True`` if this delivery id had not been seen before (caller may
        proceed). ``False`` if it is a duplicate, the id is blank, or a DB
        error occurred — in every "not clearly new" case we fail closed.
    """
    if not delivery_id:
        logger.warning("Webhook delivery missing X-GitHub-Delivery id — rejecting")
        return False

    try:
        with get_connection() as conn:
            # Opportunistic retention sweep. Index-backed by
            # idx_webhook_deliveries_received_at. ``%s::interval`` (not
            # ``interval %s``) per the psycopg substitution rule.
            conn.execute(
                "DELETE FROM webhook_deliveries "
                "WHERE received_at < now() - (%s || ' days')::interval",
                (str(_RETENTION_DAYS),),
            )
            row = conn.execute(
                "INSERT INTO webhook_deliveries (delivery_id, event) "
                "VALUES (%s, %s) "
                "ON CONFLICT (delivery_id) DO NOTHING "
                "RETURNING delivery_id",
                (delivery_id, event),
            ).fetchone()
            conn.commit()
        if row is None:
            logger.info("Webhook delivery %s already processed — rejecting replay", delivery_id)
            return False
        return True
    except Exception:
        # Fail closed: if we can't prove the delivery is new, don't run a
        # full ontology reload off it. A transient DB blip will be retried
        # by GitHub with the SAME delivery id, so we won't lose the event.
        logger.exception("Failed to record webhook delivery %s — rejecting", delivery_id)
        return False


# ---------------------------------------------------------------------------
# Coalescing rerun flag (round-2 review of #853)
# ---------------------------------------------------------------------------
#
# A push that arrives while a sync is already running must still get its
# commit synced — but starting a second concurrent sync is wrong (the
# advisory lock would reject it anyway, and the staging graph is shared).
# Instead the webhook sets a single durable "rerun once the current run
# finishes" flag; the orchestrator drains it after each run. The flag is a
# single row in ``sync_rerun_request`` so N mid-sync pushes coalesce into
# exactly one rerun.


def request_rerun(delivery_id: str | None = None) -> bool:
    """Set the durable pending-rerun flag (idempotent / coalescing).

    UPSERTs the single ``sync_rerun_request`` row. Calling it N times while
    a sync runs leaves exactly one pending rerun — the orchestrator will
    drain it once after the current run completes.

    Args:
        delivery_id: The delivery id that triggered this rerun request,
            stored for forensics only.

    Returns:
        ``True`` if the flag is set after the call, ``False`` on DB error.
        A ``False`` here means the mid-sync push could not be durably
        scheduled; the caller must NOT then record the delivery as
        processed (otherwise the commit would be stranded).
    """
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO sync_rerun_request (id, requested_at, requested_by) "
                "VALUES (TRUE, now(), %s) "
                "ON CONFLICT (id) DO UPDATE "
                "SET requested_at = now(), requested_by = EXCLUDED.requested_by",
                (delivery_id,),
            )
            conn.commit()
        return True
    except Exception:
        logger.exception("Failed to set sync rerun flag (delivery %s)", delivery_id)
        return False


def consume_rerun_request() -> bool:
    """Atomically clear the pending-rerun flag, returning whether it was set.

    Uses ``DELETE ... RETURNING`` so that if two drainers race, only the
    one that actually removes the row sees ``True`` and triggers the single
    rerun; the loser sees ``False``. A DB error returns ``False`` (treat as
    "nothing to drain") — the flag stays set and the next drain picks it up.

    Returns:
        ``True`` if a rerun was pending (and is now cleared), else ``False``.
    """
    try:
        with get_connection() as conn:
            row = conn.execute(
                "DELETE FROM sync_rerun_request WHERE id = TRUE RETURNING id"
            ).fetchone()
            conn.commit()
        return row is not None
    except Exception:
        logger.exception("Failed to consume sync rerun flag")
        return False
