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
from datetime import date, datetime, timedelta
from typing import Any, Literal, LiteralString

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


def update_draft_parent_vtk(
    conn: Any,
    draft_id: uuid.UUID | str,
    parent_vtk_id: uuid.UUID | str | None,
) -> bool:
    """Set (or clear) ``parent_vtk_id`` on *draft_id* (#640).

    Returns ``True`` when a row was updated.  Takes an explicit
    connection so the caller can batch the update with other
    operations (audit logging, ontology writes).  The caller commits
    the transaction.
    """
    fk_param = str(parent_vtk_id) if parent_vtk_id else None
    result = conn.execute(
        """
        update drafts
        set parent_vtk_id = %s,
            updated_at = now()
        where id = %s
        """,
        (fk_param, str(draft_id)),
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


# #642: maximum number of candidate IDs carried out of the q-search
# (title/filename/entity-label) before we hand them to the final
# filter+sort+paginate query.  A single org holding more than 500 drafts
# whose search term matches everything would start hurting the final
# ``id = any(...)`` scan, and at that point the user should narrow the
# query anyway.  The cap keeps worst-case latency bounded.
_CANDIDATE_CAP = 500

# Valid sort option keys accepted by ``list_drafts_for_org_filtered``.
# Kept as a small mapping so routes can reject unknown values without
# duplicating the SQL fragments.
_SORT_CLAUSES: dict[str, LiteralString] = {
    "created_desc": "created_at desc",
    "created_asc": "created_at asc",
    "title_asc": "title asc",
    "title_desc": "title desc",
    "status": "status asc, created_at desc",
}

DEFAULT_SORT = "created_desc"

_ONE_DAY = timedelta(days=1)


def list_drafts_for_org_filtered(
    org_id: uuid.UUID | str,
    *,
    q: str | None = None,
    doc_types: set[str] | None = None,
    statuses: set[str] | None = None,
    uploader_id: uuid.UUID | str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    sort: str = DEFAULT_SORT,
    limit: int = 25,
    offset: int = 0,
) -> tuple[list[Draft], int]:
    """Filtered + paginated listing for the /drafts workspace (#642).

    Returns ``(drafts, total_count)`` where ``total_count`` is the grand
    total for pagination (i.e. ignores ``limit``/``offset``).  Opens a
    fresh connection -- routes should not need to manage transactions
    just to render a listing.

    Filtering behaviour (spec §4.2):

    * ``q`` (title/filename/entity-label) runs a two-phase candidate
      lookup powered by ``pg_trgm`` GIN indexes from migration 019.
      Phase 1 scans ``drafts`` for title + filename matches.  Phase 2
      pulls distinct draft IDs whose ``draft_entities.ref_text`` matches.
      The union is capped at :data:`_CANDIDATE_CAP` IDs before the
      final ``id = any(...)`` filter runs.
    * ``doc_types`` / ``statuses`` default to "all" when ``None`` or
      empty.  Filtering is ``col = any(%s)`` over the requested subset.
    * ``uploader_id`` narrows to a single user.
    * ``date_from`` / ``date_to`` are inclusive bounds against
      ``created_at``.  ``date_to`` is rendered as ``< date_to + 1 day``
      so a single-day bound ``from=to=2026-04-01`` picks up everything
      on that calendar day regardless of time-of-day.
    * ``sort`` chooses from :data:`_SORT_CLAUSES`; unknown values fall
      back to the default.

    On DB error the function logs and returns ``([], 0)`` -- consistent
    with the rest of this module.
    """
    if limit <= 0:
        return [], 0

    order_clause = _SORT_CLAUSES.get(sort, _SORT_CLAUSES[DEFAULT_SORT])
    org_str = str(org_id)
    q_norm = q.strip() if q else ""

    # Normalise the filter sets so downstream SQL can treat "None" and
    # "empty set" identically -- both mean "no constraint on this axis".
    active_doc_types = {dt for dt in (doc_types or ()) if dt}
    active_statuses = {s for s in (statuses or ()) if s}

    where_parts: list[LiteralString] = ["org_id = %s"]
    params: list[Any] = [org_str]

    try:
        with _connect() as conn:
            # Phase 1/2: narrow to a candidate ID set when q is set.
            if q_norm:
                pattern = f"%{q_norm}%"

                # Phase 1 -- title / filename trigram match.  The GIN
                # trgm index on both columns makes ``ilike '%q%'``
                # index-supported even though the pattern is unanchored.
                phase1 = conn.execute(
                    """
                    select id
                      from drafts
                     where org_id = %s
                       and (title ilike %s or filename ilike %s)
                     limit %s
                    """,
                    (org_str, pattern, pattern, _CANDIDATE_CAP),
                ).fetchall()

                # Phase 2 -- entity-ref_text trigram match, scoped to
                # the caller's org via a sub-select.  Scoping at the SQL
                # level means a stray draft_id leak is impossible even
                # if pg_trgm surfaces an unexpected row.
                phase2 = conn.execute(
                    """
                    select distinct draft_id
                      from draft_entities
                     where ref_text ilike %s
                       and draft_id in (
                           select id from drafts where org_id = %s
                       )
                     limit %s
                    """,
                    (pattern, org_str, _CANDIDATE_CAP),
                ).fetchall()

                seen: set[str] = set()
                merged: list[str] = []
                for row in list(phase1) + list(phase2):
                    raw_id = str(row[0])
                    if raw_id in seen:
                        continue
                    seen.add(raw_id)
                    merged.append(raw_id)
                    if len(merged) >= _CANDIDATE_CAP:
                        break

                if not merged:
                    return [], 0

                where_parts.append("id = any(%s)")
                params.append(merged)

            if active_doc_types:
                where_parts.append("doc_type = any(%s)")
                params.append(sorted(active_doc_types))
            if active_statuses:
                where_parts.append("status = any(%s)")
                params.append(sorted(active_statuses))
            if uploader_id:
                where_parts.append("user_id = %s")
                params.append(str(uploader_id))
            if date_from is not None:
                where_parts.append("created_at >= %s")
                params.append(date_from)
            if date_to is not None:
                # Inclusive upper bound -- add a day and use ``<`` so
                # rows with ``created_at`` at 23:59 on the day are
                # included.
                where_parts.append("created_at < %s")
                params.append(date_to + _ONE_DAY)

            where_sql = " and ".join(where_parts)

            total_row = conn.execute(
                f"select count(*) from drafts where {where_sql}",
                tuple(params),
            ).fetchone()
            total = int(total_row[0]) if total_row else 0

            if total == 0:
                return [], 0

            rows = conn.execute(
                f"""
                select {_DRAFT_COLUMNS}
                  from drafts
                 where {where_sql}
                 order by {order_clause}
                 limit %s offset %s
                """,
                tuple(params) + (limit, max(0, offset)),
            ).fetchall()
    except Exception:
        logger.exception(
            "list_drafts_for_org_filtered failed for org=%s q=%r",
            org_id,
            q_norm,
        )
        return [], 0

    return [_row_to_draft(row) for row in rows], total


def list_vtks_for_org(
    org_id: uuid.UUID | str,
    *,
    statuses: tuple[str, ...] = ("ready", "analyzing"),
) -> list[Draft]:
    """Return VTK drafts for *org_id* suitable for the link picker (#640).

    Filters on ``doc_type='vtk'`` and restricts to the supplied pipeline
    ``statuses``.  The default set (``ready``, ``analyzing``) mirrors
    the spec — a VTK is link-worthy as soon as it has finished being
    extracted into an impact-analyzable shape, and finished VTKs remain
    selectable.  Ordered newest-first so the most recent uploads surface
    at the top of the dropdown.

    The helper opens its own connection so routes don't have to wire
    up a ``_connect()`` block just to populate a form. On DB error it
    logs and returns ``[]`` (consistent with the rest of this module).
    """
    if not statuses:
        return []
    try:
        with _connect() as conn:
            rows = conn.execute(
                f"""
                select {_DRAFT_COLUMNS}
                from drafts
                where org_id = %s
                  and doc_type = 'vtk'
                  and status = any(%s)
                order by created_at desc
                """,
                (str(org_id), list(statuses)),
            ).fetchall()
    except Exception:
        logger.exception("Failed to list VTKs for org=%s", org_id)
        return []
    return [_row_to_draft(row) for row in rows]


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
