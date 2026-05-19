"""Tests for ``app/docs/review_model.py`` (issue #817).

Covers the dataclass shape, row coercion, and the persistence helpers
(``create_review``, ``list_reviews_for_draft``, ``latest_review_outcome``).
All DB calls are mocked — no live PostgreSQL required.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from app.docs.review_model import (
    _REVIEW_COLUMNS,
    REVIEW_OUTCOME_LABELS_ET,
    REVIEW_OUTCOMES,
    DraftReview,
    _row_to_review,
    create_review,
    latest_review_outcome,
    list_reviews_for_draft,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DRAFT_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
_REVIEWER_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
_REVIEW_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")

_NOW = datetime.now(UTC)


def _make_raw_row(
    *,
    review_id: uuid.UUID = _REVIEW_ID,
    draft_id: uuid.UUID = _DRAFT_ID,
    reviewer_id: uuid.UUID | None = _REVIEWER_ID,
    reviewer_name_snapshot: str | None = "Anne Tamm",
    outcome: str = "no_issue",
    comment: str | None = None,
    created_at: datetime = _NOW,
) -> tuple:
    """Return a raw DB row tuple matching the column order in ``_REVIEW_COLUMNS``."""
    return (
        str(review_id),
        str(draft_id),
        str(reviewer_id) if reviewer_id else None,
        reviewer_name_snapshot,
        outcome,
        comment,
        created_at,
    )


# ---------------------------------------------------------------------------
# Constants — pinned so the CHECK constraint and the labels stay in sync
# ---------------------------------------------------------------------------


class TestConstants:
    def test_review_outcomes_match_check_constraint(self):
        assert set(REVIEW_OUTCOMES) == {"no_issue", "issue_found", "needs_discussion"}

    def test_outcome_labels_cover_every_outcome(self):
        for outcome in REVIEW_OUTCOMES:
            assert outcome in REVIEW_OUTCOME_LABELS_ET
            assert REVIEW_OUTCOME_LABELS_ET[outcome]

    def test_review_columns_lists_every_field(self):
        for field in (
            "id",
            "draft_id",
            "reviewer_id",
            "reviewer_name_snapshot",
            "outcome",
            "comment",
            "created_at",
        ):
            assert field in _REVIEW_COLUMNS


# ---------------------------------------------------------------------------
# _row_to_review — coercion behaviour
# ---------------------------------------------------------------------------


class TestRowToReview:
    def test_happy_path_full_row(self):
        row = _make_raw_row(comment="vajab täiendavat selgitust", outcome="needs_discussion")
        review = _row_to_review(row)

        assert isinstance(review, DraftReview)
        assert review.id == _REVIEW_ID
        assert review.draft_id == _DRAFT_ID
        assert review.reviewer_id == _REVIEWER_ID
        assert review.reviewer_name_snapshot == "Anne Tamm"
        assert review.outcome == "needs_discussion"
        assert review.comment == "vajab täiendavat selgitust"
        assert review.created_at == _NOW

    def test_null_reviewer_id_preserved(self):
        """When the reviewer's user account was deleted, reviewer_id is NULL."""
        row = _make_raw_row(reviewer_id=None, reviewer_name_snapshot="Deleted Person")
        review = _row_to_review(row)

        assert review.reviewer_id is None
        assert review.reviewer_name_snapshot == "Deleted Person"

    def test_null_comment_preserved(self):
        row = _make_raw_row(comment=None)
        review = _row_to_review(row)
        assert review.comment is None


# ---------------------------------------------------------------------------
# create_review — write helper
# ---------------------------------------------------------------------------


class TestCreateReview:
    def _make_conn(self, *, outcome: str = "no_issue", comment: str | None = None) -> MagicMock:
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = _make_raw_row(
            outcome=outcome, comment=comment
        )
        return conn

    def test_happy_path_returns_review(self):
        conn = self._make_conn()
        review = create_review(
            conn,
            draft_id=_DRAFT_ID,
            reviewer_id=_REVIEWER_ID,
            reviewer_name="Anne Tamm",
            outcome="no_issue",
        )
        assert isinstance(review, DraftReview)
        assert review.outcome == "no_issue"

    def test_rejects_unknown_outcome(self):
        conn = MagicMock()
        with pytest.raises(ValueError, match="Invalid review outcome"):
            create_review(
                conn,
                draft_id=_DRAFT_ID,
                reviewer_id=_REVIEWER_ID,
                reviewer_name="Anne",
                outcome="approved",  # not in REVIEW_OUTCOMES
            )

    def test_passes_outcome_to_insert(self):
        conn = self._make_conn(outcome="issue_found")
        create_review(
            conn,
            draft_id=_DRAFT_ID,
            reviewer_id=_REVIEWER_ID,
            reviewer_name="Anne",
            outcome="issue_found",
        )
        call_args = conn.execute.call_args
        sql: str = call_args.args[0]
        params: tuple = call_args.args[1]
        assert "INSERT INTO draft_reviews" in sql
        assert "issue_found" in params

    def test_passes_reviewer_id_as_string(self):
        conn = self._make_conn()
        create_review(
            conn,
            draft_id=_DRAFT_ID,
            reviewer_id=_REVIEWER_ID,
            reviewer_name="Anne",
            outcome="no_issue",
        )
        params: tuple = conn.execute.call_args.args[1]
        assert str(_REVIEWER_ID) in params

    def test_allows_null_reviewer_id(self):
        """A system-issued review can pass reviewer_id=None."""
        conn = self._make_conn()
        create_review(
            conn,
            draft_id=_DRAFT_ID,
            reviewer_id=None,
            reviewer_name=None,
            outcome="no_issue",
        )
        params: tuple = conn.execute.call_args.args[1]
        assert None in params

    def test_whitespace_only_comment_becomes_null(self):
        """An all-whitespace string is treated as 'no comment'."""
        conn = self._make_conn()
        create_review(
            conn,
            draft_id=_DRAFT_ID,
            reviewer_id=_REVIEWER_ID,
            reviewer_name="Anne",
            outcome="no_issue",
            comment="   \n\t ",
        )
        params: tuple = conn.execute.call_args.args[1]
        # The whitespace comment must be passed as None to SQL.
        assert None in params

    def test_real_comment_is_passed(self):
        conn = self._make_conn(comment="Sätte mõju ei ole selge.")
        create_review(
            conn,
            draft_id=_DRAFT_ID,
            reviewer_id=_REVIEWER_ID,
            reviewer_name="Anne",
            outcome="needs_discussion",
            comment="Sätte mõju ei ole selge.",
        )
        params: tuple = conn.execute.call_args.args[1]
        assert "Sätte mõju ei ole selge." in params

    def test_raises_on_empty_returning_row(self):
        """A driver that returns no row from INSERT … RETURNING is fatal."""
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        with pytest.raises(RuntimeError, match="no row"):
            create_review(
                conn,
                draft_id=_DRAFT_ID,
                reviewer_id=_REVIEWER_ID,
                reviewer_name="Anne",
                outcome="no_issue",
            )


# ---------------------------------------------------------------------------
# list_reviews_for_draft — read helper
# ---------------------------------------------------------------------------


class TestListReviewsForDraft:
    def test_returns_empty_list_on_no_rows(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []
        assert list_reviews_for_draft(conn, _DRAFT_ID) == []

    def test_returns_reviews_newest_first(self):
        conn = MagicMock()
        row1 = _make_raw_row(outcome="no_issue", created_at=_NOW)
        row2 = _make_raw_row(outcome="needs_discussion", created_at=_NOW)
        conn.execute.return_value.fetchall.return_value = [row1, row2]

        reviews = list_reviews_for_draft(conn, _DRAFT_ID)
        assert len(reviews) == 2
        assert reviews[0].outcome == "no_issue"
        assert reviews[1].outcome == "needs_discussion"

    def test_swallows_db_errors(self):
        conn = MagicMock()
        conn.execute.side_effect = RuntimeError("DB down")
        assert list_reviews_for_draft(conn, _DRAFT_ID) == []

    def test_passes_draft_id_to_query(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []
        list_reviews_for_draft(conn, _DRAFT_ID)
        params: tuple = conn.execute.call_args.args[1]
        assert str(_DRAFT_ID) in params


# ---------------------------------------------------------------------------
# latest_review_outcome — read helper for dashboard
# ---------------------------------------------------------------------------


class TestLatestReviewOutcome:
    def test_returns_none_when_no_reviews(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        assert latest_review_outcome(conn, _DRAFT_ID) is None

    def test_returns_outcome_when_review_exists(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = ("issue_found",)
        assert latest_review_outcome(conn, _DRAFT_ID) == "issue_found"

    def test_swallows_db_errors(self):
        conn = MagicMock()
        conn.execute.side_effect = RuntimeError("DB down")
        assert latest_review_outcome(conn, _DRAFT_ID) is None
