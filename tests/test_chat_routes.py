"""Integration tests for the Phase 3B AI Advisory Chat routes.

Tests exercise the full ``app.main.app`` via ``TestClient`` so
they validate the FastHTML wiring, the auth Beforeware, and the HTMX
partial swap behaviour. External dependencies -- Postgres, LLM -- are
mocked out.

Patterns follow ``tests/test_drafter_routes.py`` and
``tests/test_docs_routes.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

from app.chat.models import Conversation, Message

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ORG_ID = "11111111-1111-1111-1111-111111111111"
_OTHER_ORG_ID = "22222222-2222-2222-2222-222222222222"
_USER_ID = "33333333-3333-3333-3333-333333333333"
_CONV_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
_DRAFT_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")


def _authed_user() -> dict[str, Any]:
    return {
        "id": _USER_ID,
        "email": "kasutaja@seadusloome.ee",
        "full_name": "Test Kasutaja",
        "role": "drafter",
        "org_id": _ORG_ID,
    }


def _make_conversation(
    *,
    conv_id: uuid.UUID = _CONV_ID,
    org_id: str = _ORG_ID,
    user_id: str = _USER_ID,
    context_draft_id: uuid.UUID | None = None,
    title: str = "Test vestlus",
) -> Conversation:
    now = datetime.now(UTC)
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
    role: str = "user",
    content: str = "Tere",
    conv_id: uuid.UUID = _CONV_ID,
) -> Message:
    now = datetime.now(UTC)
    return Message(
        id=uuid.uuid4(),
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


def _stub_provider() -> MagicMock:
    """Build a provider whose ``get_current_user`` returns ``_authed_user``."""
    provider = MagicMock()
    provider.get_current_user.return_value = _authed_user()
    return provider


def _authed_client():
    """Return a TestClient with a valid ``access_token`` cookie."""
    from starlette.testclient import TestClient

    client = TestClient(
        __import__("app.main", fromlist=["app"]).app,
        follow_redirects=False,
    )
    client.cookies.set("access_token", "stub-token")
    return client


# ---------------------------------------------------------------------------
# Unauthenticated requests redirect to login
# ---------------------------------------------------------------------------


class TestAuthRequired:
    def test_chat_list_redirects_unauthenticated(self):
        from starlette.testclient import TestClient

        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.get("/chat")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"

    def test_chat_new_redirects_unauthenticated(self):
        from starlette.testclient import TestClient

        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.get("/chat/new")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"

    def test_chat_view_redirects_unauthenticated(self):
        from starlette.testclient import TestClient

        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.get(f"/chat/{_CONV_ID}")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"

    def test_chat_delete_redirects_unauthenticated(self):
        from starlette.testclient import TestClient

        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.post(f"/chat/{_CONV_ID}/delete")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"


# ---------------------------------------------------------------------------
# GET /chat -- conversation list
# ---------------------------------------------------------------------------


class TestChatList:
    @patch("app.chat.routes._count_conversations_for_user", return_value=0)
    @patch("app.chat.routes.list_conversations_for_user", return_value=[])
    @patch("app.auth.middleware._get_provider")
    def test_empty_list_shows_empty_state(self, mock_provider, mock_list, mock_count):
        mock_provider.return_value = _stub_provider()
        client = _authed_client()
        resp = client.get("/chat")
        assert resp.status_code == 200
        # Check for the empty state text
        assert "Vestlusi pole" in resp.text

    @patch("app.chat.routes._get_last_message_at", return_value=None)
    @patch("app.chat.routes._get_message_count", return_value=3)
    @patch("app.chat.routes._count_conversations_for_user", return_value=1)
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_list_with_conversations(
        self, mock_provider, mock_connect, mock_count, mock_msg_count, mock_last_msg
    ):
        mock_provider.return_value = _stub_provider()
        conv = _make_conversation()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        # list_conversations_for_user is called via the conn
        with patch("app.chat.routes.list_conversations_for_user", return_value=[conv]):
            client = _authed_client()
            resp = client.get("/chat")
            assert resp.status_code == 200
            assert "Test vestlus" in resp.text


# ---------------------------------------------------------------------------
# GET /chat/new -- create new conversation
# ---------------------------------------------------------------------------


class TestChatNew:
    @patch("app.chat.routes.log_chat_conversation_create")
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_creates_conversation_and_redirects(self, mock_provider, mock_connect, mock_audit):
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        conv = _make_conversation()
        with patch("app.chat.routes.create_conversation", return_value=conv):
            client = _authed_client()
            resp = client.get("/chat/new")
            assert resp.status_code == 303
            assert f"/chat/{conv.id}" in resp.headers["location"]

        mock_audit.assert_called_once()

    @patch("app.chat.routes.log_chat_conversation_create")
    @patch("app.chat.routes._get_draft_title", return_value="Eelnou X")
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_creates_with_draft_context(
        self, mock_provider, mock_connect, mock_draft_title, mock_audit
    ):
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        conv = _make_conversation(context_draft_id=_DRAFT_ID)
        with patch("app.chat.routes.create_conversation", return_value=conv) as mock_create:
            client = _authed_client()
            resp = client.get(f"/chat/new?draft={_DRAFT_ID}")
            assert resp.status_code == 303

            # Verify draft_id was passed to create_conversation
            call_args = mock_create.call_args
            assert call_args.kwargs.get("context_draft_id") == _DRAFT_ID


# ---------------------------------------------------------------------------
# GET /chat/{conv_id} -- conversation view
# ---------------------------------------------------------------------------


class TestChatView:
    @patch("app.chat.routes.list_messages", return_value=[])
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_renders_conversation_page(self, mock_provider, mock_connect, mock_list_msgs):
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        conv = _make_conversation()
        with patch("app.chat.routes.get_conversation", return_value=conv):
            client = _authed_client()
            resp = client.get(f"/chat/{_CONV_ID}")
            assert resp.status_code == 200
            assert "Test vestlus" in resp.text
            assert "chat-container" in resp.text

    @patch("app.chat.routes.list_messages")
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_renders_message_history(self, mock_provider, mock_connect, mock_list_msgs):
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        conv = _make_conversation()
        user_msg = _make_message("user", "Mis on TsiviilS?")
        asst_msg = _make_message("assistant", "TsiviilS on tsiviilseadustik.")
        mock_list_msgs.return_value = [user_msg, asst_msg]

        with patch("app.chat.routes.get_conversation", return_value=conv):
            client = _authed_client()
            resp = client.get(f"/chat/{_CONV_ID}")
            assert resp.status_code == 200
            assert "Mis on TsiviilS" in resp.text
            assert "tsiviilseadustik" in resp.text

    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_non_owner_returns_not_found(self, mock_provider, mock_connect):
        """Issue #569: chat conversations are owner-only. Even a same-org
        colleague must not be able to read another user's chat."""
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        # Conversation belongs to a different user in the same org.
        other_user = "99999999-9999-9999-9999-999999999999"
        conv = _make_conversation(user_id=other_user)
        with patch("app.chat.routes.get_conversation", return_value=conv):
            client = _authed_client()
            resp = client.get(f"/chat/{_CONV_ID}")
            assert resp.status_code == 200
            assert "ei leitud" in resp.text.lower() or "puudub" in resp.text.lower()

    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_cross_org_returns_not_found(self, mock_provider, mock_connect):
        """A user from a different org should also be denied (the
        common case and what the org-scoped check used to catch)."""
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        other_user = "99999999-9999-9999-9999-999999999999"
        conv = _make_conversation(org_id=_OTHER_ORG_ID, user_id=other_user)
        with patch("app.chat.routes.get_conversation", return_value=conv):
            client = _authed_client()
            resp = client.get(f"/chat/{_CONV_ID}")
            assert resp.status_code == 200
            assert "ei leitud" in resp.text.lower() or "puudub" in resp.text.lower()

    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_invalid_uuid_returns_not_found(self, mock_provider, mock_connect):
        mock_provider.return_value = _stub_provider()
        client = _authed_client()
        resp = client.get("/chat/not-a-uuid")
        assert resp.status_code == 200
        assert "ei leitud" in resp.text.lower()

    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_missing_conversation_returns_not_found(self, mock_provider, mock_connect):
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        with patch("app.chat.routes.get_conversation", return_value=None):
            client = _authed_client()
            resp = client.get(f"/chat/{_CONV_ID}")
            assert resp.status_code == 200
            assert "ei leitud" in resp.text.lower()

    @patch("app.chat.routes._get_draft_title", return_value="TestEelnou")
    @patch("app.chat.routes.list_messages", return_value=[])
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_draft_context_shown_in_view(
        self, mock_provider, mock_connect, mock_list_msgs, mock_draft_title
    ):
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        conv = _make_conversation(context_draft_id=_DRAFT_ID)
        with patch("app.chat.routes.get_conversation", return_value=conv):
            client = _authed_client()
            resp = client.get(f"/chat/{_CONV_ID}")
            assert resp.status_code == 200
            assert "Seotud eelnouga" in resp.text


# ---------------------------------------------------------------------------
# POST /chat/{conv_id}/delete
# ---------------------------------------------------------------------------


class TestChatDelete:
    @patch("app.chat.routes.log_chat_conversation_delete")
    @patch("app.chat.routes.delete_conversation")
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_delete_with_hx_redirect(self, mock_provider, mock_connect, mock_delete, mock_audit):
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        conv = _make_conversation()
        with patch("app.chat.routes.get_conversation", return_value=conv):
            client = _authed_client()
            resp = client.post(
                f"/chat/{_CONV_ID}/delete",
                headers={"HX-Request": "true"},
            )
            assert resp.status_code == 204
            assert resp.headers.get("HX-Redirect") == "/chat"

        mock_delete.assert_called_once()
        mock_audit.assert_called_once()

    @patch("app.chat.routes.log_chat_conversation_delete")
    @patch("app.chat.routes.delete_conversation")
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_delete_without_htmx_redirects_303(
        self, mock_provider, mock_connect, mock_delete, mock_audit
    ):
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        conv = _make_conversation()
        with patch("app.chat.routes.get_conversation", return_value=conv):
            client = _authed_client()
            resp = client.post(f"/chat/{_CONV_ID}/delete")
            assert resp.status_code == 303
            assert resp.headers["location"] == "/chat"

    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_delete_non_owner_returns_not_found(self, mock_provider, mock_connect):
        """Issue #569: only the owner can delete their conversation."""
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        other_user = "99999999-9999-9999-9999-999999999999"
        conv = _make_conversation(user_id=other_user)
        with patch("app.chat.routes.get_conversation", return_value=conv):
            client = _authed_client()
            resp = client.post(f"/chat/{_CONV_ID}/delete")
            assert resp.status_code == 200
            assert "ei leitud" in resp.text.lower()

    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_delete_cross_org_returns_not_found(self, mock_provider, mock_connect):
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        other_user = "99999999-9999-9999-9999-999999999999"
        conv = _make_conversation(org_id=_OTHER_ORG_ID, user_id=other_user)
        with patch("app.chat.routes.get_conversation", return_value=conv):
            client = _authed_client()
            resp = client.post(f"/chat/{_CONV_ID}/delete")
            assert resp.status_code == 200
            assert "ei leitud" in resp.text.lower()


# ---------------------------------------------------------------------------
# Regression: cross-org draft context leak (#562)
# ---------------------------------------------------------------------------


class TestCrossOrgDraftContextLeak:
    """Issue #562: new_conversation must reject drafts from other orgs."""

    @patch("app.chat.routes.log_chat_conversation_create")
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_new_conversation_rejects_foreign_draft(self, mock_provider, mock_connect, mock_audit):
        """A user in org A cannot start a conversation with org B's draft UUID."""
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        # _get_draft_title queries the DB with org_id filter; return None
        # to simulate a draft belonging to a different org.
        conn.execute.return_value.fetchone.return_value = None

        conv = _make_conversation()  # conversation that will be created (without draft)
        with patch("app.chat.routes.create_conversation", return_value=conv) as mock_create:
            client = _authed_client()
            resp = client.get(f"/chat/new?draft={_DRAFT_ID}")
            assert resp.status_code == 303

            # The conversation should have been created without draft context
            call_kwargs = mock_create.call_args.kwargs
            assert call_kwargs.get("context_draft_id") is None

    @patch("app.chat.routes.log_chat_conversation_create")
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_new_conversation_accepts_own_org_draft(self, mock_provider, mock_connect, mock_audit):
        """A user in org A can start a conversation with their own org's draft."""
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        # Simulate _get_draft_title finding the draft (org_id matches)
        conn.execute.return_value.fetchone.return_value = ("Meie eelnou",)

        conv = _make_conversation(context_draft_id=_DRAFT_ID)
        with patch("app.chat.routes.create_conversation", return_value=conv) as mock_create:
            client = _authed_client()
            resp = client.get(f"/chat/new?draft={_DRAFT_ID}")
            assert resp.status_code == 303

            # Draft context should have been passed through
            call_kwargs = mock_create.call_args.kwargs
            assert call_kwargs.get("context_draft_id") == _DRAFT_ID

    def test_get_draft_title_returns_none_for_foreign_org(self):
        """_get_draft_title with a different org_id returns None."""
        from app.chat.routes import _get_draft_title

        with patch("app.chat.routes._connect") as mock_connect:
            conn = MagicMock()
            mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
            mock_connect.return_value.__exit__ = MagicMock(return_value=False)

            # The query with org_id filter returns no rows
            conn.execute.return_value.fetchone.return_value = None

            result = _get_draft_title(str(_DRAFT_ID), _OTHER_ORG_ID)
            assert result is None

            # Verify the query included org_id
            call_args = conn.execute.call_args[0]
            assert "org_id" in call_args[0]
            assert call_args[1] == (str(_DRAFT_ID), _OTHER_ORG_ID)

    def test_get_draft_title_returns_title_for_own_org(self):
        """_get_draft_title with the correct org_id returns the filename."""
        from app.chat.routes import _get_draft_title

        with patch("app.chat.routes._connect") as mock_connect:
            conn = MagicMock()
            mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
            mock_connect.return_value.__exit__ = MagicMock(return_value=False)

            conn.execute.return_value.fetchone.return_value = ("Eelnou XYZ",)

            result = _get_draft_title(str(_DRAFT_ID), _ORG_ID)
            assert result == "Eelnou XYZ"
