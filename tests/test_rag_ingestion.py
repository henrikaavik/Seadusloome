"""Tests for ``scripts/ingest_rag`` — RAG ingestion pipeline.

All tests use mocked SPARQL client and embedding provider to avoid
network calls and database access.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from app.rag.chunker import RagChunk
from scripts.ingest_rag import (
    _chunk_entities,
    _embed_chunks,
    _fetch_entities,
    ingest,
)


class TestFetchEntities:
    def test_fetches_provisions(self):
        """Provisions with paragrahv and summary are fetched correctly."""
        mock_sparql = MagicMock()
        mock_sparql.query.side_effect = [
            # Provisions (query selects ?paragrahv and ?summary, not ?label)
            [
                {
                    "uri": "http://example.org/p1",
                    "paragrahv": "TsMS \u00a7 1",
                    "summary": "Summary 1",
                },
                {"uri": "http://example.org/p2", "paragrahv": "TsMS \u00a7 2", "summary": ""},
            ],
            # Court decisions
            [],
            # EU legislation
            [],
        ]

        entities = _fetch_entities(mock_sparql)

        assert len(entities) == 2
        assert entities[0]["source_type"] == "ontology"
        assert entities[0]["source_uri"] == "http://example.org/p1"
        assert "TsMS \u00a7 1" in entities[0]["content"]
        assert "Summary 1" in entities[0]["content"]

    def test_fetches_court_decisions(self):
        """Court decisions with label and caseNumber are fetched."""
        mock_sparql = MagicMock()
        mock_sparql.query.side_effect = [
            [],  # provisions
            [{"uri": "http://example.org/cd1", "label": "Otsus", "caseNumber": "3-2-1-100-17"}],
            [],  # EU
        ]

        entities = _fetch_entities(mock_sparql)

        assert len(entities) == 1
        assert entities[0]["source_type"] == "court_decision"
        assert "3-2-1-100-17" in entities[0]["content"]

    def test_fetches_eu_legislation(self):
        """EU legislation with label and celexNumber are fetched."""
        mock_sparql = MagicMock()
        mock_sparql.query.side_effect = [
            [],  # provisions
            [],  # court decisions
            [{"uri": "http://example.org/eu1", "label": "GDPR", "celexNumber": "32016R0679"}],
        ]

        entities = _fetch_entities(mock_sparql)

        assert len(entities) == 1
        assert entities[0]["source_type"] == "law_text"
        assert "CELEX: 32016R0679" in entities[0]["content"]

    def test_skips_entities_without_content(self):
        """Entities with no paragrahv or summary are skipped."""
        mock_sparql = MagicMock()
        mock_sparql.query.side_effect = [
            [{"uri": "http://example.org/p1", "paragrahv": "", "summary": ""}],
            [],
            [],
        ]

        entities = _fetch_entities(mock_sparql)
        assert len(entities) == 0

    def test_all_entity_types_combined(self):
        """All entity types are combined in a single list."""
        mock_sparql = MagicMock()
        mock_sparql.query.side_effect = [
            [{"uri": "http://example.org/p1", "paragrahv": "Provision", "summary": "S"}],
            [{"uri": "http://example.org/cd1", "label": "Decision", "caseNumber": "123"}],
            [{"uri": "http://example.org/eu1", "label": "Regulation", "celexNumber": "C1"}],
        ]

        entities = _fetch_entities(mock_sparql)
        assert len(entities) == 3
        types = {e["source_type"] for e in entities}
        assert types == {"ontology", "court_decision", "law_text"}


class TestChunkEntities:
    def test_chunks_entities(self):
        """Entities are chunked into RagChunk objects."""
        entities = [
            {
                "source_type": "ontology",
                "source_uri": "http://example.org/p1",
                "content": "Short provision text.",
            }
        ]

        chunks = _chunk_entities(entities)

        assert len(chunks) >= 1
        assert isinstance(chunks[0], RagChunk)
        assert chunks[0].metadata["source_type"] == "ontology"

    def test_long_entity_multiple_chunks(self):
        """A long entity produces multiple chunks."""
        entities = [
            {
                "source_type": "ontology",
                "source_uri": "http://example.org/p1",
                "content": "This is a sentence. " * 200,
            }
        ]

        chunks = _chunk_entities(entities)
        assert len(chunks) > 1


class TestEmbedChunks:
    def test_embeds_chunks_in_batches(self, monkeypatch: pytest.MonkeyPatch):
        """Chunks are embedded using the provider."""
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)

        from app.rag.embedding import VoyageProvider

        embedder = VoyageProvider()

        chunks = [RagChunk(content=f"Chunk {i}", metadata={}, chunk_index=i) for i in range(5)]

        embeddings = asyncio.run(_embed_chunks(chunks, embedder))

        assert len(embeddings) == 5
        assert all(len(e) == 1024 for e in embeddings)


class TestIngestPipeline:
    @patch("scripts.ingest_rag._delete_stale_chunks")
    @patch("scripts.ingest_rag._upsert_chunks")
    @patch("scripts.ingest_rag._embed_chunks")
    @patch("scripts.ingest_rag._fetch_entities")
    def test_ingest_full_pipeline(
        self,
        mock_fetch: MagicMock,
        mock_embed: MagicMock,
        mock_upsert: MagicMock,
        mock_delete_stale: MagicMock,
    ):
        """Full ingestion pipeline: fetch -> chunk -> embed -> upsert -> stale cleanup."""
        mock_fetch.return_value = [
            {
                "source_type": "ontology",
                "source_uri": "http://example.org/p1",
                "content": "Short provision.",
            },
        ]

        async def fake_embed(chunks, embedder=None):
            return [[0.1] * 1024 for _ in chunks]

        mock_embed.side_effect = fake_embed
        mock_upsert.return_value = 1
        mock_delete_stale.return_value = 0

        result = asyncio.run(ingest())

        assert result["entity_count"] == 1
        assert result["chunk_count"] == 1
        assert result["stale_deleted"] == 0
        mock_fetch.assert_called_once()
        mock_embed.assert_called_once()
        mock_upsert.assert_called_once()
        mock_delete_stale.assert_called_once()

    @patch("scripts.ingest_rag._fetch_entities")
    def test_ingest_no_entities(self, mock_fetch: MagicMock):
        """Ingestion with no entities exits early."""
        mock_fetch.return_value = []

        result = asyncio.run(ingest())

        assert result["entity_count"] == 0
        assert result["chunk_count"] == 0

    @patch("scripts.ingest_rag._delete_stale_chunks")
    @patch("scripts.ingest_rag._upsert_chunks")
    @patch("scripts.ingest_rag._embed_chunks")
    @patch("scripts.ingest_rag._fetch_entities")
    def test_ingest_with_custom_sparql_and_embedder(
        self,
        mock_fetch: MagicMock,
        mock_embed: MagicMock,
        mock_upsert: MagicMock,
        mock_delete_stale: MagicMock,
    ):
        """Custom sparql and embedder are passed through."""
        mock_sparql = MagicMock()
        mock_embedder = MagicMock()

        mock_fetch.return_value = [
            {
                "source_type": "ontology",
                "source_uri": "http://example.org/p1",
                "content": "Provision text for testing.",
            },
        ]

        async def fake_embed(chunks, embedder=None):
            return [[0.1] * 1024 for _ in chunks]

        mock_embed.side_effect = fake_embed
        mock_upsert.return_value = 1
        mock_delete_stale.return_value = 0

        result = asyncio.run(ingest(sparql=mock_sparql, embedder=mock_embedder))

        assert result["entity_count"] == 1
        mock_fetch.assert_called_once_with(mock_sparql)
