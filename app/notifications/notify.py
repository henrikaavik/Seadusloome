"""Fire-and-forget notification helper.

Usage::

    from app.notifications.notify import notify

    notify(user_id, "analysis_done", "Mõjuanalüüs valmis", link="/drafts/123")

The function opens its own DB connection, commits, and swallows all
errors so callers never have to worry about notification failures
disrupting the primary workflow.
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
