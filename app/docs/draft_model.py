"""``drafts`` table dataclass + query helpers.

Mirrors ``migrations/005_phase2_document_upload.sql`` for the ``drafts``
table. Every query helper enforces the same connection/logging pattern as
``app/auth/users.py`` and ``app/auth/organizations.py``:

    - ``_connect()`` context manager from ``app.db``
    - ``conn.commit()`` on writes
    - exceptions are logged and the function returns a sentinel value
      (``None`` / ``False`` / empty list) rather than raising, so a dead
      DB never takes down the whole request

Org scoping is enforced at the query layer: ``list_drafts_for_org`` and
``count_drafts_for_org`` always include ``WHERE org_id = %s``. Callers
are still expected to compare ``draft.org_id`` against the current user's
``org_id`` for single-draft operations, but the helpers make it hard to
accidentally leak other orgs' drafts in listing endpoints.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from app.db import get_connection as _connect
from app.db_utils import coerce_uuid

logger = logging.getLogger(__name__)


VALID_STATUSES = (
    "uploaded",
    "parsing",
    "extracting",
    "analyzing",
    "ready",
    "failed",
)


@dataclass
class Draft:
    """Snapshot of a row in the ``drafts`` table.

    ``id``, ``user_id`` and ``org_id`` are real ``uuid.UUID`` values so
    callers can pass them back into queries without string round-trips.
    Optional columns (``parsed_text_encrypted``, ``entity_count``, ``error_message``)
    are ``None`` until the background pipeline populates them.

    ``doc_type`` discriminates regular eelnoud (``'eelnou'``) from VTKd
    (``'vtk'``).  ``parent_vtk_id`` links an eelnou back to the VTK it
    originates from; both fields default to safe values so existing callers
    need no changes (migration 019).
    """

    id: uuid.UUID
    user_id: uuid.UUID
    org_id: uuid.UUID
    title: str
    filename: str
    content_type: str
    file_size: int
    storage_path: str
    graph_uri: str
    status: str
    parsed_text_encrypted: bytes | None
    entity_count: int | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    last_accessed_at: datetime | None = None
    doc_type: Literal["eelnou", "vtk"] = "eelnou"
    parent_vtk_id: uuid.UUID | None = None


# Column order used by every SELECT in this module. Kept in sync with
# ``_row_to_draft`` so the two never drift.
_DRAFT_COLUMNS = (
    "id, user_id, org_id, title, filename, content_type, file_size, "
    "storage_path, graph_uri, status, parsed_text_encrypted, entity_count, "
    "error_message, created_at, updated_at, last_accessed_at, "
    "doc_type, parent_vtk_id"
)


def _row_to_draft(row: tuple[Any, ...]) -> Draft:
    """Build a ``Draft`` dataclass from a raw cursor row."""
    (
        draft_id,
        user_id,
        org_id,
        title,
        filename,
        content_type,
        file_size,
        storage_path,
        graph_uri,
        status,
        parsed_text_encrypted,
        entity_count,
        error_message,
        created_at,
        updated_at,
        last_accessed_at,
        doc_type,
        parent_vtk_id,
    ) = row
    return Draft(
        id=coerce_uuid(draft_id),
        user_id=coerce_uuid(user_id),
        org_id=coerce_uuid(org_id),
        title=title,
        filename=filename,
        content_type=content_type,
        file_size=int(file_size),
        storage_path=storage_path,
        graph_uri=graph_uri,
        status=status,
        parsed_text_encrypted=parsed_text_encrypted,
        entity_count=entity_count,
        error_message=error_message,
        created_at=created_at,
        updated_at=updated_at,
        last_accessed_at=last_accessed_at,
        doc_type=doc_type,
        parent_vtk_id=coerce_uuid(parent_vtk_id) if parent_vtk_id else None,
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def create_draft(
    conn: Any,
    *,
    user_id: uuid.UUID | str,
    org_id: uuid.UUID | str,
    title: str,
    filename: str,
    content_type: str,
    file_size: int,
    storage_path: str,
    graph_uri: str,
    status: str = "uploaded",
    doc_type: Literal["eelnou", "vtk"] = "eelnou",
    parent_vtk_id: uuid.UUID | str | None = None,
) -> Draft:
    """Insert a new ``drafts`` row and return the created ``Draft``.

    This helper takes an explicit ``conn`` so the caller can run the
    insert in the same transaction as the row-level side effects
    (file cleanup on failure, audit logging, etc). The caller is
    responsible for committing the transaction.

    Raises on SQL failure -- ``handle_upload`` relies on the exception
    to trigger encrypted-file cleanup.

    ``doc_type`` defaults to ``'eelnou'`` so existing callers need no
    changes.  ``parent_vtk_id`` is ``None`` by default (no VTK link).
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid draft status: {status!r}")

    row = conn.execute(
        f"""
        insert into drafts (
            user_id, org_id, title, filename, content_type,
            file_size, storage_path, graph_uri, status,
            doc_type, parent_vtk_id
        ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        returning {_DRAFT_COLUMNS}
        """,
        (
            str(user_id),
            str(org_id),
            title,
            filename,
            content_type,
            file_size,
            storage_path,
            graph_uri,
            status,
            doc_type,
            str(parent_vtk_id) if parent_vtk_id else None,
        ),
    ).fetchone()
    if row is None:
        raise RuntimeError("INSERT ... RETURNING drafts produced no row")
    return _row_to_draft(row)


def update_draft_status(
    conn: Any,
    draft_id: uuid.UUID | str,
    status: str,
    error_message: str | None = None,
) -> bool:
    """Transition a draft into a new ``status`` (and optional error message).

    Returns ``True`` when a row was updated. Unlike the read helpers this
    one takes an explicit connection so the worker can batch the update
    with other state transitions in a single transaction.
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid draft status: {status!r}")

    result = conn.execute(
        """
        update drafts
        set status = %s,
            error_message = %s,
            updated_at = now()
        where id = %s
        """,
        (status, error_message, str(draft_id)),
    )
    return (result.rowcount or 0) > 0


def delete_draft(conn: Any, draft_id: uuid.UUID | str) -> str | None:
    """Delete a draft row and return its ``storage_path`` for file cleanup.

    The ``drafts`` table has ``ON DELETE CASCADE`` into ``draft_entities``
    and ``impact_reports``, so removing the row here automatically clears
    related records. The returned ``storage_path`` lets the caller delete
    the encrypted file *after* the DB row is gone so we never orphan
    ciphertext while the DB still points at it.

    Returns ``None`` when the draft did not exist.
    """
    row = conn.execute(
        "select storage_path from drafts where id = %s",
        (str(draft_id),),
    ).fetchone()
    if row is None:
        return None
    storage_path = row[0]
    conn.execute("delete from drafts where id = %s", (str(draft_id),))
    return storage_path


def touch_draft_access(conn: Any, draft_id: uuid.UUID | str) -> bool:
    """Reset the ``last_accessed_at`` clock on a draft (issue #572).

    Called from every route that surfaces a draft to an end user so the
    90-day auto-archive warning stays correctly timed. The caller is
    responsible for committing the transaction; errors are logged but
    never raised -- an audit-style touch failure must never break the
    primary read path.

    Returns ``True`` when a row was actually updated.
    """
    try:
        result = conn.execute(
            "update drafts set last_accessed_at = now() where id = %s",
            (str(draft_id),),
        )
    except Exception:
        logger.exception("Failed to touch last_accessed_at for draft=%s", draft_id)
        return False
    return (result.rowcount or 0) > 0


def touch_draft_access_conn(draft_id: uuid.UUID | str) -> bool:
    """Open a fresh connection and touch ``last_accessed_at``.

    Route handlers use this instead of wiring up their own
    ``_connect()`` block when they only need to bump the access time.
    Commits on success; swallows all errors.
    """
    try:
        with _connect() as conn:
            updated = touch_draft_access(conn, draft_id)
            if updated:
                conn.commit()
            return updated
    except Exception:
        logger.exception("touch_draft_access_conn failed for draft=%s", draft_id)
        return False


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get_draft(conn: Any, draft_id: uuid.UUID | str) -> Draft | None:
    """Return a single draft by id, or ``None``."""
    try:
        row = conn.execute(
            f"select {_DRAFT_COLUMNS} from drafts where id = %s",
            (str(draft_id),),
        ).fetchone()
    except Exception:
        logger.exception("Failed to fetch draft id=%s", draft_id)
        return None
    return _row_to_draft(row) if row else None


def list_drafts_for_org(
    conn: Any,
    org_id: uuid.UUID | str,
    *,
    limit: int = 25,
    offset: int = 0,
) -> list[Draft]:
    """Return drafts owned by *org_id*, newest first.

    The ``WHERE org_id = %s`` clause is load-bearing: every listing call
    in a route handler **must** pass the caller's org_id so we never
    return rows from another organisation.
    """
    if limit <= 0:
        return []
    try:
        rows = conn.execute(
            f"""
            select {_DRAFT_COLUMNS}
            from drafts
            where org_id = %s
            order by created_at desc
            limit %s offset %s
            """,
            (str(org_id), limit, max(0, offset)),
        ).fetchall()
    except Exception:
        logger.exception("Failed to list drafts for org=%s", org_id)
        return []
    return [_row_to_draft(row) for row in rows]


def count_drafts_for_org(conn: Any, org_id: uuid.UUID | str) -> int:
    """Return the number of drafts owned by *org_id*."""
    try:
        row = conn.execute(
            "select count(*) from drafts where org_id = %s",
            (str(org_id),),
        ).fetchone()
    except Exception:
        logger.exception("Failed to count drafts for org=%s", org_id)
        return 0
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# Convenience wrappers that manage their own connection
# ---------------------------------------------------------------------------


def fetch_draft(draft_id: uuid.UUID | str) -> Draft | None:
    """Open a fresh connection and return a draft by id.

    Route handlers use this instead of wiring their own ``_connect()``
    block when they just need to read a single draft.
    """
    try:
        with _connect() as conn:
            return get_draft(conn, draft_id)
    except Exception:
        logger.exception("fetch_draft failed for id=%s", draft_id)
        return None


def fetch_drafts_for_org(
    org_id: uuid.UUID | str,
    *,
    limit: int = 25,
    offset: int = 0,
) -> list[Draft]:
    """Open a fresh connection and list drafts for *org_id*."""
    try:
        with _connect() as conn:
            return list_drafts_for_org(conn, org_id, limit=limit, offset=offset)
    except Exception:
        logger.exception("fetch_drafts_for_org failed for org=%s", org_id)
        return []


def count_drafts_for_org_conn(org_id: uuid.UUID | str) -> int:
    """Open a fresh connection and return the draft count for *org_id*."""
    try:
        with _connect() as conn:
            return count_drafts_for_org(conn, org_id)
    except Exception:
        logger.exception("count_drafts_for_org_conn failed for org=%s", org_id)
        return 0
