"""``draft_versions`` table dataclass + read-only query helpers.

Mirrors the shape of ``app/docs/draft_model.py`` for the ``draft_versions``
table created by ``migrations/030_draft_versions.sql``.

Design note (§4.2 cutover, #618):
    No application code writes to ``draft_versions`` yet — the upload/analyze
    cutover happens in PR-B.  This module intentionally exposes only read
    helpers so the table can be introspected in tests and admin tooling
    without risking premature writes.

Connection/error-handling pattern matches ``app/docs/draft_model.py``:
    - ``get_connection`` context manager from ``app.db``
    - Exceptions are logged; functions return a sentinel value (``None`` /
      empty list) rather than raising so a dead DB never brings down a request.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.db_utils import coerce_uuid

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The CHECK constraint values for draft_versions.reading_stage.
# Kept as a module-level tuple so callers can validate user input without
# importing the migration file.
READING_STAGES: tuple[str, ...] = (
    "vtk",
    "reading_1",
    "reading_2",
    "reading_3",
    "enacted",
)

# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DraftVersion:
    """Snapshot of a row in the ``draft_versions`` table.

    ``id``, ``draft_id``, and ``created_by`` are real ``uuid.UUID`` values so
    callers can pass them back into queries without string round-trips.

    ``parsed_text_encrypted`` mirrors ``drafts.parsed_text_encrypted``:
    ``None`` until the background parsing pipeline has run for this version.

    ``reading_stage`` is always one of :data:`READING_STAGES`.
    """

    id: uuid.UUID
    draft_id: uuid.UUID
    version_number: int
    reading_stage: str
    parsed_text_encrypted: bytes | None
    storage_path: str
    graph_uri: str
    status: str
    created_at: datetime
    created_by: uuid.UUID


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Column order used by every SELECT in this module.  Kept in sync with
# ``_row_to_version`` so the two never drift.
_VERSION_COLUMNS = (
    "id, draft_id, version_number, reading_stage, "
    "parsed_text_encrypted, storage_path, graph_uri, "
    "status, created_at, created_by"
)


def _row_to_version(row: tuple[Any, ...]) -> DraftVersion:
    """Build a :class:`DraftVersion` dataclass from a raw cursor row."""
    (
        version_id,
        draft_id,
        version_number,
        reading_stage,
        parsed_text_encrypted,
        storage_path,
        graph_uri,
        status,
        created_at,
        created_by,
    ) = row
    return DraftVersion(
        id=coerce_uuid(version_id),
        draft_id=coerce_uuid(draft_id),
        version_number=int(version_number),
        reading_stage=reading_stage,
        parsed_text_encrypted=parsed_text_encrypted,
        storage_path=storage_path,
        graph_uri=graph_uri,
        status=status,
        created_at=created_at,
        created_by=coerce_uuid(created_by),
    )


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


def get_draft_version(conn: Any, version_id: uuid.UUID | str) -> DraftVersion | None:
    """Return a single :class:`DraftVersion` by its primary key, or ``None``.

    Takes an explicit connection so the caller can reuse an existing
    transaction without opening a second connection.
    """
    try:
        row = conn.execute(
            f"SELECT {_VERSION_COLUMNS} FROM draft_versions WHERE id = %s",
            (str(version_id),),
        ).fetchone()
    except Exception:
        logger.exception("Failed to fetch draft_version id=%s", version_id)
        return None
    return _row_to_version(row) if row else None


def list_versions_for_draft(
    conn: Any,
    draft_id: uuid.UUID | str,
) -> list[DraftVersion]:
    """Return all versions for *draft_id*, ordered by ``version_number DESC``.

    Newest (highest) version number first so the latest reading is always at
    index 0.  Takes an explicit connection.
    """
    try:
        rows = conn.execute(
            f"""
            SELECT {_VERSION_COLUMNS}
            FROM draft_versions
            WHERE draft_id = %s
            ORDER BY version_number DESC
            """,
            (str(draft_id),),
        ).fetchall()
    except Exception:
        logger.exception("Failed to list versions for draft=%s", draft_id)
        return []
    return [_row_to_version(row) for row in rows]


def get_latest_version(conn: Any, draft_id: uuid.UUID | str) -> DraftVersion | None:
    """Return the highest-numbered version for *draft_id*, or ``None``.

    Equivalent to ``list_versions_for_draft(conn, draft_id)[0]`` but issues
    a single-row query with ``LIMIT 1`` instead of fetching all rows.
    """
    try:
        row = conn.execute(
            f"""
            SELECT {_VERSION_COLUMNS}
            FROM draft_versions
            WHERE draft_id = %s
            ORDER BY version_number DESC
            LIMIT 1
            """,
            (str(draft_id),),
        ).fetchone()
    except Exception:
        logger.exception("Failed to fetch latest version for draft=%s", draft_id)
        return None
    return _row_to_version(row) if row else None
