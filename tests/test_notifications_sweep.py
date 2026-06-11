# pyright: reportArgumentType=false
"""Tests for notification dedupe/throttle + limit floor (#861-D).

Covers:
- :func:`app.notifications.wire.notify_drafter_complete` deduped per
  session (re-downloading an export must not re-notify the owner).
- :func:`app.notifications.wire.notify_sync_failed` throttled to one
  alert per ``_SYNC_FAILED_THROTTLE_MINUTES`` window.
- :func:`app.notifications.routes.api_notifications_partial` floors the
  ``?limit=`` param at 1 (negative/zero values previously fell through
  to the SQL ``LIMIT``).
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock, patch

from starlette.requests import Request

from app.notifications.routes import api_notifications_partial
from app.notifications.wire import (
    _SYNC_FAILED_THROTTLE_MINUTES,
    notify_drafter_complete,
    notify_sync_failed,
)

_USER_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_SESSION_ID = uuid.UUID("77777777-7777-7777-7777-777777777777")


class _ConnectCM:
    def __init__(self, conn: MagicMock):
        self.conn = conn

    def __enter__(self) -> MagicMock:
        return self.conn

    def __exit__(self, *_: Any) -> bool:
        return False


def _make_session() -> MagicMock:
    session = MagicMock()
    session.id = _SESSION_ID
    session.user_id = _USER_A
    session.intent = "Kodakondsusseaduse muutmine"
    return session


# ---------------------------------------------------------------------------
# notify_drafter_complete dedupe (#861-D: fires per export download)
# ---------------------------------------------------------------------------


class TestNotifyDrafterCompleteDedupe:
    @patch("app.notifications.wire.notify")
    @patch("app.db.get_connection")
    def test_sends_on_first_completion(self, mock_connect, mock_notify):
        conn = MagicMock()
        dedupe_cursor = MagicMock()
        dedupe_cursor.fetchone.return_value = None  # no prior notification
        conn.execute.return_value = dedupe_cursor
        mock_connect.return_value = _ConnectCM(conn)

        notify_drafter_complete(_make_session())

        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args[1]
        assert call_kwargs["type"] == "drafter_complete"
        assert call_kwargs["metadata"]["session_id"] == str(_SESSION_ID)

    @patch("app.notifications.wire.notify")
    @patch("app.db.get_connection")
    def test_suppressed_on_repeat_export(self, mock_connect, mock_notify):
        """A second export download (existing drafter_complete row) must
        not re-notify the owner."""
        conn = MagicMock()
        dedupe_cursor = MagicMock()
        dedupe_cursor.fetchone.return_value = (1,)  # already notified
        conn.execute.return_value = dedupe_cursor
        mock_connect.return_value = _ConnectCM(conn)

        notify_drafter_complete(_make_session())

        mock_notify.assert_not_called()

    @patch("app.notifications.wire.notify")
    @patch("app.db.get_connection")
    def test_dedupe_query_keys_on_user_type_and_session(self, mock_connect, mock_notify):
        conn = MagicMock()
        dedupe_cursor = MagicMock()
        dedupe_cursor.fetchone.return_value = None
        conn.execute.return_value = dedupe_cursor
        mock_connect.return_value = _ConnectCM(conn)

        notify_drafter_complete(_make_session())

        sql, params = conn.execute.call_args[0]
        assert "user_id = %s" in sql
        assert "type = %s" in sql
        assert "metadata->>%s = %s" in sql
        assert params == (str(_USER_A), "drafter_complete", "session_id", str(_SESSION_ID))

    @patch("app.notifications.wire.notify")
    @patch("app.db.get_connection")
    def test_dedupe_db_error_falls_open_to_send(self, mock_connect, mock_notify):
        conn = MagicMock()
        dedupe_cursor = MagicMock()
        dedupe_cursor.fetchone.side_effect = Exception("db hiccup")
        conn.execute.return_value = dedupe_cursor
        mock_connect.return_value = _ConnectCM(conn)

        notify_drafter_complete(_make_session())

        mock_notify.assert_called_once()

    @patch("app.notifications.wire.notify")
    @patch("app.db.get_connection")
    def test_missing_ids_are_noop(self, mock_connect, mock_notify):
        session = MagicMock()
        session.id = None
        session.user_id = _USER_A
        session.intent = "x"

        notify_drafter_complete(session)

        mock_connect.assert_not_called()
        mock_notify.assert_not_called()


# ---------------------------------------------------------------------------
# notify_sync_failed throttle (#861-D)
# ---------------------------------------------------------------------------


class TestNotifySyncFailedThrottle:
    @patch("app.notifications.wire.notify")
    @patch("app.db.get_connection")
    def test_sends_when_no_recent_failure(self, mock_connect, mock_notify):
        admin1 = uuid.uuid4()
        conn = MagicMock()
        throttle_cursor = MagicMock()
        throttle_cursor.fetchone.return_value = None  # nothing recent
        admins_cursor = MagicMock()
        admins_cursor.fetchall.return_value = [(admin1,)]
        conn.execute.side_effect = [throttle_cursor, admins_cursor]
        mock_connect.return_value = _ConnectCM(conn)

        notify_sync_failed("Connection refused")

        mock_notify.assert_called_once()
        assert mock_notify.call_args[1]["type"] == "sync_failed"

    @patch("app.notifications.wire.notify")
    @patch("app.db.get_connection")
    def test_suppressed_when_failure_within_window(self, mock_connect, mock_notify):
        conn = MagicMock()
        throttle_cursor = MagicMock()
        throttle_cursor.fetchone.return_value = (1,)  # recent failure exists
        conn.execute.return_value = throttle_cursor
        mock_connect.return_value = _ConnectCM(conn)

        notify_sync_failed("Connection refused")

        mock_notify.assert_not_called()
        # Only the throttle SELECT ran — the admin lookup was skipped.
        assert conn.execute.call_count == 1

    @patch("app.notifications.wire.notify")
    @patch("app.db.get_connection")
    def test_throttle_query_uses_make_interval_not_interval_placeholder(
        self, mock_connect, mock_notify
    ):
        """Gotcha guard: ``interval %s`` is rejected by the PG parser; the
        throttle must bind the window via ``make_interval(mins => %s)``."""
        conn = MagicMock()
        throttle_cursor = MagicMock()
        throttle_cursor.fetchone.return_value = None
        admins_cursor = MagicMock()
        admins_cursor.fetchall.return_value = []
        conn.execute.side_effect = [throttle_cursor, admins_cursor]
        mock_connect.return_value = _ConnectCM(conn)

        notify_sync_failed("boom")

        sql, params = conn.execute.call_args_list[0][0]
        assert "make_interval(mins => %s)" in sql
        assert "interval %s" not in sql
        assert params == (_SYNC_FAILED_THROTTLE_MINUTES,)

    @patch("app.notifications.wire.notify")
    @patch("app.db.get_connection")
    def test_throttle_db_error_falls_open_to_send(self, mock_connect, mock_notify):
        admin1 = uuid.uuid4()
        conn = MagicMock()
        throttle_cursor = MagicMock()
        throttle_cursor.fetchone.side_effect = Exception("db hiccup")
        admins_cursor = MagicMock()
        admins_cursor.fetchall.return_value = [(admin1,)]
        conn.execute.side_effect = [throttle_cursor, admins_cursor]
        mock_connect.return_value = _ConnectCM(conn)

        notify_sync_failed("boom")

        mock_notify.assert_called_once()


# ---------------------------------------------------------------------------
# api_notifications_partial limit floor (#861-D: floor limit param)
# ---------------------------------------------------------------------------

_AUTH = {
    "id": str(_USER_A),
    "email": "test@riik.ee",
    "full_name": "Test Kasutaja",
    "role": "drafter",
    "org_id": str(uuid.uuid4()),
}


def _make_request(query_string: bytes) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/notifications",
        "query_string": query_string,
        "headers": [],
        "auth": _AUTH,
    }
    return Request(scope)


class TestLimitFloor:
    @patch("app.notifications.routes._require_auth")
    @patch("app.notifications.routes._connect")
    def test_negative_limit_floored_to_one(self, mock_connect, mock_auth):
        mock_auth.return_value = _AUTH
        conn = MagicMock()
        mock_connect.return_value = _ConnectCM(conn)

        with patch(
            "app.notifications.routes.list_notifications_for_user",
            return_value=[],
        ) as mock_list:
            api_notifications_partial(_make_request(b"limit=-5"))

        assert mock_list.call_args[1]["limit"] == 1

    @patch("app.notifications.routes._require_auth")
    @patch("app.notifications.routes._connect")
    def test_zero_limit_floored_to_one(self, mock_connect, mock_auth):
        mock_auth.return_value = _AUTH
        conn = MagicMock()
        mock_connect.return_value = _ConnectCM(conn)

        with patch(
            "app.notifications.routes.list_notifications_for_user",
            return_value=[],
        ) as mock_list:
            api_notifications_partial(_make_request(b"limit=0"))

        assert mock_list.call_args[1]["limit"] == 1

    @patch("app.notifications.routes._require_auth")
    @patch("app.notifications.routes._connect")
    def test_huge_limit_still_capped_at_twenty(self, mock_connect, mock_auth):
        mock_auth.return_value = _AUTH
        conn = MagicMock()
        mock_connect.return_value = _ConnectCM(conn)

        with patch(
            "app.notifications.routes.list_notifications_for_user",
            return_value=[],
        ) as mock_list:
            api_notifications_partial(_make_request(b"limit=9999"))

        assert mock_list.call_args[1]["limit"] == 20  # noqa: PLR2004

    @patch("app.notifications.routes._require_auth")
    @patch("app.notifications.routes._connect")
    def test_valid_limit_passes_through(self, mock_connect, mock_auth):
        mock_auth.return_value = _AUTH
        conn = MagicMock()
        mock_connect.return_value = _ConnectCM(conn)

        with patch(
            "app.notifications.routes.list_notifications_for_user",
            return_value=[],
        ) as mock_list:
            api_notifications_partial(_make_request(b"limit=7"))

        assert mock_list.call_args[1]["limit"] == 7  # noqa: PLR2004
