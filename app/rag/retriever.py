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
        org_id: str | None = None,
    ) -> list[RetrievedChunk]:
        """Retrieve the top-k most similar chunks to *query*.

        Args:
            query: Natural language query to search for.
            k: Maximum number of results to return.
            source_type: Optional filter to restrict results to a
                specific source type (``'ontology'``, ``'draft'``,
                ``'law_text'``, ``'court_decision'``).
            org_id: Tenant scope. Public-corpus chunks (``org_id IS NULL``
                in the DB) are always visible. If a non-``None`` value is
                supplied, private chunks owned by that org are also
                visible. If ``None``, the caller is treated as "no org"
                and only public chunks are returned. Callers must pass
                the authenticated user's ``org_id`` — failing to do so is
                a data-leak bug, not a convenience.

        Returns:
            List of :class:`RetrievedChunk` ordered by descending
            similarity score. Empty list if no results or if the
            table has no data.
        """
        # Tenant scoping: `(org_id IS NULL OR org_id = $1)` keeps public
        # corpus (NULL) visible to everyone and gates private chunks on
        # the caller's org. See migration 016 and issue #576.

        if not query.strip():
            return []

        # 1. Embed the query
        embeddings = await self.embedder.embed([query])
        if not embeddings:
            return []
        query_embedding = embeddings[0]

        # 2. Build the SQL query
        embedding_str = "[" + ",".join(str(v) for v in query_embedding) + "]"

        # Tenant predicate: NULL org_id always matches (public); otherwise
        # match the caller's own org. psycopg binds Python `None` as SQL
        # NULL, and `NULL = NULL` is NULL (not true), so the `org_id IS
        # NULL` branch correctly keeps public rows visible even when the
        # caller has no org.
        tenant_where = "(org_id IS NULL OR org_id = %s)"

        if source_type:
            sql = (
                "SELECT content, metadata, "
                "1 - (embedding <=> %s::vector) AS score "
                "FROM rag_chunks "
                f"WHERE {tenant_where} AND source_type = %s "
                "ORDER BY embedding <=> %s::vector "
                "LIMIT %s"
            )
            params = (embedding_str, org_id, source_type, embedding_str, k)
        else:
            sql = (
                "SELECT content, metadata, "
                "1 - (embedding <=> %s::vector) AS score "
                "FROM rag_chunks "
                f"WHERE {tenant_where} "
                "ORDER BY embedding <=> %s::vector "
                "LIMIT %s"
            )
            params = (embedding_str, org_id, embedding_str, k)

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


def delete_chunks_for_draft(conn, draft_id) -> int:
    """Delete every ``rag_chunks`` row owned by the given draft.

    This is the application-level cascade for the polymorphic
    ``(source_type, source_id)`` reference documented in migration 016.
    PostgreSQL can't enforce the FK itself because ``source_id`` is
    polymorphic across source types, so every caller that deletes a
    draft row must also call this helper. See
    ``app/docs/routes.py::delete_draft_handler`` for the canonical
    wiring.

    Parameters
    ----------
    conn:
        An open psycopg connection. Commit is the caller's
        responsibility — this helper is expected to run inside the same
        transaction as the ``DELETE FROM drafts`` statement so either
        both succeed or neither does.
    draft_id:
        UUID (or string/UUID-compatible) identifying the draft. Compared
        against ``rag_chunks.source_id``.

    Returns
    -------
    int
        Number of rows removed. Zero is a legitimate outcome — e.g. a
        draft that was never RAG-ingested.
    """
    cursor = conn.execute(
        "DELETE FROM rag_chunks WHERE source_type = 'draft' AND source_id = %s",
        (str(draft_id),),
    )
    return cursor.rowcount or 0
