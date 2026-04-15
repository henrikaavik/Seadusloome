"""Unit tests for ``app.chat.feedback``.

Mocks the DB connection exactly like ``tests/test_chat_models.py`` — the
feedback helpers are thin wrappers around SQL, so we assert on the SQL
text and the parameter tuple rather than spinning up a real Postgres.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.chat.feedback import (
    MessageFeedback,
    delete_feedback,
    feedback_counts,
    get_feedback,
    upsert_feedback,
)

_MSG_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
_USER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _make_feedback_row(
    *,
    fb_id: uuid.UUID | None = None,
    message_id: uuid.UUID = _MSG_ID,
    user_id: uuid.UUID = _USER_ID,
    rating: int = 1,
    comment: str | None = None,
    created_at: datetime | None = None,
) -> tuple[Any, ...]:
    """Build a raw cursor row matching _FEEDBACK_COLUMNS order."""
    return (
        fb_id or uuid.uuid4(),
        message_id,
        user_id,
        rating,
        comment,
        created_at or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# upsert_feedback
# ---------------------------------------------------------------------------


class TestUpsertFeedback:
    def test_insert_thumbs_up(self):
        conn = MagicMock()
        row = _make_feedback_row(rating=1, comment=None)
        conn.execute.return_value.fetchone.return_value = row

        result = upsert_feedback(conn, message_id=_MSG_ID, user_id=_USER_ID, rating=1)

        assert isinstance(result, MessageFeedback)
        assert result.rating == 1
        assert result.comment is None

        sql, params = conn.execute.call_args.args
        assert "INSERT INTO message_feedback" in sql
        assert "ON CONFLICT (message_id, user_id) DO UPDATE" in sql
        assert params == (str(_MSG_ID), str(_USER_ID), 1, None)

    def test_insert_thumbs_down_with_comment(self):
        conn = MagicMock()
        row = _make_feedback_row(rating=-1, comment="Vale tsitaat")
        conn.execute.return_value.fetchone.return_value = row

        result = upsert_feedback(
            conn,
            message_id=_MSG_ID,
            user_id=_USER_ID,
            rating=-1,
            comment="Vale tsitaat",
        )

        assert result.rating == -1
        assert result.comment == "Vale tsitaat"

    def test_revote_uses_on_conflict(self):
        """Second call with same (message, user) hits the DO UPDATE branch.

        We can't assert the DB behaviour from a mock, but we can confirm
        the SQL uses ON CONFLICT so revotes don't create duplicate rows.
        """
        conn = MagicMock()
        first_row = _make_feedback_row(rating=1)
        second_row = _make_feedback_row(rating=-1, comment="Ümbermõeldud")
        conn.execute.return_value.fetchone.side_effect = [first_row, second_row]

        first = upsert_feedback(conn, message_id=_MSG_ID, user_id=_USER_ID, rating=1)
        second = upsert_feedback(
            conn,
            message_id=_MSG_ID,
            user_id=_USER_ID,
            rating=-1,
            comment="Ümbermõeldud",
        )

        assert first.rating == 1
        assert second.rating == -1
        assert second.comment == "Ümbermõeldud"

        for call in conn.execute.call_args_list:
            sql = call.args[0]
            assert "ON CONFLICT (message_id, user_id) DO UPDATE" in sql
            assert "created_at = now()" in sql

    def test_invalid_rating_zero(self):
        conn = MagicMock()
        with pytest.raises(ValueError, match="Invalid rating"):
            upsert_feedback(conn, message_id=_MSG_ID, user_id=_USER_ID, rating=0)
        conn.execute.assert_not_called()

    def test_invalid_rating_two(self):
        conn = MagicMock()
        with pytest.raises(ValueError, match="Invalid rating"):
            upsert_feedback(conn, message_id=_MSG_ID, user_id=_USER_ID, rating=2)

    def test_invalid_rating_non_int(self):
        conn = MagicMock()
        with pytest.raises(ValueError, match="Invalid rating"):
            upsert_feedback(
                conn,
                message_id=_MSG_ID,
                user_id=_USER_ID,
                rating="up",  # type: ignore[arg-type]
            )

    def test_raises_if_no_row_returned(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None

        with pytest.raises(RuntimeError, match="produced no row"):
            upsert_feedback(conn, message_id=_MSG_ID, user_id=_USER_ID, rating=1)


# ---------------------------------------------------------------------------
# get_feedback
# ---------------------------------------------------------------------------


class TestGetFeedback:
    def test_returns_feedback(self):
        conn = MagicMock()
        row = _make_feedback_row(rating=1, comment="Super")
        conn.execute.return_value.fetchone.return_value = row

        result = get_feedback(conn, _MSG_ID, _USER_ID)

        assert result is not None
        assert result.rating == 1
        assert result.comment == "Super"

    def test_returns_none_when_absent(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None

        assert get_feedback(conn, _MSG_ID, _USER_ID) is None

    def test_handles_db_error(self):
        conn = MagicMock()
        conn.execute.side_effect = RuntimeError("db down")

        assert get_feedback(conn, _MSG_ID, _USER_ID) is None


# ---------------------------------------------------------------------------
# delete_feedback
# ---------------------------------------------------------------------------


class TestDeleteFeedback:
    def test_deletes_by_message_and_user(self):
        conn = MagicMock()

        delete_feedback(conn, _MSG_ID, _USER_ID)

        conn.execute.assert_called_once()
        sql, params = conn.execute.call_args.args
        assert "DELETE FROM message_feedback" in sql
        assert "message_id = %s" in sql
        assert "user_id = %s" in sql
        assert params == (str(_MSG_ID), str(_USER_ID))


# ---------------------------------------------------------------------------
# feedback_counts
# ---------------------------------------------------------------------------


class TestFeedbackCounts:
    def test_returns_up_and_down_counts(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (7, 3)

        up, down = feedback_counts(conn, _MSG_ID)
        assert up == 7
        assert down == 3

    def test_all_zero_when_no_rows(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (0, 0)

        assert feedback_counts(conn, _MSG_ID) == (0, 0)

    def test_db_error_returns_zero_zero(self):
        conn = MagicMock()
        conn.execute.side_effect = RuntimeError("db")

        assert feedback_counts(conn, _MSG_ID) == (0, 0)

    def test_fetchone_none_returns_zero_zero(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None

        assert feedback_counts(conn, _MSG_ID) == (0, 0)
