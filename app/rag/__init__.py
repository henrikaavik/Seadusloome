"""Retrieval-Augmented Generation pipeline.

Provides embedding, chunking, ingestion, and retrieval for the RAG
system that powers the AI advisory chat and law drafter modules.

Typical usage:

    from app.rag import get_default_embedding_provider, Retriever

    retriever = Retriever()
    chunks = await retriever.retrieve("Tsiviilseadustiku muudatus")
"""

from app.rag.embedding import (
    EmbeddingProvider,
    VoyageProvider,
    _reset_default_embedding_provider,
    get_default_embedding_provider,
)

__all__ = [
    "EmbeddingProvider",
    "VoyageProvider",
    "_reset_default_embedding_provider",
    "get_default_embedding_provider",
]
