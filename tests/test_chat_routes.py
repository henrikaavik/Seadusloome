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
    rag_context: list[dict] | None = None,
    is_pinned: bool = False,
    is_truncated: bool = False,
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
        rag_context=rag_context,
        tokens_input=None,
        tokens_output=None,
        model=None,
        created_at=now,
        is_pinned=is_pinned,
        is_truncated=is_truncated,
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


class TestChatListStateFromRequest:
    """Helper that extracts (page, search_q, include_archived) from a
    request — checks query params first, then falls back to
    HX-Current-URL for HTMX-driven row mutations whose POST body lacks
    the chat-list filter state.
    """

    def _make_req(self, *, query: str = "", current_url: str | None = None):
        from starlette.requests import Request

        # Build a minimal ASGI scope with query string + headers.
        headers: list[tuple[bytes, bytes]] = []
        if current_url is not None:
            headers.append((b"hx-current-url", current_url.encode("latin-1")))
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/chat/abc/archive",
            "query_string": query.encode("latin-1"),
            "headers": headers,
        }
        return Request(scope)

    def test_reads_state_from_query_string(self):
        from app.chat.routes import _chat_list_state_from_request

        req = self._make_req(query="page=3&q=foo&archived=1")
        page, q, archived = _chat_list_state_from_request(req)
        assert page == 3
        assert q == "foo"
        assert archived is True

    def test_falls_back_to_hx_current_url(self):
        """When the request has no chat-list query params (e.g. an
        archive POST from a button), parse the page state out of the
        HX-Current-URL header HTMX always sends."""
        from app.chat.routes import _chat_list_state_from_request

        req = self._make_req(
            query="",
            current_url="http://localhost/chat?page=2&archived=1",
        )
        page, q, archived = _chat_list_state_from_request(req)
        assert page == 2
        assert q == ""
        assert archived is True

    def test_defaults_when_neither_source_provides_state(self):
        from app.chat.routes import _chat_list_state_from_request

        req = self._make_req(query="", current_url=None)
        page, q, archived = _chat_list_state_from_request(req)
        assert page == 1
        assert q == ""
        assert archived is False

    def test_invalid_page_falls_back_to_one(self):
        from app.chat.routes import _chat_list_state_from_request

        req = self._make_req(query="page=not-a-number")
        page, _q, _a = _chat_list_state_from_request(req)
        assert page == 1


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
        with patch("app.chat.routes.create_conversation", return_value=conv) as mock_create:
            client = _authed_client()
            resp = client.get("/chat/new")
            assert resp.status_code == 303
            assert f"/chat/{conv.id}" in resp.headers["location"]

            # #714: the generated title uses the "Nõustaja" framing, not "Vestlus".
            generated_title = mock_create.call_args.kwargs.get("title", "")
            assert generated_title.startswith("Nõustamine")
            assert "Vestlus" not in generated_title

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
            # #714: title is "Nõustamine — <draft>", not "Vestlus — <draft>".
            assert call_args.kwargs.get("title") == "Nõustamine — Eelnou X"


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
            # #739: the not-found page now answers an explicit 404.
            assert resp.status_code == 404
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
            assert resp.status_code == 404
            assert "ei leitud" in resp.text.lower() or "puudub" in resp.text.lower()

    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_invalid_uuid_returns_not_found(self, mock_provider, mock_connect):
        mock_provider.return_value = _stub_provider()
        client = _authed_client()
        resp = client.get("/chat/not-a-uuid")
        assert resp.status_code == 404
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
            assert resp.status_code == 404
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

    @patch("app.chat.routes.log_chat_conversation_delete")
    @patch("app.chat.routes.delete_conversation")
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_delete_from_list_returns_chat_list_body_fragment(
        self, mock_provider, mock_connect, mock_delete, mock_audit
    ):
        """Bug #663 (post-review fix): the ``/chat`` list delete form
        posts ``from_list=1``; the response is the refreshed
        ``#chat-list-body`` fragment with HX-Reswap+HX-Retarget so the
        row vanishes AND pagination counts update in place — without
        the scroll-loss the previous HX-Refresh caused.
        """
        from fastcore.xml import Div

        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        fake_fragment = Div("stubbed-list-body", id="chat-list-body")
        conv = _make_conversation()
        with (
            patch("app.chat.routes.get_conversation", return_value=conv),
            patch(
                "app.chat.routes._render_chat_list_body",
                return_value=fake_fragment,
            ),
        ):
            client = _authed_client()
            resp = client.post(
                f"/chat/{_CONV_ID}/delete",
                headers={"HX-Request": "true", "HX-Current-URL": "/chat"},
                data={"from_list": "1"},
            )
            assert resp.status_code == 200
            assert "stubbed-list-body" in resp.text
            assert 'id="chat-list-body"' in resp.text
            assert resp.headers.get("HX-Reswap") == "outerHTML"
            assert resp.headers.get("HX-Retarget") == "#chat-list-body"
            # No more HX-Refresh / HX-Redirect on the from_list path.
            assert "HX-Refresh" not in resp.headers
            assert "HX-Redirect" not in resp.headers
        mock_delete.assert_called_once()

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
            # #739: the not-found page now answers an explicit 404.
            assert resp.status_code == 404
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
            assert resp.status_code == 404
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


# ---------------------------------------------------------------------------
# Vestlus UX polish (migration 017): header meta, sources, actions,
# empty state, draft prompts, list search + pin/archive actions.
# ---------------------------------------------------------------------------


class TestConversationViewPolish:
    """Assert the structural UI affordances added by the polish sweep."""

    def _setup(self, mock_connect):
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        return conn

    @patch("app.chat.routes.list_messages", return_value=[])
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_header_meta_present_on_view(self, mock_provider, mock_connect, _mock_list):
        mock_provider.return_value = _stub_provider()
        self._setup(mock_connect)
        conv = _make_conversation()
        with patch("app.chat.routes.get_conversation", return_value=conv):
            client = _authed_client()
            resp = client.get(f"/chat/{_CONV_ID}")
            assert resp.status_code == 200
            assert 'id="chat-status"' in resp.text
            assert 'id="chat-quota"' in resp.text
            assert "chat-header-meta" in resp.text
            # Quota pill must start as aria-busy so screen readers do not
            # announce the placeholder "0" before the first /api/me/usage
            # response paints real values (chat.js clears these afterwards).
            assert 'aria-busy="true"' in resp.text
            assert 'data-initial="true"' in resp.text
            # Vendor + chat.js wiring
            assert "/static/js/chat.js" in resp.text
            # Exact patch-pinned versions — updating them requires refreshing
            # the SRI hashes below.
            assert "marked@12.0.2" in resp.text
            assert "dompurify@3.1.6" in resp.text
            # Subresource Integrity hashes for both CDN scripts. Computed via
            #   curl -sL <url> | openssl dgst -sha384 -binary | openssl base64 -A
            assert (
                "sha384-/TQbtLCAerC3jgaim+N78RZSDYV7ryeoBCVqTuzRrFec2akfBkHS7ACQ3PQhvMVi"
                in resp.text
            )
            assert (
                "sha384-+VfUPEb0PdtChMwmBcBmykRMDd+v6D/oFmB3rZM/puCMDYcIvF968OimRh4KQY9a"
                in resp.text
            )
            assert 'crossorigin="anonymous"' in resp.text

    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_sources_panel_rendered_for_rag_context(self, mock_provider, mock_connect):
        mock_provider.return_value = _stub_provider()
        self._setup(mock_connect)
        conv = _make_conversation()
        rag = [
            {
                "source_uri": "https://example.org/laws/KarS",
                "content": "Karistusseadustiku § 113 kaitseb inimese elu...",
                "score": 0.9,
            },
            {
                "source_uri": "https://example.org/laws/PS",
                "content": "Pohiseaduse § 13 kohaselt on igaul oigus...",
                "score": 0.8,
            },
        ]
        msg = _make_message("assistant", "Vastus", rag_context=rag)
        with (
            patch("app.chat.routes.get_conversation", return_value=conv),
            patch("app.chat.routes.list_messages", return_value=[msg]),
        ):
            client = _authed_client()
            resp = client.get(f"/chat/{_CONV_ID}")
            assert resp.status_code == 200
            assert "chat-sources" in resp.text
            assert "Allikad (2)" in resp.text
            assert "KarS" in resp.text
            assert "PS" in resp.text

    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_rag_sources_carry_oiguskaart_deeplink(self, mock_provider, mock_connect):
        """#759: each cited source with a URI gets a "vaata kaardil →"
        affordance pointing at ``/explorer?focus=<urlencoded-uri>``."""
        mock_provider.return_value = _stub_provider()
        self._setup(mock_connect)
        conv = _make_conversation()
        provision_uri = "https://data.riik.ee/ontology/estleg#KarS_par_113"
        rag = [
            {
                "source_uri": provision_uri,
                "content": "Karistusseadustiku § 113 kaitseb inimese elu...",
                "score": 0.9,
            },
        ]
        msg = _make_message("assistant", "Vastus", rag_context=rag)
        with (
            patch("app.chat.routes.get_conversation", return_value=conv),
            patch("app.chat.routes.list_messages", return_value=[msg]),
        ):
            client = _authed_client()
            resp = client.get(f"/chat/{_CONV_ID}")
            assert resp.status_code == 200
            assert "vaata kaardil" in resp.text
            assert "chat-source-map-link" in resp.text
            assert "/explorer?focus=" in resp.text
            # The estleg ``#`` must be percent-encoded so ``focus`` is not
            # truncated to ".../estleg" by the browser fragment parser.
            assert "%23KarS_par_113" in resp.text

    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_rag_sources_without_uri_have_no_map_link(self, mock_provider, mock_connect):
        """#759: a source chunk with no ``source_uri`` renders as plain
        text — no "vaata kaardil" affordance to point nowhere."""
        mock_provider.return_value = _stub_provider()
        self._setup(mock_connect)
        conv = _make_conversation()
        rag = [
            {"source_uri": "", "content": "Mingi taustateave ilma allika URI-ta.", "score": 0.5},
        ]
        msg = _make_message("assistant", "Vastus", rag_context=rag)
        with (
            patch("app.chat.routes.get_conversation", return_value=conv),
            patch("app.chat.routes.list_messages", return_value=[msg]),
        ):
            client = _authed_client()
            resp = client.get(f"/chat/{_CONV_ID}")
            assert resp.status_code == 200
            assert "chat-sources" in resp.text
            assert "chat-source-map-link" not in resp.text

    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_action_row_present_on_assistant_messages(self, mock_provider, mock_connect):
        mock_provider.return_value = _stub_provider()
        self._setup(mock_connect)
        conv = _make_conversation()
        msg = _make_message("assistant", "Vastus")
        with (
            patch("app.chat.routes.get_conversation", return_value=conv),
            patch("app.chat.routes.list_messages", return_value=[msg]),
        ):
            client = _authed_client()
            resp = client.get(f"/chat/{_CONV_ID}")
            assert resp.status_code == 200
            assert "chat-message-actions" in resp.text
            assert 'data-action="regenerate"' in resp.text
            assert 'data-action="copy"' in resp.text
            assert 'data-action="feedback-up"' in resp.text
            assert 'data-action="feedback-down"' in resp.text

    @patch("app.chat.routes.list_messages", return_value=[])
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_empty_state_with_five_default_prompts(self, mock_provider, mock_connect, _mock_list):
        mock_provider.return_value = _stub_provider()
        self._setup(mock_connect)
        conv = _make_conversation()
        with patch("app.chat.routes.get_conversation", return_value=conv):
            client = _authed_client()
            resp = client.get(f"/chat/{_CONV_ID}")
            assert resp.status_code == 200
            assert 'id="chat-empty-state"' in resp.text
            # Five default prompts (each button carries the chat-example-prompt class)
            assert resp.text.count("chat-example-prompt-label") == 5
            # A distinctive phrase from the default prompts — asserted in
            # its full accented form so an accidental ASCII-fallback would
            # surface immediately.
            assert "Isikuandmete töötlemine" in resp.text

    @patch("app.chat.routes.list_messages", return_value=[])
    @patch("app.chat.routes._get_draft_title", return_value="Eelnou X")
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_draft_specific_prompts_when_context_draft_set(
        self, mock_provider, mock_connect, _mock_title, _mock_list
    ):
        mock_provider.return_value = _stub_provider()
        self._setup(mock_connect)
        conv = _make_conversation(context_draft_id=_DRAFT_ID)
        with patch("app.chat.routes.get_conversation", return_value=conv):
            client = _authed_client()
            resp = client.get(f"/chat/{_CONV_ID}")
            assert resp.status_code == 200
            # Draft-specific wording — asserted with the original Estonian
            # characters so an accidental unaccented rewrite is caught.
            assert "Võrdle kehtiva õigusega" in resp.text
            assert "Sarnased eelnõud" in resp.text
            # The "Vaata mõju" link from the draft header
            assert "Vaata mõju" in resp.text

    @patch("app.chat.routes.list_messages")
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_truncated_note_appended_to_assistant_message(
        self, mock_provider, mock_connect, mock_list
    ):
        mock_provider.return_value = _stub_provider()
        self._setup(mock_connect)
        conv = _make_conversation()
        msg = _make_message("assistant", "Pooleli vastus", is_truncated=True)
        mock_list.return_value = [msg]
        with patch("app.chat.routes.get_conversation", return_value=conv):
            client = _authed_client()
            resp = client.get(f"/chat/{_CONV_ID}")
            assert resp.status_code == 200
            # The truncated-response note is rendered as the full Estonian
            # phrase, preceded by a U+2014 em-dash and wrapped in the
            # ``chat-message-truncated`` em tag. Assert both halves so an
            # accidental ASCII hyphen or missing class would surface here.
            assert "\u2014" in resp.text  # em-dash separator
            assert '<em class="chat-message-truncated">vastus katkestati</em>' in resp.text


class TestChatListPolish:
    """Search input + pin/archive/rename forms on the list view."""

    @patch("app.chat.routes._count_conversations_for_user", return_value=1)
    @patch("app.chat.routes._get_last_message_at", return_value=None)
    @patch("app.chat.routes._get_message_count", return_value=0)
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_search_input_and_actions_present(
        self,
        mock_provider,
        mock_connect,
        _mock_msg_count,
        _mock_last,
        _mock_total,
    ):
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        conv = _make_conversation()
        with patch("app.chat.routes.list_conversations_for_user", return_value=[conv]):
            client = _authed_client()
            resp = client.get("/chat")
            assert resp.status_code == 200
            # Search input present
            assert 'name="q"' in resp.text
            assert "Otsi vestlusi" in resp.text
            # Archived toggle
            assert 'name="archived"' in resp.text
            # Action forms targeting the pin/archive/rename handlers
            assert f"/chat/{_CONV_ID}/pin" in resp.text
            assert f"/chat/{_CONV_ID}/archive" in resp.text
            assert f"/chat/{_CONV_ID}/rename" in resp.text

    @patch("app.chat.routes._count_conversations_for_user", return_value=1)
    @patch("app.chat.routes._get_last_message_at", return_value=None)
    @patch("app.chat.routes._get_message_count", return_value=0)
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_search_query_passed_through(
        self,
        mock_provider,
        mock_connect,
        _mock_msg_count,
        _mock_last,
        _mock_total,
    ):
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        with patch("app.chat.routes.list_conversations_for_user", return_value=[]) as mock_list:
            client = _authed_client()
            resp = client.get("/chat?q=karistus")
            assert resp.status_code == 200
            # The search term was forwarded to the models helper as a kwarg.
            kwargs = mock_list.call_args.kwargs
            assert kwargs.get("search") == "karistus"


class TestChatRouteOrdering:
    """Route-registration order regression tests.

    Starlette matches routes in the order they are added. ``/chat/search``
    and similar static paths MUST be registered before ``/chat/{conv_id}``
    so the dispatcher resolves them as handlers rather than capturing
    ``search`` as a UUID (which would 404 because it is not a valid UUID).
    """

    @patch("app.chat.handlers._connect")
    @patch("app.auth.middleware._get_provider")
    def test_search_path_is_not_captured_as_conv_id(self, mock_provider, mock_connect):
        """``GET /chat/search?q=foo`` must reach the search handler.

        A non-HTMX browser request returns a 303 redirect to
        ``/chat?q=foo``; the important assertion is that the response is NOT
        a 404 (which would mean the ``/chat/{conv_id}`` catch-all captured
        the path and rejected "search" as an invalid UUID).
        """
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        client = _authed_client()
        resp = client.get("/chat/search?q=foo")
        # Must not be 404 (the catch-all would reject "search" as !UUID).
        assert resp.status_code != 404
        # And should be a 2xx or the documented 303 redirect for non-HTMX.
        assert resp.status_code in (200, 303)

    @patch("app.chat.handlers._search_conversations_impl", return_value=[])
    @patch("app.chat.handlers._connect")
    @patch("app.auth.middleware._get_provider")
    def test_search_path_htmx_returns_fragment(self, mock_provider, mock_connect, _mock_search):
        """HTMX request hits the search handler directly (no redirect)."""
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        client = _authed_client()
        resp = client.get("/chat/search?q=foo", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert "chat-search-results" in resp.text


# ---------------------------------------------------------------------------
# #724 — POST /chat/seed (stash a single-use chat-seed token)
# ---------------------------------------------------------------------------

_SEED_TOKEN = uuid.UUID("77777777-7777-7777-7777-777777777777")


class TestChatSeedPost:
    def test_unauthenticated_redirects_to_login(self):
        from starlette.testclient import TestClient

        from app.main import app

        client = TestClient(app, follow_redirects=False)
        resp = client.post("/chat/seed", data={"seed_text": "Selgita seda leidu"})
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"

    @patch("app.chat.routes.create_pending_seed", return_value=str(_SEED_TOKEN))
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_seed_text_redirects_to_chat_new_with_token(
        self, mock_provider, mock_connect, mock_create
    ):
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        client = _authed_client()
        resp = client.post("/chat/seed", data={"seed_text": "Selgita seda mõjuanalüüsi leidu"})
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/chat/new?seed={_SEED_TOKEN}"
        # create_pending_seed was called with the seed text + the user's org.
        kwargs = mock_create.call_args.kwargs
        assert kwargs["seed_text"] == "Selgita seda mõjuanalüüsi leidu"
        assert kwargs["org_id"] == _ORG_ID
        assert kwargs["draft_id"] is None

    @patch("app.chat.routes.create_pending_seed", return_value=str(_SEED_TOKEN))
    @patch("app.chat.routes._get_draft_title", return_value="Minu eelnõu")
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_seed_with_valid_draft_id_threads_it_through(
        self, mock_provider, mock_connect, mock_draft_title, mock_create
    ):
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        client = _authed_client()
        resp = client.post(
            "/chat/seed",
            data={"seed_text": "Selgita seda leidu", "draft_id": str(_DRAFT_ID)},
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/chat/new?seed={_SEED_TOKEN}"
        assert mock_create.call_args.kwargs["draft_id"] == _DRAFT_ID

    @patch("app.chat.routes.create_pending_seed", return_value=str(_SEED_TOKEN))
    @patch("app.chat.routes._get_draft_title", return_value=None)
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_seed_with_unvisible_draft_id_is_dropped(
        self, mock_provider, mock_connect, mock_draft_title, mock_create
    ):
        """A draft the caller can't see is dropped — the seed is still stashed."""
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        client = _authed_client()
        resp = client.post(
            "/chat/seed",
            data={"seed_text": "Selgita seda leidu", "draft_id": str(_DRAFT_ID)},
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/chat/new?seed={_SEED_TOKEN}"
        # The draft was dropped because _get_draft_title returned None.
        assert mock_create.call_args.kwargs["draft_id"] is None

    @patch("app.chat.routes.create_pending_seed")
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_blank_seed_text_redirects_to_chat_new_without_token(
        self, mock_provider, mock_connect, mock_create
    ):
        mock_provider.return_value = _stub_provider()
        client = _authed_client()
        resp = client.post("/chat/seed", data={"seed_text": "   "})
        assert resp.status_code == 303
        assert resp.headers["location"] == "/chat/new"
        # No seed was stashed.
        mock_create.assert_not_called()

    @patch("app.chat.routes.create_pending_seed", return_value=None)
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_failed_stash_falls_back_to_plain_chat_new(
        self, mock_provider, mock_connect, mock_create
    ):
        """When create_pending_seed returns None we redirect to a plain /chat/new."""
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        client = _authed_client()
        resp = client.post("/chat/seed", data={"seed_text": "Selgita seda leidu"})
        assert resp.status_code == 303
        assert resp.headers["location"] == "/chat/new"


# ---------------------------------------------------------------------------
# #724 — GET /chat/new?seed=<token> (peek the token, bind draft context)
# ---------------------------------------------------------------------------


class TestChatNewWithSeed:
    @patch("app.chat.routes.log_chat_conversation_create")
    @patch("app.chat.routes._get_draft_title", return_value="Eelnõu seest leid")
    @patch("app.chat.routes.peek_pending_seed")
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_seed_token_binds_draft_context_and_redirects_with_token(
        self, mock_provider, mock_connect, mock_peek, mock_draft_title, mock_audit
    ):
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        # The peeked token carries a draft_id.
        mock_peek.return_value = ("Selgita seda leidu", _DRAFT_ID)

        conv = _make_conversation(context_draft_id=_DRAFT_ID)
        with patch("app.chat.routes.create_conversation", return_value=conv) as mock_create:
            client = _authed_client()
            resp = client.get(f"/chat/new?seed={_SEED_TOKEN}")
            assert resp.status_code == 303
            # Redirects to the view page, carrying the token through.
            assert resp.headers["location"] == f"/chat/{conv.id}?seed={_SEED_TOKEN}"
            # context_draft_id was set from the token's draft_id.
            assert mock_create.call_args.kwargs["context_draft_id"] == _DRAFT_ID
            # Title reflects the draft.
            assert mock_create.call_args.kwargs["title"] == "Nõustamine — Eelnõu seest leid"

    @patch("app.chat.routes.log_chat_conversation_create")
    @patch("app.chat.routes.peek_pending_seed", return_value=None)
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_invalid_seed_token_still_creates_conversation(
        self, mock_provider, mock_connect, mock_peek, mock_audit
    ):
        """An expired/invalid token doesn't block conversation creation."""
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        conv = _make_conversation()
        with patch("app.chat.routes.create_conversation", return_value=conv) as mock_create:
            client = _authed_client()
            resp = client.get(f"/chat/new?seed={_SEED_TOKEN}")
            assert resp.status_code == 303
            # Still carries the (useless) token through — the view page just
            # ignores it.
            assert resp.headers["location"] == f"/chat/{conv.id}?seed={_SEED_TOKEN}"
            # No draft context (peek returned None).
            assert mock_create.call_args.kwargs["context_draft_id"] is None

    @patch("app.chat.routes.log_chat_conversation_create")
    @patch("app.chat.routes.peek_pending_seed")
    @patch("app.chat.routes._get_draft_title", return_value="Seemnest eelnõu")
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_seed_draft_wins_over_query_draft(
        self, mock_provider, mock_connect, mock_draft_title, mock_peek, mock_audit
    ):
        """When both ?draft= and ?seed= are present, the seed's draft_id wins."""
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        seed_draft_id = uuid.UUID("88888888-8888-8888-8888-888888888888")
        mock_peek.return_value = ("Selgita seda leidu", seed_draft_id)

        conv = _make_conversation(context_draft_id=seed_draft_id)
        with patch("app.chat.routes.create_conversation", return_value=conv) as mock_create:
            client = _authed_client()
            resp = client.get(f"/chat/new?draft={_DRAFT_ID}&seed={_SEED_TOKEN}")
            assert resp.status_code == 303
            # The seed's draft_id (not the query ?draft=) is what's bound.
            assert mock_create.call_args.kwargs["context_draft_id"] == seed_draft_id


# ---------------------------------------------------------------------------
# #724 — GET /chat/{conv_id}?seed=<token> (consume the token, pre-fill input)
# ---------------------------------------------------------------------------


class TestChatViewWithSeed:
    @patch("app.chat.routes.consume_pending_seed")
    @patch("app.chat.routes.list_messages", return_value=[])
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_valid_seed_token_prefills_textarea(
        self, mock_provider, mock_connect, mock_list_msgs, mock_consume
    ):
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_consume.return_value = ("seleta seda leidu", None)

        conv = _make_conversation()
        with patch("app.chat.routes.get_conversation", return_value=conv):
            client = _authed_client()
            resp = client.get(f"/chat/{_CONV_ID}?seed={_SEED_TOKEN}")
            assert resp.status_code == 200
            # The textarea body contains the seed text.
            assert "seleta seda leidu" in resp.text
            # The token was consumed with the caller's user id.
            assert mock_consume.call_args.kwargs["user_id"] == _USER_ID

    @patch("app.chat.routes.consume_pending_seed", return_value=None)
    @patch("app.chat.routes.list_messages", return_value=[])
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_invalid_seed_token_empty_textarea_no_crash(
        self, mock_provider, mock_connect, mock_list_msgs, mock_consume
    ):
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        conv = _make_conversation()
        with patch("app.chat.routes.get_conversation", return_value=conv):
            client = _authed_client()
            resp = client.get(f"/chat/{_CONV_ID}?seed=bad-token")
            assert resp.status_code == 200
            assert "chat-container" in resp.text
            # The textarea is the normal empty one.
            assert '<textarea id="chat-input"' in resp.text or 'id="chat-input"' in resp.text

    @patch("app.chat.routes.consume_pending_seed")
    @patch("app.chat.routes.list_messages", return_value=[])
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_no_seed_param_does_not_consume(
        self, mock_provider, mock_connect, mock_list_msgs, mock_consume
    ):
        mock_provider.return_value = _stub_provider()
        conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        conv = _make_conversation()
        with patch("app.chat.routes.get_conversation", return_value=conv):
            client = _authed_client()
            resp = client.get(f"/chat/{_CONV_ID}")
            assert resp.status_code == 200
        # No ?seed= → consume_pending_seed must not be called.
        mock_consume.assert_not_called()
