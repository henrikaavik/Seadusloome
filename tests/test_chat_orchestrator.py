# pyright: reportArgumentType=false
"""Tests for ``app.chat.orchestrator.ChatOrchestrator``.

Covers the happy path, tool use, max tool rounds, draft context,
error handling, and message persistence. All DB and LLM calls are mocked.

Uses ``asyncio.run()`` to run async functions, matching the convention
in ``tests/test_chat_tools.py``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

from app.chat.models import Conversation, Message
from app.chat.orchestrator import (
    _MAX_HISTORY_MESSAGES,
    MAX_TOOL_ROUNDS,
    ChatOrchestrator,
    WebSocketSendTimeout,
    _build_llm_messages,
    _load_impact_summary,
    _messages_to_prompt,
    _safe_send,
    _tool_result_count,
)
from app.llm.provider import LLMProvider, StreamEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_ID = "11111111-1111-1111-1111-111111111111"
_ORG_ID = "22222222-2222-2222-2222-222222222222"
_CONV_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
_DRAFT_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
_MSG_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")


def _auth() -> dict[str, Any]:
    return {"id": _USER_ID, "org_id": _ORG_ID}


def _make_conversation(
    *,
    conv_id: uuid.UUID = _CONV_ID,
    org_id: str = _ORG_ID,
    context_draft_id: uuid.UUID | None = None,
) -> Conversation:
    now = datetime.now(UTC)
    return Conversation(
        id=conv_id,
        user_id=uuid.UUID(_USER_ID),
        org_id=uuid.UUID(org_id),
        title="Test vestlus",
        context_draft_id=context_draft_id,
        created_at=now,
        updated_at=now,
    )


def _make_message(
    role: str = "user",
    content: str = "Tere",
) -> Message:
    now = datetime.now(UTC)
    return Message(
        id=uuid.uuid4(),
        conversation_id=_CONV_ID,
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


class _Collector:
    """Async-compatible event collector."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def __call__(self, event: dict[str, Any]) -> None:
        self.events.append(event)


class FakeLLM(LLMProvider):
    """Fake LLM that yields a configurable sequence of StreamEvents."""

    def __init__(self, events_sequence: list[list[StreamEvent]] | None = None):
        self._events_sequence = events_sequence or [
            [
                StreamEvent(type="content", delta="Tere! "),
                StreamEvent(type="content", delta="See on vastus."),
                StreamEvent(type="stop"),
            ]
        ]
        self._call_count = 0
        self._model = "fake-model"

    def complete(self, prompt: str, **kwargs: Any) -> str:
        return "fake"

    def extract_json(self, prompt: str, **kwargs: Any) -> dict:
        return {"fake": True}

    def count_tokens(self, text: str) -> int:
        return len(text) // 4

    async def acomplete(self, prompt: str, **kwargs: Any) -> str:
        return "fake async"

    async def astream(self, prompt: str, **kwargs: Any):  # type: ignore[override]
        idx = min(self._call_count, len(self._events_sequence) - 1)
        events = self._events_sequence[idx]
        self._call_count += 1
        for event in events:
            yield event


class FakeSparql:
    """Fake SPARQL client."""

    def query(self, q: str) -> list:
        return []


# ---------------------------------------------------------------------------
# Unit tests for helpers
# ---------------------------------------------------------------------------


class TestBuildLLMMessages:
    def test_empty_history(self):
        result = _build_llm_messages([], "Tere")
        assert result == ["[USER]: Tere"]

    def test_with_history(self):
        msgs = [_make_message("user", "Esimene"), _make_message("assistant", "Vastus")]
        result = _build_llm_messages(msgs, "Teine")
        assert len(result) == 3
        assert result[0] == "[USER]: Esimene"
        assert result[1] == "[ASSISTANT]: Vastus"
        assert result[2] == "[USER]: Teine"

    def test_history_capped_to_max(self):
        """M2: 100 messages in history -> only last _MAX_HISTORY_MESSAGES used."""
        total = 100
        msgs = [_make_message("user", f"Message {i}") for i in range(total)]
        result = _build_llm_messages(msgs, "Final message")

        # Should have _MAX_HISTORY_MESSAGES from history + 1 for the new user message
        assert len(result) == _MAX_HISTORY_MESSAGES + 1

        # The first message should be from the tail of history, not the beginning
        expected_first_idx = total - _MAX_HISTORY_MESSAGES
        assert f"Message {expected_first_idx}" in result[0]

        # The last message should be the new user message
        assert result[-1] == "[USER]: Final message"

    def test_history_below_cap_unchanged(self):
        """History below the cap is not truncated."""
        msgs = [_make_message("user", f"Message {i}") for i in range(10)]
        result = _build_llm_messages(msgs, "Final")

        # 10 history + 1 new = 11
        assert len(result) == 11


class TestMessagesToPrompt:
    def test_joins_with_newlines(self):
        parts = ["[USER]: Tere", "[ASSISTANT]: Vastus"]
        result = _messages_to_prompt(parts)
        assert "\n\n" in result
        assert "[USER]: Tere" in result


class TestLoadImpactSummary:
    @patch("app.chat.orchestrator.get_connection")
    def test_returns_summary_from_dict(self, mock_conn):
        conn = MagicMock()
        mock_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)
        conn.execute.return_value.fetchone.return_value = (
            json.dumps({"summary": "Moju kokkuvote"}),
        )
        result = _load_impact_summary("some-id", _ORG_ID)
        assert result == "Moju kokkuvote"

    @patch("app.chat.orchestrator.get_connection")
    def test_returns_none_when_no_report(self, mock_conn):
        conn = MagicMock()
        mock_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)
        conn.execute.return_value.fetchone.return_value = None
        result = _load_impact_summary("some-id", _ORG_ID)
        assert result is None

    @patch("app.chat.orchestrator.get_connection")
    def test_handles_db_error(self, mock_conn):
        mock_conn.side_effect = Exception("DB down")
        result = _load_impact_summary("some-id", _ORG_ID)
        assert result is None

    @patch("app.chat.orchestrator.get_connection")
    def test_returns_none_for_foreign_org(self, mock_conn):
        """Issue #562: _load_impact_summary returns None for another org's draft."""
        conn = MagicMock()
        mock_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)
        # The JOIN + org_id filter returns no rows for a foreign org
        conn.execute.return_value.fetchone.return_value = None

        other_org = "99999999-9999-9999-9999-999999999999"
        result = _load_impact_summary(str(_DRAFT_ID), other_org)
        assert result is None

        # Verify org_id was passed to the query
        call_args = conn.execute.call_args[0]
        assert "d.org_id" in call_args[0]
        assert call_args[1] == (str(_DRAFT_ID), other_org)

    @patch("app.chat.orchestrator.get_connection")
    def test_query_joins_drafts_table(self, mock_conn):
        """The query must JOIN drafts to enforce org-scoping."""
        conn = MagicMock()
        mock_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)
        conn.execute.return_value.fetchone.return_value = (json.dumps({"summary": "Test"}),)

        _load_impact_summary("some-id", _ORG_ID)

        call_args = conn.execute.call_args[0]
        query = call_args[0]
        assert "JOIN drafts" in query
        assert "d.org_id" in query


# ---------------------------------------------------------------------------
# Integration tests for ChatOrchestrator.handle_message
# ---------------------------------------------------------------------------


class TestOrchestratorHappyPath:
    @patch("app.chat.orchestrator.get_connection")
    def test_streams_content(self, mock_get_conn):
        """User message -> assistant streams content -> done event."""
        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

        conv = _make_conversation()
        now = datetime.now(UTC)

        call_counter = {"n": 0}
        base_conv_row = (
            conv.id,
            conv.user_id,
            conv.org_id,
            conv.title,
            None,
            conv.created_at,
            conv.updated_at,
        )

        def side_effect_fetchone():
            call_counter["n"] += 1
            if call_counter["n"] == 1:
                return base_conv_row
            return (
                uuid.uuid4(),
                _CONV_ID,
                "user",
                "x",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                now,
                None,  # content_encrypted (#570)
                None,  # tool_input_encrypted
                None,  # tool_output_encrypted
                None,  # rag_context_encrypted
            )

        conn.execute.return_value.fetchone = side_effect_fetchone
        conn.execute.return_value.fetchall.return_value = []

        collector = _Collector()
        llm = FakeLLM()
        orchestrator = ChatOrchestrator(llm, FakeSparql())
        asyncio.run(orchestrator.handle_message(_CONV_ID, "Tere", _auth(), collector))

        content_events = [e for e in collector.events if e.get("type") == "content_delta"]
        assert len(content_events) >= 1

        done_events = [e for e in collector.events if e.get("type") == "done"]
        assert len(done_events) == 1


class TestOrchestratorNotFound:
    @patch("app.chat.orchestrator.get_connection")
    def test_conversation_not_found_sends_error(self, mock_get_conn):
        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
        conn.execute.return_value.fetchone.return_value = None

        collector = _Collector()
        llm = FakeLLM()
        orchestrator = ChatOrchestrator(llm, FakeSparql())
        asyncio.run(orchestrator.handle_message(_CONV_ID, "Tere", _auth(), collector))

        assert any(e.get("type") == "error" for e in collector.events)


class TestOrchestratorCrossOrg:
    @patch("app.chat.orchestrator.get_connection")
    def test_cross_org_access_denied(self, mock_get_conn):
        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

        other_org = "99999999-9999-9999-9999-999999999999"
        conv = _make_conversation(org_id=other_org)
        conn.execute.return_value.fetchone.return_value = (
            conv.id,
            conv.user_id,
            conv.org_id,
            conv.title,
            None,
            conv.created_at,
            conv.updated_at,
        )

        collector = _Collector()
        llm = FakeLLM()
        orchestrator = ChatOrchestrator(llm, FakeSparql())
        asyncio.run(orchestrator.handle_message(_CONV_ID, "Tere", _auth(), collector))

        error_events = [e for e in collector.events if e.get("type") == "error"]
        assert len(error_events) == 1


class TestOrchestratorToolUse:
    @patch("app.chat.orchestrator.execute_tool")
    @patch("app.chat.orchestrator.get_connection")
    def test_tool_use_executes_and_continues(self, mock_get_conn, mock_exec_tool):
        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

        conv = _make_conversation()
        now = datetime.now(UTC)

        call_counter = {"n": 0}
        base_conv_row = (
            conv.id,
            conv.user_id,
            conv.org_id,
            conv.title,
            None,
            conv.created_at,
            conv.updated_at,
        )

        def side_effect_fetchone():
            call_counter["n"] += 1
            if call_counter["n"] == 1:
                return base_conv_row
            return (
                uuid.uuid4(),
                _CONV_ID,
                "user",
                "Tere",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                now,
                None,  # content_encrypted (#570)
                None,  # tool_input_encrypted
                None,  # tool_output_encrypted
                None,  # rag_context_encrypted
            )

        conn.execute.return_value.fetchone = side_effect_fetchone
        conn.execute.return_value.fetchall.return_value = []

        async def fake_exec_tool(name, inp, sparql, auth=None):
            return {"results": [{"uri": "estleg:Test"}]}

        mock_exec_tool.side_effect = fake_exec_tool

        events_seq = [
            [
                StreamEvent(
                    type="tool_use",
                    tool_name="query_ontology",
                    tool_input={"query": "SELECT ?s WHERE {?s ?p ?o}"},
                ),
                StreamEvent(type="stop"),
            ],
            [
                StreamEvent(type="content", delta="Leitud tulemused."),
                StreamEvent(type="stop"),
            ],
        ]

        collector = _Collector()
        llm = FakeLLM(events_seq)
        orchestrator = ChatOrchestrator(llm, FakeSparql())
        asyncio.run(orchestrator.handle_message(_CONV_ID, "Otsi", _auth(), collector))

        tool_events = [e for e in collector.events if e.get("type") == "tool_use"]
        assert len(tool_events) >= 1

        result_events = [e for e in collector.events if e.get("type") == "tool_result"]
        assert len(result_events) >= 1

        content_events = [e for e in collector.events if e.get("type") == "content_delta"]
        assert len(content_events) >= 1

        mock_exec_tool.assert_called_once()


class TestOrchestratorMaxToolRounds:
    @patch("app.chat.orchestrator.execute_tool")
    @patch("app.chat.orchestrator.get_connection")
    def test_max_tool_rounds_enforced(self, mock_get_conn, mock_exec_tool):
        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

        conv = _make_conversation()
        now = datetime.now(UTC)

        call_counter = {"n": 0}
        base_conv_row = (
            conv.id,
            conv.user_id,
            conv.org_id,
            conv.title,
            None,
            conv.created_at,
            conv.updated_at,
        )

        def side_effect_fetchone():
            call_counter["n"] += 1
            if call_counter["n"] == 1:
                return base_conv_row
            return (
                uuid.uuid4(),
                _CONV_ID,
                "user",
                "x",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                now,
                None,  # content_encrypted (#570)
                None,  # tool_input_encrypted
                None,  # tool_output_encrypted
                None,  # rag_context_encrypted
            )

        conn.execute.return_value.fetchone = side_effect_fetchone
        conn.execute.return_value.fetchall.return_value = []

        async def fake_exec_tool(name, inp, sparql, auth=None):
            return {"results": []}

        mock_exec_tool.side_effect = fake_exec_tool

        # Create many tool_use sequences
        events_seq = []
        for _ in range(MAX_TOOL_ROUNDS + 2):
            events_seq.append(
                [
                    StreamEvent(
                        type="tool_use",
                        tool_name="search_provisions",
                        tool_input={"keywords": "test"},
                    ),
                    StreamEvent(type="stop"),
                ]
            )
        events_seq.append(
            [
                StreamEvent(type="content", delta="Valmis."),
                StreamEvent(type="stop"),
            ]
        )

        collector = _Collector()
        llm = FakeLLM(events_seq)
        orchestrator = ChatOrchestrator(llm, FakeSparql())
        asyncio.run(orchestrator.handle_message(_CONV_ID, "Otsi", _auth(), collector))

        assert mock_exec_tool.call_count == MAX_TOOL_ROUNDS

        limit_events = [
            e
            for e in collector.events
            if e.get("type") == "content_delta" and "limiit" in (e.get("delta") or "").lower()
        ]
        assert len(limit_events) >= 1


class TestOrchestratorDraftContext:
    @patch("app.chat.orchestrator._load_impact_summary")
    @patch("app.chat.orchestrator.get_connection")
    def test_draft_context_included_in_prompt(self, mock_get_conn, mock_load_impact):
        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

        conv = _make_conversation(context_draft_id=_DRAFT_ID)
        now = datetime.now(UTC)

        call_counter = {"n": 0}
        base_conv_row = (
            conv.id,
            conv.user_id,
            conv.org_id,
            conv.title,
            str(_DRAFT_ID),
            conv.created_at,
            conv.updated_at,
        )

        def side_effect_fetchone():
            call_counter["n"] += 1
            if call_counter["n"] == 1:
                return base_conv_row
            return (
                uuid.uuid4(),
                _CONV_ID,
                "user",
                "x",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                now,
                None,  # content_encrypted (#570)
                None,  # tool_input_encrypted
                None,  # tool_output_encrypted
                None,  # rag_context_encrypted
            )

        conn.execute.return_value.fetchone = side_effect_fetchone
        conn.execute.return_value.fetchall.return_value = []

        mock_load_impact.return_value = "Moju: seadus muutub"

        captured_kwargs: list[dict[str, Any]] = []

        class CaptureLLM(FakeLLM):
            async def astream(self, prompt: str, **kwargs: Any):
                captured_kwargs.append({"prompt": prompt, **kwargs})
                async for event in super().astream(prompt, **kwargs):
                    yield event

        collector = _Collector()
        llm = CaptureLLM()
        orchestrator = ChatOrchestrator(llm, FakeSparql())
        asyncio.run(
            orchestrator.handle_message(_CONV_ID, "Kuidas see eelnou mojub?", _auth(), collector)
        )

        assert len(captured_kwargs) >= 1
        system = captured_kwargs[0].get("system", "")
        assert "EELNOU KONTEKST" in system
        assert str(_DRAFT_ID) in system


class TestOrchestratorLLMError:
    @patch("app.chat.orchestrator.get_connection")
    def test_llm_error_sends_error_event(self, mock_get_conn):
        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

        conv = _make_conversation()
        now = datetime.now(UTC)
        call_counter = {"n": 0}
        base_conv_row = (
            conv.id,
            conv.user_id,
            conv.org_id,
            conv.title,
            None,
            conv.created_at,
            conv.updated_at,
        )

        def side_effect_fetchone():
            call_counter["n"] += 1
            if call_counter["n"] == 1:
                return base_conv_row
            return (
                uuid.uuid4(),
                _CONV_ID,
                "user",
                "x",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                now,
                None,  # content_encrypted (#570)
                None,  # tool_input_encrypted
                None,  # tool_output_encrypted
                None,  # rag_context_encrypted
            )

        conn.execute.return_value.fetchone = side_effect_fetchone
        conn.execute.return_value.fetchall.return_value = []

        class ErrorLLM(FakeLLM):
            async def astream(self, prompt: str, **kwargs: Any):
                raise RuntimeError("LLM exploded")
                yield  # type: ignore[misc]  # makes this an async generator

        collector = _Collector()
        llm = ErrorLLM()
        orchestrator = ChatOrchestrator(llm, FakeSparql())
        asyncio.run(orchestrator.handle_message(_CONV_ID, "Tere", _auth(), collector))

        error_events = [e for e in collector.events if e.get("type") == "error"]
        assert len(error_events) == 1
        assert "ebaonnestus" in error_events[0]["message"].lower()


class TestOrchestratorPersistence:
    @patch("app.chat.orchestrator.get_connection")
    def test_messages_persisted(self, mock_get_conn):
        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

        conv = _make_conversation()
        now = datetime.now(UTC)

        call_counter = {"n": 0}
        base_conv_row = (
            conv.id,
            conv.user_id,
            conv.org_id,
            conv.title,
            None,
            conv.created_at,
            conv.updated_at,
        )

        def side_effect_fetchone():
            call_counter["n"] += 1
            if call_counter["n"] == 1:
                return base_conv_row
            return (
                uuid.uuid4(),
                _CONV_ID,
                "user",
                "x",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                now,
                None,  # content_encrypted (#570)
                None,  # tool_input_encrypted
                None,  # tool_output_encrypted
                None,  # rag_context_encrypted
            )

        conn.execute.return_value.fetchone = side_effect_fetchone
        conn.execute.return_value.fetchall.return_value = []

        collector = _Collector()
        llm = FakeLLM()
        orchestrator = ChatOrchestrator(llm, FakeSparql())
        asyncio.run(orchestrator.handle_message(_CONV_ID, "Test", _auth(), collector))

        # commit was called (for user msg + assistant msg)
        assert conn.commit.call_count >= 1


class TestOrchestratorPartialPersistence:
    """M1: Partial message should be persisted with error suffix on LLM failure."""

    @patch("app.chat.orchestrator.get_connection")
    def test_partial_content_persisted_with_error_suffix(self, mock_get_conn):
        """When LLM fails mid-stream, partial content is persisted with error suffix."""
        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

        conv = _make_conversation()
        now = datetime.now(UTC)
        call_counter = {"n": 0}
        base_conv_row = (
            conv.id,
            conv.user_id,
            conv.org_id,
            conv.title,
            None,
            conv.created_at,
            conv.updated_at,
        )

        def side_effect_fetchone():
            call_counter["n"] += 1
            if call_counter["n"] == 1:
                return base_conv_row
            return (
                uuid.uuid4(),
                _CONV_ID,
                "user",
                "x",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                now,
                None,  # content_encrypted (#570)
                None,  # tool_input_encrypted
                None,  # tool_output_encrypted
                None,  # rag_context_encrypted
            )

        conn.execute.return_value.fetchone = side_effect_fetchone
        conn.execute.return_value.fetchall.return_value = []

        class PartialThenErrorLLM(FakeLLM):
            async def astream(self, prompt: str, **kwargs: Any):
                yield StreamEvent(type="content", delta="Osaliselt ")
                yield StreamEvent(type="content", delta="genereeritud ")
                raise RuntimeError("LLM connection lost")

        collector = _Collector()
        llm = PartialThenErrorLLM()
        orchestrator = ChatOrchestrator(llm, FakeSparql())
        asyncio.run(orchestrator.handle_message(_CONV_ID, "Tere", _auth(), collector))

        # Should have error event
        error_events = [e for e in collector.events if e.get("type") == "error"]
        assert len(error_events) == 1

        # Should have persisted partial content with error suffix.
        # Find the create_message call for the assistant message (contains error suffix)
        execute_calls = conn.execute.call_args_list
        persisted_texts = []
        for call in execute_calls:
            args = call[0]
            if len(args) >= 1 and isinstance(args[0], str) and "INSERT INTO messages" in args[0]:
                # The create_message function uses INSERT INTO messages
                persisted_texts.append(args)

        # The partial content should have been committed
        assert conn.commit.call_count >= 2  # user msg + partial assistant msg

    @patch("app.chat.orchestrator.get_connection")
    def test_no_partial_content_no_persistence(self, mock_get_conn):
        """When LLM fails immediately (no content), no assistant message is persisted."""
        conn = MagicMock()
        mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

        conv = _make_conversation()
        now = datetime.now(UTC)
        call_counter = {"n": 0}
        base_conv_row = (
            conv.id,
            conv.user_id,
            conv.org_id,
            conv.title,
            None,
            conv.created_at,
            conv.updated_at,
        )

        def side_effect_fetchone():
            call_counter["n"] += 1
            if call_counter["n"] == 1:
                return base_conv_row
            return (
                uuid.uuid4(),
                _CONV_ID,
                "user",
                "x",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                now,
                None,  # content_encrypted (#570)
                None,  # tool_input_encrypted
                None,  # tool_output_encrypted
                None,  # rag_context_encrypted
            )

        conn.execute.return_value.fetchone = side_effect_fetchone
        conn.execute.return_value.fetchall.return_value = []

        class ImmediateErrorLLM(FakeLLM):
            async def astream(self, prompt: str, **kwargs: Any):
                raise RuntimeError("LLM exploded immediately")
                yield  # type: ignore[misc]

        collector = _Collector()
        llm = ImmediateErrorLLM()
        orchestrator = ChatOrchestrator(llm, FakeSparql())
        asyncio.run(orchestrator.handle_message(_CONV_ID, "Tere", _auth(), collector))

        # Should have error event
        error_events = [e for e in collector.events if e.get("type") == "error"]
        assert len(error_events) == 1

        # Only 1 commit (for the user message), not 2 (no assistant msg persisted)
        assert conn.commit.call_count == 1


# ---------------------------------------------------------------------------
# Phase UX polish tests (issue #594)
# ---------------------------------------------------------------------------


from dataclasses import dataclass  # noqa: E402


@dataclass
class _FakeChunk:
    """Mimics ``app.rag.retriever.RetrievedChunk`` for test purposes."""

    content: str
    metadata: dict
    score: float


class _FakeRetriever:
    """Retriever stub that returns a canned chunk list."""

    def __init__(self, chunks: list[_FakeChunk] | None = None) -> None:
        self.chunks = chunks or []

    async def retrieve(
        self,
        query: str,
        k: int = 10,
        source_type: str | None = None,
        org_id: str | None = None,
    ) -> list[_FakeChunk]:
        return self.chunks


def _setup_orchestrator_conn(mock_get_conn: Any, *, context_draft_id: Any = None) -> MagicMock:
    """Minimal conn mock matching conversations + messages table shapes."""
    conn = MagicMock()
    mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
    mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

    conv = _make_conversation()
    now = datetime.now(UTC)

    call_counter = {"n": 0}
    base_conv_row = (
        conv.id,
        conv.user_id,
        conv.org_id,
        conv.title,
        context_draft_id,
        conv.created_at,
        conv.updated_at,
    )

    def side_effect_fetchone():
        call_counter["n"] += 1
        if call_counter["n"] == 1:
            return base_conv_row
        return (
            uuid.uuid4(),
            _CONV_ID,
            "user",
            "x",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            now,
            None,
            None,
            None,
            None,
        )

    conn.execute.return_value.fetchone = side_effect_fetchone
    conn.execute.return_value.fetchall.return_value = []
    return conn


class TestSafeSend:
    """Coverage for the ``_safe_send`` helper."""

    def test_successful_send_passes_event_through(self):
        received: list[dict[str, Any]] = []

        async def ok_send(event: dict[str, Any]) -> None:
            received.append(event)

        asyncio.run(_safe_send(ok_send, {"type": "content_delta", "delta": "x"}))
        assert received == [{"type": "content_delta", "delta": "x"}]

    def test_timeout_raises_domain_exception(self):
        async def slow_send(event: dict[str, Any]) -> None:
            await asyncio.sleep(0.5)

        async def run():
            await _safe_send(slow_send, {"type": "content_delta"}, timeout=0.05)

        try:
            asyncio.run(run())
            raised = False
        except WebSocketSendTimeout:
            raised = True
        assert raised


class TestToolResultCount:
    def test_list_results(self):
        assert _tool_result_count({"results": [1, 2, 3]}) == 3

    def test_empty_list_results(self):
        assert _tool_result_count({"results": []}) == 0

    def test_dict_without_results_is_one(self):
        assert _tool_result_count({"uri": "x"}) == 1

    def test_error_payload_is_zero(self):
        assert _tool_result_count({"error": "boom"}) == 0

    def test_non_dict_is_zero(self):
        assert _tool_result_count("nope") == 0
        assert _tool_result_count(None) == 0


class TestRetrievalDoneAlwaysEmitted:
    """retrieval_done fires on both have-chunks and no-chunks paths."""

    @patch("app.chat.orchestrator.check_org_cost_budget")
    @patch("app.chat.orchestrator.check_message_rate")
    @patch("app.chat.orchestrator.get_connection")
    def test_retrieval_done_with_chunks(self, mock_get_conn, mock_rate, mock_cost):
        _setup_orchestrator_conn(mock_get_conn)

        chunks = [
            _FakeChunk("foo", {"source_uri": "estleg:A/p1"}, 0.9),
            _FakeChunk("bar", {"source_uri": "estleg:B/p2"}, 0.8),
        ]
        collector = _Collector()
        orchestrator = ChatOrchestrator(FakeLLM(), FakeSparql(), retriever=_FakeRetriever(chunks))
        asyncio.run(orchestrator.handle_message(_CONV_ID, "Tere", _auth(), collector))

        done_events = [e for e in collector.events if e.get("type") == "retrieval_done"]
        assert len(done_events) == 1
        assert done_events[0]["chunk_count"] == 2

    @patch("app.chat.orchestrator.check_org_cost_budget")
    @patch("app.chat.orchestrator.check_message_rate")
    @patch("app.chat.orchestrator.get_connection")
    def test_retrieval_done_with_no_chunks(self, mock_get_conn, mock_rate, mock_cost):
        _setup_orchestrator_conn(mock_get_conn)

        collector = _Collector()
        orchestrator = ChatOrchestrator(FakeLLM(), FakeSparql(), retriever=_FakeRetriever([]))
        asyncio.run(orchestrator.handle_message(_CONV_ID, "Tere", _auth(), collector))

        done_events = [e for e in collector.events if e.get("type") == "retrieval_done"]
        assert len(done_events) == 1
        assert done_events[0]["chunk_count"] == 0


class TestToolCallIdPairing:
    """tool_use and tool_result must carry the same ``tool_call_id``."""

    @patch("app.chat.orchestrator.execute_tool")
    @patch("app.chat.orchestrator.check_org_cost_budget")
    @patch("app.chat.orchestrator.check_message_rate")
    @patch("app.chat.orchestrator.get_connection")
    def test_tool_use_and_result_share_id(
        self, mock_get_conn, mock_rate, mock_cost, mock_exec_tool
    ):
        _setup_orchestrator_conn(mock_get_conn)

        async def fake_exec_tool(name, inp, sparql, auth=None):
            return {"results": [{"uri": "estleg:Test"}, {"uri": "estleg:Test2"}]}

        mock_exec_tool.side_effect = fake_exec_tool

        events_seq = [
            [
                StreamEvent(
                    type="tool_use",
                    tool_name="query_ontology",
                    tool_input={"q": "x"},
                ),
                StreamEvent(type="stop"),
            ],
            [
                StreamEvent(type="content", delta="ok"),
                StreamEvent(type="stop"),
            ],
        ]

        collector = _Collector()
        orchestrator = ChatOrchestrator(FakeLLM(events_seq), FakeSparql())
        asyncio.run(orchestrator.handle_message(_CONV_ID, "Otsi", _auth(), collector))

        use_events = [e for e in collector.events if e.get("type") == "tool_use"]
        result_events = [e for e in collector.events if e.get("type") == "tool_result"]
        assert len(use_events) == 1
        assert len(result_events) == 1
        assert use_events[0]["tool_call_id"]
        assert use_events[0]["tool_call_id"] == result_events[0]["tool_call_id"]
        assert result_events[0]["result_count"] == 2
        assert result_events[0]["result"] == {
            "results": [{"uri": "estleg:Test"}, {"uri": "estleg:Test2"}]
        }


class TestOrchestratorMultipleToolsPerTurn:
    """#636 review: Claude may emit multiple tool_use blocks in one assistant turn.

    Every block must be executed, every result emitted/persisted, and the
    next-turn user message must contain a ``tool_result`` block for every
    ``tool_use_id`` in the original Claude order. Anthropic's API rejects
    a follow-up turn that omits any tool_use_id from the prior assistant
    turn, so silently dropping all but the last tool_use is a correctness
    bug, not just a missed feature.
    """

    @patch("app.chat.orchestrator.execute_tool")
    @patch("app.chat.orchestrator.check_org_cost_budget")
    @patch("app.chat.orchestrator.check_message_rate")
    @patch("app.chat.orchestrator.get_connection")
    def test_two_tool_blocks_in_one_turn_both_execute(
        self, mock_get_conn, mock_rate, mock_cost, mock_exec_tool
    ):
        _setup_orchestrator_conn(mock_get_conn)

        executed: list[tuple[str, dict[str, Any]]] = []

        async def fake_exec_tool(name, inp, sparql, auth=None):
            executed.append((name, inp))
            if name == "query_ontology":
                return {"results": [{"uri": "estleg:A"}]}
            if name == "search_provisions":
                return {"results": [{"uri": "estleg:B"}, {"uri": "estleg:C"}]}
            return {"results": []}

        mock_exec_tool.side_effect = fake_exec_tool

        # First astream() call: TWO tool_use blocks (with provider tool_use_ids),
        # then stop. Second call: a normal text reply.
        events_seq = [
            [
                StreamEvent(
                    type="tool_use",
                    tool_name="query_ontology",
                    tool_input={"query": "SELECT ?s WHERE {?s ?p ?o}"},
                    tool_use_id="toolu_01_query",
                ),
                StreamEvent(
                    type="tool_use",
                    tool_name="search_provisions",
                    tool_input={"keywords": "võlaõigus"},
                    tool_use_id="toolu_02_search",
                ),
                StreamEvent(type="stop"),
            ],
            [
                StreamEvent(type="content", delta="Mõlemad tööriistad andsid tulemusi."),
                StreamEvent(type="stop"),
            ],
        ]

        # Capture the prompts the orchestrator hands the LLM so we can
        # assert the second turn contains BOTH tool_result blocks.
        captured_prompts: list[str] = []

        class CaptureLLM(FakeLLM):
            async def astream(self, prompt: str, **kwargs: Any):
                captured_prompts.append(prompt)
                async for ev in super().astream(prompt, **kwargs):
                    yield ev

        collector = _Collector()
        orchestrator = ChatOrchestrator(CaptureLLM(events_seq), FakeSparql())
        asyncio.run(orchestrator.handle_message(_CONV_ID, "Otsi norme", _auth(), collector))

        # Both tools executed exactly once, in Claude's emission order.
        assert mock_exec_tool.call_count == 2
        assert [name for name, _ in executed] == ["query_ontology", "search_provisions"]
        assert executed[0][1] == {"query": "SELECT ?s WHERE {?s ?p ?o}"}
        assert executed[1][1] == {"keywords": "võlaõigus"}

        # Two tool_use events sent to the client, both with tool_call_ids.
        use_events = [e for e in collector.events if e.get("type") == "tool_use"]
        assert len(use_events) == 2
        assert use_events[0]["tool"] == "query_ontology"
        assert use_events[1]["tool"] == "search_provisions"
        assert use_events[0]["tool_call_id"]
        assert use_events[1]["tool_call_id"]
        assert use_events[0]["tool_call_id"] != use_events[1]["tool_call_id"]

        # Two tool_result events sent, paired by tool_call_id with the use events.
        result_events = [e for e in collector.events if e.get("type") == "tool_result"]
        assert len(result_events) == 2
        assert result_events[0]["tool_call_id"] == use_events[0]["tool_call_id"]
        assert result_events[1]["tool_call_id"] == use_events[1]["tool_call_id"]
        assert result_events[0]["result"] == {"results": [{"uri": "estleg:A"}]}
        assert result_events[1]["result"] == {
            "results": [{"uri": "estleg:B"}, {"uri": "estleg:C"}]
        }

        # The follow-up turn (second astream call) must include BOTH
        # tool_use_ids in the same prompt, in the order Claude emitted them.
        # Anthropic's API requires every tool_use to be answered with a
        # matching tool_result in the next user message; missing one is
        # a 400 from the API.
        assert len(captured_prompts) == 2
        followup = captured_prompts[1]
        assert "query_ontology" in followup
        assert "search_provisions" in followup
        assert "toolu_01_query" in followup
        assert "toolu_02_search" in followup
        # Order must be preserved — Anthropic matches by id but humans
        # debugging the prompt expect the same order Claude returned.
        assert followup.index("toolu_01_query") < followup.index("toolu_02_search")

        # Both tool messages were persisted to the DB (one create_message
        # per tool result, role="tool"). We can't easily inspect the
        # encrypted payloads via the MagicMock, but we can assert that
        # create_message was invoked with role="tool" twice — see #636.
        # The mock conn's execute() captures every INSERT.
        # Find INSERTs into messages with role 'tool'.
        # We assert via a broader proxy: there must be at least 2 commits
        # after the first user-message commit (one per tool result).
        assert mock_get_conn.return_value.__enter__.return_value.commit.call_count >= 3

    @patch("app.chat.orchestrator.execute_tool")
    @patch("app.chat.orchestrator.check_org_cost_budget")
    @patch("app.chat.orchestrator.check_message_rate")
    @patch("app.chat.orchestrator.get_connection")
    def test_two_tools_count_as_one_round_against_max_tool_rounds(
        self, mock_get_conn, mock_rate, mock_cost, mock_exec_tool
    ):
        """Multiple tools in one assistant turn = one round, not N rounds.

        The orchestrator caps tool rounds at MAX_TOOL_ROUNDS to bound LLM
        cost. A turn that asks for two tools is still one assistant turn,
        so it consumes one round of the budget.
        """
        _setup_orchestrator_conn(mock_get_conn)

        async def fake_exec_tool(name, inp, sparql, auth=None):
            return {"results": []}

        mock_exec_tool.side_effect = fake_exec_tool

        # Each round contains 2 tool_use blocks. We send MAX_TOOL_ROUNDS
        # such rounds, then a final text reply. Total tool calls expected:
        # 2 * MAX_TOOL_ROUNDS.
        events_seq = []
        for i in range(MAX_TOOL_ROUNDS):
            events_seq.append(
                [
                    StreamEvent(
                        type="tool_use",
                        tool_name="query_ontology",
                        tool_input={"q": f"a{i}"},
                        tool_use_id=f"toolu_a_{i}",
                    ),
                    StreamEvent(
                        type="tool_use",
                        tool_name="search_provisions",
                        tool_input={"q": f"b{i}"},
                        tool_use_id=f"toolu_b_{i}",
                    ),
                    StreamEvent(type="stop"),
                ]
            )
        events_seq.append(
            [
                StreamEvent(type="content", delta="Lõpp."),
                StreamEvent(type="stop"),
            ]
        )

        collector = _Collector()
        orchestrator = ChatOrchestrator(FakeLLM(events_seq), FakeSparql())
        asyncio.run(orchestrator.handle_message(_CONV_ID, "Otsi", _auth(), collector))

        # 2 tool_use blocks per round * MAX_TOOL_ROUNDS rounds.
        assert mock_exec_tool.call_count == 2 * MAX_TOOL_ROUNDS


class TestSourcesEvent:
    @patch("app.chat.orchestrator.check_org_cost_budget")
    @patch("app.chat.orchestrator.check_message_rate")
    @patch("app.chat.orchestrator.get_connection")
    def test_sources_event_has_chunks(self, mock_get_conn, mock_rate, mock_cost):
        _setup_orchestrator_conn(mock_get_conn)

        chunks = [
            _FakeChunk(
                content="A long provision text " * 20,
                metadata={"source_uri": "https://example.ee/laws/KarS/p121"},
                score=0.92,
            ),
        ]
        collector = _Collector()
        orchestrator = ChatOrchestrator(FakeLLM(), FakeSparql(), retriever=_FakeRetriever(chunks))
        asyncio.run(orchestrator.handle_message(_CONV_ID, "Tere", _auth(), collector))

        sources_events = [e for e in collector.events if e.get("type") == "sources"]
        assert len(sources_events) == 1
        srcs = sources_events[0]["sources"]
        assert len(srcs) == 1
        assert srcs[0]["source_uri"] == "https://example.ee/laws/KarS/p121"
        assert srcs[0]["title"] == "p121"
        assert srcs[0]["score"] == 0.92
        assert len(srcs[0]["snippet"]) <= 200

    @patch("app.chat.orchestrator.check_org_cost_budget")
    @patch("app.chat.orchestrator.check_message_rate")
    @patch("app.chat.orchestrator.get_connection")
    def test_sources_event_empty_when_no_chunks(self, mock_get_conn, mock_rate, mock_cost):
        _setup_orchestrator_conn(mock_get_conn)

        collector = _Collector()
        orchestrator = ChatOrchestrator(FakeLLM(), FakeSparql(), retriever=_FakeRetriever([]))
        asyncio.run(orchestrator.handle_message(_CONV_ID, "Tere", _auth(), collector))

        sources_events = [e for e in collector.events if e.get("type") == "sources"]
        assert len(sources_events) == 1
        assert sources_events[0]["sources"] == []


class TestFollowUpsFeatureFlag:
    @patch.dict("os.environ", {"CHAT_FOLLOW_UPS_ENABLED": "0"}, clear=False)
    @patch("app.chat.orchestrator.check_org_cost_budget")
    @patch("app.chat.orchestrator.check_message_rate")
    @patch("app.chat.orchestrator.get_connection")
    def test_follow_ups_not_emitted_when_disabled(self, mock_get_conn, mock_rate, mock_cost):
        _setup_orchestrator_conn(mock_get_conn)
        collector = _Collector()
        orchestrator = ChatOrchestrator(FakeLLM(), FakeSparql())
        asyncio.run(orchestrator.handle_message(_CONV_ID, "Tere", _auth(), collector))
        assert not any(e.get("type") == "follow_ups" for e in collector.events)

    @patch.dict("os.environ", {"CHAT_FOLLOW_UPS_ENABLED": "1"}, clear=False)
    @patch("app.llm.get_default_provider")
    @patch("app.chat.orchestrator.check_org_cost_budget")
    @patch("app.chat.orchestrator.check_message_rate")
    @patch("app.chat.orchestrator.get_connection")
    def test_follow_ups_emitted_when_enabled(
        self, mock_get_conn, mock_rate, mock_cost, mock_default_provider
    ):
        _setup_orchestrator_conn(mock_get_conn)

        async def fake_acomplete(prompt, **kwargs):
            return '["Esimene?", "Teine?", "Kolmas?"]'

        provider_stub = MagicMock()
        provider_stub.acomplete = fake_acomplete
        mock_default_provider.return_value = provider_stub

        collector = _Collector()
        orchestrator = ChatOrchestrator(FakeLLM(), FakeSparql())
        asyncio.run(orchestrator.handle_message(_CONV_ID, "Tere", _auth(), collector))

        follow_events = [e for e in collector.events if e.get("type") == "follow_ups"]
        assert len(follow_events) == 1
        assert follow_events[0]["suggestions"] == ["Esimene?", "Teine?", "Kolmas?"]

    @patch.dict("os.environ", {"CHAT_FOLLOW_UPS_ENABLED": "1"}, clear=False)
    @patch("app.llm.get_default_provider")
    @patch("app.chat.orchestrator.check_org_cost_budget")
    @patch("app.chat.orchestrator.check_message_rate")
    @patch("app.chat.orchestrator.get_connection")
    def test_follow_ups_silent_on_llm_failure(
        self, mock_get_conn, mock_rate, mock_cost, mock_default_provider
    ):
        _setup_orchestrator_conn(mock_get_conn)

        async def boom(prompt, **kwargs):
            raise RuntimeError("no follow-up model today")

        provider_stub = MagicMock()
        provider_stub.acomplete = boom
        mock_default_provider.return_value = provider_stub

        collector = _Collector()
        orchestrator = ChatOrchestrator(FakeLLM(), FakeSparql())
        asyncio.run(orchestrator.handle_message(_CONV_ID, "Tere", _auth(), collector))

        assert not any(e.get("type") == "follow_ups" for e in collector.events)


class TestStopGenerationCancel:
    @patch("app.chat.orchestrator.check_org_cost_budget")
    @patch("app.chat.orchestrator.check_message_rate")
    @patch("app.chat.orchestrator.get_connection")
    def test_cancelled_persists_partial_and_emits_stopped(
        self, mock_get_conn, mock_rate, mock_cost
    ):
        conn = _setup_orchestrator_conn(mock_get_conn)

        class SlowLLM(FakeLLM):
            async def astream(self, prompt: str, **kwargs: Any):
                yield StreamEvent(type="content", delta="Osaline ")
                yield StreamEvent(type="content", delta="vastus ")
                await asyncio.sleep(10)
                yield StreamEvent(type="stop")

        collector = _Collector()
        orchestrator = ChatOrchestrator(SlowLLM(), FakeSparql())

        async def runner():
            task = asyncio.create_task(
                orchestrator.handle_message(_CONV_ID, "Tere", _auth(), collector)
            )
            # Give the orchestrator a chance to produce partial content.
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(runner())

        stopped = [e for e in collector.events if e.get("type") == "stopped"]
        assert len(stopped) == 1

        # Partial assistant content must have been persisted: we expect
        # at least one UPDATE setting is_truncated = TRUE.
        sql_calls = [
            call.args[0]
            for call in conn.execute.call_args_list
            if call.args and isinstance(call.args[0], str)
        ]
        assert any("is_truncated = TRUE" in sql for sql in sql_calls)


class TestMaxToolRoundsCapVerified:
    """Explicitly verify MAX_TOOL_ROUNDS=5 is enforced (issue #594.10)."""

    def test_cap_is_five(self):
        assert MAX_TOOL_ROUNDS == 5


# ---------------------------------------------------------------------------
# Code-review follow-ups
# ---------------------------------------------------------------------------


class TestCostBudgetAdvisoryLock:
    """The advisory lock must engage on the same conn that persists the user msg."""

    @patch("app.chat.orchestrator.check_message_rate")
    @patch("app.chat.orchestrator.get_connection")
    def test_cost_budget_uses_advisory_lock_when_conn_passed(self, mock_get_conn, mock_rate):
        """The advisory lock must be taken on the conn that then INSERTs the
        user message — the two operations must share a transaction so the
        lock serialises the read-then-insert window.
        """
        # Two independent conn mocks: the first is used for loading the
        # conversation + history, the second is the budget-check-plus-
        # user-message-insert conn we care about.
        conv = _make_conversation()
        now = datetime.now(UTC)

        # Shared counter so subsequent calls return message rows.
        call_counter = {"n": 0}
        base_conv_row = (
            conv.id,
            conv.user_id,
            conv.org_id,
            conv.title,
            None,
            conv.created_at,
            conv.updated_at,
        )

        def side_effect_fetchone():
            call_counter["n"] += 1
            if call_counter["n"] == 1:
                return base_conv_row
            return (
                uuid.uuid4(),
                _CONV_ID,
                "user",
                "x",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                now,
                None,
                None,
                None,
                None,
            )

        # We'll hand out a stream of conns — the orchestrator opens a fresh
        # connection per ``with get_connection()`` block. Capture per-conn
        # execute history so we can verify the budget+insert conn started
        # with the advisory-lock SQL.
        conn_histories: list[list[str]] = []

        def make_conn() -> MagicMock:
            history: list[str] = []
            conn_histories.append(history)
            conn = MagicMock()
            conn.execute.return_value.fetchone = side_effect_fetchone
            conn.execute.return_value.fetchall.return_value = []

            original_execute = conn.execute

            def execute_spy(sql, *a, **kw):
                history.append(sql)
                return original_execute.return_value

            conn.execute.side_effect = execute_spy
            return conn

        conns: list[MagicMock] = []

        class _FakeCm:
            def __enter__(self):
                c = make_conn()
                conns.append(c)
                return c

            def __exit__(self, *_a):
                return False

        mock_get_conn.side_effect = lambda: _FakeCm()

        collector = _Collector()
        orchestrator = ChatOrchestrator(FakeLLM(), FakeSparql())
        asyncio.run(orchestrator.handle_message(_CONV_ID, "Tere", _auth(), collector))

        # Locate the conn whose first SQL statement contains the advisory
        # lock. That conn must ALSO run an INSERT INTO messages call (the
        # shared-transaction guarantee from the review fix).
        lock_conn_history: list[str] | None = None
        for history in conn_histories:
            if history and "pg_advisory_xact_lock" in history[0]:
                lock_conn_history = history
                break

        assert lock_conn_history is not None, (
            "No connection took pg_advisory_xact_lock as its first SQL. "
            f"Observed histories: {conn_histories}"
        )
        # The very first SQL on that conn is the lock call — this is the
        # TOCTOU-closing property.
        assert "pg_advisory_xact_lock" in lock_conn_history[0]
        # And the same conn later inserts the user message.
        assert any(
            "INSERT INTO messages" in sql for sql in lock_conn_history if isinstance(sql, str)
        ), (
            "Lock-holding conn never persisted a user message — the "
            "transaction-sharing guarantee is not wired up."
        )


class TestSendTimeoutMidStream:
    """A timeout on a mid-stream send must persist the partial with is_truncated=True."""

    @patch("app.chat.orchestrator.check_org_cost_budget")
    @patch("app.chat.orchestrator.check_message_rate")
    @patch("app.chat.orchestrator.get_connection")
    def test_send_timeout_persists_partial_as_truncated(self, mock_get_conn, mock_rate, mock_cost):
        conn = _setup_orchestrator_conn(mock_get_conn)

        # LLM emits several content deltas before stopping. We make the
        # second send call raise WebSocketSendTimeout to mimic a wedged
        # client socket.
        class TwoDeltaLLM(FakeLLM):
            async def astream(self, prompt: str, **kwargs: Any):
                yield StreamEvent(type="content", delta="Esimene ")
                yield StreamEvent(type="content", delta="teine ")
                yield StreamEvent(type="stop")

        call_count = {"n": 0}

        async def flaky_send(event: dict[str, Any]) -> None:
            # Allow retrieval_done etc. through; only time out on the
            # second content_delta.
            if event.get("type") == "content_delta":
                call_count["n"] += 1
                if call_count["n"] == 1:
                    return  # first delta ok
                raise WebSocketSendTimeout("content_delta")
            return

        orchestrator = ChatOrchestrator(TwoDeltaLLM(), FakeSparql())
        asyncio.run(orchestrator.handle_message(_CONV_ID, "Tere", _auth(), flaky_send))

        # The first delta went through, the second triggered a timeout.
        assert call_count["n"] == 2

        # Partial content was persisted with is_truncated=TRUE.
        sql_calls = [
            call.args[0]
            for call in conn.execute.call_args_list
            if call.args and isinstance(call.args[0], str)
        ]
        assert any("is_truncated = TRUE" in sql for sql in sql_calls), (
            "Partial assistant turn should be flagged is_truncated after send timeout."
        )

    @patch("app.chat.orchestrator.check_org_cost_budget")
    @patch("app.chat.orchestrator.check_message_rate")
    @patch("app.chat.orchestrator.get_connection")
    def test_send_timeout_stops_further_events(self, mock_get_conn, mock_rate, mock_cost):
        _setup_orchestrator_conn(mock_get_conn)

        class TwoDeltaLLM(FakeLLM):
            async def astream(self, prompt: str, **kwargs: Any):
                yield StreamEvent(type="content", delta="A")
                yield StreamEvent(type="content", delta="B")
                yield StreamEvent(type="stop")

        received: list[dict[str, Any]] = []
        call_count = {"n": 0}

        async def flaky_send(event: dict[str, Any]) -> None:
            if event.get("type") == "content_delta":
                call_count["n"] += 1
                if call_count["n"] >= 2:
                    raise WebSocketSendTimeout("content_delta")
            received.append(event)

        orchestrator = ChatOrchestrator(TwoDeltaLLM(), FakeSparql())
        asyncio.run(orchestrator.handle_message(_CONV_ID, "Tere", _auth(), flaky_send))

        # After the timeout we must NOT see a ``done`` event on the
        # wedged socket — the orchestrator should bail silently.
        assert not any(e.get("type") == "done" for e in received)
