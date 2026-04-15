"""Per-message thumbs-up / thumbs-down feedback helpers.

Mirrors the ``message_feedback`` table added in
``migrations/017_chat_ux_features.sql``:

    CREATE TABLE message_feedback (
        id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        message_id UUID NOT NULL REFERENCES messages(id)  ON DELETE CASCADE,
        user_id    UUID NOT NULL REFERENCES users(id)     ON DELETE CASCADE,
        rating     SMALLINT NOT NULL CHECK (rating IN (-1, 1)),
        comment    TEXT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (message_id, user_id)
    );

Follows the same query-helper conventions as ``app.chat.models``:

    * Explicit ``conn`` parameter from the caller
    * ``conn.commit()`` on writes is the caller's responsibility
    * **Reads** (:func:`get_feedback`, :func:`feedback_counts`) swallow DB
      exceptions, log them, and return a safe default (``None`` / ``(0, 0)``).
      The feedback widget is a non-critical UI affordance — a transient DB
      blip must not blank the surrounding page. Callers therefore treat the
      safe default as a normal result.
    * **Writes** (:func:`upsert_feedback`, :func:`delete_feedback`) raise
      on error. Silently swallowing a write would lose a user's vote, so
      the HTTP handler converts the exception into a 500 for the client
      to retry explicitly.

Kept in a separate module (``feedback.py`` vs. ``models.py``) so the
feedback surface can grow — comment moderation, admin analytics, etc. —
without bloating the chat message model.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.db_utils import coerce_uuid

logger = logging.getLogger(__name__)


VALID_RATINGS = frozenset({-1, 1})

_FEEDBACK_COLUMNS = "id, message_id, user_id, rating, comment, created_at"


@dataclass(frozen=True)
class MessageFeedback:
    """Snapshot of a row in the ``message_feedback`` table."""

    id: uuid.UUID
    message_id: uuid.UUID
    user_id: uuid.UUID
    rating: int  # -1 or +1
    comment: str | None
    created_at: datetime


def _row_to_feedback(row: tuple[Any, ...]) -> MessageFeedback:
    """Build a ``MessageFeedback`` from a raw cursor row."""
    fb_id, message_id, user_id, rating, comment, created_at = row
    return MessageFeedback(
        id=coerce_uuid(fb_id),
        message_id=coerce_uuid(message_id),
        user_id=coerce_uuid(user_id),
        rating=int(rating),
        comment=comment,
        created_at=created_at,
    )


def _validate_rating(rating: int) -> int:
    """Coerce to ``int`` and validate against the CHECK constraint."""
    try:
        value = int(rating)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid rating: {rating!r}") from exc
    if value not in VALID_RATINGS:
        raise ValueError(f"Invalid rating {value}: must be -1 (thumbs down) or 1 (thumbs up)")
    return value


def upsert_feedback(
    conn: Any,
    *,
    message_id: uuid.UUID | str,
    user_id: uuid.UUID | str,
    rating: int,
    comment: str | None = None,
) -> MessageFeedback:
    """Insert or update the feedback row for ``(message_id, user_id)``.

    Re-voting (e.g. switching thumbs-up to thumbs-down) overwrites the
    prior rating in place rather than creating a new row; ``created_at``
    is refreshed to ``now()`` so the feedback timeline reflects the most
    recent signal.

    Raises ``ValueError`` if ``rating`` is not in ``{-1, 1}`` so the
    CHECK constraint never fires at the DB level.
    """
    validated_rating = _validate_rating(rating)

    row = conn.execute(
        f"""
        INSERT INTO message_feedback (message_id, user_id, rating, comment)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (message_id, user_id) DO UPDATE
            SET rating = EXCLUDED.rating,
                comment = EXCLUDED.comment,
                created_at = now()
        RETURNING {_FEEDBACK_COLUMNS}
        """,
        (str(message_id), str(user_id), validated_rating, comment),
    ).fetchone()
    if row is None:
        raise RuntimeError("INSERT ... RETURNING message_feedback produced no row")
    return _row_to_feedback(row)


def get_feedback(
    conn: Any,
    message_id: uuid.UUID | str,
    user_id: uuid.UUID | str,
) -> MessageFeedback | None:
    """Return the feedback row for ``(message_id, user_id)``, or ``None``."""
    try:
        row = conn.execute(
            f"""
            SELECT {_FEEDBACK_COLUMNS}
            FROM message_feedback
            WHERE message_id = %s AND user_id = %s
            """,
            (str(message_id), str(user_id)),
        ).fetchone()
    except Exception:
        logger.exception(
            "Failed to fetch feedback message_id=%s user_id=%s",
            message_id,
            user_id,
        )
        return None
    return _row_to_feedback(row) if row else None


def delete_feedback(
    conn: Any,
    message_id: uuid.UUID | str,
    user_id: uuid.UUID | str,
) -> None:
    """Remove the current user's feedback for a message (a "retract vote")."""
    conn.execute(
        """
        DELETE FROM message_feedback
        WHERE message_id = %s AND user_id = %s
        """,
        (str(message_id), str(user_id)),
    )


def feedback_counts(
    conn: Any,
    message_id: uuid.UUID | str,
) -> tuple[int, int]:
    """Return ``(up_count, down_count)`` for a message.

    Returns ``(0, 0)`` on DB error so the caller can render a safe
    default without special-casing failure modes.
    """
    try:
        row = conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN rating =  1 THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN rating = -1 THEN 1 ELSE 0 END), 0)
            FROM message_feedback
            WHERE message_id = %s
            """,
            (str(message_id),),
        ).fetchone()
    except Exception:
        logger.exception(
            "Failed to compute feedback counts for message=%s",
            message_id,
        )
        return (0, 0)
    if row is None:
        return (0, 0)
    up_count, down_count = row
    return (int(up_count or 0), int(down_count or 0))
