"""``annotations`` and ``annotation_replies`` table dataclasses + query helpers.

Every query helper follows the same pattern as ``app/chat/models.py``:

    - Explicit ``conn`` parameter from the caller
    - ``conn.commit()`` on writes is the caller's responsibility
    - Exceptions are logged and the function returns a sentinel value
      (``None`` / empty list) rather than raising, so a dead DB never
      takes down the whole request
    - Org scoping: list queries include ``AND org_id = %s`` where appropriate

Single-item lookups return None if the row doesn't exist; callers are
expected to compare ``annotation.org_id`` against the current user's
``org_id`` for access control.
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
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Annotation:
    """Snapshot of a row in the ``annotations`` table."""

    id: uuid.UUID
    user_id: uuid.UUID
    org_id: uuid.UUID
    target_type: str
    target_id: str
    target_metadata: dict | None
    content: str
    resolved: bool
    resolved_by: uuid.UUID | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass
class AnnotationReply:
    """Snapshot of a row in the ``annotation_replies`` table."""

    id: uuid.UUID
    annotation_id: uuid.UUID
    user_id: uuid.UUID
    content: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_ANNOTATION_COLUMNS = (
    "id, user_id, org_id, target_type, target_id, target_metadata, "
    "content, resolved, resolved_by, resolved_at, created_at, updated_at"
)

_REPLY_COLUMNS = "id, annotation_id, user_id, content, created_at"


def _row_to_annotation(row: tuple[Any, ...]) -> Annotation:
    """Build an ``Annotation`` from a raw cursor row."""
    (
        ann_id,
        user_id,
        org_id,
        target_type,
        target_id,
        target_metadata_raw,
        content,
        resolved,
        resolved_by,
        resolved_at,
        created_at,
        updated_at,
    ) = row

    target_metadata = parse_jsonb(target_metadata_raw)
    if target_metadata is not None and not isinstance(target_metadata, dict):
        target_metadata = None

    return Annotation(
        id=coerce_uuid(ann_id),
        user_id=coerce_uuid(user_id),
        org_id=coerce_uuid(org_id),
        target_type=target_type,
        target_id=str(target_id),
        target_metadata=target_metadata,
        content=content,
        resolved=bool(resolved),
        resolved_by=coerce_uuid(resolved_by) if resolved_by else None,
        resolved_at=resolved_at,
        created_at=created_at,
        updated_at=updated_at,
    )


def _row_to_reply(row: tuple[Any, ...]) -> AnnotationReply:
    """Build an ``AnnotationReply`` from a raw cursor row."""
    (
        reply_id,
        annotation_id,
        user_id,
        content,
        created_at,
    ) = row

    return AnnotationReply(
        id=coerce_uuid(reply_id),
        annotation_id=coerce_uuid(annotation_id),
        user_id=coerce_uuid(user_id),
        content=content,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# Annotation CRUD
# ---------------------------------------------------------------------------


VALID_TARGET_TYPES = ("draft", "provision", "conversation", "entity")


def create_annotation(
    conn: Any,
    user_id: uuid.UUID | str,
    org_id: uuid.UUID | str,
    target_type: str,
    target_id: str,
    content: str,
    target_metadata: dict | None = None,
) -> Annotation:
    """Insert a new ``annotations`` row and return the created annotation.

    The caller is responsible for committing the transaction.
    """
    if target_type not in VALID_TARGET_TYPES:
        raise ValueError(f"Invalid target_type: {target_type!r}")

    row = conn.execute(
        f"""
        INSERT INTO annotations
            (user_id, org_id, target_type, target_id, target_metadata, content)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s)
        RETURNING {_ANNOTATION_COLUMNS}
        """,
        (
            str(user_id),
            str(org_id),
            target_type,
            target_id,
            json.dumps(target_metadata) if target_metadata is not None else None,
            content,
        ),
    ).fetchone()
    if row is None:
        raise RuntimeError("INSERT ... RETURNING annotations produced no row")
    return _row_to_annotation(row)


def list_annotations_for_target(
    conn: Any,
    target_type: str,
    target_id: str,
    org_id: uuid.UUID | str,
) -> list[Annotation]:
    """Return annotations for a target within an org, newest first.

    Results are always org-scoped to prevent cross-org data leakage.
    """
    try:
        rows = conn.execute(
            f"""
            SELECT {_ANNOTATION_COLUMNS}
            FROM annotations
            WHERE target_type = %s
              AND target_id = %s
              AND org_id = %s
            ORDER BY created_at DESC
            """,
            (target_type, target_id, str(org_id)),
        ).fetchall()
    except Exception:
        logger.exception(
            "Failed to list annotations for target_type=%s target_id=%s org=%s",
            target_type,
            target_id,
            org_id,
        )
        return []
    return [_row_to_annotation(row) for row in rows]


def get_annotation(
    conn: Any,
    annotation_id: uuid.UUID | str,
) -> Annotation | None:
    """Return a single annotation by id, or ``None``."""
    try:
        row = conn.execute(
            f"SELECT {_ANNOTATION_COLUMNS} FROM annotations WHERE id = %s",
            (str(annotation_id),),
        ).fetchone()
    except Exception:
        logger.exception("Failed to fetch annotation id=%s", annotation_id)
        return None
    return _row_to_annotation(row) if row else None


def resolve_annotation(
    conn: Any,
    annotation_id: uuid.UUID | str,
    resolved_by_user_id: uuid.UUID | str,
) -> Annotation | None:
    """Mark an annotation as resolved and return the updated row.

    The caller is responsible for committing the transaction.
    Returns ``None`` if the annotation does not exist or the DB errors.
    """
    try:
        row = conn.execute(
            f"""
            UPDATE annotations
            SET resolved = TRUE,
                resolved_by = %s,
                resolved_at = now(),
                updated_at = now()
            WHERE id = %s
            RETURNING {_ANNOTATION_COLUMNS}
            """,
            (str(resolved_by_user_id), str(annotation_id)),
        ).fetchone()
    except Exception:
        logger.exception("Failed to resolve annotation id=%s", annotation_id)
        return None
    return _row_to_annotation(row) if row else None


def delete_annotation(
    conn: Any,
    annotation_id: uuid.UUID | str,
) -> None:
    """Delete an annotation. FK CASCADE removes associated replies.

    The caller is responsible for committing the transaction.
    """
    conn.execute(
        "DELETE FROM annotations WHERE id = %s",
        (str(annotation_id),),
    )


# ---------------------------------------------------------------------------
# Reply CRUD
# ---------------------------------------------------------------------------


def create_reply(
    conn: Any,
    annotation_id: uuid.UUID | str,
    user_id: uuid.UUID | str,
    content: str,
) -> AnnotationReply:
    """Insert a new ``annotation_replies`` row and return the created reply.

    The caller is responsible for committing the transaction.
    """
    row = conn.execute(
        f"""
        INSERT INTO annotation_replies (annotation_id, user_id, content)
        VALUES (%s, %s, %s)
        RETURNING {_REPLY_COLUMNS}
        """,
        (
            str(annotation_id),
            str(user_id),
            content,
        ),
    ).fetchone()
    if row is None:
        raise RuntimeError("INSERT ... RETURNING annotation_replies produced no row")
    return _row_to_reply(row)


def list_replies(
    conn: Any,
    annotation_id: uuid.UUID | str,
) -> list[AnnotationReply]:
    """Return all replies for an annotation, ordered by ``created_at`` ASC."""
    try:
        rows = conn.execute(
            f"""
            SELECT {_REPLY_COLUMNS}
            FROM annotation_replies
            WHERE annotation_id = %s
            ORDER BY created_at ASC
            """,
            (str(annotation_id),),
        ).fetchall()
    except Exception:
        logger.exception(
            "Failed to list replies for annotation=%s",
            annotation_id,
        )
        return []
    return [_row_to_reply(row) for row in rows]
