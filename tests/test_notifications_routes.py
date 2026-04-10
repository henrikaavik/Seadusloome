# pyright: reportArgumentType=false
"""Unit tests for ``app.notifications.routes``.

Tests the route handlers using mocked DB connections. Same patterns as
``tests/test_drafter_routes.py`` — mock ``_connect``, ``_require_auth``,
and verify the returned FT components.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

from fasthtml.common import to_xml
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from app.notifications.models import Notification
from app.notifications.routes import (
    api_notifications_partial,
    api_unread_count,
    mark_all_read_handler,
    mark_single_read,
    notifications_page,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_NOTIF_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_NOW = datetime.now(UTC)

_AUTH = {
    "id": str(_USER_ID),
    "email": "test@riik.ee",
    "full_name": "Test Kasutaja",
    "role": "drafter",
    "org_id": str(uuid.uuid4()),
}


def _make_notification(
    *,
    notif_id: uuid.UUID = _NOTIF_ID,
    read: bool = False,
    title: str = "Test teavitus",
    body: str | None = "Sisu.",
    link: str | None = "/drafts/123",
) -> Notification:
    return Notification(
        id=notif_id,
        user_id=_USER_ID,
        type="analysis_done",
        title=title,
        body=body,
        link=link,
        metadata=None,
        read=read,
        created_at=_NOW,
    )


def _make_request(path: str = "/notifications", **scope_overrides: Any) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": b"",
        "headers": [],
        "auth": _AUTH,
        **scope_overrides,
    }
    return Request(scope)


class _ConnectCM:
    """Context-manager wrapper around a mock connection."""

    def __init__(self, conn: MagicMock):
        self.conn = conn

    def __enter__(self) -> MagicMock:
        return self.conn

    def __exit__(self, *_: Any) -> bool:
        return False


# ---------------------------------------------------------------------------
# Tests: GET /notifications
# ---------------------------------------------------------------------------


class TestNotificationsPage:
    @patch("app.notifications.routes._require_auth")
    @patch("app.notifications.routes._connect")
    def test_returns_page_with_notifications(self, mock_connect, mock_auth):
        mock_auth.return_value = _AUTH
        notifications = [
            _make_notification(),
            _make_notification(notif_id=uuid.uuid4(), read=True),
        ]
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (1,)

        mock_connect.return_value = _ConnectCM(conn)

        with (
            patch(
                "app.notifications.routes.list_notifications_for_user",
                return_value=notifications,
            ),
            patch("app.notifications.routes.count_unread", return_value=1),
        ):
            req = _make_request()
            result = notifications_page(req)

        # PageShell returns a tuple of FT elements
        assert result is not None

    @patch("app.notifications.routes._require_auth")
    def test_redirects_unauthenticated(self, mock_auth):
        mock_auth.return_value = RedirectResponse("/auth/login", status_code=303)

        req = _make_request()
        result = notifications_page(req)

        assert isinstance(result, RedirectResponse)


# ---------------------------------------------------------------------------
# Tests: POST /notifications/{id}/read
# ---------------------------------------------------------------------------


class TestMarkSingleRead:
    @patch("app.notifications.routes._require_auth")
    @patch("app.notifications.routes._connect")
    def test_marks_notification_read(self, mock_connect, mock_auth):
        mock_auth.return_value = _AUTH
        conn = MagicMock()
        mock_connect.return_value = _ConnectCM(conn)

        with patch("app.notifications.routes.mark_read") as mock_mark:
            req = _make_request(path=f"/notifications/{_NOTIF_ID}/read", method="POST")
            result = mark_single_read(req, str(_NOTIF_ID))

        mock_mark.assert_called_once()
        assert result is not None

    @patch("app.notifications.routes._require_auth")
    def test_returns_400_for_invalid_id(self, mock_auth):
        mock_auth.return_value = _AUTH

        req = _make_request(path="/notifications/not-a-uuid/read", method="POST")
        result = mark_single_read(req, "not-a-uuid")

        assert isinstance(result, Response)
        assert result.status_code == 400

    @patch("app.notifications.routes._require_auth")
    def test_redirects_unauthenticated(self, mock_auth):
        mock_auth.return_value = RedirectResponse("/auth/login", status_code=303)

        req = _make_request()
        result = mark_single_read(req, str(_NOTIF_ID))

        assert isinstance(result, RedirectResponse)


# ---------------------------------------------------------------------------
# Tests: POST /notifications/read-all
# ---------------------------------------------------------------------------


class TestMarkAllRead:
    @patch("app.notifications.routes._require_auth")
    @patch("app.notifications.routes._connect")
    def test_marks_all_read_and_returns_list(self, mock_connect, mock_auth):
        mock_auth.return_value = _AUTH
        conn = MagicMock()
        mock_connect.return_value = _ConnectCM(conn)

        with (
            patch("app.notifications.routes.mark_all_read") as mock_mark,
            patch(
                "app.notifications.routes.list_notifications_for_user",
                return_value=[_make_notification(read=True)],
            ),
        ):
            req = _make_request(path="/notifications/read-all", method="POST")
            result = mark_all_read_handler(req)

        mock_mark.assert_called_once()
        assert result is not None


# ---------------------------------------------------------------------------
# Tests: GET /api/notifications/unread-count
# ---------------------------------------------------------------------------


class TestApiUnreadCount:
    @patch("app.notifications.routes._require_auth")
    @patch("app.notifications.routes._connect")
    def test_returns_oob_badge_with_count(self, mock_connect, mock_auth):
        mock_auth.return_value = _AUTH
        conn = MagicMock()
        mock_connect.return_value = _ConnectCM(conn)

        with patch("app.notifications.routes.count_unread", return_value=7):
            req = _make_request(path="/api/notifications/unread-count")
            result = api_unread_count(req)

        html = to_xml(result)
        assert 'id="bell-badge"' in html
        assert 'hx-swap-oob="true"' in html
        assert ">7<" in html
        assert "bell-badge--hidden" not in html

    @patch("app.notifications.routes._require_auth")
    def test_returns_hidden_badge_when_unauthenticated(self, mock_auth):
        mock_auth.return_value = RedirectResponse("/auth/login", status_code=303)

        req = _make_request(path="/api/notifications/unread-count")
        result = api_unread_count(req)

        html = to_xml(result)
        assert 'id="bell-badge"' in html
        assert 'hx-swap-oob="true"' in html
        assert "bell-badge--hidden" in html


# ---------------------------------------------------------------------------
# Tests: GET /api/notifications
# ---------------------------------------------------------------------------


class TestApiNotificationsPartial:
    @patch("app.notifications.routes._require_auth")
    @patch("app.notifications.routes._connect")
    def test_returns_partial_with_notifications(self, mock_connect, mock_auth):
        mock_auth.return_value = _AUTH
        conn = MagicMock()
        mock_connect.return_value = _ConnectCM(conn)

        with patch(
            "app.notifications.routes.list_notifications_for_user",
            return_value=[_make_notification()],
        ):
            req = _make_request(path="/api/notifications", query_string=b"limit=5")
            result = api_notifications_partial(req)

        assert result is not None

    @patch("app.notifications.routes._require_auth")
    @patch("app.notifications.routes._connect")
    def test_respects_limit_param(self, mock_connect, mock_auth):
        mock_auth.return_value = _AUTH
        conn = MagicMock()
        mock_connect.return_value = _ConnectCM(conn)

        with patch(
            "app.notifications.routes.list_notifications_for_user",
            return_value=[],
        ) as mock_list:
            req = _make_request(path="/api/notifications", query_string=b"limit=3")
            api_notifications_partial(req)

        mock_list.assert_called_once()
        # limit should be 3
        call_kwargs = mock_list.call_args
        assert call_kwargs[1]["limit"] == 3 or call_kwargs[0][2] == 3  # noqa: PLR2004
