"""Vector-similarity retriever for RAG chunks.

Retrieves the most relevant chunks from the ``rag_chunks`` table
using pgvector's cosine distance operator (``<=>``) against an
embedded query string.

Usage:

    from app.rag.retriever import Retriever

    retriever = Retriever()
    results = await retriever.retrieve("Tsiviilseadustiku muudatus", k=5)
    for chunk in results:
        print(chunk.score, chunk.content[:80])
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.db import get_connection
from app.rag.embedding import EmbeddingProvider, get_default_embedding_provider

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetrievedChunk:
    """A chunk retrieved from the vector store.

    Attributes:
        content: The chunk text.
        metadata: Original metadata dict from the chunk.
        score: Similarity score (1 - cosine distance). Higher is better.
            Range: [0, 2] in theory but typically [0, 1] for normalized vectors.
    """

    content: str
    metadata: dict
    score: float


class Retriever:
    """Vector-similarity retriever backed by pgvector.

    Args:
        embedding_provider: Provider to embed query strings. Falls back
            to the module-level default if not provided.
    """

    def __init__(self, embedding_provider: EmbeddingProvider | None = None) -> None:
        self.embedder = embedding_provider or get_default_embedding_provider()

    async def retrieve(
        self,
        query: str,
        *,
        k: int = 10,
        source_type: str | None = None,
    ) -> list[RetrievedChunk]:
        """Retrieve the top-k most similar chunks to *query*.

        Args:
            query: Natural language query to search for.
            k: Maximum number of results to return.
            source_type: Optional filter to restrict results to a
                specific source type (``'ontology'``, ``'draft'``,
                ``'law_text'``, ``'court_decision'``).

        Returns:
            List of :class:`RetrievedChunk` ordered by descending
            similarity score. Empty list if no results or if the
            table has no data.
        """
        if not query.strip():
            return []

        # 1. Embed the query
        embeddings = await self.embedder.embed([query])
        if not embeddings:
            return []
        query_embedding = embeddings[0]

        # 2. Build the SQL query
        embedding_str = "[" + ",".join(str(v) for v in query_embedding) + "]"

        if source_type:
            sql = (
                "SELECT content, metadata, "
                "1 - (embedding <=> %s::vector) AS score "
                "FROM rag_chunks "
                "WHERE source_type = %s "
                "ORDER BY embedding <=> %s::vector "
                "LIMIT %s"
            )
            params = (embedding_str, source_type, embedding_str, k)
        else:
            sql = (
                "SELECT content, metadata, "
                "1 - (embedding <=> %s::vector) AS score "
                "FROM rag_chunks "
                "ORDER BY embedding <=> %s::vector "
                "LIMIT %s"
            )
            params = (embedding_str, embedding_str, k)

        # 3. Execute
        try:
            with get_connection() as conn:
                cursor = conn.execute(sql, params)
                rows = cursor.fetchall()
        except Exception:
            logger.exception("Failed to retrieve RAG chunks")
            return []

        # 4. Parse results
        import json

        if not rows:
            logger.info(
                "RAG retrieval returned 0 results for query (first 80 chars): %.80s "
                "— the rag_chunks table may be empty. Run "
                "'uv run python scripts/ingest_rag.py' to populate it.",
                query,
            )
            return []

        results: list[RetrievedChunk] = []
        for row in rows:
            content = row[0]
            metadata_raw = row[1]
            score = float(row[2])

            # metadata may come back as a dict or a JSON string
            if isinstance(metadata_raw, str):
                try:
                    metadata = json.loads(metadata_raw)
                except json.JSONDecodeError:
                    metadata = {}
            elif isinstance(metadata_raw, dict):
                metadata = metadata_raw
            else:
                metadata = {}

            results.append(
                RetrievedChunk(
                    content=content,
                    metadata=metadata,
                    score=score,
                )
            )

        return results
