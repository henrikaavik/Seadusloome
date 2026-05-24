"""Ontology snapshot tag for chat messages (issue #352).

This is the chat-module's local helper for resolving the live ontology
snapshot tag. The format intentionally mirrors what
``app.docs.analyze_handler._get_current_ontology_version`` and
``app.docs.report_routes._current_ontology_version`` write into
``impact_reports.ontology_version`` (#345 / migration 032) so a future
audit query can compare a chat message's snapshot to an impact report's
snapshot with a direct string equality check.

We keep a separate copy rather than importing from ``app.docs`` so the
chat module does not pull the impact-analysis dependency tree (and so a
test patching one path does not silently affect the other module's
behaviour).

Format: ``"<iso-timestamp>@<entity_count>"`` taken from the most recent
successful ``sync_log`` row, or ``"unknown"`` when the table is empty or
the query fails — the same fallback the analyze handler uses so the
"unknown != unknown" drift case never triggers a false-positive banner
(both sides of the comparison resolve to ``"unknown"``).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.db import get_connection

logger = logging.getLogger(__name__)


def get_current_ontology_version() -> str:
    """Return the live Jena sync snapshot tag.

    Best-effort: any DB error (sync_log missing, pool starved, ...)
    degrades to ``"unknown"`` so the drift detection never blocks chat.
    Matches the format used by
    :func:`app.docs.analyze_handler._get_current_ontology_version` so
    direct string equality detects drift.
    """
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT started_at, entity_count
                FROM sync_log
                WHERE status = 'success'
                ORDER BY started_at DESC
                LIMIT 1
                """
            ).fetchone()
    except Exception:  # noqa: BLE001 — reproducibility tag is best-effort
        logger.warning(
            "chat.ontology_version: could not read sync_log; treating as 'unknown'",
            exc_info=True,
        )
        return "unknown"

    if row is None:
        return "unknown"
    started_at, entity_count = row
    if started_at is None:
        return "unknown"
    if isinstance(started_at, datetime):
        ts = started_at.astimezone(UTC).isoformat()
    else:
        ts = str(started_at)
    return f"{ts}@{entity_count or 0}"
