# pyright: reportArgumentType=false
"""Tests for RAG integration in ``app.chat.orchestrator.ChatOrchestrator``.

Covers:
- Chunks appear in the system prompt when retriever returns results
- rag_context JSONB persisted on the assistant message
- retrieval_started and retrieval_done events sent to the client
- Graceful degradation when retriever is None / stubbed
- Graceful degradation when retriever.retrieve raises
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

from app.chat.models import Conversation
from app.chat.orchestrator import ChatOrchestrator
from app.chat.rate_limiter import CostBudgetExceededError, RateLimitExceededError
from app.llm.provider import LLMProvider, StreamEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_ID = "11111111-1111-1111-1111-111111111111"
_ORG_ID = "22222222-2222-2222-2222-222222222222"
_CONV_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


def _auth() -> dict[str, Any]:
    return {"id": _USER_ID, "org_id": _ORG_ID}


def _make_conversation(
    *,
    conv_id: uuid.UUID = _CONV_ID,
    org_id: str = _ORG_ID,
) -> Conversation:
    now = datetime.now(UTC)
    return Conversation(
        id=conv_id,
        user_id=uuid.UUID(_USER_ID),
        org_id=uuid.UUID(org_id),
        title="Test vestlus",
        context_draft_id=None,
        created_at=now,
        updated_at=now,
    )


class _Collector:
    """Async-compatible event collector."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def __call__(self, event: dict[str, Any]) -> None:
        self.events.append(event)


class FakeLLM(LLMProvider):
    """Fake LLM that yields a configurable sequence of StreamEvents."""

    def __init__(self) -> None:
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
        self._last_system = kwargs.get("system", "")
        yield StreamEvent(type="content", delta="Vastus RAG-iga.")
        yield StreamEvent(type="stop")


class FakeSparql:
    def query(self, q: str) -> list:
        return []


@dataclass
class FakeChunk:
    """Mimics ``RetrievedChunk`` from ``app.rag.retriever``."""

    content: str
    metadata: dict
    score: float


class FakeRetriever:
    """Mimics ``Retriever`` from ``app.rag.retriever``."""

    def __init__(self, chunks: list[FakeChunk] | None = None) -> None:
        self.chunks = chunks or []
        self.call_count = 0

    async def retrieve(
        self,
        query: str,
        k: int = 10,
        source_type: str | None = None,
        org_id: str | None = None,
    ) -> list[FakeChunk]:
        self.call_count += 1
        self.last_org_id = org_id
        return self.chunks


class ErrorRetriever:
    """A retriever that always raises."""

    async def retrieve(
        self,
        query: str,
        k: int = 10,
        source_type: str | None = None,
        org_id: str | None = None,
    ) -> list:
        raise RuntimeError("Embedding service unavailable")


def _setup_mock_conn(mock_get_conn: MagicMock) -> MagicMock:
    """Configure mock get_connection for standard orchestrator flow."""
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
    return conn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRAGChunksInSystemPrompt:
    @patch("app.chat.orchestrator.check_org_cost_budget")
    @patch("app.chat.orchestrator.check_message_rate")
    @patch("app.chat.orchestrator.get_connection")
    def test_chunks_included_in_system_prompt(self, mock_get_conn, mock_rate, mock_cost):
        """When retriever returns chunks, they appear in the system prompt."""
        _setup_mock_conn(mock_get_conn)

        chunks = [
            FakeChunk(
                content="Tsiviilseadustiku par 1 sate 1",
                metadata={"source_uri": "estleg:TsUS_p1_s1"},
                score=0.92,
            ),
            FakeChunk(
                content="Karistusseadustik par 121",
                metadata={"source_uri": "estleg:KarS_p121"},
                score=0.85,
            ),
        ]
        retriever = FakeRetriever(chunks)
        llm = FakeLLM()

        collector = _Collector()
        orchestrator = ChatOrchestrator(llm, FakeSparql(), retriever=retriever)
        asyncio.run(orchestrator.handle_message(_CONV_ID, "Mis on seadus?", _auth(), collector))

        # The system prompt passed to the LLM should contain the chunk text
        assert "Relevant legal context:" in llm._last_system
        assert "Tsiviilseadustiku par 1 sate 1" in llm._last_system
        assert "Karistusseadustik par 121" in llm._last_system

    @patch("app.chat.orchestrator.check_org_cost_budget")
    @patch("app.chat.orchestrator.check_message_rate")
    @patch("app.chat.orchestrator.get_connection")
    def test_empty_chunks_no_context_appended(self, mock_get_conn, mock_rate, mock_cost):
        """When retriever returns no chunks, system prompt is unchanged."""
        _setup_mock_conn(mock_get_conn)

        retriever = FakeRetriever([])
        llm = FakeLLM()

        collector = _Collector()
        orchestrator = ChatOrchestrator(llm, FakeSparql(), retriever=retriever)
        asyncio.run(orchestrator.handle_message(_CONV_ID, "Tere", _auth(), collector))

        assert "Relevant legal context:" not in llm._last_system


class TestRAGContextPersisted:
    @patch("app.chat.orchestrator.check_org_cost_budget")
    @patch("app.chat.orchestrator.check_message_rate")
    @patch("app.chat.orchestrator.get_connection")
    def test_rag_context_persisted_on_assistant_message(self, mock_get_conn, mock_rate, mock_cost):
        """rag_context JSONB is passed to create_message for the assistant."""
        conn = _setup_mock_conn(mock_get_conn)

        chunks = [
            FakeChunk(
                content="Seaduse tekst",
                metadata={"source_uri": "estleg:Test"},
                score=0.90,
            ),
        ]
        retriever = FakeRetriever(chunks)
        llm = FakeLLM()

        collector = _Collector()
        orchestrator = ChatOrchestrator(llm, FakeSparql(), retriever=retriever)
        asyncio.run(orchestrator.handle_message(_CONV_ID, "Tere", _auth(), collector))

        # Find the INSERT INTO messages calls for the assistant message
        execute_calls = conn.execute.call_args_list
        assistant_inserts = []
        for call in execute_calls:
            args = call[0]
            if (
                len(args) >= 2
                and isinstance(args[0], str)
                and "INSERT INTO messages" in args[0]
                and len(args[1]) >= 8
                and args[1][1] == "assistant"
            ):
                assistant_inserts.append(args)

        assert len(assistant_inserts) >= 1
        # #570: rag_context is now encrypted at rest. Post-migration the
        # INSERT params are (conv_id, role, tool_name, tokens_in, tokens_out,
        # model, content_ct, tool_input_ct, tool_output_ct, rag_context_ct).
        # Index 9 is the Fernet ciphertext — decrypt + parse to assert.
        rag_param = assistant_inserts[0][1][9]
        assert rag_param is not None
        assert isinstance(rag_param, bytes)
        from app.storage import decrypt_text

        parsed = json.loads(decrypt_text(rag_param))
        assert isinstance(parsed, list)
        assert len(parsed) == 1
        assert parsed[0]["content"] == "Seaduse tekst"
        assert parsed[0]["source_uri"] == "estleg:Test"
        assert parsed[0]["score"] == 0.90


class TestRetrievalEvents:
    @patch("app.chat.orchestrator.check_org_cost_budget")
    @patch("app.chat.orchestrator.check_message_rate")
    @patch("app.chat.orchestrator.get_connection")
    def test_retrieval_events_sent(self, mock_get_conn, mock_rate, mock_cost):
        """retrieval_started and retrieval_done events are sent to the client."""
        _setup_mock_conn(mock_get_conn)

        chunks = [
            FakeChunk(content="chunk1", metadata={}, score=0.8),
            FakeChunk(content="chunk2", metadata={}, score=0.7),
        ]
        retriever = FakeRetriever(chunks)
        llm = FakeLLM()

        collector = _Collector()
        orchestrator = ChatOrchestrator(llm, FakeSparql(), retriever=retriever)
        asyncio.run(orchestrator.handle_message(_CONV_ID, "Test", _auth(), collector))

        types = [e["type"] for e in collector.events]
        assert "retrieval_started" in types
        assert "retrieval_done" in types

        done_event = next(e for e in collector.events if e["type"] == "retrieval_done")
        assert done_event["chunk_count"] == 2


class TestRAGGracefulDegradation:
    @patch("app.chat.orchestrator.check_org_cost_budget")
    @patch("app.chat.orchestrator.check_message_rate")
    @patch("app.chat.orchestrator.get_connection")
    def test_no_retriever_still_works(self, mock_get_conn, mock_rate, mock_cost):
        """When retriever is None, chat completes without RAG."""
        _setup_mock_conn(mock_get_conn)

        llm = FakeLLM()
        collector = _Collector()
        # Pass retriever=None explicitly (simulates no app.rag module)
        orchestrator = ChatOrchestrator(llm, FakeSparql(), retriever=None)
        # Mark as initialised so it doesn't try to construct one
        orchestrator._retriever_initialised = True

        asyncio.run(orchestrator.handle_message(_CONV_ID, "Tere", _auth(), collector))

        # Should still get content and done events
        types = [e["type"] for e in collector.events]
        assert "content_delta" in types
        assert "done" in types
        # No retrieval events
        assert "retrieval_started" not in types
        assert "retrieval_done" not in types

    @patch("app.chat.orchestrator.check_org_cost_budget")
    @patch("app.chat.orchestrator.check_message_rate")
    @patch("app.chat.orchestrator.get_connection")
    def test_retriever_error_still_completes(self, mock_get_conn, mock_rate, mock_cost):
        """When retriever.retrieve raises, chat continues without RAG."""
        _setup_mock_conn(mock_get_conn)

        llm = FakeLLM()
        collector = _Collector()
        orchestrator = ChatOrchestrator(llm, FakeSparql(), retriever=ErrorRetriever())

        asyncio.run(orchestrator.handle_message(_CONV_ID, "Tere", _auth(), collector))

        types = [e["type"] for e in collector.events]
        # Should still get content and done
        assert "content_delta" in types
        assert "done" in types
        # retrieval_started is sent before the error
        assert "retrieval_started" in types
        # No error event -- RAG failure is non-fatal
        error_events = [e for e in collector.events if e["type"] == "error"]
        assert len(error_events) == 0


class TestOrchestratorRateLimitIntegration:
    """Verify rate limit / cost budget errors are sent via WS."""

    @patch("app.chat.orchestrator.check_org_cost_budget")
    @patch(
        "app.chat.orchestrator.check_message_rate",
        side_effect=RateLimitExceededError("Limiit on 100 sonumit tunnis."),
    )
    @patch("app.chat.orchestrator.get_connection")
    def test_rate_limit_sends_error_event(self, mock_get_conn, mock_rate, mock_cost):
        """RateLimitExceededError triggers an error event, no LLM call."""
        llm = FakeLLM()
        collector = _Collector()
        orchestrator = ChatOrchestrator(llm, FakeSparql())

        asyncio.run(orchestrator.handle_message(_CONV_ID, "Tere", _auth(), collector))

        assert len(collector.events) == 1
        assert collector.events[0]["type"] == "error"
        assert "100" in collector.events[0]["message"]

    @patch(
        "app.chat.orchestrator.check_org_cost_budget",
        side_effect=CostBudgetExceededError("Kulueelarve on taidetud."),
    )
    @patch("app.chat.orchestrator.check_message_rate")
    @patch(
        "app.chat.orchestrator.get_conversation",
        return_value=_make_conversation(),
    )
    @patch("app.chat.orchestrator.list_messages", return_value=[])
    @patch("app.chat.orchestrator.get_connection")
    def test_cost_budget_sends_error_event(
        self, mock_get_conn, mock_list, mock_get_conv, mock_rate, mock_cost
    ):
        """CostBudgetExceededError triggers an error event, no LLM call.

        The budget check was relocated inside the conversation-load
        transaction so the advisory lock can actually serialise
        concurrent checks (TOCTOU fix). The test therefore needs to
        feed past conversation loading before the cost check fires.
        """
        llm = FakeLLM()
        collector = _Collector()
        orchestrator = ChatOrchestrator(llm, FakeSparql())

        asyncio.run(orchestrator.handle_message(_CONV_ID, "Tere", _auth(), collector))

        error_events = [e for e in collector.events if e["type"] == "error"]
        assert len(error_events) == 1
        assert "taidetud" in error_events[0]["message"].lower()
