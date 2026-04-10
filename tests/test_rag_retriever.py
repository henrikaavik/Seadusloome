"""Tests for ``app.rag.retriever`` — vector-similarity retriever.

All tests mock the database and embedding provider. No real DB or API
calls are made.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from app.rag.retriever import RetrievedChunk, Retriever


def _make_stub_embedder():
    """Create a mock embedding provider that returns deterministic vectors."""
    embedder = MagicMock()

    async def fake_embed(texts):
        return [[0.1] * 1024 for _ in texts]

    embedder.embed = fake_embed
    embedder.dimensions = 1024
    return embedder


class TestRetriever:
    @patch("app.rag.retriever.get_connection")
    def test_retrieve_returns_chunks(self, mock_get_conn: MagicMock):
        """Retrieve returns RetrievedChunk objects from DB results."""
        embedder = _make_stub_embedder()
        retriever = Retriever(embedding_provider=embedder)

        # Mock DB response
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            ("Provision text", json.dumps({"source_type": "ontology"}), 0.95),
            ("Court decision text", json.dumps({"source_type": "court_decision"}), 0.87),
        ]
        mock_conn.execute.return_value = mock_cursor
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        results = asyncio.run(retriever.retrieve("test query"))

        assert len(results) == 2
        assert isinstance(results[0], RetrievedChunk)
        assert results[0].content == "Provision text"
        assert results[0].metadata == {"source_type": "ontology"}
        assert results[0].score == 0.95
        assert results[1].score == 0.87

    @patch("app.rag.retriever.get_connection")
    def test_retrieve_with_source_type_filter(self, mock_get_conn: MagicMock):
        """source_type filter is included in the SQL query."""
        embedder = _make_stub_embedder()
        retriever = Retriever(embedding_provider=embedder)

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            ("Court text", json.dumps({"source_type": "court_decision"}), 0.9),
        ]
        mock_conn.execute.return_value = mock_cursor
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        results = asyncio.run(retriever.retrieve("test", source_type="court_decision"))

        assert len(results) == 1
        # Verify the SQL included the source_type parameter
        call_args = mock_conn.execute.call_args
        sql = call_args[0][0]
        params = call_args[0][1]
        assert "WHERE source_type" in sql
        assert "court_decision" in params

    @patch("app.rag.retriever.get_connection")
    def test_retrieve_empty_results(self, mock_get_conn: MagicMock):
        """Empty DB result returns empty list."""
        embedder = _make_stub_embedder()
        retriever = Retriever(embedding_provider=embedder)

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_conn.execute.return_value = mock_cursor
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        results = asyncio.run(retriever.retrieve("test query"))
        assert results == []

    def test_retrieve_empty_query(self):
        """Empty or whitespace query returns empty list without DB call."""
        embedder = _make_stub_embedder()
        retriever = Retriever(embedding_provider=embedder)

        results = asyncio.run(retriever.retrieve(""))
        assert results == []

        results = asyncio.run(retriever.retrieve("   "))
        assert results == []

    @patch("app.rag.retriever.get_connection")
    def test_retrieve_custom_k(self, mock_get_conn: MagicMock):
        """Custom k parameter is passed to the LIMIT clause."""
        embedder = _make_stub_embedder()
        retriever = Retriever(embedding_provider=embedder)

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_conn.execute.return_value = mock_cursor
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        asyncio.run(retriever.retrieve("test", k=5))

        call_args = mock_conn.execute.call_args
        params = call_args[0][1]
        assert 5 in params

    @patch("app.rag.retriever.get_connection")
    def test_retrieve_handles_dict_metadata(self, mock_get_conn: MagicMock):
        """Metadata returned as a dict (from psycopg JSONB) is handled."""
        embedder = _make_stub_embedder()
        retriever = Retriever(embedding_provider=embedder)

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            ("Text", {"source_type": "ontology"}, 0.9),
        ]
        mock_conn.execute.return_value = mock_cursor
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        results = asyncio.run(retriever.retrieve("test"))

        assert results[0].metadata == {"source_type": "ontology"}

    @patch("app.rag.retriever.get_connection")
    def test_retrieve_handles_db_error(self, mock_get_conn: MagicMock):
        """Database errors return empty list instead of crashing."""
        embedder = _make_stub_embedder()
        retriever = Retriever(embedding_provider=embedder)

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = Exception("DB connection failed")
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        results = asyncio.run(retriever.retrieve("test"))
        assert results == []

    @patch("app.rag.retriever.get_connection")
    def test_score_ordering(self, mock_get_conn: MagicMock):
        """Results maintain the score ordering from the DB."""
        embedder = _make_stub_embedder()
        retriever = Retriever(embedding_provider=embedder)

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            ("Best match", "{}", 0.99),
            ("Good match", "{}", 0.85),
            ("OK match", "{}", 0.70),
        ]
        mock_conn.execute.return_value = mock_cursor
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        results = asyncio.run(retriever.retrieve("test"))

        assert len(results) == 3
        assert results[0].score > results[1].score > results[2].score
        assert results[0].content == "Best match"

    @patch("app.rag.retriever.get_connection")
    def test_retrieve_handles_invalid_json_metadata(self, mock_get_conn: MagicMock):
        """Invalid JSON metadata falls back to empty dict."""
        embedder = _make_stub_embedder()
        retriever = Retriever(embedding_provider=embedder)

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            ("Text", "not-valid-json", 0.9),
        ]
        mock_conn.execute.return_value = mock_cursor
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        results = asyncio.run(retriever.retrieve("test"))

        assert results[0].metadata == {}


class TestRetrievedChunkDataclass:
    def test_retrieved_chunk_is_frozen(self):
        """RetrievedChunk instances are immutable."""
        chunk = RetrievedChunk(content="test", metadata={}, score=0.9)
        with pytest.raises(AttributeError):
            chunk.content = "modified"  # type: ignore[misc]

    def test_retrieved_chunk_equality(self):
        """Two RetrievedChunks with same fields are equal."""
        a = RetrievedChunk(content="test", metadata={"k": "v"}, score=0.9)
        b = RetrievedChunk(content="test", metadata={"k": "v"}, score=0.9)
        assert a == b
