"""``draft_reviews`` table dataclass + persistence helpers (issue #817).

Mirrors ``migrations/035_draft_reviews.sql`` — see that file for the design
rationale (separate table, nullable reviewer_id with SET NULL, snapshot of
display name, CHECK-constrained outcome enum).

Connection / error-handling pattern matches the rest of ``app/docs``:
- ``get_connection`` context manager from ``app.db``
- Exceptions are logged; helpers return a sentinel value (``None`` /
  empty list) rather than raising so a dead DB never takes down the
  request.

The three outcome values are kept in :data:`REVIEW_OUTCOMES` so route
handlers can validate user input without re-importing the migration's
CHECK constraint.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.db import get_connection as _connect
from app.db_utils import coerce_uuid

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The CHECK constraint values for draft_reviews.outcome. Kept as a tuple
# so route handlers can validate user input cheaply (``outcome in
# REVIEW_OUTCOMES``) without importing the migration file.
REVIEW_OUTCOMES: tuple[str, ...] = (
    "no_issue",
    "issue_found",
    "needs_discussion",
)

# Estonian labels matching the design copy in docs/2026-05-19-usability-fixes-plan.md.
REVIEW_OUTCOME_LABELS_ET: dict[str, str] = {
    "no_issue": "Puuduvad probleemid",
    "issue_found": "Leitud probleem",
    "needs_discussion": "Vajab arutelu",
}


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DraftReview:
    """Snapshot of a row in the ``draft_reviews`` table.

    ``id`` and ``draft_id`` are real ``uuid.UUID`` values so callers can
    pass them back into queries without string round-trips.

    ``reviewer_id`` is **nullable**: when the reviewer's user account is
    later deleted the FK flips to NULL via ``ON DELETE SET NULL`` and the
    review record is preserved. ``reviewer_name_snapshot`` is the display
    name captured at review time so the UI can render
    "Anne Tamm (kustutatud kasutaja)" instead of a bare "—".

    ``outcome`` is always one of :data:`REVIEW_OUTCOMES`. ``comment`` is
    ``None`` when the reviewer did not provide a narrative.
    """

    id: uuid.UUID
    draft_id: uuid.UUID
    reviewer_id: uuid.UUID | None
    reviewer_name_snapshot: str | None
    outcome: str
    comment: str | None
    created_at: datetime


# Column order used by every SELECT in this module. Kept in sync with
# ``_row_to_review`` so the two never drift.
_REVIEW_COLUMNS = "id, draft_id, reviewer_id, reviewer_name_snapshot, outcome, comment, created_at"


def _row_to_review(row: tuple[Any, ...]) -> DraftReview:
    """Build a :class:`DraftReview` dataclass from a raw cursor row."""
    (
        review_id,
        draft_id,
        reviewer_id,
        reviewer_name_snapshot,
        outcome,
        comment,
        created_at,
    ) = row
    return DraftReview(
        id=coerce_uuid(review_id),
        draft_id=coerce_uuid(draft_id),
        reviewer_id=coerce_uuid(reviewer_id) if reviewer_id else None,
        reviewer_name_snapshot=reviewer_name_snapshot,
        outcome=outcome,
        comment=comment,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def create_review(
    conn: Any,
    *,
    draft_id: uuid.UUID | str,
    reviewer_id: uuid.UUID | str | None,
    reviewer_name: str | None,
    outcome: str,
    comment: str | None = None,
) -> DraftReview:
    """Insert a new ``draft_reviews`` row and return the created review.

    Takes an explicit ``conn`` so the caller can land the insert in the
    same transaction as the audit-log emit, mirroring the pattern used by
    :func:`app.docs.draft_model.create_draft`. The caller is responsible
    for committing the transaction.

    Args:
        conn: Open psycopg connection. Caller commits.
        draft_id: Parent draft. FK violation surfaces as the underlying
            ``IntegrityError`` so the route handler can roll back.
        reviewer_id: User id of the reviewer. ``None`` is technically
            permitted by the schema (anonymous review by a deleted user)
            but the route handler always passes the authenticated user's
            id; the optional shape is for testing / future system-issued
            reviews.
        reviewer_name: Display name snapshot. Persisted so the UI can
            render the original name even after the reviewer's user
            account is deleted.
        outcome: One of :data:`REVIEW_OUTCOMES`. Validated client-side so
            a typo surfaces as ``ValueError`` before SQL runs (the DB
            ``CHECK`` constraint would catch it too, but the early guard
            gives a friendlier traceback).
        comment: Optional narrative. ``None`` or empty string both store
            NULL — the UI treats empty input as "no comment".

    Returns:
        The freshly-inserted :class:`DraftReview`.

    Raises:
        ValueError: ``outcome`` is not in :data:`REVIEW_OUTCOMES`.
    """
    if outcome not in REVIEW_OUTCOMES:
        raise ValueError(f"Invalid review outcome: {outcome!r}")

    # Normalise the comment: an all-whitespace string is treated as NULL
    # so the UI never has to distinguish "no comment" from "blank string".
    comment_value: str | None = None
    if comment is not None:
        stripped = comment.strip()
        comment_value = stripped if stripped else None

    row = conn.execute(
        f"""
        INSERT INTO draft_reviews (
            draft_id, reviewer_id, reviewer_name_snapshot,
            outcome, comment
        ) VALUES (%s, %s, %s, %s, %s)
        RETURNING {_REVIEW_COLUMNS}
        """,
        (
            str(draft_id),
            str(reviewer_id) if reviewer_id else None,
            reviewer_name,
            outcome,
            comment_value,
        ),
    ).fetchone()
    if row is None:
        raise RuntimeError("INSERT ... RETURNING draft_reviews produced no row")
    return _row_to_review(row)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def list_reviews_for_draft(
    conn: Any,
    draft_id: uuid.UUID | str,
) -> list[DraftReview]:
    """Return every review for *draft_id*, ordered newest first.

    Takes an explicit connection so the caller can reuse an existing
    transaction. On DB error the function logs and returns ``[]``,
    consistent with the rest of ``app/docs``.
    """
    try:
        rows = conn.execute(
            f"""
            SELECT {_REVIEW_COLUMNS}
            FROM draft_reviews
            WHERE draft_id = %s
            ORDER BY created_at DESC
            """,
            (str(draft_id),),
        ).fetchall()
    except Exception:
        logger.exception("Failed to list reviews for draft=%s", draft_id)
        return []
    return [_row_to_review(row) for row in rows]


def latest_review_outcome(
    conn: Any,
    draft_id: uuid.UUID | str,
) -> str | None:
    """Return the most recent review outcome for *draft_id*, or ``None``.

    Used by the reviewer Töölaud to surface the current status chip per
    draft without loading the full review history. Issues a single-row
    query with ``LIMIT 1`` so it stays cheap when called once per draft
    on the dashboard.
    """
    try:
        row = conn.execute(
            """
            SELECT outcome
            FROM draft_reviews
            WHERE draft_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (str(draft_id),),
        ).fetchone()
    except Exception:
        logger.exception("Failed to fetch latest review outcome for draft=%s", draft_id)
        return None
    if row is None:
        return None
    return str(row[0]) if row[0] else None


# ---------------------------------------------------------------------------
# Convenience wrappers that manage their own connection
# ---------------------------------------------------------------------------


def fetch_reviews_for_draft(draft_id: uuid.UUID | str) -> list[DraftReview]:
    """Open a fresh connection and list reviews for *draft_id*.

    Route handlers that only need the review list (no other DB ops) use
    this rather than wiring up their own ``_connect()`` block.
    """
    try:
        with _connect() as conn:
            return list_reviews_for_draft(conn, draft_id)
    except Exception:
        logger.exception("fetch_reviews_for_draft failed for draft=%s", draft_id)
        return []
