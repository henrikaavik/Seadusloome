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

**Joining a caller's transaction** — a caller that needs the insert to
be atomic with its own work (e.g. the cost-alert dedupe in
``app/notifications/wire.py``, which takes an advisory lock and must
insert under it so two concurrent budget checks can't both fan out) may
pass an open ``conn``. In that mode the insert runs on the caller's
connection and ``notify`` does **not** commit and does **not** push —
the caller owns the transaction boundary and is responsible for the
post-commit WS push via :func:`push_notification`. For every other
caller (``conn=None``) the behaviour is unchanged: own connection,
commit, fire-and-forget push.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from app.db import get_connection
from app.notifications.models import Notification, create_notification

if TYPE_CHECKING:
    import psycopg

logger = logging.getLogger(__name__)


def push_notification(result: Notification) -> None:
    """Fire-and-forget the real-time WS push for an already-durable row.

    Split out of :func:`notify` so a caller that inserts inside its own
    transaction (``notify(..., conn=conn)``) can perform the push *after*
    it commits — the DB row must be durable before we announce it. The
    push is wrapped in its own try/except so a WS failure never disturbs
    the primary workflow or the durable write that already happened.
    """
    try:
        from app.notifications.websocket import push_to_user

        push_to_user(
            result.user_id,
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
            result.user_id,
            exc_info=True,
        )


def notify(
    user_id: UUID | str,
    type: str,
    title: str,
    body: str | None = None,
    link: str | None = None,
    metadata: dict[str, Any] | None = None,
    *,
    conn: psycopg.Connection | None = None,  # type: ignore[type-arg]
) -> Notification | None:
    """Fire-and-forget notification creation. Swallows DB errors.

    With ``conn=None`` (the default, every existing caller): opens a new
    DB connection, inserts the notification, commits, closes, then
    fire-and-forgets a WS push. Any exception is logged at WARNING level
    but never propagated.

    With an open ``conn``: inserts on that connection and returns the
    created :class:`~app.notifications.models.Notification` **without
    committing and without pushing**. The caller owns the transaction —
    it must ``conn.commit()`` and then call :func:`push_notification` for
    each returned row once the commit succeeds. This lets a caller make
    the insert atomic with surrounding work (e.g. an advisory-lock-guarded
    dedupe). Returns ``None`` if the insert failed.

    Returns the created notification (both modes) or ``None`` on failure.
    """
    if conn is not None:
        # Join the caller's transaction: insert only. The caller commits
        # and pushes. ``create_notification`` already swallows its own DB
        # errors and returns None, so a failed insert here does not abort
        # the caller's transaction by raising.
        return create_notification(
            conn,
            user_id=user_id,
            type=type,
            title=title,
            body=body,
            link=link,
            metadata=metadata,
        )

    try:
        with get_connection() as own_conn:
            result = create_notification(
                own_conn,
                user_id=user_id,
                type=type,
                title=title,
                body=body,
                link=link,
                metadata=metadata,
            )
            if result is not None:
                own_conn.commit()
    except Exception:
        logger.warning(
            "Failed to send notification type=%s to user_id=%s (non-critical)",
            type,
            user_id,
            exc_info=True,
        )
        return None

    # #180 — real-time push. Import locally so importing this module
    # never triggers WS module import side effects (the WS module
    # captures a module-level lock + an event-loop slot).
    if result is None:
        return None

    push_notification(result)
    return result
