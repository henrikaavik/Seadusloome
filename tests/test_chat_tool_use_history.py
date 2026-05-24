# pyright: reportArgumentType=false
"""Tests for tool_use / tool_result history persistence (#315).

Migration 036 added two columns to ``messages``:

    * ``tool_use_id`` (TEXT NULL) — Claude's ``toolu_...`` identifier.
    * ``parent_message_id`` (UUID NULL, FK → messages.id ON DELETE CASCADE)
      — points at the assistant turn that triggered the tool call.

Plus two partial indexes (``idx_messages_parent``,
``idx_messages_tool_use``) and a defensive CHECK constraint
(``messages_tool_use_id_role_chk``) that pins ``tool_use_id`` to
``role='tool'`` rows.

These tests cover:

    * The Message dataclass exposes the two new fields.
    * ``create_message`` writes and reads them.
    * ``list_messages`` returns them.
    * ``_build_llm_messages`` renders persisted tool turns into the
      prompt so a multi-turn history replay does not silently drop the
      tool_use / tool_result pair.
    * The orchestrator inserts a parent assistant row up-front when a
      turn invokes tools and persists the tool row with
      ``tool_use_id`` + ``parent_message_id``.
    * A schema-level integration test (skipped unless ``DATABASE_URL``
      is set) verifies the migration applied with the expected columns,
      constraints, and indexes.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet

from app.chat.models import Message, _row_to_message, create_message
from app.chat.orchestrator import (
    ChatOrchestrator,
    _build_llm_messages,
)
from app.llm.provider import LLMProvider, StreamEvent

# ---------------------------------------------------------------------------
# Fernet key fixture — same shape as test_chat_models_encryption.py
# ---------------------------------------------------------------------------

_CONV_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
_USER_ID = "11111111-1111-1111-1111-111111111111"
_ORG_ID = "22222222-2222-2222-2222-222222222222"


@pytest.fixture(autouse=True)
def _fernet_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a deterministic Fernet key for every test in this module."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    import app.storage.encrypted as encrypted_module

    monkeypatch.setattr(encrypted_module, "_fernet", None)


# ---------------------------------------------------------------------------
# Row fixture helpers
# ---------------------------------------------------------------------------


def _make_message_row(
    *,
    msg_id: uuid.UUID | None = None,
    role: str = "tool",
    content: str = "{}",
    tool_name: str | None = "query_ontology",
    tool_input: dict | None = None,
    tool_output: dict | None = None,
    tool_use_id: str | None = "toolu_test_001",
    parent_message_id: uuid.UUID | None = None,
) -> tuple[Any, ...]:
    """Build a 16-tuple row matching :data:`app.chat.models._MESSAGE_COLUMNS`."""
    from app.storage import encrypt_text

    tool_input_ct = encrypt_text(json.dumps(tool_input)) if tool_input is not None else None
    tool_output_ct = encrypt_text(json.dumps(tool_output)) if tool_output is not None else None
    return (
        msg_id or uuid.uuid4(),
        _CONV_ID,
        role,
        tool_name,
        None,  # tokens_input
        None,  # tokens_output
        None,  # model
        datetime.now(UTC),
        encrypt_text(content),
        tool_input_ct,
        tool_output_ct,
        None,  # rag_context_encrypted
        False,  # is_pinned
        False,  # is_truncated
        tool_use_id,
        str(parent_message_id) if parent_message_id else None,
    )


def _echo_row_for_create(conn_mock: MagicMock):
    """Replay a ``RETURNING`` row from the params bound to the INSERT.

    The INSERT in :func:`create_message` binds 12 positional params
    (post-#315). We zip them back into the 16-column row order so the
    round-trip returns a faithful Message.
    """

    def _build_row():
        params = conn_mock.execute.call_args.args[1]
        (
            _conversation_id,
            role,
            tool_name,
            tokens_input,
            tokens_output,
            model,
            content_ct,
            tool_input_ct,
            tool_output_ct,
            rag_context_ct,
            tool_use_id,
            parent_message_id,
        ) = params
        return [
            uuid.uuid4(),
            _CONV_ID,
            role,
            tool_name,
            tokens_input,
            tokens_output,
            model,
            datetime.now(UTC),
            content_ct,
            tool_input_ct,
            tool_output_ct,
            rag_context_ct,
            False,
            False,
            tool_use_id,
            parent_message_id,
        ]

    return _build_row


# ---------------------------------------------------------------------------
# Dataclass + row mapping
# ---------------------------------------------------------------------------


class TestMessageDataclass:
    def test_defaults_to_none(self):
        msg = Message(
            id=uuid.uuid4(),
            conversation_id=_CONV_ID,
            role="user",
            content="Tere",
            tool_name=None,
            tool_input=None,
            tool_output=None,
            rag_context=None,
            tokens_input=None,
            tokens_output=None,
            model=None,
            created_at=datetime.now(UTC),
        )
        assert msg.tool_use_id is None
        assert msg.parent_message_id is None

    def test_accepts_tool_use_id_and_parent(self):
        parent = uuid.uuid4()
        msg = Message(
            id=uuid.uuid4(),
            conversation_id=_CONV_ID,
            role="tool",
            content="{}",
            tool_name="query_ontology",
            tool_input={"q": "?s"},
            tool_output={"results": []},
            rag_context=None,
            tokens_input=None,
            tokens_output=None,
            model=None,
            created_at=datetime.now(UTC),
            tool_use_id="toolu_abc",
            parent_message_id=parent,
        )
        assert msg.tool_use_id == "toolu_abc"
        assert msg.parent_message_id == parent


class TestRowToMessage:
    def test_reads_v036_columns(self):
        parent = uuid.uuid4()
        row = _make_message_row(
            tool_use_id="toolu_xyz",
            parent_message_id=parent,
            tool_input={"q": "select"},
            tool_output={"results": []},
        )
        msg = _row_to_message(row)
        assert msg.tool_use_id == "toolu_xyz"
        assert msg.parent_message_id == parent
        assert msg.tool_input == {"q": "select"}
        assert msg.tool_output == {"results": []}

    def test_pre_036_row_loads_with_none_tool_use_fields(self):
        """A 14-tuple row (pre-#315 fixtures) must still load cleanly."""
        from app.storage import encrypt_text

        row = (
            uuid.uuid4(),
            _CONV_ID,
            "tool",
            "search_provisions",
            None,
            None,
            None,
            datetime.now(UTC),
            encrypt_text("{}"),
            None,
            None,
            None,
            False,
            False,
        )
        msg = _row_to_message(row)
        assert msg.tool_use_id is None
        assert msg.parent_message_id is None
        assert msg.role == "tool"


# ---------------------------------------------------------------------------
# create_message
# ---------------------------------------------------------------------------


class TestCreateMessageToolUseFields:
    def test_persists_tool_use_id_and_parent(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.side_effect = _echo_row_for_create(conn)

        parent_id = uuid.uuid4()
        msg = create_message(
            conn,
            _CONV_ID,
            "tool",
            json.dumps({"results": []}),
            tool_name="query_ontology",
            tool_input={"query": "SELECT ?s WHERE { ?s ?p ?o }"},
            tool_output={"results": []},
            tool_use_id="toolu_42",
            parent_message_id=parent_id,
        )

        assert msg.role == "tool"
        assert msg.tool_use_id == "toolu_42"
        assert msg.parent_message_id == parent_id

        # Confirm the bound SQL params include the new fields.
        params = conn.execute.call_args.args[1]
        # tool_use_id is the second-to-last positional bind.
        assert params[-2] == "toolu_42"
        # parent_message_id is the last positional bind, stringified.
        assert params[-1] == str(parent_id)

    def test_persists_without_tool_fields_for_assistant_turn(self):
        """A non-tool message must not set the new fields."""
        conn = MagicMock()
        conn.execute.return_value.fetchone.side_effect = _echo_row_for_create(conn)

        msg = create_message(conn, _CONV_ID, "assistant", "Tere!")
        assert msg.tool_use_id is None
        assert msg.parent_message_id is None
        params = conn.execute.call_args.args[1]
        assert params[-2] is None
        assert params[-1] is None


# ---------------------------------------------------------------------------
# _build_llm_messages — multi-turn history replay
# ---------------------------------------------------------------------------


def _make_message_obj(
    *,
    role: str,
    content: str,
    tool_name: str | None = None,
    tool_input: dict | None = None,
    tool_use_id: str | None = None,
) -> Message:
    return Message(
        id=uuid.uuid4(),
        conversation_id=_CONV_ID,
        role=role,
        content=content,
        tool_name=tool_name,
        tool_input=tool_input,
        tool_output=None,
        rag_context=None,
        tokens_input=None,
        tokens_output=None,
        model=None,
        created_at=datetime.now(UTC),
        tool_use_id=tool_use_id,
        parent_message_id=None,
    )


class TestBuildLLMMessagesPreservesToolTurns:
    def test_mixed_history_includes_tool_call_segment(self):
        """The replay prompt must surface every persisted tool turn so a
        history-driven follow-up call sees what the assistant asked for
        and what it received (mirrors the live tool-call segment shape)."""
        history = [
            _make_message_obj(role="user", content="Otsi sätteid"),
            _make_message_obj(role="assistant", content="Otsin..."),
            _make_message_obj(
                role="tool",
                content='{"results": [{"uri": "estleg:X"}]}',
                tool_name="search_provisions",
                tool_input={"keywords": "andmekaitse"},
                tool_use_id="toolu_history_1",
            ),
            _make_message_obj(role="assistant", content="Leidsin §1."),
        ]
        parts = _build_llm_messages(history, "Mis veel?")

        joined = "\n\n".join(parts)
        # The tool turn must render as a TOOL_CALL/TOOL_RESULT segment.
        assert "[TOOL_CALL" in joined
        assert "[TOOL_RESULT" in joined
        assert "search_provisions" in joined
        assert "toolu_history_1" in joined
        # The tail must be the new user message.
        assert parts[-1] == "[USER]: Mis veel?"

    def test_tool_without_use_id_still_renders(self):
        """Pre-#315 persisted tool rows have NULL tool_use_id but must
        still appear in the prompt; otherwise older history would be
        silently truncated."""
        history = [
            _make_message_obj(role="user", content="Otsi"),
            _make_message_obj(
                role="tool",
                content='{"results": []}',
                tool_name="query_ontology",
                tool_input={"q": "?"},
                tool_use_id=None,
            ),
        ]
        parts = _build_llm_messages(history, "ok")
        joined = "\n\n".join(parts)
        assert "[TOOL_CALL" in joined
        assert "[TOOL_RESULT" in joined
        assert "query_ontology" in joined


# ---------------------------------------------------------------------------
# Orchestrator integration — tool turn persists with parent linkage
# ---------------------------------------------------------------------------


class _Collector:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def __call__(self, event: dict[str, Any]) -> None:
        self.events.append(event)


class _FakeToolLLM(LLMProvider):
    """Emits one tool_use, then text on the follow-up round."""

    def __init__(self) -> None:
        self._model = "fake-model"
        self._calls = 0

    def complete(self, prompt: str, **kwargs: Any) -> str:  # pragma: no cover
        return "x"

    def extract_json(self, prompt: str, **kwargs: Any) -> dict:  # pragma: no cover
        return {}

    def count_tokens(self, text: str) -> int:  # pragma: no cover
        return 0

    async def acomplete(self, prompt: str, **kwargs: Any) -> str:  # pragma: no cover
        return "x"

    async def astream(self, prompt: str, **kwargs: Any):  # type: ignore[override]
        if self._calls == 0:
            self._calls += 1
            yield StreamEvent(
                type="tool_use",
                tool_name="search_provisions",
                tool_input={"keywords": "andmekaitse"},
                tool_use_id="toolu_orchestrator_001",
            )
            yield StreamEvent(type="stop")
        else:
            yield StreamEvent(type="content", delta="Leidsin sätte.")
            yield StreamEvent(type="stop")


class _FakeSparql:
    def query(self, q: str) -> list:  # pragma: no cover
        return []


def _make_conversation():
    now = datetime.now(UTC)
    from app.chat.models import Conversation

    return Conversation(
        id=_CONV_ID,
        user_id=uuid.UUID(_USER_ID),
        org_id=uuid.UUID(_ORG_ID),
        title="Test",
        context_draft_id=None,
        created_at=now,
        updated_at=now,
    )


class TestOrchestratorPersistsToolUseLinkage:
    @patch("app.chat.orchestrator.execute_tool")
    @patch("app.chat.orchestrator.get_connection")
    def test_tool_turn_writes_parent_and_use_id(self, mock_get_conn, mock_exec_tool):
        """A turn that invokes a tool must:

        * Insert a parent assistant row up-front.
        * Insert the tool row with ``tool_use_id`` + ``parent_message_id``
          populated.
        """
        # Capture every create_message call so we can assert ordering.
        creates: list[dict[str, Any]] = []

        # We patch ``create_message`` at the orchestrator module so we
        # can intercept WITHOUT having to fake the full RETURNING row
        # shape three times.
        with patch("app.chat.orchestrator.create_message") as mock_create:
            assistant_uuid = uuid.uuid4()

            def _fake_create(conn, conv_id, role, content, **kwargs):
                creates.append({"role": role, "content": content, **kwargs})
                # The orchestrator captures the returned id as the
                # parent for subsequent tool inserts; give the assistant
                # a known UUID so we can assert it propagated.
                msg_id = assistant_uuid if role == "assistant" else uuid.uuid4()
                return MagicMock(id=msg_id)

            mock_create.side_effect = _fake_create

            conn = MagicMock()
            mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

            conv = _make_conversation()
            # ``side_effect_fetchone`` returns the conversation row on
            # the first ``execute().fetchone()`` (the load) and then a
            # dummy assistant row on subsequent reads (history load is
            # routed to ``fetchall`` instead).
            base_conv_row = (
                conv.id,
                conv.user_id,
                conv.org_id,
                conv.title,
                None,
                conv.created_at,
                conv.updated_at,
            )
            conn.execute.return_value.fetchone.return_value = base_conv_row
            conn.execute.return_value.fetchall.return_value = []

            async def fake_exec_tool(name, inp, sparql, auth=None):
                return {"results": [{"uri": "estleg:X"}]}

            mock_exec_tool.side_effect = fake_exec_tool

            collector = _Collector()
            orchestrator = ChatOrchestrator(_FakeToolLLM(), _FakeSparql())
            asyncio.run(
                orchestrator.handle_message(
                    _CONV_ID, "Otsi", {"id": _USER_ID, "org_id": _ORG_ID}, collector
                )
            )

        # Pick out the create_message calls by role.
        assistant_creates = [c for c in creates if c["role"] == "assistant"]
        tool_creates = [c for c in creates if c["role"] == "tool"]
        user_creates = [c for c in creates if c["role"] == "user"]

        assert user_creates, "user message must be persisted"
        assert assistant_creates, "an assistant placeholder must be inserted before the tool row"
        assert tool_creates, "the tool row must be persisted"

        tool_call = tool_creates[0]
        assert tool_call["tool_use_id"] == "toolu_orchestrator_001"
        assert tool_call["parent_message_id"] == assistant_uuid
        assert tool_call["tool_name"] == "search_provisions"
        assert tool_call["tool_input"] == {"keywords": "andmekaitse"}


# ---------------------------------------------------------------------------
# Migration schema test (integration — skipped without DATABASE_URL)
# ---------------------------------------------------------------------------


def _skip_if_no_db():
    if not os.getenv("DATABASE_URL"):
        pytest.skip("integration test — DATABASE_URL not set")


def test_migration_036_adds_columns():
    _skip_if_no_db()
    from app.db import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_name = 'messages'
              AND column_name IN ('tool_use_id', 'parent_message_id')
            ORDER BY column_name
            """
        )
        cols = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

    assert "tool_use_id" in cols, "migration 036 must add tool_use_id"
    assert "parent_message_id" in cols, "migration 036 must add parent_message_id"

    # tool_use_id is TEXT NULL
    tool_use_type, tool_use_nullable = cols["tool_use_id"]
    assert tool_use_type == "text"
    assert tool_use_nullable == "YES"

    # parent_message_id is UUID NULL
    parent_type, parent_nullable = cols["parent_message_id"]
    assert parent_type == "uuid"
    assert parent_nullable == "YES"


def test_migration_036_creates_indexes():
    _skip_if_no_db()
    from app.db import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT indexname FROM pg_indexes
            WHERE tablename = 'messages'
              AND indexname IN ('idx_messages_parent', 'idx_messages_tool_use')
            ORDER BY indexname
            """
        )
        names = {r[0] for r in cur.fetchall()}
    assert "idx_messages_parent" in names
    assert "idx_messages_tool_use" in names


def test_migration_036_adds_check_constraint():
    _skip_if_no_db()
    from app.db import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT conname FROM pg_constraint
            WHERE conname = 'messages_tool_use_id_role_chk'
            """
        )
        rows = cur.fetchall()
    assert rows, "migration 036 must add the messages_tool_use_id_role_chk CHECK"


def test_parent_message_cascade_delete():
    """Deleting a parent assistant message cascades to its tool children."""
    _skip_if_no_db()
    from app.db import get_connection

    with get_connection() as conn:
        # Set up a tiny fixture: org + user + conversation + assistant
        # + tool linked by parent_message_id.
        org_id = uuid.uuid4()
        user_id = uuid.uuid4()
        conv_id = uuid.uuid4()
        conn.execute(
            "INSERT INTO organizations (id, name) VALUES (%s, %s)",
            (str(org_id), "tool-cascade-test-org"),
        )
        conn.execute(
            (
                "INSERT INTO users (id, email, password_hash, org_id, role) "
                "VALUES (%s, %s, %s, %s, %s)"
            ),
            (
                str(user_id),
                f"toolcascade-{uuid.uuid4().hex}@example.com",
                "x",
                str(org_id),
                "drafter",
            ),
        )
        conn.execute(
            ("INSERT INTO conversations (id, user_id, org_id, title) VALUES (%s, %s, %s, %s)"),
            (str(conv_id), str(user_id), str(org_id), "test"),
        )

        from app.storage import encrypt_text

        assistant_id = uuid.uuid4()
        tool_id = uuid.uuid4()
        conn.execute(
            (
                "INSERT INTO messages "
                "(id, conversation_id, role, content_encrypted) "
                "VALUES (%s, %s, %s, %s)"
            ),
            (str(assistant_id), str(conv_id), "assistant", encrypt_text("hi")),
        )
        conn.execute(
            (
                "INSERT INTO messages "
                "(id, conversation_id, role, content_encrypted, "
                " tool_use_id, parent_message_id) "
                "VALUES (%s, %s, %s, %s, %s, %s)"
            ),
            (
                str(tool_id),
                str(conv_id),
                "tool",
                encrypt_text("{}"),
                "toolu_cascade_test",
                str(assistant_id),
            ),
        )
        conn.commit()

        # Delete the parent. The CASCADE must remove the tool child.
        conn.execute("DELETE FROM messages WHERE id = %s", (str(assistant_id),))
        conn.commit()

        row = conn.execute("SELECT 1 FROM messages WHERE id = %s", (str(tool_id),)).fetchone()
        assert row is None, "tool row should have cascaded with its parent"

        # Cleanup the fixture rows we created.
        conn.execute("DELETE FROM conversations WHERE id = %s", (str(conv_id),))
        conn.execute("DELETE FROM users WHERE id = %s", (str(user_id),))
        conn.execute("DELETE FROM organizations WHERE id = %s", (str(org_id),))
        conn.commit()


def test_check_constraint_rejects_tool_use_id_on_non_tool_role():
    """Defensive: tool_use_id may only be set on role='tool'."""
    _skip_if_no_db()
    import psycopg

    from app.db import get_connection
    from app.storage import encrypt_text

    with get_connection() as conn:
        org_id = uuid.uuid4()
        user_id = uuid.uuid4()
        conv_id = uuid.uuid4()
        conn.execute(
            "INSERT INTO organizations (id, name) VALUES (%s, %s)",
            (str(org_id), "tool-check-test-org"),
        )
        conn.execute(
            (
                "INSERT INTO users (id, email, password_hash, org_id, role) "
                "VALUES (%s, %s, %s, %s, %s)"
            ),
            (
                str(user_id),
                f"toolcheck-{uuid.uuid4().hex}@example.com",
                "x",
                str(org_id),
                "drafter",
            ),
        )
        conn.execute(
            ("INSERT INTO conversations (id, user_id, org_id, title) VALUES (%s, %s, %s, %s)"),
            (str(conv_id), str(user_id), str(org_id), "test"),
        )
        conn.commit()

        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                (
                    "INSERT INTO messages "
                    "(conversation_id, role, content_encrypted, tool_use_id) "
                    "VALUES (%s, %s, %s, %s)"
                ),
                (str(conv_id), "assistant", encrypt_text("nope"), "toolu_bad"),
            )
        conn.rollback()

        # Cleanup.
        conn.execute("DELETE FROM conversations WHERE id = %s", (str(conv_id),))
        conn.execute("DELETE FROM users WHERE id = %s", (str(user_id),))
        conn.execute("DELETE FROM organizations WHERE id = %s", (str(org_id),))
        conn.commit()
