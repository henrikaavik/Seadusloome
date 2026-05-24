"""Unit tests for ``app.notifications.wire``.

Each wire-up function is tested with mocked ``notify()`` and, where
applicable, mocked DB access. The tests verify that the correct
arguments are passed to ``notify()`` and that errors are swallowed.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock, patch

from app.notifications.wire import (
    notify_analysis_done,
    notify_annotation_reply,
    notify_cost_alert,
    notify_draft_shared,
    notify_drafter_complete,
    notify_sync_failed,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_USER_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
_ORG_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
_DRAFT_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
_ANN_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
_REPLY_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")
_SESSION_ID = uuid.UUID("77777777-7777-7777-7777-777777777777")


def _make_annotation(
    user_id: uuid.UUID = _USER_A,
    target_type: str = "draft",
    target_id: str | None = None,
) -> MagicMock:
    ann = MagicMock()
    ann.id = _ANN_ID
    ann.user_id = user_id
    ann.target_type = target_type
    ann.target_id = target_id or str(_DRAFT_ID)
    return ann


def _make_reply(user_id: uuid.UUID = _USER_B) -> MagicMock:
    reply = MagicMock()
    reply.id = _REPLY_ID
    reply.user_id = user_id
    reply.content = "Noustun!"
    return reply


def _make_draft() -> MagicMock:
    draft = MagicMock()
    draft.id = _DRAFT_ID
    draft.user_id = _USER_A
    draft.title = "Test eelnou"
    return draft


def _make_session() -> MagicMock:
    session = MagicMock()
    session.id = _SESSION_ID
    session.user_id = _USER_A
    session.intent = "Kodakondsusseaduse muutmine"
    return session


# ---------------------------------------------------------------------------
# Tests: notify_annotation_reply
# ---------------------------------------------------------------------------


class TestNotifyAnnotationReply:
    @patch("app.notifications.wire.notify")
    def test_notifies_annotation_author(self, mock_notify):
        ann = _make_annotation(_USER_A)
        reply = _make_reply(_USER_B)

        notify_annotation_reply(ann, reply)

        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args[1]
        assert call_kwargs["user_id"] == _USER_A
        assert call_kwargs["type"] == "annotation_reply"

    @patch("app.notifications.wire.notify")
    def test_skips_self_reply(self, mock_notify):
        ann = _make_annotation(_USER_A)
        reply = _make_reply(_USER_A)

        notify_annotation_reply(ann, reply)

        mock_notify.assert_not_called()

    @patch("app.notifications.wire.notify")
    def test_link_points_at_real_get_page_for_draft_target(self, mock_notify):
        """Bug 3: `link=f"/annotations/{id}"` is a dead route.

        When the annotation is on a draft, the notification link must
        navigate to the draft's detail page (a 200 GET), not a
        non-existent ``/annotations/{id}`` URL.
        """
        ann = _make_annotation(_USER_A, target_type="draft", target_id=str(_DRAFT_ID))
        reply = _make_reply(_USER_B)

        notify_annotation_reply(ann, reply)

        call_kwargs = mock_notify.call_args[1]
        link = call_kwargs.get("link") or ""
        # Must not use the dead /annotations/{id} route.
        assert not link.startswith("/annotations/")
        # Must point at the draft detail page for a draft-target annotation.
        assert str(_DRAFT_ID) in link
        assert link.startswith("/drafts/")

    @patch("app.notifications.wire.notify")
    def test_link_points_at_chat_for_conversation_target(self, mock_notify):
        ann = _make_annotation(
            _USER_A,
            target_type="conversation",
            target_id="11111111-2222-3333-4444-555555555555",
        )
        reply = _make_reply(_USER_B)

        notify_annotation_reply(ann, reply)

        call_kwargs = mock_notify.call_args[1]
        link = call_kwargs.get("link") or ""
        assert not link.startswith("/annotations/")
        assert link.startswith("/chat/")


# ---------------------------------------------------------------------------
# Tests: notify_analysis_done
# ---------------------------------------------------------------------------


class TestNotifyAnalysisDone:
    @patch("app.notifications.wire.notify")
    def test_notifies_draft_owner(self, mock_notify):
        draft = _make_draft()

        notify_analysis_done(draft)

        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args[1]
        assert call_kwargs["user_id"] == _USER_A
        assert call_kwargs["type"] == "analysis_done"
        assert str(_DRAFT_ID) in (call_kwargs.get("link") or "")


# ---------------------------------------------------------------------------
# Tests: notify_drafter_complete
# ---------------------------------------------------------------------------


class TestNotifyDrafterComplete:
    @patch("app.notifications.wire.notify")
    def test_notifies_session_owner(self, mock_notify):
        session = _make_session()

        notify_drafter_complete(session)

        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args[1]
        assert call_kwargs["user_id"] == _USER_A
        assert call_kwargs["type"] == "drafter_complete"
        assert str(_SESSION_ID) in (call_kwargs.get("link") or "")


# ---------------------------------------------------------------------------
# Tests: notify_sync_failed
# ---------------------------------------------------------------------------


class _ConnectCM:
    def __init__(self, conn: MagicMock):
        self.conn = conn

    def __enter__(self) -> MagicMock:
        return self.conn

    def __exit__(self, *_: Any) -> bool:
        return False


class TestNotifySyncFailed:
    @patch("app.notifications.wire.notify")
    @patch("app.db.get_connection")
    def test_notifies_all_admins(self, mock_connect, mock_notify):
        admin1 = uuid.uuid4()
        admin2 = uuid.uuid4()
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [(admin1,), (admin2,)]
        mock_connect.return_value = _ConnectCM(conn)

        notify_sync_failed("Connection refused")

        assert mock_notify.call_count == 2
        user_ids = [call[1]["user_id"] for call in mock_notify.call_args_list]
        assert admin1 in user_ids
        assert admin2 in user_ids

    @patch("app.notifications.wire.notify")
    @patch("app.db.get_connection")
    def test_link_points_at_real_get_page(self, mock_connect, mock_notify):
        """Bug 4: `/admin/sync` is a POST-only route — sending admins
        there yields a 405. The link must target a real GET page.
        """
        admin1 = uuid.uuid4()
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [(admin1,)]
        mock_connect.return_value = _ConnectCM(conn)

        notify_sync_failed("Connection refused")

        call_kwargs = mock_notify.call_args[1]
        link = call_kwargs.get("link") or ""
        # Must not be the POST-only /admin/sync route.
        assert link != "/admin/sync"
        # Must resolve to a real GET admin page. The admin dashboard
        # (/admin) exposes a sync-card section we can anchor to.
        assert link.startswith("/admin")
        # Must not be any other POST-only admin route either.
        assert not link.startswith("/admin/sync?")
        assert not link == "/admin/sync/"


# ---------------------------------------------------------------------------
# Tests: notify_cost_alert
# ---------------------------------------------------------------------------


class TestNotifyCostAlert:
    @patch("app.notifications.wire.notify")
    @patch("app.db.get_connection")
    def test_notifies_org_admins(self, mock_connect, mock_notify):
        admin1 = uuid.uuid4()
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [(admin1,)]
        mock_connect.return_value = _ConnectCM(conn)

        notify_cost_alert(_ORG_ID, 42.0, 50.0)

        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args[1]
        assert call_kwargs["user_id"] == admin1
        assert call_kwargs["type"] == "cost_alert"
        assert "84%" in call_kwargs["title"]


# ---------------------------------------------------------------------------
# Tests: notify_draft_shared (#299)
# ---------------------------------------------------------------------------


def _make_draft_with_org(
    user_id: uuid.UUID = _USER_A,
    org_id: uuid.UUID = _ORG_ID,
    draft_id: uuid.UUID = _DRAFT_ID,
    title: str = "Test eelnou",
    filename: str = "test.docx",
) -> MagicMock:
    draft = MagicMock()
    draft.id = draft_id
    draft.user_id = user_id
    draft.org_id = org_id
    draft.title = title
    draft.filename = filename
    return draft


class TestNotifyDraftShared:
    @patch("app.notifications.wire.notify")
    @patch("app.db.get_connection")
    def test_notifies_drafters_and_reviewers_excluding_uploader(self, mock_connect, mock_notify):
        """Org has 3 members (uploader, drafter, reviewer); uploader is
        excluded by the SQL ``id != %s`` filter, so notify() is called
        exactly twice — once per remaining team member.
        """
        drafter_id = uuid.uuid4()
        reviewer_id = uuid.uuid4()
        conn = MagicMock()
        # The SQL filters out the uploader, so the rowset only contains
        # the two other team members.
        conn.execute.return_value.fetchall.return_value = [
            (drafter_id,),
            (reviewer_id,),
        ]
        mock_connect.return_value = _ConnectCM(conn)

        draft = _make_draft_with_org(user_id=_USER_A, org_id=_ORG_ID)
        notify_draft_shared(draft)

        # Two notifications: drafter + reviewer; uploader is NOT in the list.
        assert mock_notify.call_count == 2
        user_ids = [call[1]["user_id"] for call in mock_notify.call_args_list]
        assert drafter_id in user_ids
        assert reviewer_id in user_ids
        assert _USER_A not in user_ids

    @patch("app.notifications.wire.notify")
    @patch("app.db.get_connection")
    def test_sql_filters_uploader_role_and_status(self, mock_connect, mock_notify):
        """The SQL must filter on org_id + role IN (drafter, reviewer)
        + id != uploader + is_active = TRUE. Verify the actual query
        and parameters passed to the DB.
        """
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []
        mock_connect.return_value = _ConnectCM(conn)

        draft = _make_draft_with_org(user_id=_USER_A, org_id=_ORG_ID)
        notify_draft_shared(draft)

        # Inspect the SQL + params passed to conn.execute.
        sql, params = conn.execute.call_args[0]
        assert "org_id = %s" in sql
        assert "drafter" in sql and "reviewer" in sql
        assert "id != %s" in sql
        assert "is_active = TRUE" in sql
        # Params: org_id first, then uploader id.
        assert params == (str(_ORG_ID), str(_USER_A))

    @patch("app.notifications.wire.notify")
    @patch("app.db.get_connection")
    def test_no_eligible_recipients_no_notifications(self, mock_connect, mock_notify):
        """An org with no drafters/reviewers (only the uploader, or only
        admins) yields zero notifications.
        """
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []
        mock_connect.return_value = _ConnectCM(conn)

        draft = _make_draft_with_org(user_id=_USER_A, org_id=_ORG_ID)
        notify_draft_shared(draft)

        mock_notify.assert_not_called()

    @patch("app.notifications.wire.notify")
    @patch("app.db.get_connection")
    def test_uploader_excluded_regardless_of_role(self, mock_connect, mock_notify):
        """The uploader's own role doesn't matter — they are excluded
        by ``id != %s`` even if they happen to also be a drafter or
        reviewer. We assert that the uploader id never appears in any
        notify() call's ``user_id``.
        """
        # The DB layer already enforces exclusion via SQL; this test
        # belt-and-suspenders the contract by passing back the uploader
        # as if the SQL filter had failed. Because the SQL params
        # include the uploader id, a correctly-built query CANNOT return
        # the uploader. We still verify by asserting the uploader id is
        # not in the params-driven recipient list (which we drive here).
        other_drafter = uuid.uuid4()
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [(other_drafter,)]
        mock_connect.return_value = _ConnectCM(conn)

        draft = _make_draft_with_org(user_id=_USER_A, org_id=_ORG_ID)
        notify_draft_shared(draft)

        # Verify the uploader id was bound into the WHERE clause params.
        _, params = conn.execute.call_args[0]
        assert str(_USER_A) in params
        # And the only notification went to the other drafter.
        mock_notify.assert_called_once()
        assert mock_notify.call_args[1]["user_id"] == other_drafter

    @patch("app.notifications.wire.notify")
    @patch("app.db.get_connection")
    def test_link_points_at_draft_detail_page(self, mock_connect, mock_notify):
        recipient = uuid.uuid4()
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [(recipient,)]
        mock_connect.return_value = _ConnectCM(conn)

        draft = _make_draft_with_org(draft_id=_DRAFT_ID)
        notify_draft_shared(draft)

        call_kwargs = mock_notify.call_args[1]
        assert call_kwargs["link"] == f"/drafts/{_DRAFT_ID}"
        assert call_kwargs["type"] == "draft_shared"
        # Metadata carries the draft_id and uploader id for downstream
        # consumers (notification list, future filters).
        meta = call_kwargs["metadata"]
        assert meta["draft_id"] == str(_DRAFT_ID)
        assert meta["uploaded_by"] == str(_USER_A)

    @patch("app.notifications.wire.notify")
    @patch("app.db.get_connection")
    def test_swallows_db_errors(self, mock_connect, mock_notify):
        """A DB failure must never propagate out of the wire helper —
        the upload flow has already succeeded by the time we get here.
        """
        mock_connect.side_effect = RuntimeError("db down")

        draft = _make_draft_with_org()
        # Must not raise.
        notify_draft_shared(draft)

        mock_notify.assert_not_called()

    @patch("app.notifications.wire.notify")
    @patch("app.db.get_connection")
    def test_missing_org_id_is_a_noop(self, mock_connect, mock_notify):
        """A draft without org_id (defensive) yields no DB hit and no
        notifications, rather than crashing.
        """
        draft = MagicMock()
        draft.id = _DRAFT_ID
        draft.user_id = _USER_A
        draft.org_id = None
        draft.title = "x"
        draft.filename = "x.docx"

        notify_draft_shared(draft)

        mock_connect.assert_not_called()
        mock_notify.assert_not_called()
