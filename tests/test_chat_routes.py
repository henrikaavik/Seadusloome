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

    @patch("app.chat.routes.log_chat_conversation_delete")
    @patch("app.chat.routes.delete_conversation")
    @patch("app.chat.routes._connect")
    @patch("app.auth.middleware._get_provider")
    def test_delete_from_list_returns_empty_row_swap(
        self, mock_provider, mock_connect, mock_delete, mock_audit
    ):
        """Bug #654: the ``/chat`` list delete form posts ``from_list=1``
        so htmx can swap the row out in place (empty 200 body, no
        HX-Redirect).
        """
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
                data={"from_list": "1"},
            )
            assert resp.status_code == 200
            assert resp.text == ""
            # No HX-Redirect — row just disappears from the list.
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
