-- Phase 3C: RAG chunks table for vector search
-- Stores chunked content with embeddings for retrieval-augmented generation.

CREATE TABLE IF NOT EXISTS rag_chunks (
    id BIGSERIAL PRIMARY KEY,
    source_type TEXT NOT NULL CHECK (source_type IN ('ontology', 'draft', 'law_text', 'court_decision')),
    source_uri TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    metadata JSONB,
    embedding vector(1024),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(source_type, source_uri, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_embedding ON rag_chunks
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_source ON rag_chunks(source_type, source_uri);
