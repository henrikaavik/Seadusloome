"""``drafting_sessions`` table dataclass + query helpers.

Mirrors ``migrations/006_encrypt_parsed_text_and_drafting_tables.sql``
for the ``drafting_sessions`` and ``drafting_session_versions`` tables.

Every query helper enforces the same connection/logging pattern as
``app/docs/draft_model.py``:

    - Explicit ``conn`` parameter from the caller
    - ``conn.commit()`` on writes (caller's responsibility)
    - Exceptions are logged and the function returns a sentinel value
      (``None`` / empty list) rather than raising, so a dead DB never
      takes down the whole request
    - Org scoping: every list query includes ``AND org_id = %s``

Single-item lookups return None if the row doesn't exist; callers are
expected to compare ``session.org_id`` against the current user's
``org_id`` for access control.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.db import get_connection as _connect

logger = logging.getLogger(__name__)


VALID_STATUSES = ("active", "completed", "abandoned")
VALID_WORKFLOW_TYPES = ("full_law", "vtk")


@dataclass
class DraftingSession:
    """Snapshot of a row in the ``drafting_sessions`` table.

    UUID columns are real ``uuid.UUID`` values. Optional encrypted
    columns are ``None`` until the pipeline populates them.
    """

    id: uuid.UUID
    user_id: uuid.UUID
    org_id: uuid.UUID
    workflow_type: str
    current_step: int
    intent: str | None
    clarifications: list[dict[str, Any]]
    research_data_encrypted: bytes | None
    proposed_structure: dict[str, Any] | None
    draft_content_encrypted: bytes | None
    integrated_draft_id: uuid.UUID | None
    status: str
    created_at: datetime
    updated_at: datetime


# Column order used by every SELECT in this module. Kept in sync with
# ``_row_to_session`` so the two never drift.
_SESSION_COLUMNS = (
    "id, user_id, org_id, workflow_type, current_step, intent, "
    "clarifications, research_data_encrypted, proposed_structure, "
    "draft_content_encrypted, integrated_draft_id, status, "
    "created_at, updated_at"
)


def _coerce_uuid(value: Any) -> uuid.UUID:
    """Return a ``UUID`` from either a string or a ``UUID`` instance."""
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def _parse_jsonb(value: Any) -> Any:
    """Parse a JSONB value that psycopg may return as a string or dict."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


def _row_to_session(row: tuple[Any, ...]) -> DraftingSession:
    """Build a ``DraftingSession`` dataclass from a raw cursor row."""
    (
        session_id,
        user_id,
        org_id,
        workflow_type,
        current_step,
        intent,
        clarifications_raw,
        research_data_encrypted,
        proposed_structure_raw,
        draft_content_encrypted,
        integrated_draft_id,
        status,
        created_at,
        updated_at,
    ) = row

    clarifications = _parse_jsonb(clarifications_raw)
    if not isinstance(clarifications, list):
        clarifications = []

    proposed_structure = _parse_jsonb(proposed_structure_raw)
    if proposed_structure is not None and not isinstance(proposed_structure, dict):
        proposed_structure = None

    return DraftingSession(
        id=_coerce_uuid(session_id),
        user_id=_coerce_uuid(user_id),
        org_id=_coerce_uuid(org_id),
        workflow_type=workflow_type,
        current_step=int(current_step),
        intent=intent,
        clarifications=clarifications,
        research_data_encrypted=research_data_encrypted,
        proposed_structure=proposed_structure,
        draft_content_encrypted=draft_content_encrypted,
        integrated_draft_id=_coerce_uuid(integrated_draft_id) if integrated_draft_id else None,
        status=status,
        created_at=created_at,
        updated_at=updated_at,
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def create_session(
    conn: Any,
    user_id: uuid.UUID | str,
    org_id: uuid.UUID | str,
    workflow_type: str,
) -> DraftingSession:
    """Insert a new ``drafting_sessions`` row and return the created session.

    The caller is responsible for committing the transaction.
    Raises on SQL failure.
    """
    if workflow_type not in VALID_WORKFLOW_TYPES:
        raise ValueError(f"Invalid workflow type: {workflow_type!r}")

    row = conn.execute(
        f"""
        INSERT INTO drafting_sessions (user_id, org_id, workflow_type)
        VALUES (%s, %s, %s)
        RETURNING {_SESSION_COLUMNS}
        """,
        (str(user_id), str(org_id), workflow_type),
    ).fetchone()
    if row is None:
        raise RuntimeError("INSERT ... RETURNING drafting_sessions produced no row")
    return _row_to_session(row)


def update_session(
    conn: Any,
    session_id: uuid.UUID | str,
    **fields: Any,
) -> None:
    """Partial update of a drafting session with ``updated_at`` bump.

    Only the provided keyword arguments are updated. Unknown column names
    are silently ignored.
    """
    allowed = {
        "current_step",
        "intent",
        "clarifications",
        "research_data_encrypted",
        "proposed_structure",
        "draft_content_encrypted",
        "integrated_draft_id",
        "status",
    }
    to_update = {k: v for k, v in fields.items() if k in allowed}
    if not to_update:
        return

    set_clauses = []
    params: list[Any] = []
    for col, val in to_update.items():
        if col in ("clarifications", "proposed_structure"):
            set_clauses.append(f"{col} = %s::jsonb")
            params.append(json.dumps(val, default=str) if val is not None else None)
        else:
            set_clauses.append(f"{col} = %s")
            params.append(val)

    set_clauses.append("updated_at = now()")
    params.append(str(session_id))

    sql = f"UPDATE drafting_sessions SET {', '.join(set_clauses)} WHERE id = %s"
    conn.execute(sql, tuple(params))


def create_version_snapshot(
    conn: Any,
    session_id: uuid.UUID | str,
    step: int,
    snapshot_data: bytes,
) -> None:
    """Insert a snapshot into ``drafting_session_versions``.

    Called by ``advance_step`` before each transition so the audit trail
    captures every intermediate state.
    """
    conn.execute(
        """
        INSERT INTO drafting_session_versions (session_id, step, snapshot_encrypted)
        VALUES (%s, %s, %s)
        """,
        (str(session_id), step, snapshot_data),
    )


def abandon_session(
    conn: Any,
    session_id: uuid.UUID | str,
) -> None:
    """Mark a session as abandoned (soft-delete — no row removal)."""
    conn.execute(
        """
        UPDATE drafting_sessions
        SET status = 'abandoned', updated_at = now()
        WHERE id = %s
        """,
        (str(session_id),),
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get_session(
    conn: Any,
    session_id: uuid.UUID | str,
) -> DraftingSession | None:
    """Return a single session by id, or ``None``."""
    try:
        row = conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM drafting_sessions WHERE id = %s",
            (str(session_id),),
        ).fetchone()
    except Exception:
        logger.exception("Failed to fetch drafting session id=%s", session_id)
        return None
    return _row_to_session(row) if row else None


def list_sessions_for_user(
    conn: Any,
    user_id: uuid.UUID | str,
    org_id: uuid.UUID | str,
    *,
    limit: int = 25,
    offset: int = 0,
) -> list[DraftingSession]:
    """Return sessions owned by *user_id* within *org_id*, newest first.

    The ``WHERE org_id = %s`` clause is load-bearing for org-scoped
    access control.
    """
    if limit <= 0:
        return []
    try:
        rows = conn.execute(
            f"""
            SELECT {_SESSION_COLUMNS}
            FROM drafting_sessions
            WHERE user_id = %s AND org_id = %s
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
            """,
            (str(user_id), str(org_id), limit, max(0, offset)),
        ).fetchall()
    except Exception:
        logger.exception(
            "Failed to list drafting sessions for user=%s org=%s",
            user_id,
            org_id,
        )
        return []
    return [_row_to_session(row) for row in rows]


def count_sessions_for_user(
    conn: Any,
    user_id: uuid.UUID | str,
    org_id: uuid.UUID | str,
) -> int:
    """Return the number of sessions owned by *user_id* within *org_id*."""
    try:
        row = conn.execute(
            """
            SELECT count(*)
            FROM drafting_sessions
            WHERE user_id = %s AND org_id = %s
            """,
            (str(user_id), str(org_id)),
        ).fetchone()
    except Exception:
        logger.exception(
            "Failed to count drafting sessions for user=%s org=%s",
            user_id,
            org_id,
        )
        return 0
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# Convenience wrappers that manage their own connection
# ---------------------------------------------------------------------------


def fetch_session(session_id: uuid.UUID | str) -> DraftingSession | None:
    """Open a fresh connection and return a session by id."""
    try:
        with _connect() as conn:
            return get_session(conn, session_id)
    except Exception:
        logger.exception("fetch_session failed for id=%s", session_id)
        return None


def fetch_sessions_for_user(
    user_id: uuid.UUID | str,
    org_id: uuid.UUID | str,
    *,
    limit: int = 25,
    offset: int = 0,
) -> list[DraftingSession]:
    """Open a fresh connection and list sessions for *user_id* in *org_id*."""
    try:
        with _connect() as conn:
            return list_sessions_for_user(conn, user_id, org_id, limit=limit, offset=offset)
    except Exception:
        logger.exception(
            "fetch_sessions_for_user failed for user=%s org=%s",
            user_id,
            org_id,
        )
        return []


def count_sessions_for_user_conn(
    user_id: uuid.UUID | str,
    org_id: uuid.UUID | str,
) -> int:
    """Open a fresh connection and return the session count."""
    try:
        with _connect() as conn:
            return count_sessions_for_user(conn, user_id, org_id)
    except Exception:
        logger.exception(
            "count_sessions_for_user_conn failed for user=%s org=%s",
            user_id,
            org_id,
        )
        return 0
