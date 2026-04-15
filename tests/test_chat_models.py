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
from cryptography.fernet import Fernet

from app.chat.models import (
    Conversation,
    Message,
    create_conversation,
    create_message,
    delete_conversation,
    delete_messages_after,
    fork_conversation,
    get_conversation,
    list_conversations_for_user,
    list_messages,
    list_pinned_messages,
    set_conversation_archived,
    set_conversation_pinned,
    set_message_pinned,
    update_conversation_title,
    update_message_truncated,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ORG_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_DRAFT_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")


@pytest.fixture(autouse=True)
def _fernet_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a real Fernet key so ``create_message`` can encrypt.

    #570: new inserts encrypt ``content`` via ``app.storage.encrypt_text``,
    which refuses to run in production without a key. Tests run under
    ``APP_ENV=development`` by default so an ephemeral key would be fine,
    but we set an explicit key here so the encrypted bytes are stable
    across tests that compare or inspect them.
    """
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    # Reset the cached Fernet singleton so the new key takes effect.
    import app.storage.encrypted as encrypted_module

    monkeypatch.setattr(encrypted_module, "_fernet", None)


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
    content_encrypted: bytes | None = None,
    tool_input_encrypted: bytes | None = None,
    tool_output_encrypted: bytes | None = None,
    rag_context_encrypted: bytes | None = None,
) -> tuple[Any, ...]:
    """Build a raw cursor row matching _MESSAGE_COLUMNS order.

    #570 adds four encrypted BYTEA columns. The plaintext defaults above
    exercise the fallback path used for pre-backfill rows.
    """
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
        content_encrypted,
        tool_input_encrypted,
        tool_output_encrypted,
        rag_context_encrypted,
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

    def test_update_title_defaults_title_is_custom_false(self):
        """Default invocation (auto-title job) writes title_is_custom=False."""
        conn = MagicMock()
        update_conversation_title(conn, uuid.uuid4(), "Automaatne pealkiri")
        params = conn.execute.call_args.args[1]
        # Params: (title, is_custom, id)
        assert params[0] == "Automaatne pealkiri"
        assert params[1] is False

    def test_update_title_is_custom_true_sets_flag(self):
        """Manual rename flips title_is_custom so auto-titling is skipped."""
        conn = MagicMock()
        update_conversation_title(conn, uuid.uuid4(), "Käsitsi pealkiri", is_custom=True)
        params = conn.execute.call_args.args[1]
        assert params[0] == "Käsitsi pealkiri"
        assert params[1] is True
        sql = conn.execute.call_args.args[0]
        assert "title_is_custom" in sql

    def test_update_title_does_not_clobber_custom_flag(self):
        """Auto-title job (is_custom=False) must preserve an existing TRUE flag.

        Regression: the old SQL unconditionally wrote ``title_is_custom = %s``
        so an auto-title call after a manual rename re-opened the row to
        further auto-title overwrites. The fix uses a CASE WHEN that only
        transitions FALSE → TRUE, never TRUE → FALSE.
        """
        conn = MagicMock()
        update_conversation_title(conn, uuid.uuid4(), "Automaatne", is_custom=False)
        sql = conn.execute.call_args.args[0]
        # SQL must preserve the existing flag on is_custom=False.
        assert "CASE WHEN %s THEN TRUE ELSE title_is_custom END" in sql


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


# ---------------------------------------------------------------------------
# set_conversation_pinned / set_conversation_archived
# ---------------------------------------------------------------------------


class TestSetConversationPinned:
    def test_pin_sets_pinned_at_now(self):
        conn = MagicMock()
        conv_id = uuid.uuid4()
        set_conversation_pinned(conn, conv_id, True)

        conn.execute.assert_called_once()
        sql, params = conn.execute.call_args.args
        assert "is_pinned" in sql
        assert "pinned_at" in sql
        # Params: (is_pinned, is_pinned_for_case_when, id)
        assert params[0] is True
        assert params[1] is True
        assert params[2] == str(conv_id)

    def test_unpin_sets_pinned_at_null(self):
        conn = MagicMock()
        conv_id = uuid.uuid4()
        set_conversation_pinned(conn, conv_id, False)

        sql, params = conn.execute.call_args.args
        assert params[0] is False
        # CASE WHEN FALSE THEN now() ELSE NULL uses the same flag.
        assert params[1] is False
        assert "NULL" in sql


class TestSetConversationArchived:
    def test_archive(self):
        conn = MagicMock()
        conv_id = uuid.uuid4()
        set_conversation_archived(conn, conv_id, True)

        sql, params = conn.execute.call_args.args
        assert "is_archived" in sql
        assert params == (True, str(conv_id))

    def test_unarchive(self):
        conn = MagicMock()
        conv_id = uuid.uuid4()
        set_conversation_archived(conn, conv_id, False)

        _, params = conn.execute.call_args.args
        assert params == (False, str(conv_id))


# ---------------------------------------------------------------------------
# list_conversations_for_user — ordering, filters, search
# ---------------------------------------------------------------------------


class TestListConversationsAdvanced:
    def test_default_excludes_archived(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []

        list_conversations_for_user(conn, _USER_ID)

        sql = conn.execute.call_args.args[0]
        assert "is_archived = FALSE" in sql

    def test_include_archived_removes_filter(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []

        list_conversations_for_user(conn, _USER_ID, include_archived=True)

        sql = conn.execute.call_args.args[0]
        assert "is_archived = FALSE" not in sql

    def test_pinned_first_orders_by_is_pinned(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []

        list_conversations_for_user(conn, _USER_ID, pinned_first=True)

        sql = conn.execute.call_args.args[0]
        assert "is_pinned DESC" in sql
        assert "pinned_at DESC NULLS LAST" in sql

    def test_pinned_first_false_orders_by_updated_at_only(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []

        list_conversations_for_user(conn, _USER_ID, pinned_first=False)

        sql = conn.execute.call_args.args[0]
        assert "is_pinned DESC" not in sql
        assert "updated_at DESC" in sql

    def test_search_uses_ilike(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []

        list_conversations_for_user(conn, _USER_ID, search="maks")

        sql, params = conn.execute.call_args.args
        assert "ILIKE" in sql
        assert "%maks%" in params

    def test_search_escapes_percent_wildcard(self):
        """A literal ``%`` in the search term must not act as a wildcard."""
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []

        list_conversations_for_user(conn, _USER_ID, search="50%")

        sql, params = conn.execute.call_args.args
        # SQL must declare the escape character.
        assert "ESCAPE '\\'" in sql
        # The percent sign inside the search term is prefixed with a
        # backslash so it matches literally; the surrounding wildcards
        # stay unescaped.
        assert r"%50\%%" in params

    def test_search_escapes_underscore_wildcard(self):
        """A literal ``_`` must not match any single character."""
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []

        list_conversations_for_user(conn, _USER_ID, search="a_b")

        _, params = conn.execute.call_args.args
        assert r"%a\_b%" in params

    def test_search_escapes_backslash(self):
        """Backslashes themselves are doubled so ESCAPE stays unambiguous."""
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []

        list_conversations_for_user(conn, _USER_ID, search="a\\b")

        _, params = conn.execute.call_args.args
        # Input ``a\b`` → escape the backslash to ``a\\b`` inside a
        # pattern that says ``ESCAPE '\'``.
        assert r"%a\\b%" in params

    def test_no_search_omits_ilike(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []

        list_conversations_for_user(conn, _USER_ID)

        sql = conn.execute.call_args.args[0]
        assert "ILIKE" not in sql

    def test_pre_017_row_hydrates_with_defaults(self):
        """A 7-tuple row (pre-migration 017) should still hydrate cleanly."""
        conn = MagicMock()
        row = _make_conversation_row(title="Old row")  # 7-tuple
        conn.execute.return_value.fetchall.return_value = [row]

        result = list_conversations_for_user(conn, _USER_ID)
        assert len(result) == 1
        assert result[0].is_pinned is False
        assert result[0].is_archived is False
        assert result[0].pinned_at is None
        assert result[0].title_is_custom is False


# ---------------------------------------------------------------------------
# Message pin / truncated / list_pinned
# ---------------------------------------------------------------------------


class TestSetMessagePinned:
    def test_pin(self):
        conn = MagicMock()
        msg_id = uuid.uuid4()
        set_message_pinned(conn, msg_id, True)

        sql, params = conn.execute.call_args.args
        assert "is_pinned" in sql
        assert params == (True, str(msg_id))

    def test_unpin(self):
        conn = MagicMock()
        msg_id = uuid.uuid4()
        set_message_pinned(conn, msg_id, False)

        _, params = conn.execute.call_args.args
        assert params == (False, str(msg_id))


class TestUpdateMessageTruncated:
    def test_mark_truncated_default_true(self):
        conn = MagicMock()
        msg_id = uuid.uuid4()
        update_message_truncated(conn, msg_id)

        sql, params = conn.execute.call_args.args
        assert "is_truncated" in sql
        assert params == (True, str(msg_id))

    def test_mark_truncated_false(self):
        conn = MagicMock()
        msg_id = uuid.uuid4()
        update_message_truncated(conn, msg_id, truncated=False)

        _, params = conn.execute.call_args.args
        assert params == (False, str(msg_id))


class TestListPinnedMessages:
    def test_filters_by_pinned_true(self):
        conn = MagicMock()
        conv_id = uuid.uuid4()
        row = _make_message_row(conversation_id=conv_id)
        conn.execute.return_value.fetchall.return_value = [row]

        result = list_pinned_messages(conn, conv_id)
        assert len(result) == 1

        sql = conn.execute.call_args.args[0]
        assert "is_pinned = TRUE" in sql

    def test_empty(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []

        assert list_pinned_messages(conn, uuid.uuid4()) == []


# ---------------------------------------------------------------------------
# delete_messages_after
# ---------------------------------------------------------------------------


class TestDeleteMessagesAfter:
    def test_delete_uses_strict_greater_than(self):
        """Messages *at* the boundary created_at must NOT be deleted."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.rowcount = 3
        conn.execute.return_value = cursor
        boundary = datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC)

        result = delete_messages_after(conn, uuid.uuid4(), boundary)

        assert result == 3
        sql, params = conn.execute.call_args.args
        assert "DELETE" in sql
        assert "created_at > %s" in sql
        assert params[1] == boundary

    def test_returns_zero_on_db_error(self):
        conn = MagicMock()
        conn.execute.side_effect = RuntimeError("boom")

        result = delete_messages_after(conn, uuid.uuid4(), datetime.now(UTC))
        assert result == 0


# ---------------------------------------------------------------------------
# fork_conversation
# ---------------------------------------------------------------------------


class TestForkConversation:
    def test_fork_copies_messages_up_to_boundary(self):
        conn = MagicMock()
        source_conv_id = uuid.uuid4()
        boundary_msg_id = uuid.uuid4()
        boundary_created_at = datetime(2026, 4, 14, 9, 30, tzinfo=UTC)

        # 1. boundary lookup 2. get_conversation 3. create_conversation
        # 4. INSERT ... SELECT
        source_row = _make_conversation_row(conv_id=source_conv_id, title="Algvestlus")
        new_conv_id = uuid.uuid4()
        new_row = _make_conversation_row(conv_id=new_conv_id, title="Jätk: Algvestlus")

        fetchone_results = [
            (boundary_created_at, source_conv_id),  # boundary lookup
            source_row,  # get_conversation(source)
            new_row,  # create_conversation(new)
        ]
        conn.execute.return_value.fetchone.side_effect = fetchone_results

        new_conv = fork_conversation(
            conn,
            source_conv_id,
            boundary_msg_id,
            user_id=_USER_ID,
            org_id=_ORG_ID,
        )

        assert new_conv.id == new_conv_id
        assert new_conv.title == "Jätk: Algvestlus"

        # Final call should be the INSERT ... SELECT with created_at bound.
        final_call = conn.execute.call_args_list[-1]
        sql = final_call.args[0]
        params = final_call.args[1]
        assert "INSERT INTO messages" in sql
        assert "created_at <= %s" in sql
        assert "content_encrypted" in sql
        assert "tool_input_encrypted" in sql
        assert "tool_output_encrypted" in sql
        assert "rag_context_encrypted" in sql
        assert params == (
            str(new_conv_id),
            str(source_conv_id),
            boundary_created_at,
        )

    def test_fork_title_prefixes_jatk(self):
        conn = MagicMock()
        source_conv_id = uuid.uuid4()
        boundary_msg_id = uuid.uuid4()
        created_at = datetime.now(UTC)

        source_row = _make_conversation_row(conv_id=source_conv_id, title="Maksureform")
        new_row = _make_conversation_row(title="Jätk: Maksureform")

        conn.execute.return_value.fetchone.side_effect = [
            (created_at, source_conv_id),
            source_row,
            new_row,
        ]

        fork_conversation(
            conn,
            source_conv_id,
            boundary_msg_id,
            user_id=_USER_ID,
            org_id=_ORG_ID,
        )

        # create_conversation call — look for the INSERT INTO conversations
        insert_calls = [
            c for c in conn.execute.call_args_list if "INSERT INTO conversations" in c.args[0]
        ]
        assert len(insert_calls) == 1
        params = insert_calls[0].args[1]
        # (user_id, org_id, title, context_draft_id)
        assert params[2] == "Jätk: Maksureform"

    def test_fork_raises_if_boundary_message_missing(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None

        with pytest.raises(ValueError, match="not found"):
            fork_conversation(
                conn,
                uuid.uuid4(),
                uuid.uuid4(),
                user_id=_USER_ID,
                org_id=_ORG_ID,
            )

    def test_fork_raises_if_boundary_belongs_to_other_conversation(self):
        conn = MagicMock()
        source_conv_id = uuid.uuid4()
        other_conv_id = uuid.uuid4()

        conn.execute.return_value.fetchone.return_value = (
            datetime.now(UTC),
            other_conv_id,
        )

        with pytest.raises(ValueError, match="does not belong"):
            fork_conversation(
                conn,
                source_conv_id,
                uuid.uuid4(),
                user_id=_USER_ID,
                org_id=_ORG_ID,
            )
