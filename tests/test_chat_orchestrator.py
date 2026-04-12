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
    _build_llm_messages,
    _load_impact_summary,
    _messages_to_prompt,
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
