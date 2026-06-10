"""Replay-protection store for GitHub webhook deliveries (#853 / H5).

GitHub stamps every webhook POST with a unique ``X-GitHub-Delivery`` UUID.
A replayed payload keeps its (valid) HMAC signature forever, so signature
verification alone cannot stop a capture-and-replay attack or an
accidental GitHub redelivery from re-triggering a full ontology reload.

This module persists processed delivery ids in the ``webhook_deliveries``
table (``migrations/039_webhook_deliveries.sql``) and exposes a single
atomic "have we seen this before?" primitive used by the webhook handler.

The dedupe is done with ``INSERT ... ON CONFLICT (delivery_id) DO NOTHING``
so two concurrent deliveries with the same id can never both be treated as
new — exactly one INSERT affects a row; the other sees ``rowcount == 0``.
Retention is opportunistic: every insert also prunes rows older than the
retention window, keeping the table small without a scheduled job.
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
