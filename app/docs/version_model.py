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


def next_reading_stage(current: str) -> str:
    """Return the next stage in the happy-path legislative pipeline.

    Used by :mod:`app.docs.upload` when creating a new version off an
    existing draft: the new version inherits the parent draft's latest
    reading stage one step forward (``vtk`` -> ``reading_1``,
    ``reading_1`` -> ``reading_2``, ...).

    The terminal stage ``enacted`` has no successor: an attempt to step
    forward from it returns ``enacted`` itself rather than raising,
    matching the spec where republishing an enacted law is allowed but
    does not advance the pipeline.

    Raises:
        ValueError: ``current`` is not a known :data:`READING_STAGES`
            value.  Callers should validate user input via
            :data:`READING_STAGES` before passing it through.
    """
    if current not in READING_STAGES:
        raise ValueError(f"Unknown reading stage: {current!r}")
    if current == "enacted":
        return "enacted"
    idx = READING_STAGES.index(current)
    return READING_STAGES[idx + 1]


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


# ---------------------------------------------------------------------------
# Write helpers (#618 PR-B cutover)
# ---------------------------------------------------------------------------


def create_draft_version(
    conn: Any,
    *,
    draft_id: uuid.UUID | str,
    version_number: int,
    reading_stage: str,
    storage_path: str,
    graph_uri: str,
    status: str,
    created_by: uuid.UUID | str,
    parsed_text_encrypted: bytes | None = None,
) -> DraftVersion:
    """Insert a new ``draft_versions`` row and return it.

    Used by :mod:`app.docs.upload` for both the initial v1 row of every
    new draft AND for follow-on versions uploaded against an existing
    draft (the v2+ branch).  Takes an explicit connection so the caller
    can land the insert in the same transaction as the parent
    :func:`app.docs.draft_model.create_draft` call -- a partial commit
    must never leave a draft without a v1 row.

    Args:
        conn: Open psycopg connection.  Caller commits.
        draft_id: Parent draft.  FK violation will surface as the
            underlying ``IntegrityError`` so callers know to roll back.
        version_number: 1-based version index.  Caller is responsible
            for computing the value (typically
            ``MAX(version_number) + 1`` for v2+ uploads, ``1`` for new
            drafts).  Uniqueness is enforced by the DB ``UNIQUE``
            constraint on ``(draft_id, version_number)``.
        reading_stage: One of :data:`READING_STAGES`.  Validated
            client-side so a typo surfaces as ``ValueError`` before SQL
            runs (the DB ``CHECK`` constraint would catch it too, but
            the client-side guard gives a friendlier traceback).
        storage_path: Fernet-encrypted file path produced by
            :func:`app.storage.store_file`.
        graph_uri: Per-version Jena named graph URI.  See the §9.5
            scheme: ``...drafts/{draft_id}/v{version_number}``.
        status: Initial pipeline status; always ``'uploaded'`` for the
            normal upload flow.  Validated against
            :data:`app.docs.status.STATUS_BY_VALUE` so a typo cannot
            slip past the application layer.
        created_by: User id of the uploader.  Inherited from the
            authenticated session.
        parsed_text_encrypted: Optional Fernet ciphertext.  ``None``
            until the parse pipeline runs for this version (matches the
            ``drafts.parsed_text_encrypted`` lifecycle).

    Returns:
        The freshly-inserted :class:`DraftVersion`.

    Raises:
        ValueError: ``reading_stage`` or ``status`` is not in the
            allowed set.  Surfaced before any SQL runs.
    """
    # Local import to avoid a circular: status -> draft_model -> version_model
    # would form a cycle if status was imported at module top.
    from app.docs.status import STATUS_BY_VALUE

    if reading_stage not in READING_STAGES:
        raise ValueError(f"Unknown reading stage: {reading_stage!r}")
    if status not in STATUS_BY_VALUE:
        raise ValueError(f"Unknown draft status: {status!r}")

    row = conn.execute(
        f"""
        INSERT INTO draft_versions (
            draft_id, version_number, reading_stage,
            parsed_text_encrypted, storage_path, graph_uri,
            status, created_by
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING {_VERSION_COLUMNS}
        """,
        (
            str(draft_id),
            version_number,
            reading_stage,
            parsed_text_encrypted,
            storage_path,
            graph_uri,
            status,
            str(created_by),
        ),
    ).fetchone()
    if row is None:
        raise RuntimeError("INSERT ... RETURNING draft_versions produced no row")
    return _row_to_version(row)


def get_next_version_number(conn: Any, draft_id: uuid.UUID | str) -> int:
    """Return ``MAX(version_number) + 1`` for *draft_id*.

    Used by the v2+ upload branch in :mod:`app.docs.upload` to allocate
    the next version slot under the same lock the caller already holds
    on the parent draft row.  Returns ``1`` when the parent has no
    versions yet (cannot happen post migration 030's backfill, but the
    safe default keeps tests independent).

    Takes an explicit connection so the read can sit inside the same
    transaction as the subsequent :func:`create_draft_version` call --
    otherwise two concurrent v2 uploads against the same parent could
    race on the unique-version-number constraint.
    """
    row = conn.execute(
        """
        SELECT COALESCE(MAX(version_number), 0)
        FROM draft_versions
        WHERE draft_id = %s
        """,
        (str(draft_id),),
    ).fetchone()
    current = int(row[0]) if row and row[0] is not None else 0
    return current + 1
