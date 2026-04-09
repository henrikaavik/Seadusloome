"""Audit logging for the Seadusloome application.

Inserts records into the ``audit_log`` table.  All database errors are
caught and logged so that audit failures never break the calling code.
"""

from __future__ import annotations

import json
import logging

from app.db import get_connection

logger = logging.getLogger(__name__)


def log_action(
    user_id: str | None,
    action: str,
    detail: dict | None = None,  # type: ignore[type-arg]
) -> None:
    """Record an auditable action.

    Parameters
    ----------
    user_id:
        UUID of the acting user, or ``None`` for system-level events.
    action:
        Short action label, e.g. ``"org.create"`` or ``"user.deactivate"``.
    detail:
        Optional JSON-serialisable dict with contextual data.

    This function is intentionally fire-and-forget: database errors are
    logged but never raised to the caller.
    """
    try:
        detail_json = json.dumps(detail, default=str) if detail else None
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO audit_log (user_id, action, detail) VALUES (%s, %s, %s::jsonb)",
                (user_id, action, detail_json),
            )
            conn.commit()
    except Exception:
        logger.exception("Failed to write audit log: action=%s user_id=%s", action, user_id)
