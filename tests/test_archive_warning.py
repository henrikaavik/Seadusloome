"""Tests for the 90-day draft auto-archive warning (#572).

Covers:

* ``scan_stale_drafts`` returns drafts where ``last_accessed_at`` is
  older than 90 days and emits the notification factory.
* Drafts accessed within the threshold are not returned.
* A warning already emitted within the dedup window suppresses a
  duplicate.
* ``touch_draft_access`` resets the clock.
* ``POST /drafts/{id}/keep`` resets the clock for the owner and rejects
  non-owners (same policy as delete).
* Archived drafts are excluded from the scan.

External dependencies (Postgres, the ``notify`` helper) are mocked at
the module boundary so the tests run without a real database.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

from app.docs.draft_model import Draft
from app.jobs.archive_warning import scan_stale_drafts

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_ORG_ID = "11111111-1111-1111-1111-111111111111"
_OTHER_ORG_ID = "22222222-2222-2222-2222-222222222222"
_USER_ID = "33333333-3333-3333-3333-333333333333"
_OTHER_USER_ID = "44444444-4444-4444-4444-444444444444"


def _authed_user(
    *,
    user_id: str = _USER_ID,
    org_id: str = _ORG_ID,
    role: str = "drafter",
) -> dict[str, Any]:
    return {
        "id": user_id,
        "email": "test@seadusloome.ee",
        "full_name": "Test Kasutaja",
        "role": role,
        "org_id": org_id,
    }


def _stub_provider(user: dict[str, Any]) -> MagicMock:
    provider = MagicMock()
    provider.get_current_user.return_value = user
    return provider


def _authed_client() -> TestClient:
    client = TestClient(
        __import__("app.main", fromlist=["app"]).app,
        follow_redirects=False,
    )
    client.cookies.set("access_token", "stub-token")
    return client


def _make_draft(
    *,
    draft_id: uuid.UUID | None = None,
    org_id: str = _ORG_ID,
    user_id: str = _USER_ID,
    status: str = "uploaded",
    title: str = "Test eelnou",
    last_accessed_at: datetime | None = None,
) -> Draft:
    now = datetime.now(UTC)
    resolved = draft_id or uuid.UUID("55555555-5555-5555-5555-555555555555")
    return Draft(
        id=resolved,
        user_id=uuid.UUID(user_id),
        org_id=uuid.UUID(org_id),
        title=title,
        filename="eelnou.docx",
        content_type=("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        file_size=1024,
        storage_path="/tmp/x.enc",
        graph_uri=f"https://data.riik.ee/ontology/estleg/drafts/{resolved}",
        status=status,
        parsed_text_encrypted=None,
        entity_count=None,
        error_message=None,
        created_at=now,
        updated_at=now,
        last_accessed_at=last_accessed_at or now,
    )


def _row_from_draft(draft: Draft) -> tuple:
    """Shape a ``Draft`` back into the raw DB row tuple that
    ``_row_to_draft`` consumes. Kept in lockstep with the column order
    in ``app.docs.draft_model._DRAFT_COLUMNS``."""
    return (
        str(draft.id),
        str(draft.user_id),
        str(draft.org_id),
        draft.title,
        draft.filename,
        draft.content_type,
        draft.file_size,
        draft.storage_path,
        draft.graph_uri,
        draft.status,
        draft.parsed_text_encrypted,
        draft.entity_count,
        draft.error_message,
        draft.created_at,
        draft.updated_at,
        draft.last_accessed_at,
        draft.doc_type,  # (#639)
        str(draft.parent_vtk_id) if draft.parent_vtk_id else None,  # (#639)
        draft.processing_completed_at,  # (#670)
    )


# ---------------------------------------------------------------------------
# scan_stale_drafts
# ---------------------------------------------------------------------------


class TestScanStaleDrafts:
    """The stale-draft scan query itself is executed against PostgreSQL
    at runtime, so we mock ``get_connection`` and verify the Python
    logic: every row returned by the query is converted to a Draft and
    passed to ``notify_draft_archive_warning``."""

    @patch("app.jobs.archive_warning.notify_draft_archive_warning")
    @patch("app.jobs.archive_warning.get_connection")
    def test_stale_drafts_get_notified(self, mock_conn, mock_notify):
        stale_draft = _make_draft(
            draft_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            last_accessed_at=datetime.now(UTC) - timedelta(days=120),
        )
        cursor = MagicMock()
        cursor.fetchall.return_value = [_row_from_draft(stale_draft)]
        conn = MagicMock()
        conn.execute.return_value = cursor
        mock_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        result = scan_stale_drafts(threshold_days=90, dedupe_window_days=7)

        # Exactly one notification fired, for the one stale row.
        mock_notify.assert_called_once()
        passed_draft = mock_notify.call_args.args[0]
        assert str(passed_draft.id) == str(stale_draft.id)

        # Return shape includes the fields the admin dashboard will read.
        assert len(result) == 1
        assert result[0]["draft_id"] == str(stale_draft.id)
        assert result[0]["user_id"] == str(stale_draft.user_id)
        assert result[0]["title"] == stale_draft.title

        # Threshold and dedup window were forwarded to the SQL layer so
        # the query actually filters on 90 days / 7 days. The call args
        # are (threshold_days, dedupe_window_days) in that order.
        sql_args = conn.execute.call_args.args[1]
        assert sql_args == (90, 7)

    @patch("app.jobs.archive_warning.notify_draft_archive_warning")
    @patch("app.jobs.archive_warning.get_connection")
    def test_fresh_drafts_are_not_returned(self, mock_conn, mock_notify):
        """A fresh ``last_accessed_at`` means the SQL WHERE clause
        excludes the row at the DB level. We simulate that here by
        having the mocked cursor return an empty result set for any
        query whose threshold filter excluded the row."""
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        conn = MagicMock()
        conn.execute.return_value = cursor
        mock_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        result = scan_stale_drafts()

        mock_notify.assert_not_called()
        assert result == []

    @patch("app.jobs.archive_warning.notify_draft_archive_warning")
    @patch("app.jobs.archive_warning.get_connection")
    def test_dedup_suppresses_duplicate_warnings(self, mock_conn, mock_notify):
        """The dedup clause is an ``NOT EXISTS`` subquery against the
        ``notifications`` table. When a warning was emitted within
        the dedup window the DB query returns no rows — which maps to
        an empty cursor here."""
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        conn = MagicMock()
        conn.execute.return_value = cursor
        mock_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        result = scan_stale_drafts(threshold_days=90, dedupe_window_days=7)

        mock_notify.assert_not_called()
        assert result == []

        # The SQL must carry the dedup subquery.
        sql = conn.execute.call_args.args[0]
        assert "NOT EXISTS" in sql
        assert "draft_archive_warning" in sql
        # Dedup window parameter was passed.
        assert conn.execute.call_args.args[1][1] == 7

    @patch("app.jobs.archive_warning.notify_draft_archive_warning")
    @patch("app.jobs.archive_warning.get_connection")
    def test_archived_drafts_are_excluded(self, mock_conn, mock_notify):
        """The scan SQL filters out archived rows at the query layer.
        This test asserts the WHERE clause contains the ``status !=
        'archived'`` predicate so the exclusion cannot silently break
        under future query refactors."""
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        conn = MagicMock()
        conn.execute.return_value = cursor
        mock_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        scan_stale_drafts()

        sql = conn.execute.call_args.args[0]
        assert "status != 'archived'" in sql

    @patch("app.jobs.archive_warning.get_connection")
    def test_db_error_returns_empty_list(self, mock_conn):
        """A dead DB must never crash the scheduler thread — the scan
        swallows the exception and returns an empty list so the next
        tick can retry cleanly."""
        mock_conn.side_effect = RuntimeError("boom")

        result = scan_stale_drafts()

        assert result == []


# ---------------------------------------------------------------------------
# touch_draft_access
# ---------------------------------------------------------------------------


class TestTouchDraftAccess:
    def test_touch_draft_access_runs_update(self):
        """``touch_draft_access`` issues the SET last_accessed_at = now()
        UPDATE and returns True when a row was affected."""
        from app.docs.draft_model import touch_draft_access

        conn = MagicMock()
        result = MagicMock()
        result.rowcount = 1
        conn.execute.return_value = result

        draft_id = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        ok = touch_draft_access(conn, draft_id)

        assert ok is True
        call = conn.execute.call_args
        sql = call.args[0].lower()
        assert "update drafts" in sql
        assert "last_accessed_at" in sql
        assert "now()" in sql
        # The update is scoped to the requested draft id.
        assert call.args[1] == (str(draft_id),)

    def test_touch_draft_access_swallows_db_error(self):
        """A DB failure on the touch path must never take down the
        primary read request."""
        from app.docs.draft_model import touch_draft_access

        conn = MagicMock()
        conn.execute.side_effect = RuntimeError("boom")

        ok = touch_draft_access(conn, uuid.uuid4())

        assert ok is False


# ---------------------------------------------------------------------------
# POST /drafts/{draft_id}/keep
# ---------------------------------------------------------------------------


class TestKeepDraftRoute:
    @patch("app.docs.routes.log_action")
    @patch("app.docs.routes.touch_draft_access")
    @patch("app.docs.routes._connect")
    @patch("app.docs.routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_owner_can_keep_resets_clock(
        self,
        mock_get_provider,
        mock_fetch,
        mock_connect,
        mock_touch,
        mock_log,
    ):
        mock_get_provider.return_value = _stub_provider(_authed_user())
        draft = _make_draft(user_id=_USER_ID, org_id=_ORG_ID)
        mock_fetch.return_value = draft

        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_touch.return_value = True

        client = _authed_client()
        resp = client.post(f"/drafts/{draft.id}/keep")

        assert resp.status_code == 303
        assert resp.headers["location"] == f"/drafts/{draft.id}"
        mock_touch.assert_called_once()
        # Audit log captures the governance action.
        mock_log.assert_called_once()
        action = mock_log.call_args.args[1]
        assert action == "draft.keep"

    @patch("app.docs.routes.touch_draft_access")
    @patch("app.docs.routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_non_owner_same_org_is_rejected(
        self,
        mock_get_provider,
        mock_fetch,
        mock_touch,
    ):
        """A same-org colleague who is not the owner must not be able
        to reset the archive clock — the policy mirrors delete."""
        mock_get_provider.return_value = _stub_provider(_authed_user())
        draft = _make_draft(user_id=_OTHER_USER_ID, org_id=_ORG_ID)
        mock_fetch.return_value = draft

        client = _authed_client()
        resp = client.post(f"/drafts/{draft.id}/keep")

        # Route returns the 404 page (never 403) so we don't leak the
        # existence of another user's draft.
        assert resp.status_code == 200
        assert "Eelnõu ei leitud" in resp.text
        mock_touch.assert_not_called()

    @patch("app.docs.routes.touch_draft_access")
    @patch("app.docs.routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_cross_org_user_is_rejected(
        self,
        mock_get_provider,
        mock_fetch,
        mock_touch,
    ):
        mock_get_provider.return_value = _stub_provider(_authed_user())
        draft = _make_draft(org_id=_OTHER_ORG_ID)
        mock_fetch.return_value = draft

        client = _authed_client()
        resp = client.post(f"/drafts/{draft.id}/keep")

        assert resp.status_code == 200
        assert "Eelnõu ei leitud" in resp.text
        mock_touch.assert_not_called()

    @patch("app.docs.routes.fetch_draft")
    @patch("app.auth.middleware._get_provider")
    def test_missing_draft_returns_not_found(
        self,
        mock_get_provider,
        mock_fetch,
    ):
        mock_get_provider.return_value = _stub_provider(_authed_user())
        mock_fetch.return_value = None

        client = _authed_client()
        resp = client.post("/drafts/99999999-9999-9999-9999-999999999999/keep")

        assert resp.status_code == 200
        assert "Eelnõu ei leitud" in resp.text


# ---------------------------------------------------------------------------
# notify_draft_archive_warning factory
# ---------------------------------------------------------------------------


class TestNotifyDraftArchiveWarning:
    @patch("app.notifications.wire.notify")
    def test_factory_emits_notification_with_metadata(self, mock_notify):
        from app.notifications.wire import notify_draft_archive_warning

        draft = _make_draft(
            draft_id=uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
            last_accessed_at=datetime.now(UTC) - timedelta(days=100),
        )

        notify_draft_archive_warning(draft)

        mock_notify.assert_called_once()
        kwargs = mock_notify.call_args.kwargs
        assert kwargs["type"] == "draft_archive_warning"
        assert kwargs["user_id"] == draft.user_id
        assert kwargs["link"] == f"/drafts/{draft.id}"
        assert kwargs["metadata"]["draft_id"] == str(draft.id)
        assert kwargs["metadata"]["title"] == draft.title
        # ISO-encoded timestamp so the notification UI can render a
        # relative "last accessed X days ago" label without re-parsing.
        assert kwargs["metadata"]["last_accessed_at"] is not None
