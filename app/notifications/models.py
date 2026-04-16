"""``notifications`` table dataclass + query helpers.

Follows the same connection/logging pattern as ``app/annotations/models.py``:

    - Explicit ``conn`` parameter from the caller
    - ``conn.commit()`` on writes is the caller's responsibility
    - Exceptions are logged and the function returns a sentinel value
      (``None`` / empty list / 0) rather than raising, so a dead DB
      never takes down the whole request
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.db_utils import coerce_uuid, parse_jsonb

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass
class Notification:
    """Snapshot of a row in the ``notifications`` table."""

    id: uuid.UUID
    user_id: uuid.UUID
    type: str
    title: str
    body: str | None
    link: str | None
    metadata: dict | None
    read: bool
    created_at: datetime


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_NOTIFICATION_COLUMNS = "id, user_id, type, title, body, link, metadata, read, created_at"


def _row_to_notification(row: tuple[Any, ...]) -> Notification:
    """Build a ``Notification`` from a raw cursor row."""
    (
        notif_id,
        user_id,
        notif_type,
        title,
        body,
        link,
        metadata_raw,
        read,
        created_at,
    ) = row

    metadata = parse_jsonb(metadata_raw)
    if metadata is not None and not isinstance(metadata, dict):
        metadata = None

    return Notification(
        id=coerce_uuid(notif_id),
        user_id=coerce_uuid(user_id),
        type=notif_type,
        title=title,
        body=body,
        link=link,
        metadata=metadata,
        read=bool(read),
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def create_notification(
    conn: Any,
    user_id: uuid.UUID | str,
    type: str,
    title: str,
    body: str | None = None,
    link: str | None = None,
    metadata: dict | None = None,
) -> Notification | None:
    """Insert a new ``notifications`` row and return the created notification.

    The caller is responsible for committing the transaction.
    Returns ``None`` on DB errors.
    """
    try:
        row = conn.execute(
            f"""
            INSERT INTO notifications
                (user_id, type, title, body, link, metadata)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            RETURNING {_NOTIFICATION_COLUMNS}
            """,
            (
                str(user_id),
                type,
                title,
                body,
                link,
                json.dumps(metadata) if metadata is not None else None,
            ),
        ).fetchone()
    except Exception:
        logger.exception(
            "Failed to create notification for user_id=%s type=%s",
            user_id,
            type,
        )
        return None
    if row is None:
        return None
    return _row_to_notification(row)


def list_notifications_for_user(
    conn: Any,
    user_id: uuid.UUID | str,
    *,
    unread_only: bool = False,
    limit: int = 20,
) -> list[Notification]:
    """Return notifications for a user, newest first.

    Args:
        conn: Database connection.
        user_id: The user whose notifications to retrieve.
        unread_only: If ``True``, only return unread notifications.
        limit: Maximum number of notifications to return.
    """
    where_clause = "WHERE user_id = %s"
    params: list[Any] = [str(user_id)]

    if unread_only:
        where_clause += " AND read = FALSE"

    try:
        rows = conn.execute(
            f"""
            SELECT {_NOTIFICATION_COLUMNS}
            FROM notifications
            {where_clause}
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (*params, limit),
        ).fetchall()
    except Exception:
        logger.exception(
            "Failed to list notifications for user_id=%s",
            user_id,
        )
        return []
    return [_row_to_notification(row) for row in rows]


def get_notification(
    conn: Any,
    notification_id: uuid.UUID | str,
    user_id: uuid.UUID | str | None = None,
) -> Notification | None:
    """Return a single notification by id, or ``None``.

    When *user_id* is provided, the SELECT also filters on ownership so
    that one user cannot fetch another user's notification.
    """
    try:
        if user_id is not None:
            row = conn.execute(
                f"SELECT {_NOTIFICATION_COLUMNS} FROM notifications "
                "WHERE id = %s AND user_id = %s",
                (str(notification_id), str(user_id)),
            ).fetchone()
        else:
            row = conn.execute(
                f"SELECT {_NOTIFICATION_COLUMNS} FROM notifications WHERE id = %s",
                (str(notification_id),),
            ).fetchone()
    except Exception:
        logger.exception("Failed to fetch notification id=%s", notification_id)
        return None
    return _row_to_notification(row) if row else None


def count_unread(conn: Any, user_id: uuid.UUID | str) -> int:
    """Return the number of unread notifications for a user."""
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id = %s AND read = FALSE",
            (str(user_id),),
        ).fetchone()
    except Exception:
        logger.exception(
            "Failed to count unread notifications for user_id=%s",
            user_id,
        )
        return 0
    return row[0] if row else 0


def mark_read(
    conn: Any,
    notification_id: uuid.UUID | str,
    user_id: uuid.UUID | str | None = None,
) -> bool:
    """Mark a single notification as read.

    The caller is responsible for committing the transaction.
    When *user_id* is provided, the UPDATE also filters on ownership
    so that one user cannot mark another user's notification as read.
    Returns ``True`` if a row was updated, ``False`` otherwise.
    """
    try:
        if user_id is not None:
            result = conn.execute(
                "UPDATE notifications SET read = TRUE "
                "WHERE id = %s AND user_id = %s AND read = FALSE",
                (str(notification_id), str(user_id)),
            )
        else:
            result = conn.execute(
                "UPDATE notifications SET read = TRUE WHERE id = %s AND read = FALSE",
                (str(notification_id),),
            )
        return result.rowcount > 0
    except Exception:
        logger.exception(
            "Failed to mark notification %s as read",
            notification_id,
        )
        return False


def mark_all_read(conn: Any, user_id: uuid.UUID | str) -> int:
    """Mark all unread notifications for a user as read.

    The caller is responsible for committing the transaction.
    Returns the number of rows updated.
    """
    try:
        result = conn.execute(
            "UPDATE notifications SET read = TRUE WHERE user_id = %s AND read = FALSE",
            (str(user_id),),
        )
        return result.rowcount
    except Exception:
        logger.exception(
            "Failed to mark all notifications as read for user_id=%s",
            user_id,
        )
        return 0
