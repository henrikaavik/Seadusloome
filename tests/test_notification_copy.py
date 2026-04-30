"""Regression tests for Estonian diacritic correctness in notification copy.

A 2026-04-29 UI review (docs/2026-04-29-ui-review-seadusloome-live.md)
found that notification dropdown copy was missing Estonian diacritics
("Moju analuus", "Eelnou", "Vaata koiki" etc.) while the rest of the
UI used proper diacritics — making the notification strings read as
broken to native speakers.

This test module guards against future regressions by asserting that
every user-facing string emitted by the notifications subsystem uses
proper diacritics (õ, ü, ä, ö) where the language requires them.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock, patch

from fasthtml.common import to_xml
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.notifications.routes import api_notifications_partial
from app.notifications.wire import (
    notify_analysis_done,
    notify_annotation_reply,
    notify_draft_archive_warning,
    notify_drafter_complete,
    notify_sync_failed,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_USER_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
_DRAFT_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
_ANN_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
_REPLY_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")
_SESSION_ID = uuid.UUID("77777777-7777-7777-7777-777777777777")

_AUTH = {
    "id": str(_USER_A),
    "email": "test@riik.ee",
    "full_name": "Test Kasutaja",
    "role": "drafter",
    "org_id": str(uuid.uuid4()),
}


def _make_draft() -> MagicMock:
    draft = MagicMock()
    draft.id = _DRAFT_ID
    draft.user_id = _USER_A
    draft.title = "Test eelnõu"
    return draft


def _make_archive_draft() -> MagicMock:
    from datetime import UTC, datetime, timedelta

    draft = MagicMock()
    draft.id = _DRAFT_ID
    draft.user_id = _USER_A
    draft.title = "Test eelnõu"
    draft.last_accessed_at = datetime.now(UTC) - timedelta(days=91)
    return draft


def _make_session() -> MagicMock:
    session = MagicMock()
    session.id = _SESSION_ID
    session.user_id = _USER_A
    session.intent = "Kodakondsusseaduse muutmine"
    return session


def _make_annotation(target_id: str | None = None) -> MagicMock:
    ann = MagicMock()
    ann.id = _ANN_ID
    ann.user_id = _USER_A
    ann.target_type = "draft"
    ann.target_id = target_id or str(_DRAFT_ID)
    return ann


def _make_reply() -> MagicMock:
    reply = MagicMock()
    reply.id = _REPLY_ID
    reply.user_id = _USER_B
    reply.content = "Nõustun!"
    return reply


def _make_request(path: str = "/api/notifications", **scope_overrides: Any) -> Request:
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
    def __init__(self, conn: MagicMock):
        self.conn = conn

    def __enter__(self) -> MagicMock:
        return self.conn

    def __exit__(self, *_: Any) -> bool:
        return False


# Banned (un-diacritic'd) substrings we never want to see in any notification.
# Each entry is a (banned, expected) pair to give actionable failure messages.
_BANNED_SUBSTRINGS = [
    ("Moju", "Mõju"),
    ("moju", "mõju"),
    ("analuus", "analüüs"),
    ("Eelnou", "Eelnõu"),
    ("eelnou", "eelnõu"),
    ("koiki", "kõiki"),
    ("naidata", "näidata"),
    ("oigus", "õigus"),
    ("ulevaade", "ülevaade"),
    ("sunkroon", "sünkroon"),
    ("ebaonnestus", "ebaõnnestus"),
    ("tahelepan", "tähelepan"),
    ("paeva", "päeva"),
    ("margistus", "märgistus"),
]


def _assert_no_banned_substrings(text: str, *, where: str) -> None:
    """Assert that *text* contains no un-diacritic'd Estonian fragments."""
    for banned, expected in _BANNED_SUBSTRINGS:
        assert banned not in text, (
            f"{where}: found un-diacritic'd '{banned}' — should be '{expected}'. "
            f"Full text: {text!r}"
        )


# ---------------------------------------------------------------------------
# Wire-up: notify_analysis_done
# ---------------------------------------------------------------------------


class TestAnalysisDoneCopy:
    @patch("app.notifications.wire.notify")
    def test_title_uses_proper_diacritics(self, mock_notify):
        notify_analysis_done(_make_draft())

        mock_notify.assert_called_once()
        kwargs = mock_notify.call_args[1]
        assert kwargs["title"] == "Mõjuanalüüs valmis"
        _assert_no_banned_substrings(kwargs["title"], where="analysis_done.title")

    @patch("app.notifications.wire.notify")
    def test_body_uses_proper_diacritics(self, mock_notify):
        notify_analysis_done(_make_draft())

        kwargs = mock_notify.call_args[1]
        body = kwargs["body"]
        # Required Estonian diacritics must be present.
        assert "õ" in body, f"body missing 'õ': {body!r}"
        assert "ü" in body, f"body missing 'ü': {body!r}"
        # Specific phrasing must match.
        assert "Eelnõu" in body
        assert "mõjuanalüüs" in body
        _assert_no_banned_substrings(body, where="analysis_done.body")


# ---------------------------------------------------------------------------
# Wire-up: notify_drafter_complete
# ---------------------------------------------------------------------------


class TestDrafterCompleteCopy:
    @patch("app.notifications.wire.notify")
    def test_title_uses_proper_diacritics(self, mock_notify):
        notify_drafter_complete(_make_session())

        kwargs = mock_notify.call_args[1]
        assert kwargs["title"] == "Eelnõu koostamine valmis"
        _assert_no_banned_substrings(kwargs["title"], where="drafter_complete.title")

    @patch("app.notifications.wire.notify")
    def test_default_title_text_uses_diacritics_when_no_intent(self, mock_notify):
        session = _make_session()
        session.intent = None

        notify_drafter_complete(session)

        kwargs = mock_notify.call_args[1]
        body = kwargs["body"]
        # Falls back to "Eelnõu" with diacritic.
        assert "Eelnõu" in body
        _assert_no_banned_substrings(body, where="drafter_complete.body(no-intent)")


# ---------------------------------------------------------------------------
# Wire-up: notify_annotation_reply
# ---------------------------------------------------------------------------


class TestAnnotationReplyCopy:
    @patch("app.notifications.wire.notify")
    def test_title_uses_proper_diacritics(self, mock_notify):
        notify_annotation_reply(_make_annotation(), _make_reply())

        kwargs = mock_notify.call_args[1]
        assert kwargs["title"] == "Uus vastus teie märgistusele"
        _assert_no_banned_substrings(kwargs["title"], where="annotation_reply.title")


# ---------------------------------------------------------------------------
# Wire-up: notify_sync_failed
# ---------------------------------------------------------------------------


class TestSyncFailedCopy:
    @patch("app.notifications.wire.notify")
    @patch("app.db.get_connection")
    def test_title_uses_proper_diacritics(self, mock_connect, mock_notify):
        admin_id = uuid.uuid4()
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [(admin_id,)]
        mock_connect.return_value = _ConnectCM(conn)

        notify_sync_failed("Connection refused")

        kwargs = mock_notify.call_args[1]
        assert kwargs["title"] == "Ontoloogia sünkroonimine ebaõnnestus"
        _assert_no_banned_substrings(kwargs["title"], where="sync_failed.title")


# ---------------------------------------------------------------------------
# Wire-up: notify_draft_archive_warning
# ---------------------------------------------------------------------------


class TestDraftArchiveWarningCopy:
    @patch("app.notifications.wire.notify")
    def test_title_uses_proper_diacritics(self, mock_notify):
        notify_draft_archive_warning(_make_archive_draft())

        kwargs = mock_notify.call_args[1]
        assert kwargs["title"] == "Eelnõu vajab tähelepanu"
        _assert_no_banned_substrings(kwargs["title"], where="archive_warning.title")

    @patch("app.notifications.wire.notify")
    def test_body_uses_proper_diacritics(self, mock_notify):
        notify_draft_archive_warning(_make_archive_draft())

        kwargs = mock_notify.call_args[1]
        body = kwargs["body"]
        # Required diacritics for "päeva" and "Eelnõu".
        assert "päeva" in body, f"body missing 'päeva': {body!r}"
        assert "Eelnõu" in body, f"body missing 'Eelnõu': {body!r}"
        _assert_no_banned_substrings(body, where="archive_warning.body")


# ---------------------------------------------------------------------------
# Routes: GET /api/notifications dropdown partial
# ---------------------------------------------------------------------------


class TestApiNotificationsDropdownCopy:
    @patch("app.notifications.routes._require_auth")
    @patch("app.notifications.routes._connect")
    def test_view_all_link_uses_proper_diacritics(self, mock_connect, mock_auth):
        """The 'Vaata kõiki' link in the notification dropdown partial
        must use the proper diacritic — the prior copy 'Vaata koiki'
        was caught in the 2026-04-29 UI review.
        """
        mock_auth.return_value = _AUTH
        conn = MagicMock()
        mock_connect.return_value = _ConnectCM(conn)

        with patch(
            "app.notifications.routes.list_notifications_for_user",
            return_value=[],
        ):
            req = _make_request(path="/api/notifications")
            result = api_notifications_partial(req)

        html = to_xml(result)
        assert "Vaata kõiki" in html, f"dropdown partial missing 'Vaata kõiki': {html!r}"
        _assert_no_banned_substrings(html, where="api_notifications_partial")

    @patch("app.notifications.routes._require_auth")
    def test_unauthenticated_response_has_no_banned_substrings(self, mock_auth):
        mock_auth.return_value = RedirectResponse("/auth/login", status_code=303)

        req = _make_request(path="/api/notifications")
        result = api_notifications_partial(req)

        html = to_xml(result)
        _assert_no_banned_substrings(html, where="api_notifications_partial(unauth)")
