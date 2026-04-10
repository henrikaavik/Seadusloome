"""Unit tests for ``app.notifications.models``.

All DB access is mocked — same patterns as ``tests/test_chat_models.py``.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

from app.notifications.models import (
    Notification,
    count_unread,
    create_notification,
    list_notifications_for_user,
    mark_all_read,
    mark_read,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_NOTIF_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_NOW = datetime.now(UTC)


def _make_notification_row(
    *,
    notif_id: uuid.UUID = _NOTIF_ID,
    user_id: uuid.UUID = _USER_ID,
    notif_type: str = "analysis_done",
    title: str = "Moju analuus valmis",
    body: str | None = "Eelnou analuus on valmis.",
    link: str | None = "/drafts/123/report",
    metadata: dict | None = None,
    read: bool = False,
    created_at: datetime = _NOW,
) -> tuple[Any, ...]:
    """Build a raw cursor row matching _NOTIFICATION_COLUMNS order."""
    return (
        notif_id,
        user_id,
        notif_type,
        title,
        body,
        link,
        json.dumps(metadata) if metadata else None,
        read,
        created_at,
    )


# ---------------------------------------------------------------------------
# Tests: create_notification
# ---------------------------------------------------------------------------


class TestCreateNotification:
    def test_creates_and_returns_notification(self):
        row = _make_notification_row()
        cursor = MagicMock()
        cursor.fetchone.return_value = row
        conn = MagicMock()
        conn.execute.return_value = cursor

        result = create_notification(
            conn,
            user_id=_USER_ID,
            type="analysis_done",
            title="Moju analuus valmis",
            body="Eelnou analuus on valmis.",
            link="/drafts/123/report",
        )

        assert result is not None
        assert isinstance(result, Notification)
        assert result.id == _NOTIF_ID
        assert result.user_id == _USER_ID
        assert result.type == "analysis_done"
        assert result.title == "Moju analuus valmis"
        assert result.read is False

    def test_returns_none_on_db_error(self):
        conn = MagicMock()
        conn.execute.side_effect = RuntimeError("DB down")

        result = create_notification(
            conn,
            user_id=_USER_ID,
            type="test",
            title="Test",
        )

        assert result is None

    def test_metadata_stored_as_jsonb(self):
        metadata = {"draft_id": "123", "score": 42}
        row = _make_notification_row(metadata=metadata)
        cursor = MagicMock()
        cursor.fetchone.return_value = row
        conn = MagicMock()
        conn.execute.return_value = cursor

        result = create_notification(
            conn,
            user_id=_USER_ID,
            type="analysis_done",
            title="Test",
            metadata=metadata,
        )

        assert result is not None
        # Verify the SQL received JSON-serialised metadata
        call_args = conn.execute.call_args
        params = call_args[0][1]
        assert json.dumps(metadata) in params


# ---------------------------------------------------------------------------
# Tests: list_notifications_for_user
# ---------------------------------------------------------------------------


class TestListNotifications:
    def test_returns_notifications_list(self):
        rows = [
            _make_notification_row(notif_id=uuid.uuid4()),
            _make_notification_row(notif_id=uuid.uuid4(), read=True),
        ]
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = rows

        result = list_notifications_for_user(conn, _USER_ID)

        assert len(result) == 2
        assert all(isinstance(n, Notification) for n in result)

    def test_unread_only_filter(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []

        list_notifications_for_user(conn, _USER_ID, unread_only=True)

        sql = conn.execute.call_args[0][0]
        assert "read = FALSE" in sql

    def test_returns_empty_list_on_error(self):
        conn = MagicMock()
        conn.execute.side_effect = RuntimeError("DB down")

        result = list_notifications_for_user(conn, _USER_ID)

        assert result == []


# ---------------------------------------------------------------------------
# Tests: count_unread
# ---------------------------------------------------------------------------


class TestCountUnread:
    def test_returns_count(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (5,)

        result = count_unread(conn, _USER_ID)

        assert result == 5

    def test_returns_zero_on_error(self):
        conn = MagicMock()
        conn.execute.side_effect = RuntimeError("DB down")

        result = count_unread(conn, _USER_ID)

        assert result == 0


# ---------------------------------------------------------------------------
# Tests: mark_read
# ---------------------------------------------------------------------------


class TestMarkRead:
    def test_marks_notification_read(self):
        result_mock = MagicMock()
        result_mock.rowcount = 1
        conn = MagicMock()
        conn.execute.return_value = result_mock

        success = mark_read(conn, _NOTIF_ID)

        assert success is True
        sql = conn.execute.call_args[0][0]
        assert "read = TRUE" in sql

    def test_returns_false_when_not_found(self):
        result_mock = MagicMock()
        result_mock.rowcount = 0
        conn = MagicMock()
        conn.execute.return_value = result_mock

        success = mark_read(conn, _NOTIF_ID)

        assert success is False


# ---------------------------------------------------------------------------
# Tests: mark_all_read
# ---------------------------------------------------------------------------


class TestMarkAllRead:
    def test_marks_all_and_returns_count(self):
        result_mock = MagicMock()
        result_mock.rowcount = 3
        conn = MagicMock()
        conn.execute.return_value = result_mock

        count = mark_all_read(conn, _USER_ID)

        assert count == 3
