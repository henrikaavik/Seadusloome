"""Integration tests for :mod:`app.chat.handlers`.

Mirrors the conventions used in ``tests/test_chat_routes.py``: a stub
``AuthProvider`` supplies an authenticated user, ``_connect`` and the
feedback/models helpers are patched out, and the full FastHTML ASGI app
is exercised through ``starlette.testclient.TestClient``.

The handlers module is registered exactly once on the production ``rt``
at collection time — future handler registrations land through
``register_chat_handler_routes`` too, so a repeat call would be a no-op
duplicate for starlette's router.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Importing ``app.main`` runs ``register_chat_routes`` at module-load
# time, which delegates to ``register_chat_handler_routes`` FIRST so the
# static handler paths (``/chat/search``, ``/api/me/usage``,
# ``/chat/{conv_id}/pin`` etc.) land in ``app.routes`` ahead of the
# dynamic ``/chat/{conv_id}`` view. Re-registering here would re-insert
# the same paths and — because FastHTML's ``rt`` de-duplicates by path —
# pull the dynamic route ahead of ``/chat/search``, which would then
# swallow search requests as conv_id lookups. Just importing ``app.main``
# triggers the correct registration order.
import app.main  # noqa: F401 — module import is load-bearing
from app.chat.models import Conversation, Message
from app.chat.rate_limiter import UserQuota

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_ORG_ID = "11111111-1111-1111-1111-111111111111"
_OTHER_ORG_ID = "22222222-2222-2222-2222-222222222222"
_USER_ID = "33333333-3333-3333-3333-333333333333"
_OTHER_USER_ID = "99999999-9999-9999-9999-999999999999"
_CONV_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
_MSG_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
_DRAFT_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")


def _authed_user() -> dict[str, Any]:
    return {
        "id": _USER_ID,
        "email": "kasutaja@seadusloome.ee",
        "full_name": "Test Kasutaja",
        "role": "drafter",
        "org_id": _ORG_ID,
    }


def _stub_provider() -> MagicMock:
    provider = MagicMock()
    provider.get_current_user.return_value = _authed_user()
    return provider


def _make_conversation(
    *,
    conv_id: uuid.UUID = _CONV_ID,
    org_id: str = _ORG_ID,
    user_id: str = _USER_ID,
    title: str = "Test vestlus",
    context_draft_id: uuid.UUID | None = None,
) -> Conversation:
    now = datetime(2026, 4, 14, 9, 30, tzinfo=UTC)
    return Conversation(
        id=conv_id,
        user_id=uuid.UUID(user_id),
        org_id=uuid.UUID(org_id),
        title=title,
        context_draft_id=context_draft_id,
        created_at=now,
        updated_at=now,
    )


def _make_message(
    *,
    msg_id: uuid.UUID = _MSG_ID,
    role: str = "user",
    content: str = "Tere",
    conv_id: uuid.UUID = _CONV_ID,
) -> Message:
    now = datetime(2026, 4, 14, 9, 30, tzinfo=UTC)
    return Message(
        id=msg_id,
        conversation_id=conv_id,
        role=role,
        content=content,
        tool_name=None,
        tool_input=None,
        tool_output=None,
        rag_context=None,
        tokens_input=None,
        tokens_output=None,
        model=None,
        created_at=now,
    )


def _authed_client():
    from starlette.testclient import TestClient

    client = TestClient(
        __import__("app.main", fromlist=["app"]).app,
        follow_redirects=False,
    )
    client.cookies.set("access_token", "stub-token")
    return client


@pytest.fixture()
def provider_patch():
    with patch("app.auth.middleware._get_provider") as mock_provider:
        mock_provider.return_value = _stub_provider()
        yield mock_provider


@pytest.fixture()
def mock_conn():
    """Patch ``app.chat.handlers._connect`` with a no-op connection."""
    with patch("app.chat.handlers._connect") as mock_connect:
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        conn.execute.return_value.fetchall.return_value = []
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        yield mock_connect, conn


# ---------------------------------------------------------------------------
# Unauthenticated redirects
# ---------------------------------------------------------------------------


class TestAuthRequired:
    def test_pin_redirects_unauthenticated(self):
        from starlette.testclient import TestClient

        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.post(f"/chat/{_CONV_ID}/pin")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"

    def test_usage_redirects_unauthenticated(self):
        from starlette.testclient import TestClient

        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.get("/api/me/usage")
        assert resp.status_code == 303

    def test_export_redirects_unauthenticated(self):
        from starlette.testclient import TestClient

        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.get(f"/chat/{_CONV_ID}/export.md")
        assert resp.status_code == 303


# ---------------------------------------------------------------------------
# Pin conversation
# ---------------------------------------------------------------------------


class TestPinConversation:
    def test_pin_toggles_and_returns_204(self, provider_patch, mock_conn):
        conv = _make_conversation()
        with (
            patch("app.chat.handlers.get_conversation", return_value=conv),
            patch("app.chat.handlers._set_conversation_pinned") as mock_set,
        ):
            client = _authed_client()
            resp = client.post(f"/chat/{_CONV_ID}/pin", data={"pinned": "1"})
            assert resp.status_code == 204
            assert resp.headers.get("HX-Trigger") == "chat:conversation-updated"
            mock_set.assert_called_once()
            _, _, pinned = mock_set.call_args[0]
            assert pinned is True

    def test_pin_cross_org_returns_404(self, provider_patch, mock_conn):
        conv = _make_conversation(org_id=_OTHER_ORG_ID, user_id=_OTHER_USER_ID)
        with patch("app.chat.handlers.get_conversation", return_value=conv):
            client = _authed_client()
            resp = client.post(f"/chat/{_CONV_ID}/pin")
            assert resp.status_code == 404

    def test_pin_invalid_uuid_returns_404(self, provider_patch, mock_conn):
        client = _authed_client()
        resp = client.post("/chat/not-a-uuid/pin")
        assert resp.status_code == 404

    def test_pin_missing_conversation_returns_404(self, provider_patch, mock_conn):
        with patch("app.chat.handlers.get_conversation", return_value=None):
            client = _authed_client()
            resp = client.post(f"/chat/{_CONV_ID}/pin")
            assert resp.status_code == 404

    def test_pin_htmx_returns_row_fragment(self, provider_patch, mock_conn):
        """Bug #654: htmx pin returns a refreshed ``<tr>`` for in-place swap.

        The initial ``get_conversation`` call loads the pre-mutation row
        for the access check; the second call (after the DB write) reads
        the fresh state so the returned fragment reflects the new
        ``is_pinned`` value (★ indicator).
        """
        conv_before = _make_conversation()
        conv_after = _make_conversation()
        conv_after.is_pinned = True
        with (
            patch(
                "app.chat.handlers.get_conversation",
                side_effect=[conv_before, conv_after],
            ),
            patch("app.chat.handlers._set_conversation_pinned"),
        ):
            client = _authed_client()
            resp = client.post(
                f"/chat/{_CONV_ID}/pin",
                headers={"HX-Request": "true"},
                data={"pinned": "1"},
            )
            assert resp.status_code == 200
            # Row fragment must contain the conversation id (as a DOM id
            # and inside the links) and the pin star for the pinned state.
            assert "<tr" in resp.text
            assert str(_CONV_ID) in resp.text
            assert "\u2605" in resp.text  # pin star indicator


# ---------------------------------------------------------------------------
# Archive conversation
# ---------------------------------------------------------------------------


class TestArchiveConversation:
    def test_archive_htmx_returns_empty_row_swap(self, provider_patch, mock_conn):
        """Bug #654: htmx archive removes the row in place (empty 200).

        The HX-Trigger event is preserved so sidebar counters listening
        for ``chat:conversation-updated`` still refresh.
        """
        conv = _make_conversation()
        with (
            patch("app.chat.handlers.get_conversation", return_value=conv),
            patch("app.chat.handlers._set_conversation_archived") as mock_set,
        ):
            client = _authed_client()
            resp = client.post(
                f"/chat/{_CONV_ID}/archive",
                headers={"HX-Request": "true"},
                data={"archived": "1"},
            )
            assert resp.status_code == 200
            assert resp.text == ""
            assert resp.headers.get("HX-Trigger") == "chat:conversation-updated"
            mock_set.assert_called_once()

    def test_archive_non_htmx_redirects_303(self, provider_patch, mock_conn):
        conv = _make_conversation()
        with (
            patch("app.chat.handlers.get_conversation", return_value=conv),
            patch("app.chat.handlers._set_conversation_archived"),
        ):
            client = _authed_client()
            resp = client.post(f"/chat/{_CONV_ID}/archive", data={"archived": "1"})
            assert resp.status_code == 303
            assert resp.headers["location"] == "/chat"

    def test_archive_cross_org_404(self, provider_patch, mock_conn):
        conv = _make_conversation(org_id=_OTHER_ORG_ID, user_id=_OTHER_USER_ID)
        with patch("app.chat.handlers.get_conversation", return_value=conv):
            client = _authed_client()
            resp = client.post(f"/chat/{_CONV_ID}/archive")
            assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Rename conversation
# ---------------------------------------------------------------------------


class TestRenameConversation:
    def test_rename_success(self, provider_patch, mock_conn):
        conv = _make_conversation()
        with (
            patch("app.chat.handlers.get_conversation", return_value=conv),
            patch("app.chat.handlers._update_conversation_title") as mock_update,
        ):
            client = _authed_client()
            resp = client.post(
                f"/chat/{_CONV_ID}/rename",
                data={"title": "  Uus pealkiri  "},
            )
            assert resp.status_code == 204
            assert resp.headers.get("HX-Trigger") == "chat:conversation-updated"
            mock_update.assert_called_once()
            _, _, title = mock_update.call_args[0]
            assert title == "Uus pealkiri"

    def test_rename_empty_returns_400(self, provider_patch, mock_conn):
        conv = _make_conversation()
        with patch("app.chat.handlers.get_conversation", return_value=conv):
            client = _authed_client()
            resp = client.post(f"/chat/{_CONV_ID}/rename", data={"title": "   "})
            assert resp.status_code == 400

    def test_rename_truncates_to_200_chars(self, provider_patch, mock_conn):
        conv = _make_conversation()
        long_title = "x" * 500
        with (
            patch("app.chat.handlers.get_conversation", return_value=conv),
            patch("app.chat.handlers._update_conversation_title") as mock_update,
        ):
            client = _authed_client()
            resp = client.post(
                f"/chat/{_CONV_ID}/rename",
                data={"title": long_title},
            )
            assert resp.status_code == 204
            title = mock_update.call_args[0][2]
            assert len(title) == 200

    def test_rename_cross_org_404(self, provider_patch, mock_conn):
        conv = _make_conversation(org_id=_OTHER_ORG_ID, user_id=_OTHER_USER_ID)
        with patch("app.chat.handlers.get_conversation", return_value=conv):
            client = _authed_client()
            resp = client.post(f"/chat/{_CONV_ID}/rename", data={"title": "X"})
            assert resp.status_code == 404

    def test_rename_htmx_returns_row_fragment_with_new_title(self, provider_patch, mock_conn):
        """Bug #654: htmx rename returns the refreshed ``<tr>`` with the
        new title, so the list row updates in place.
        """
        conv_before = _make_conversation(title="Vana pealkiri")
        conv_after = _make_conversation(title="Uus pealkiri")
        with (
            patch(
                "app.chat.handlers.get_conversation",
                side_effect=[conv_before, conv_after],
            ),
            patch("app.chat.handlers._update_conversation_title"),
        ):
            client = _authed_client()
            resp = client.post(
                f"/chat/{_CONV_ID}/rename",
                headers={"HX-Request": "true"},
                data={"title": "Uus pealkiri"},
            )
            assert resp.status_code == 200
            assert "<tr" in resp.text
            assert str(_CONV_ID) in resp.text
            assert "Uus pealkiri" in resp.text


# ---------------------------------------------------------------------------
# Fork conversation
# ---------------------------------------------------------------------------


class TestForkConversation:
    def test_fork_htmx_returns_hx_redirect(self, provider_patch, mock_conn):
        conv = _make_conversation()
        new_id = uuid.UUID("77777777-7777-7777-7777-777777777777")
        with (
            patch("app.chat.handlers.get_conversation", return_value=conv),
            patch("app.chat.handlers._fork_conversation", return_value=new_id),
        ):
            client = _authed_client()
            resp = client.post(
                f"/chat/{_CONV_ID}/fork",
                data={"message_id": str(_MSG_ID)},
                headers={"HX-Request": "true"},
            )
            assert resp.status_code == 204
            assert resp.headers.get("HX-Redirect") == f"/chat/{new_id}"

    def test_fork_non_htmx_redirects_303(self, provider_patch, mock_conn):
        conv = _make_conversation()
        new_id = uuid.UUID("77777777-7777-7777-7777-777777777777")
        with (
            patch("app.chat.handlers.get_conversation", return_value=conv),
            patch("app.chat.handlers._fork_conversation", return_value=new_id),
        ):
            client = _authed_client()
            resp = client.post(
                f"/chat/{_CONV_ID}/fork",
                data={"message_id": str(_MSG_ID)},
            )
            assert resp.status_code == 303
            assert resp.headers["location"] == f"/chat/{new_id}"

    def test_fork_bad_message_id_400(self, provider_patch, mock_conn):
        conv = _make_conversation()
        with patch("app.chat.handlers.get_conversation", return_value=conv):
            client = _authed_client()
            resp = client.post(
                f"/chat/{_CONV_ID}/fork",
                data={"message_id": "not-a-uuid"},
            )
            assert resp.status_code == 400

    def test_fork_cross_org_404(self, provider_patch, mock_conn):
        conv = _make_conversation(org_id=_OTHER_ORG_ID, user_id=_OTHER_USER_ID)
        with patch("app.chat.handlers.get_conversation", return_value=conv):
            client = _authed_client()
            resp = client.post(
                f"/chat/{_CONV_ID}/fork",
                data={"message_id": str(_MSG_ID)},
            )
            assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Pin message
# ---------------------------------------------------------------------------


class TestPinMessage:
    def test_pin_message_success(self, provider_patch, mock_conn):
        conv = _make_conversation()
        msg = _make_message()
        with (
            patch("app.chat.handlers.get_conversation", return_value=conv),
            patch("app.chat.handlers.list_messages", return_value=[msg]),
            patch("app.chat.handlers._set_message_pinned") as mock_set,
        ):
            client = _authed_client()
            resp = client.post(
                f"/chat/{_CONV_ID}/messages/{_MSG_ID}/pin",
                data={"pinned": "1"},
            )
            assert resp.status_code == 204
            assert resp.headers.get("HX-Trigger") == "chat:message-updated"
            mock_set.assert_called_once()

    def test_pin_wrong_conversation_message_404(self, provider_patch, mock_conn):
        conv = _make_conversation()
        # Message list is empty — the msg_id isn't part of this thread.
        with (
            patch("app.chat.handlers.get_conversation", return_value=conv),
            patch("app.chat.handlers.list_messages", return_value=[]),
        ):
            client = _authed_client()
            resp = client.post(f"/chat/{_CONV_ID}/messages/{_MSG_ID}/pin")
            assert resp.status_code == 404

    def test_pin_message_cross_org_404(self, provider_patch, mock_conn):
        conv = _make_conversation(org_id=_OTHER_ORG_ID, user_id=_OTHER_USER_ID)
        with patch("app.chat.handlers.get_conversation", return_value=conv):
            client = _authed_client()
            resp = client.post(f"/chat/{_CONV_ID}/messages/{_MSG_ID}/pin")
            assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------


class TestFeedback:
    def test_post_feedback_returns_json(self, provider_patch, mock_conn):
        conv = _make_conversation()
        msg = _make_message(role="assistant")
        with (
            patch("app.chat.handlers.get_conversation", return_value=conv),
            patch("app.chat.handlers.list_messages", return_value=[msg]),
            patch("app.chat.handlers._upsert_feedback") as mock_upsert,
            patch("app.chat.handlers._feedback_counts", return_value=(3, 1)),
        ):
            client = _authed_client()
            resp = client.post(
                f"/chat/{_CONV_ID}/messages/{_MSG_ID}/feedback",
                data={"rating": "1", "comment": "Hea vastus"},
            )
            assert resp.status_code == 200
            payload = resp.json()
            assert payload == {"up": 3, "down": 1, "user_rating": 1}
            mock_upsert.assert_called_once()

    def test_post_invalid_rating_400(self, provider_patch, mock_conn):
        conv = _make_conversation()
        msg = _make_message(role="assistant")
        with (
            patch("app.chat.handlers.get_conversation", return_value=conv),
            patch("app.chat.handlers.list_messages", return_value=[msg]),
        ):
            client = _authed_client()
            resp = client.post(
                f"/chat/{_CONV_ID}/messages/{_MSG_ID}/feedback",
                data={"rating": "5"},
            )
            assert resp.status_code == 400

    def test_post_non_numeric_rating_400(self, provider_patch, mock_conn):
        conv = _make_conversation()
        msg = _make_message(role="assistant")
        with (
            patch("app.chat.handlers.get_conversation", return_value=conv),
            patch("app.chat.handlers.list_messages", return_value=[msg]),
        ):
            client = _authed_client()
            resp = client.post(
                f"/chat/{_CONV_ID}/messages/{_MSG_ID}/feedback",
                data={"rating": "thumbs-up"},
            )
            assert resp.status_code == 400

    def test_delete_feedback_returns_json(self, provider_patch, mock_conn):
        conv = _make_conversation()
        msg = _make_message(role="assistant")
        with (
            patch("app.chat.handlers.get_conversation", return_value=conv),
            patch("app.chat.handlers.list_messages", return_value=[msg]),
            patch("app.chat.handlers._delete_feedback_row") as mock_delete,
            patch("app.chat.handlers._feedback_counts", return_value=(2, 0)),
        ):
            client = _authed_client()
            resp = client.delete(f"/chat/{_CONV_ID}/messages/{_MSG_ID}/feedback")
            assert resp.status_code == 200
            payload = resp.json()
            assert payload == {"up": 2, "down": 0, "user_rating": None}
            mock_delete.assert_called_once()

    def test_feedback_cross_org_404(self, provider_patch, mock_conn):
        conv = _make_conversation(org_id=_OTHER_ORG_ID, user_id=_OTHER_USER_ID)
        with patch("app.chat.handlers.get_conversation", return_value=conv):
            client = _authed_client()
            resp = client.post(
                f"/chat/{_CONV_ID}/messages/{_MSG_ID}/feedback",
                data={"rating": "1"},
            )
            assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Regenerate
# ---------------------------------------------------------------------------


class TestRegenerate:
    def test_regenerate_deletes_and_returns_204(self, provider_patch, mock_conn):
        conv = _make_conversation()
        msg = _make_message(role="user")
        with (
            patch("app.chat.handlers.get_conversation", return_value=conv),
            patch("app.chat.handlers.list_messages", return_value=[msg]),
            patch("app.chat.handlers._delete_messages_after_pivot", return_value=2) as mock_del,
        ):
            client = _authed_client()
            resp = client.post(f"/chat/{_CONV_ID}/messages/{_MSG_ID}/regenerate")
            assert resp.status_code == 204
            assert resp.headers.get("HX-Trigger") == "chat:message-regenerate"
            mock_del.assert_called_once()

    def test_regenerate_bad_msg_id_404(self, provider_patch, mock_conn):
        conv = _make_conversation()
        with patch("app.chat.handlers.get_conversation", return_value=conv):
            client = _authed_client()
            resp = client.post(f"/chat/{_CONV_ID}/messages/not-a-uuid/regenerate")
            assert resp.status_code == 404

    def test_regenerate_cross_org_404(self, provider_patch, mock_conn):
        conv = _make_conversation(org_id=_OTHER_ORG_ID, user_id=_OTHER_USER_ID)
        with patch("app.chat.handlers.get_conversation", return_value=conv):
            client = _authed_client()
            resp = client.post(f"/chat/{_CONV_ID}/messages/{_MSG_ID}/regenerate")
            assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------


class TestEditMessage:
    def test_edit_user_message_success(self, provider_patch, mock_conn):
        conv = _make_conversation()
        msg = _make_message(role="user", content="vana")
        with (
            patch("app.chat.handlers.get_conversation", return_value=conv),
            patch("app.chat.handlers.list_messages", return_value=[msg]),
            patch("app.chat.handlers._update_message_content") as mock_update,
            patch("app.chat.handlers._delete_messages_after_pivot", return_value=0),
        ):
            client = _authed_client()
            resp = client.post(
                f"/chat/{_CONV_ID}/messages/{_MSG_ID}/edit",
                data={"content": "uus sisu"},
            )
            assert resp.status_code == 204
            assert resp.headers.get("HX-Trigger") == "chat:message-edited"
            mock_update.assert_called_once()

    def test_edit_rejects_assistant_message(self, provider_patch, mock_conn):
        conv = _make_conversation()
        msg = _make_message(role="assistant")
        with (
            patch("app.chat.handlers.get_conversation", return_value=conv),
            patch("app.chat.handlers.list_messages", return_value=[msg]),
        ):
            client = _authed_client()
            resp = client.post(
                f"/chat/{_CONV_ID}/messages/{_MSG_ID}/edit",
                data={"content": "uus"},
            )
            assert resp.status_code == 400

    def test_edit_empty_content_400(self, provider_patch, mock_conn):
        conv = _make_conversation()
        msg = _make_message(role="user")
        with (
            patch("app.chat.handlers.get_conversation", return_value=conv),
            patch("app.chat.handlers.list_messages", return_value=[msg]),
        ):
            client = _authed_client()
            resp = client.post(
                f"/chat/{_CONV_ID}/messages/{_MSG_ID}/edit",
                data={"content": "   "},
            )
            assert resp.status_code == 400

    def test_edit_cross_org_404(self, provider_patch, mock_conn):
        conv = _make_conversation(org_id=_OTHER_ORG_ID, user_id=_OTHER_USER_ID)
        with patch("app.chat.handlers.get_conversation", return_value=conv):
            client = _authed_client()
            resp = client.post(
                f"/chat/{_CONV_ID}/messages/{_MSG_ID}/edit",
                data={"content": "X"},
            )
            assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


class TestExport:
    def test_export_md_returns_markdown(self, provider_patch, mock_conn):
        conv = _make_conversation(title="Vestlus AI-ga")
        msg = _make_message(role="user", content="Tere")
        with (
            patch("app.chat.handlers.get_conversation", return_value=conv),
            patch("app.chat.handlers.list_messages", return_value=[msg]),
        ):
            client = _authed_client()
            resp = client.get(f"/chat/{_CONV_ID}/export.md")
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/markdown")
            assert "vestlus-vestlus-ai-ga" in resp.headers.get("content-disposition", "")
            assert "# Vestlus AI-ga" in resp.text

    def test_export_docx_returns_bytes(self, provider_patch, mock_conn):
        conv = _make_conversation(title="Testvestlus")
        msg = _make_message(role="user", content="Tere")
        with (
            patch("app.chat.handlers.get_conversation", return_value=conv),
            patch("app.chat.handlers.list_messages", return_value=[msg]),
        ):
            client = _authed_client()
            resp = client.get(f"/chat/{_CONV_ID}/export.docx")
            assert resp.status_code == 200
            assert "wordprocessingml" in resp.headers["content-type"]
            # .docx files are ZIP archives — check the magic number.
            assert resp.content[:2] == b"PK"

    def test_export_md_cross_org_404(self, provider_patch, mock_conn):
        conv = _make_conversation(org_id=_OTHER_ORG_ID, user_id=_OTHER_USER_ID)
        with patch("app.chat.handlers.get_conversation", return_value=conv):
            client = _authed_client()
            resp = client.get(f"/chat/{_CONV_ID}/export.md")
            assert resp.status_code == 404

    def test_export_docx_invalid_uuid_404(self, provider_patch, mock_conn):
        client = _authed_client()
        resp = client.get("/chat/not-a-uuid/export.docx")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class TestSearch:
    def test_search_non_htmx_redirects(self, provider_patch, mock_conn):
        client = _authed_client()
        resp = client.get("/chat/search?q=eelnou")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/chat?q=eelnou"

    def test_search_htmx_returns_fragment(self, provider_patch, mock_conn):
        conv = _make_conversation(title="Eelnou X analuus")
        with patch("app.chat.handlers._search_conversations_impl", return_value=[conv]):
            client = _authed_client()
            resp = client.get("/chat/search?q=eelnou", headers={"HX-Request": "true"})
            assert resp.status_code == 200
            assert "chat-search-results" in resp.text
            assert "Eelnou X analuus" in resp.text

    def test_search_htmx_empty_term_returns_fragment(self, provider_patch, mock_conn):
        client = _authed_client()
        resp = client.get("/chat/search?q=", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        # No term → empty state fragment.
        assert "chat-search-results" in resp.text

    def test_search_htmx_no_matches(self, provider_patch, mock_conn):
        with patch("app.chat.handlers._search_conversations_impl", return_value=[]):
            client = _authed_client()
            resp = client.get("/chat/search?q=zzz", headers={"HX-Request": "true"})
            assert resp.status_code == 200
            assert "ei vastanud" in resp.text


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


class TestUserUsage:
    def test_usage_returns_expected_shape(self, provider_patch):
        fake_quota = UserQuota(
            messages_this_hour=5,
            message_limit_per_hour=100,
            cost_this_month_usd=Decimal("12.345"),
            cost_limit_per_month_usd=Decimal("50.00"),
            cost_alert_threshold_usd=Decimal("40.00"),
        )
        with (
            patch("app.chat.handlers.get_user_quota", return_value=fake_quota),
            patch("app.chat.handlers.seconds_until_hourly_reset", return_value=1234),
        ):
            client = _authed_client()
            resp = client.get("/api/me/usage")
            assert resp.status_code == 200
            data = resp.json()

        assert data["messages_this_hour"] == 5
        assert data["message_limit_per_hour"] == 100
        assert data["messages_remaining"] == 95
        # Decimal → string in JSON payload
        assert data["cost_this_month_usd"] == "12.35"
        assert data["cost_limit_per_month_usd"] == "50.00"
        # 50.00 - 12.345 = 37.655 → ROUND_HALF_UP → 37.66
        assert data["cost_remaining_usd"] == "37.66"
        assert data["cost_alert_threshold_usd"] == "40.00"
        assert data["seconds_until_reset"] == 1234
        assert data["percentages"]["messages"] == 5.0
        assert data["percentages"]["cost"] == pytest.approx(24.7, rel=0.1)

    def test_usage_no_org_id_returns_400(self):
        provider = MagicMock()
        user = dict(_authed_user())
        user["org_id"] = None
        provider.get_current_user.return_value = user
        with patch("app.auth.middleware._get_provider", return_value=provider):
            client = _authed_client()
            resp = client.get("/api/me/usage")
            assert resp.status_code == 400

    def test_usage_zero_limit_percentages(self, provider_patch):
        fake_quota = UserQuota(
            messages_this_hour=0,
            message_limit_per_hour=0,
            cost_this_month_usd=Decimal("0.00"),
            cost_limit_per_month_usd=Decimal("0.00"),
            cost_alert_threshold_usd=Decimal("0.00"),
        )
        with (
            patch("app.chat.handlers.get_user_quota", return_value=fake_quota),
            patch("app.chat.handlers.seconds_until_hourly_reset", return_value=0),
        ):
            client = _authed_client()
            resp = client.get("/api/me/usage")
            data = resp.json()
            assert data["percentages"]["messages"] == 0.0
            assert data["percentages"]["cost"] == 0.0


# ---------------------------------------------------------------------------
# Route ordering regression (P1): /chat/search must resolve before the
# dynamic /chat/{conv_id} catch-all.
# ---------------------------------------------------------------------------


class TestRouteOrdering:
    def test_chat_search_routes_before_dynamic_conv_id(self, provider_patch, mock_conn):
        """``/chat/search`` must not be swallowed by ``/chat/{conv_id}``.

        Regression for the dead-code ``_reorder_static_routes_first``
        hack: the handler module used to re-sort ``app.routes`` at
        startup because it was registered AFTER routes.py's dynamic
        ``/chat/{conv_id}`` view. ``register_chat_routes`` now invokes
        ``register_chat_handler_routes`` first, so the reorder is a
        no-op. This test locks in the registration order.
        """
        client = _authed_client()
        resp = client.get("/chat/search?q=test", headers={"HX-Request": "true"})
        # The search handler responds 200 with the results fragment; the
        # dynamic conv_id route would either 404 (bad UUID "search") or
        # render a completely different page.
        assert resp.status_code == 200
        assert "chat-search-results" in resp.text


# ---------------------------------------------------------------------------
# P2.4: audit log keys are namespaced "chat.*" consistently.
# ---------------------------------------------------------------------------


class TestAuditKeys:
    def test_fork_audit_key_is_chat_namespaced(self, provider_patch, mock_conn):
        conv = _make_conversation()
        new_id = uuid.UUID("77777777-7777-7777-7777-777777777777")
        with (
            patch("app.chat.handlers.get_conversation", return_value=conv),
            patch("app.chat.handlers._fork_conversation", return_value=new_id),
            patch("app.chat.handlers.log_action") as mock_log,
        ):
            client = _authed_client()
            resp = client.post(
                f"/chat/{_CONV_ID}/fork",
                data={"message_id": str(_MSG_ID)},
                headers={"HX-Request": "true"},
            )
            assert resp.status_code == 204
            mock_log.assert_called_once()
            action_key = mock_log.call_args.args[1]
            assert action_key == "chat.conversation.fork"

    def test_feedback_post_audit_key_is_chat_namespaced(self, provider_patch, mock_conn):
        conv = _make_conversation()
        msg = _make_message(role="assistant")
        with (
            patch("app.chat.handlers.get_conversation", return_value=conv),
            patch("app.chat.handlers.list_messages", return_value=[msg]),
            patch("app.chat.handlers._upsert_feedback"),
            patch("app.chat.handlers._feedback_counts", return_value=(1, 0)),
            patch("app.chat.handlers.log_action") as mock_log,
        ):
            client = _authed_client()
            resp = client.post(
                f"/chat/{_CONV_ID}/messages/{_MSG_ID}/feedback",
                data={"rating": "1"},
            )
            assert resp.status_code == 200
            assert mock_log.call_args.args[1] == "chat.message.feedback"

    def test_feedback_delete_audit_key_is_chat_namespaced(self, provider_patch, mock_conn):
        conv = _make_conversation()
        msg = _make_message(role="assistant")
        with (
            patch("app.chat.handlers.get_conversation", return_value=conv),
            patch("app.chat.handlers.list_messages", return_value=[msg]),
            patch("app.chat.handlers._delete_feedback_row"),
            patch("app.chat.handlers._feedback_counts", return_value=(0, 0)),
            patch("app.chat.handlers.log_action") as mock_log,
        ):
            client = _authed_client()
            resp = client.delete(f"/chat/{_CONV_ID}/messages/{_MSG_ID}/feedback")
            assert resp.status_code == 200
            assert mock_log.call_args.args[1] == "chat.message.feedback.delete"


# ---------------------------------------------------------------------------
# P2.5: _update_message_content fail-safe in production.
# ---------------------------------------------------------------------------


class TestUpdateMessageContentFailSafe:
    def test_dev_falls_back_to_plaintext_on_encryption_failure(self, provider_patch, monkeypatch):
        """In dev (``APP_ENV != production``) encrypt errors write plaintext."""
        from app.chat.handlers import _update_message_content

        monkeypatch.setenv("APP_ENV", "development")

        conn = MagicMock()

        def boom(_text: str) -> bytes:
            raise RuntimeError("no key")

        # Patch the import site used inside _update_message_content.
        with patch("app.storage.encrypt_text", side_effect=boom):
            _update_message_content(conn, _MSG_ID, "uus sisu")

        # Plaintext-path UPDATE landed.
        sql, params = conn.execute.call_args.args
        assert "content = %s" in sql
        assert "content_encrypted = NULL" in sql
        assert params == ("uus sisu", str(_MSG_ID))

    def test_prod_reraises_on_encryption_failure(self, provider_patch, monkeypatch):
        """In production encrypt errors must NOT fall back to plaintext."""
        from app.chat.handlers import _update_message_content

        monkeypatch.setenv("APP_ENV", "production")

        conn = MagicMock()

        def boom(_text: str) -> bytes:
            raise RuntimeError("no key")

        with patch("app.storage.encrypt_text", side_effect=boom):
            with pytest.raises(RuntimeError, match="no key"):
                _update_message_content(conn, _MSG_ID, "tundlik sisu")

        # No UPDATE was executed — the write never touched the DB.
        conn.execute.assert_not_called()


# ---------------------------------------------------------------------------
# P2.6: Content-Disposition header is not double-slugified.
# ---------------------------------------------------------------------------


class TestContentDisposition:
    def test_disposition_preserves_extension_dot(self):
        from app.chat.handlers import _content_disposition

        header = _content_disposition("vestlus-test-2026-04-14.md")
        # Ascii part keeps the dot so the browser saves it as ``*.md``.
        assert 'filename="vestlus-test-2026-04-14.md"' in header
        # RFC 5987 variant is percent-encoded.
        assert "filename*=UTF-8''vestlus-test-2026-04-14.md" in header

    def test_disposition_preserves_docx_extension(self):
        from app.chat.handlers import _content_disposition

        header = _content_disposition("vestlus-pealkiri-2026-04-14.docx")
        assert 'filename="vestlus-pealkiri-2026-04-14.docx"' in header

    def test_export_md_disposition_has_md_extension(self, provider_patch, mock_conn):
        conv = _make_conversation(title="Test vestlus")
        msg = _make_message(role="user", content="Tere")
        with (
            patch("app.chat.handlers.get_conversation", return_value=conv),
            patch("app.chat.handlers.list_messages", return_value=[msg]),
        ):
            client = _authed_client()
            resp = client.get(f"/chat/{_CONV_ID}/export.md")
            assert resp.status_code == 200
            disposition = resp.headers.get("content-disposition", "")
            # Dot must be present — no "-md" at the end.
            assert ".md" in disposition
            assert '-md"' not in disposition
