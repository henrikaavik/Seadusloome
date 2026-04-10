"""Unit tests for ``app.chat.models``.

Tests the CRUD helpers for ``conversations`` and ``messages``.
All DB access is mocked — same patterns as ``tests/test_drafter_session_model.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.chat.models import (
    Conversation,
    Message,
    create_conversation,
    create_message,
    delete_conversation,
    get_conversation,
    list_conversations_for_user,
    list_messages,
    update_conversation_title,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ORG_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_DRAFT_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")


def _make_conversation_row(
    *,
    conv_id: uuid.UUID | None = None,
    user_id: uuid.UUID = _USER_ID,
    org_id: uuid.UUID = _ORG_ID,
    title: str = "Uus vestlus",
    context_draft_id: uuid.UUID | None = None,
) -> tuple[Any, ...]:
    """Build a raw cursor row matching _CONVERSATION_COLUMNS order."""
    now = datetime.now(UTC)
    return (
        conv_id or uuid.uuid4(),
        user_id,
        org_id,
        title,
        context_draft_id,
        now,
        now,
    )


def _make_message_row(
    *,
    msg_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
    role: str = "user",
    content: str = "Mis on tsiviilseadustik?",
    tool_name: str | None = None,
    tool_input: str | None = None,
    tool_output: str | None = None,
    rag_context: str | None = None,
    tokens_input: int | None = None,
    tokens_output: int | None = None,
    model: str | None = None,
) -> tuple[Any, ...]:
    """Build a raw cursor row matching _MESSAGE_COLUMNS order."""
    now = datetime.now(UTC)
    return (
        msg_id or uuid.uuid4(),
        conversation_id or uuid.uuid4(),
        role,
        content,
        tool_name,
        tool_input,
        tool_output,
        rag_context,
        tokens_input,
        tokens_output,
        model,
        now,
    )


# ---------------------------------------------------------------------------
# create_conversation
# ---------------------------------------------------------------------------


class TestCreateConversation:
    def test_create_returns_conversation(self):
        conn = MagicMock()
        conv_id = uuid.uuid4()
        row = _make_conversation_row(conv_id=conv_id)
        conn.execute.return_value.fetchone.return_value = row

        result = create_conversation(conn, _USER_ID, _ORG_ID, "Test vestlus")

        assert isinstance(result, Conversation)
        assert result.id == conv_id
        assert result.user_id == _USER_ID
        assert result.org_id == _ORG_ID
        conn.execute.assert_called_once()

    def test_create_with_draft_context(self):
        conn = MagicMock()
        row = _make_conversation_row(context_draft_id=_DRAFT_ID)
        conn.execute.return_value.fetchone.return_value = row

        result = create_conversation(
            conn, _USER_ID, _ORG_ID, "Eelnou vestlus", context_draft_id=_DRAFT_ID
        )

        assert result.context_draft_id == _DRAFT_ID

    def test_create_raises_on_no_row(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None

        with pytest.raises(RuntimeError, match="produced no row"):
            create_conversation(conn, _USER_ID, _ORG_ID)


# ---------------------------------------------------------------------------
# get_conversation
# ---------------------------------------------------------------------------


class TestGetConversation:
    def test_get_returns_conversation(self):
        conn = MagicMock()
        conv_id = uuid.uuid4()
        row = _make_conversation_row(conv_id=conv_id, title="My chat")
        conn.execute.return_value.fetchone.return_value = row

        result = get_conversation(conn, conv_id)
        assert result is not None
        assert result.id == conv_id
        assert result.title == "My chat"

    def test_get_returns_none_for_missing(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None

        result = get_conversation(conn, uuid.uuid4())
        assert result is None

    def test_get_handles_db_error(self):
        conn = MagicMock()
        conn.execute.side_effect = RuntimeError("DB error")

        result = get_conversation(conn, uuid.uuid4())
        assert result is None


# ---------------------------------------------------------------------------
# list_conversations_for_user
# ---------------------------------------------------------------------------


class TestListConversations:
    def test_list_returns_conversations(self):
        conn = MagicMock()
        row1 = _make_conversation_row(title="First")
        row2 = _make_conversation_row(title="Second")
        conn.execute.return_value.fetchall.return_value = [row1, row2]

        result = list_conversations_for_user(conn, _USER_ID)
        assert len(result) == 2
        assert result[0].title == "First"

    def test_list_empty(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []

        result = list_conversations_for_user(conn, _USER_ID)
        assert result == []

    def test_list_zero_limit_returns_empty(self):
        conn = MagicMock()

        result = list_conversations_for_user(conn, _USER_ID, limit=0)
        assert result == []
        conn.execute.assert_not_called()


# ---------------------------------------------------------------------------
# update_conversation_title
# ---------------------------------------------------------------------------


class TestUpdateConversationTitle:
    def test_update_title(self):
        conn = MagicMock()
        conv_id = uuid.uuid4()

        update_conversation_title(conn, conv_id, "Uuendatud pealkiri")

        conn.execute.assert_called_once()
        sql = conn.execute.call_args.args[0]
        params = conn.execute.call_args.args[1]
        assert "title" in sql
        assert "updated_at" in sql
        assert "Uuendatud pealkiri" in params


# ---------------------------------------------------------------------------
# delete_conversation
# ---------------------------------------------------------------------------


class TestDeleteConversation:
    def test_delete(self):
        conn = MagicMock()
        conv_id = uuid.uuid4()

        delete_conversation(conn, conv_id)

        conn.execute.assert_called_once()
        sql = conn.execute.call_args.args[0]
        assert "DELETE" in sql
        assert "conversations" in sql


# ---------------------------------------------------------------------------
# create_message
# ---------------------------------------------------------------------------


class TestCreateMessage:
    def test_create_user_message(self):
        conn = MagicMock()
        conv_id = uuid.uuid4()
        msg_id = uuid.uuid4()
        row = _make_message_row(msg_id=msg_id, conversation_id=conv_id)
        conn.execute.return_value.fetchone.return_value = row

        result = create_message(conn, conv_id, "user", "Tere!")

        assert isinstance(result, Message)
        assert result.id == msg_id
        assert result.role == "user"
        conn.execute.assert_called_once()

    def test_create_tool_message(self):
        conn = MagicMock()
        conv_id = uuid.uuid4()
        row = _make_message_row(
            conversation_id=conv_id,
            role="tool",
            tool_name="query_ontology",
            tool_input='{"query": "SELECT ..."}',
            tool_output='{"results": []}',
        )
        conn.execute.return_value.fetchone.return_value = row

        result = create_message(
            conn,
            conv_id,
            "tool",
            "Tool result",
            tool_name="query_ontology",
            tool_input={"query": "SELECT ..."},
            tool_output={"results": []},
        )

        assert result.tool_name == "query_ontology"

    def test_create_rejects_invalid_role(self):
        conn = MagicMock()
        with pytest.raises(ValueError, match="Invalid message role"):
            create_message(conn, uuid.uuid4(), "invalid_role", "content")


# ---------------------------------------------------------------------------
# list_messages
# ---------------------------------------------------------------------------


class TestListMessages:
    def test_list_returns_messages(self):
        conn = MagicMock()
        conv_id = uuid.uuid4()
        row = _make_message_row(conversation_id=conv_id)
        conn.execute.return_value.fetchall.return_value = [row]

        result = list_messages(conn, conv_id)
        assert len(result) == 1
        assert isinstance(result[0], Message)

    def test_list_empty(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []

        result = list_messages(conn, uuid.uuid4())
        assert result == []
