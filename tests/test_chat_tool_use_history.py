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


# ---------------------------------------------------------------------------
# #315 review fixes — placeholder UPDATE shifts created_at; partial persist
# UPDATEs the placeholder instead of inserting a sibling row.
# ---------------------------------------------------------------------------


class TestUpdateAssistantPayloadBumpsCreatedAt:
    """Fix 1 (ordering): ``_update_assistant_payload`` must bump
    ``created_at`` to ``NOW()`` so the placeholder sorts AFTER the tool
    rows that were inserted between the initial placeholder INSERT and
    the final UPDATE. Otherwise ``list_messages`` ``ORDER BY created_at
    ASC`` returns the placeholder first and the replay path emits
    ``[assistant_final, tool_call, tool_result]`` instead of
    ``[tool_call, tool_result, assistant_final]``.
    """

    def test_update_sql_includes_created_at_now(self):
        from app.chat.orchestrator import _update_assistant_payload

        conn = MagicMock()
        _update_assistant_payload(
            conn,
            uuid.uuid4(),
            content="Lõplik vastus",
            tokens_input=100,
            tokens_output=50,
            rag_context=None,
        )
        # The UPDATE statement is the only call.
        sql = conn.execute.call_args.args[0]
        # The SET clause must include ``created_at = NOW()`` so the
        # placeholder sorts after the tool rows inserted while streaming.
        normalized = " ".join(sql.split()).lower()
        assert "set" in normalized
        assert "created_at = now()" in normalized
        # The non-deprecated payload columns are still updated.
        assert "content_encrypted" in normalized
        assert "tokens_input" in normalized
        assert "tokens_output" in normalized
        assert "rag_context_encrypted" in normalized


class TestToolTurnReplayOrdering:
    """Fix 1 end-to-end: simulate a tool turn through the orchestrator
    and assert the persisted ordering is suitable for replay.

    We can't observe row ``created_at`` directly without a real DB, so
    we assert the *UPDATE* on the assistant placeholder fires AFTER the
    tool row INSERTs — which is the operational guarantee that, combined
    with ``SET created_at = NOW()``, produces the
    ``tool_call → tool_result → assistant_final`` order on the next
    ``list_messages`` call.
    """

    @patch("app.chat.orchestrator.execute_tool")
    @patch("app.chat.orchestrator.get_connection")
    def test_placeholder_update_happens_after_tool_inserts(self, mock_get_conn, mock_exec_tool):
        # Order log: each create_message INSERT and each
        # _update_assistant_payload UPDATE appends here.
        ordering: list[str] = []

        assistant_uuid = uuid.uuid4()

        def _fake_create(conn, conv_id, role, content, **kwargs):
            ordering.append(f"INSERT:{role}")
            msg_id = assistant_uuid if role == "assistant" else uuid.uuid4()
            return MagicMock(id=msg_id)

        def _fake_update(conn, message_id, **kwargs):
            ordering.append(f"UPDATE:{message_id}")

        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

        conv = _make_conversation()
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

        with (
            patch("app.chat.orchestrator.create_message", side_effect=_fake_create),
            patch("app.chat.orchestrator._update_assistant_payload", side_effect=_fake_update),
        ):
            orchestrator = ChatOrchestrator(_FakeToolLLM(), _FakeSparql())
            collector = _Collector()
            asyncio.run(
                orchestrator.handle_message(
                    _CONV_ID, "Otsi", {"id": _USER_ID, "org_id": _ORG_ID}, collector
                )
            )

        # Filter to the events we care about for ordering.
        tool_inserts = [i for i, ev in enumerate(ordering) if ev == "INSERT:tool"]
        assistant_updates = [i for i, ev in enumerate(ordering) if ev.startswith("UPDATE:")]
        assistant_inserts = [i for i, ev in enumerate(ordering) if ev == "INSERT:assistant"]

        assert tool_inserts, "tool row INSERT must occur"
        assert assistant_inserts, "assistant placeholder INSERT must occur"
        assert assistant_updates, (
            "the assistant placeholder must be UPDATEd with the final answer "
            "(this is the path that bumps created_at to NOW())"
        )
        # The placeholder INSERT comes first, then tool rows, then the
        # final UPDATE — that UPDATE is where created_at is bumped, so
        # the row's effective sort key ends up AFTER the tool rows.
        assert assistant_inserts[0] < tool_inserts[0], (
            "placeholder must be inserted before tool rows so they can link via parent_message_id"
        )
        assert tool_inserts[-1] < assistant_updates[-1], (
            "tool rows must be inserted before the placeholder UPDATE — "
            "otherwise NOW() on the UPDATE wouldn't push created_at past the tool rows"
        )

    def test_list_messages_replay_order_after_update(self):
        """Direct unit test on the replay shape: when an assistant row's
        ``created_at`` is greater than the tool rows' ``created_at``
        (the post-fix state), ``_build_llm_messages`` over a
        ``created_at`` ASC ordering renders
        ``[TOOL_CALL → TOOL_RESULT → assistant_final]`` — the correct
        replay order for the next Claude turn.
        """
        from datetime import timedelta

        from app.chat.orchestrator import _build_llm_messages

        t0 = datetime.now(UTC)

        user_msg = Message(
            id=uuid.uuid4(),
            conversation_id=_CONV_ID,
            role="user",
            content="Otsi sätteid",
            tool_name=None,
            tool_input=None,
            tool_output=None,
            rag_context=None,
            tokens_input=None,
            tokens_output=None,
            model=None,
            created_at=t0,
        )
        tool_msg = Message(
            id=uuid.uuid4(),
            conversation_id=_CONV_ID,
            role="tool",
            content='{"results": []}',
            tool_name="search_provisions",
            tool_input={"keywords": "andmekaitse"},
            tool_output=None,
            rag_context=None,
            tokens_input=None,
            tokens_output=None,
            model=None,
            created_at=t0 + timedelta(seconds=1),
            tool_use_id="toolu_ordering_001",
        )
        # POST-FIX: the placeholder UPDATE bumps created_at past the
        # tool row, so the ASC-ordered list sees the assistant row LAST.
        assistant_msg = Message(
            id=uuid.uuid4(),
            conversation_id=_CONV_ID,
            role="assistant",
            content="Leidsin §1.",
            tool_name=None,
            tool_input=None,
            tool_output=None,
            rag_context=None,
            tokens_input=None,
            tokens_output=None,
            model=None,
            created_at=t0 + timedelta(seconds=2),
        )

        history = [user_msg, tool_msg, assistant_msg]
        parts = _build_llm_messages(history, "Mis veel?")

        # Find the indices of the three semantically relevant entries.
        tool_idx = next(i for i, p in enumerate(parts) if "TOOL_CALL" in p and "TOOL_RESULT" in p)
        assistant_idx = next(i for i, p in enumerate(parts) if p.startswith("[ASSISTANT]:"))
        user_idx = next(i for i, p in enumerate(parts) if p == "[USER]: Mis veel?")

        # The tool turn comes BEFORE the assistant's final answer,
        # which comes BEFORE the new user message. This is the order
        # Claude requires; the pre-fix state had assistant_idx <
        # tool_idx and broke replay.
        assert tool_idx < assistant_idx < user_idx, (
            f"Expected tool_call -> assistant_final -> user, got order: {parts}"
        )


class TestPartialPersistUpdatesPlaceholder:
    """Fix 2 (double-insert): when a placeholder assistant row already
    exists for a tool turn, ``_persist_partial_assistant`` must UPDATE
    that row in place rather than INSERTing a second assistant row.
    Otherwise the timeout / cancel / error paths leave two assistant
    messages for a single logical turn — the empty parent + a partial
    sibling — and replay sees a corrupted history.
    """

    @patch("app.chat.orchestrator._update_assistant_payload")
    @patch("app.chat.orchestrator.create_message")
    @patch("app.chat.orchestrator.get_connection")
    def test_with_placeholder_updates_in_place(
        self, mock_get_conn, mock_create_msg, mock_update_payload
    ):
        """When ``placeholder_message_id`` is set, the helper must:

        * NOT call ``create_message`` (would insert a second row).
        * Call ``_update_assistant_payload`` with the existing id.
        * Return the placeholder id unchanged.
        """
        from app.chat.orchestrator import _persist_partial_assistant

        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

        placeholder_id = uuid.uuid4()

        result = _persist_partial_assistant(
            _CONV_ID,
            "osaline sisu",
            "fake-model",
            None,
            is_truncated=True,
            tokens_input=42,
            tokens_output=17,
            placeholder_message_id=placeholder_id,
        )

        assert result == placeholder_id, (
            "Returning the placeholder id signals to callers that the same row was reused"
        )
        # Critical: no second assistant INSERT happened.
        mock_create_msg.assert_not_called()
        # The placeholder was UPDATEd with the partial content + tokens.
        mock_update_payload.assert_called_once()
        call_kwargs = mock_update_payload.call_args.kwargs
        # The id passed positionally as the second arg.
        positional = mock_update_payload.call_args.args
        assert positional[1] == placeholder_id
        assert call_kwargs.get("content") == "osaline sisu"
        assert call_kwargs.get("tokens_input") == 42
        assert call_kwargs.get("tokens_output") == 17

    @patch("app.chat.orchestrator._update_assistant_payload")
    @patch("app.chat.orchestrator.create_message")
    @patch("app.chat.orchestrator.get_connection")
    def test_without_placeholder_inserts_as_before(
        self, mock_get_conn, mock_create_msg, mock_update_payload
    ):
        """Backwards compatibility: non-tool turns (no placeholder)
        still INSERT a fresh assistant row as before."""
        from app.chat.orchestrator import _persist_partial_assistant

        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
        new_msg_id = uuid.uuid4()
        mock_create_msg.return_value = MagicMock(id=new_msg_id)

        result = _persist_partial_assistant(
            _CONV_ID,
            "osaline sisu",
            "fake-model",
            None,
            is_truncated=True,
            tokens_input=42,
            tokens_output=17,
            placeholder_message_id=None,
        )

        assert result == new_msg_id
        mock_create_msg.assert_called_once()
        # The UPDATE helper must NOT have been used in the non-tool path.
        mock_update_payload.assert_not_called()

    @patch("app.chat.orchestrator.execute_tool")
    @patch("app.chat.orchestrator._persist_partial_assistant")
    @patch("app.chat.orchestrator.get_connection")
    def test_timeout_on_tool_turn_passes_placeholder(
        self, mock_get_conn, mock_persist, mock_exec_tool
    ):
        """End-to-end: a tool turn that hits the turn deadline AFTER
        the placeholder is inserted must pass ``placeholder_message_id``
        into ``_persist_partial_assistant`` so the helper UPDATEs the
        existing placeholder rather than inserting a second assistant
        row."""
        from app.chat.orchestrator import _TURN_DEADLINE_SECONDS  # noqa: F401

        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

        conv = _make_conversation()
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
            return {"results": []}

        mock_exec_tool.side_effect = fake_exec_tool

        assistant_uuid = uuid.uuid4()

        # The LLM emits a tool_use on round 1 (which inserts the
        # placeholder), then hangs on round 2 by sleeping past the
        # turn deadline. We force a TimeoutError directly to avoid a
        # real 120s sleep.
        class HangingToolLLM(LLMProvider):
            def __init__(self):
                self._model = "fake-model"
                self._calls = 0

            def complete(self, prompt, **kw):  # pragma: no cover
                return "x"

            def extract_json(self, prompt, **kw):  # pragma: no cover
                return {}

            def count_tokens(self, text):  # pragma: no cover
                return 0

            async def acomplete(self, prompt, **kw):  # pragma: no cover
                return "x"

            async def astream(self, prompt, **kw):  # type: ignore[override]
                if self._calls == 0:
                    self._calls += 1
                    yield StreamEvent(type="content", delta="Otsin...")
                    yield StreamEvent(
                        type="tool_use",
                        tool_name="search_provisions",
                        tool_input={"keywords": "x"},
                        tool_use_id="toolu_timeout",
                    )
                    yield StreamEvent(type="stop")
                else:
                    # Simulate a stuck upstream by sleeping forever; the
                    # orchestrator's asyncio.wait_for will cancel us.
                    await asyncio.sleep(3600)

        with patch("app.chat.orchestrator.create_message") as mock_create:

            def _fake_create(conn, conv_id, role, content, **kwargs):
                msg_id = assistant_uuid if role == "assistant" else uuid.uuid4()
                return MagicMock(id=msg_id)

            mock_create.side_effect = _fake_create

            # Squeeze the turn deadline so the test runs in <1s.
            with patch("app.chat.orchestrator._TURN_DEADLINE_SECONDS", 0.05):
                orchestrator = ChatOrchestrator(HangingToolLLM(), _FakeSparql())
                collector = _Collector()
                asyncio.run(
                    orchestrator.handle_message(
                        _CONV_ID, "Otsi", {"id": _USER_ID, "org_id": _ORG_ID}, collector
                    )
                )

        # _persist_partial_assistant must have been called from the
        # timeout path with the placeholder id forwarded.
        assert mock_persist.called, "the timeout path must call _persist_partial_assistant"
        call_kwargs = mock_persist.call_args.kwargs
        assert call_kwargs.get("placeholder_message_id") == assistant_uuid, (
            "Fix 2: the timeout path must forward the placeholder id so the "
            "partial-persist UPDATEs the existing assistant row instead of "
            "inserting a sibling. Got "
            f"placeholder_message_id={call_kwargs.get('placeholder_message_id')!r}, "
            f"expected={assistant_uuid!r}"
        )

    @patch("app.chat.orchestrator._update_assistant_payload")
    @patch("app.chat.orchestrator.create_message")
    @patch("app.chat.orchestrator.get_connection")
    def test_only_one_row_for_timed_out_tool_turn(
        self, mock_get_conn, mock_create_msg, mock_update_payload
    ):
        """End-to-end count: when a tool turn times out after the
        placeholder is inserted, the partial-persist path must NOT call
        ``create_message`` again. Otherwise the conversation ends up with
        TWO assistant rows (the original empty placeholder + a new
        partial sibling) for one logical turn — the exact double-insert
        bug Fix 2 addresses.
        """
        from app.chat.orchestrator import _persist_partial_assistant

        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

        # Simulate the orchestrator-level flow: the placeholder was
        # already inserted earlier in the tool-turn pre-insert step
        # (mocked away here; we just hold its id). Now the timeout path
        # calls _persist_partial_assistant with the placeholder id.
        placeholder_id = uuid.uuid4()

        result = _persist_partial_assistant(
            _CONV_ID,
            "osaline...",
            "fake-model",
            None,
            is_truncated=True,
            error_suffix=" [Viga: aegus]",
            tokens_input=10,
            tokens_output=5,
            placeholder_message_id=placeholder_id,
        )

        assert result == placeholder_id
        # Crucial assertion: NO new assistant INSERT happened during the
        # partial-persist path. Pre-fix this method always called
        # create_message, producing the double-insert.
        mock_create_msg.assert_not_called()
        # And the UPDATE on the placeholder did happen.
        mock_update_payload.assert_called_once()
        positional = mock_update_payload.call_args.args
        assert positional[1] == placeholder_id

    @patch("app.chat.orchestrator._update_assistant_payload")
    @patch("app.chat.orchestrator.create_message")
    @patch("app.chat.orchestrator.get_connection")
    def test_empty_content_with_placeholder_deletes_row(
        self, mock_get_conn, mock_create_msg, mock_update_payload
    ):
        """Post-review regression: when a tool turn is interrupted
        (CancelledError / WebSocketSendTimeout / Exception) BEFORE any
        text has been streamed, ``_persist_partial_assistant`` used to
        early-return at ``if not full_content and not error_suffix``,
        leaving the up-front placeholder row as an empty assistant
        bubble forever in conversation history.

        The fix: when called with ``placeholder_message_id`` set but no
        content and no error_suffix, the helper must DELETE the
        placeholder row instead of returning silently. Non-tool turns
        (no placeholder) still just return None — nothing was inserted
        for them to begin with.
        """
        from app.chat.orchestrator import _persist_partial_assistant

        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

        placeholder_id = uuid.uuid4()

        result = _persist_partial_assistant(
            _CONV_ID,
            "",  # No content streamed before the interrupt.
            "fake-model",
            None,
            is_truncated=True,
            error_suffix=None,
            tokens_input=None,
            tokens_output=None,
            placeholder_message_id=placeholder_id,
        )

        assert result is None, "When there is nothing to persist the helper must still return None"
        # No new INSERT and no UPDATE — nothing to write.
        mock_create_msg.assert_not_called()
        mock_update_payload.assert_not_called()
        # Critical: the orphan placeholder row must be deleted so the
        # conversation history doesn't keep an empty assistant bubble.
        delete_calls = [
            call
            for call in conn.execute.call_args_list
            if call.args and "DELETE FROM messages" in call.args[0]
        ]
        assert len(delete_calls) == 1, (
            "The empty placeholder must be DELETEd exactly once. "
            f"Got execute calls: {conn.execute.call_args_list}"
        )
        # The DELETE must target the placeholder id we passed in.
        assert delete_calls[0].args[1] == (str(placeholder_id),)
        conn.commit.assert_called_once()

    @patch("app.chat.orchestrator._update_assistant_payload")
    @patch("app.chat.orchestrator.create_message")
    @patch("app.chat.orchestrator.get_connection")
    def test_empty_content_without_placeholder_is_still_noop(
        self, mock_get_conn, mock_create_msg, mock_update_payload
    ):
        """Backwards-compat partner to the DELETE regression above:
        non-tool turns (no placeholder) with no content and no
        error_suffix must remain a pure noop — no DB access at all.
        """
        from app.chat.orchestrator import _persist_partial_assistant

        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

        result = _persist_partial_assistant(
            _CONV_ID,
            "",
            "fake-model",
            None,
            is_truncated=True,
            error_suffix=None,
            tokens_input=None,
            tokens_output=None,
            placeholder_message_id=None,
        )

        assert result is None
        mock_create_msg.assert_not_called()
        mock_update_payload.assert_not_called()
        # No connection should have been opened — pure early return.
        mock_get_conn.assert_not_called()
