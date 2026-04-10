"""RAG ingestion script: reads entities from Jena, chunks, embeds, upserts.

Reads provisions, court decisions, and EU legislation from the Jena
SPARQL endpoint, chunks each entity using :func:`app.rag.chunker.chunk_entity`,
embeds via :class:`app.rag.embedding.VoyageProvider` (or stub), and
upserts into the ``rag_chunks`` PostgreSQL table.

Usage:
    uv run python scripts/ingest_rag.py

Supports re-ingestion: uses ON CONFLICT DO UPDATE so running the script
multiple times is safe and idempotent.
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.db import get_connection
from app.ontology.sparql_client import SparqlClient
from app.rag.chunker import RagChunk, chunk_entity
from app.rag.embedding import EmbeddingProvider, VoyageProvider, get_default_embedding_provider

logger = logging.getLogger(__name__)

# SPARQL queries for each entity type
_PROVISION_QUERY = """
PREFIX estleg: <http://data.seadus.ee/ontology#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT ?uri ?label ?summary WHERE {
    ?uri a estleg:paragrahv .
    OPTIONAL { ?uri rdfs:label ?label }
    OPTIONAL { ?uri estleg:summary ?summary }
}
"""

_COURT_DECISION_QUERY = """
PREFIX estleg: <http://data.seadus.ee/ontology#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT ?uri ?label ?caseNumber WHERE {
    ?uri a estleg:CourtDecision .
    OPTIONAL { ?uri rdfs:label ?label }
    OPTIONAL { ?uri estleg:caseNumber ?caseNumber }
}
"""

_EU_LEGISLATION_QUERY = """
PREFIX euleg: <http://data.seadus.ee/eu-ontology#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT ?uri ?label ?celexNumber WHERE {
    ?uri a euleg:EULegislation .
    OPTIONAL { ?uri rdfs:label ?label }
    OPTIONAL { ?uri euleg:celexNumber ?celexNumber }
}
"""

# Batch size for embedding API calls
_EMBED_BATCH_SIZE = 64


def _fetch_entities(sparql: SparqlClient) -> list[dict[str, str]]:
    """Fetch all entity types from Jena and tag with source_type."""
    entities: list[dict[str, str]] = []

    logger.info("Fetching provisions...")
    for row in sparql.query(_PROVISION_QUERY):
        content_parts = []
        if row.get("label"):
            content_parts.append(row["label"])
        if row.get("summary"):
            content_parts.append(row["summary"])
        if content_parts:
            entities.append(
                {
                    "source_type": "ontology",
                    "source_uri": row["uri"],
                    "content": "\n\n".join(content_parts),
                }
            )

    logger.info("Fetching court decisions...")
    for row in sparql.query(_COURT_DECISION_QUERY):
        content_parts = []
        if row.get("label"):
            content_parts.append(row["label"])
        if row.get("caseNumber"):
            content_parts.append(f"Kohtuasi nr {row['caseNumber']}")
        if content_parts:
            entities.append(
                {
                    "source_type": "court_decision",
                    "source_uri": row["uri"],
                    "content": "\n\n".join(content_parts),
                }
            )

    logger.info("Fetching EU legislation...")
    for row in sparql.query(_EU_LEGISLATION_QUERY):
        content_parts = []
        if row.get("label"):
            content_parts.append(row["label"])
        if row.get("celexNumber"):
            content_parts.append(f"CELEX: {row['celexNumber']}")
        if content_parts:
            entities.append(
                {
                    "source_type": "law_text",
                    "source_uri": row["uri"],
                    "content": "\n\n".join(content_parts),
                }
            )

    return entities


def _chunk_entities(entities: list[dict[str, str]]) -> list[RagChunk]:
    """Chunk all entities into RAG-sized pieces."""
    all_chunks: list[RagChunk] = []
    for entity in entities:
        metadata = {
            "source_type": entity["source_type"],
            "source_uri": entity["source_uri"],
        }
        chunks = chunk_entity(entity["content"], metadata)
        all_chunks.extend(chunks)
    return all_chunks


async def _embed_chunks(
    chunks: list[RagChunk],
    embedder: VoyageProvider | EmbeddingProvider | None = None,
) -> list[list[float]]:
    """Embed all chunks in batches."""
    if embedder is None:
        embedder = get_default_embedding_provider()

    all_embeddings: list[list[float]] = []
    texts = [c.content for c in chunks]

    for i in range(0, len(texts), _EMBED_BATCH_SIZE):
        batch = texts[i : i + _EMBED_BATCH_SIZE]
        batch_embeddings = await embedder.embed(batch)
        all_embeddings.extend(batch_embeddings)
        if i > 0 and i % (_EMBED_BATCH_SIZE * 10) == 0:
            logger.info("  Embedded %d / %d chunks...", i, len(texts))

    return all_embeddings


def _upsert_chunks(
    chunks: list[RagChunk],
    embeddings: list[list[float]],
) -> int:
    """Upsert chunks and embeddings into rag_chunks table.

    Returns the number of rows upserted.
    """
    upserted = 0
    with get_connection() as conn:
        for chunk, embedding in zip(chunks, embeddings):
            embedding_str = "[" + ",".join(str(v) for v in embedding) + "]"
            conn.execute(
                """INSERT INTO rag_chunks
                   (source_type, source_uri, chunk_index, content, metadata, embedding)
                   VALUES (%s, %s, %s, %s, %s::jsonb, %s::vector)
                   ON CONFLICT (source_type, source_uri, chunk_index)
                   DO UPDATE SET
                       content = EXCLUDED.content,
                       metadata = EXCLUDED.metadata,
                       embedding = EXCLUDED.embedding,
                       created_at = now()""",
                (
                    chunk.metadata["source_type"],
                    chunk.metadata["source_uri"],
                    chunk.chunk_index,
                    chunk.content,
                    __import__("json").dumps(chunk.metadata),
                    embedding_str,
                ),
            )
            upserted += 1
        conn.commit()
    return upserted


async def ingest(
    sparql: SparqlClient | None = None,
    embedder: VoyageProvider | EmbeddingProvider | None = None,
) -> dict[str, int]:
    """Run the full ingestion pipeline.

    Args:
        sparql: Optional SPARQL client (for testing/override).
        embedder: Optional embedding provider (for testing/override).

    Returns:
        Dict with ``entity_count``, ``chunk_count``, ``elapsed_seconds``.
    """
    start = time.monotonic()

    if sparql is None:
        sparql = SparqlClient()

    logger.info("Starting RAG ingestion...")

    entities = _fetch_entities(sparql)
    logger.info("Fetched %d entities from Jena", len(entities))

    chunks = _chunk_entities(entities)
    logger.info("Created %d chunks from %d entities", len(chunks), len(entities))

    if not chunks:
        logger.warning("No chunks to embed/upsert — exiting early")
        elapsed = time.monotonic() - start
        return {"entity_count": 0, "chunk_count": 0, "elapsed_seconds": int(elapsed)}

    logger.info("Embedding %d chunks...", len(chunks))
    embeddings = await _embed_chunks(chunks, embedder)

    logger.info("Upserting %d chunks into rag_chunks...", len(chunks))
    upserted = _upsert_chunks(chunks, embeddings)

    elapsed = time.monotonic() - start
    logger.info(
        "RAG ingestion complete: %d entities, %d chunks, %.1fs",
        len(entities),
        upserted,
        elapsed,
    )

    return {
        "entity_count": len(entities),
        "chunk_count": upserted,
        "elapsed_seconds": int(elapsed),
    }


async def ingest_modified_entities(
    sparql: SparqlClient | None = None,
    embedder: VoyageProvider | EmbeddingProvider | None = None,
) -> dict[str, int]:
    """Lightweight re-ingestion for entities modified since last sync.

    Called by the sync orchestrator after a successful sync. For now
    this does a full re-ingestion (which is idempotent via ON CONFLICT).
    Phase 4 can optimize by tracking modification timestamps.
    """
    return await ingest(sparql=sparql, embedder=embedder)


def main() -> None:
    """CLI entry point for manual ingestion runs."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    result = asyncio.run(ingest())
    print(f"Entities: {result['entity_count']}")
    print(f"Chunks:   {result['chunk_count']}")
    print(f"Time:     {result['elapsed_seconds']}s")


if __name__ == "__main__":
    main()
