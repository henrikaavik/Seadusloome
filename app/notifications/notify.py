"""Fire-and-forget notification helper.

Usage::

    from app.notifications.notify import notify

    notify(user_id, "analysis_done", "Mõjuanalüüs valmis", link="/drafts/123")

The function opens its own DB connection, commits, and swallows all
errors so callers never have to worry about notification failures
disrupting the primary workflow.

When a row is successfully inserted, the helper also fire-and-forgets a
real-time push to any open ``/ws/notifications`` socket the user holds
(#180). The WS push is best-effort: if the loop is not registered or
the user has no live sockets, the bell UI's existing 30 s polling will
still pick the new row up.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from app.db import get_connection
from app.notifications.models import create_notification

logger = logging.getLogger(__name__)


def notify(
    user_id: UUID | str,
    type: str,
    title: str,
    body: str | None = None,
    link: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Fire-and-forget notification creation. Swallows DB errors.

    Opens a new DB connection, inserts the notification, commits, and
    closes. Any exception is logged at WARNING level but never propagated.

    After a successful insert the helper pushes the new notification to
    any live ``/ws/notifications`` socket owned by ``user_id`` so the
    bell badge updates in real time. The push is wrapped in its own
    try/except so a WS failure cannot revert the durable DB write or
    disturb the primary workflow.
    """
    try:
        with get_connection() as conn:
            result = create_notification(
                conn,
                user_id=user_id,
                type=type,
                title=title,
                body=body,
                link=link,
                metadata=metadata,
            )
            if result is not None:
                conn.commit()
    except Exception:
        logger.warning(
            "Failed to send notification type=%s to user_id=%s (non-critical)",
            type,
            user_id,
            exc_info=True,
        )
        return

    # #180 — real-time push. Import locally so importing this module
    # never triggers WS module import side effects (the WS module
    # captures a module-level lock + an event-loop slot).
    if result is None:
        return

    try:
        from app.notifications.websocket import push_to_user

        push_to_user(
            user_id,
            {
                "type": "notification",
                "id": str(result.id),
                "notification_type": result.type,
                "title": result.title,
                "body": result.body,
                "link": result.link,
                "created_at": result.created_at.isoformat()
                if result.created_at is not None
                else None,
            },
        )
    except Exception:
        # Push is fire-and-forget; the DB row is already durable.
        logger.debug(
            "Failed to push WS notification for user_id=%s (non-critical)",
            user_id,
            exc_info=True,
        )
