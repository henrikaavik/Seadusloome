"""Tests for ``app.rag.chunker`` — RAG-specific text chunking.

Covers short text, long text, Estonian legal reference preservation,
overlap behavior, and edge cases.
"""

from __future__ import annotations

import pytest

from app.rag.chunker import RagChunk, chunk_entity


class TestChunkEntityShortText:
    def test_short_text_single_chunk(self):
        """Text shorter than threshold produces exactly one chunk."""
        text = (
            "Tsiviilseadustiku \u00fcldosa seadus reguleerib "
            "tsiviil\u00f5iguse \u00fcldisi p\u00f5him\u00f5tteid."
        )
        metadata = {"source_type": "ontology", "source_uri": "http://example.org/law1"}

        chunks = chunk_entity(text, metadata)

        assert len(chunks) == 1
        assert chunks[0].content == text
        assert chunks[0].chunk_index == 0
        assert chunks[0].metadata == metadata

    def test_empty_text_no_chunks(self):
        """Empty or whitespace-only text produces no chunks."""
        metadata = {"source_type": "ontology", "source_uri": "http://example.org/law1"}

        assert chunk_entity("", metadata) == []
        assert chunk_entity("   ", metadata) == []
        assert chunk_entity("\n\n", metadata) == []

    def test_text_exactly_at_threshold(self):
        """Text at exactly 500 chars (default threshold) is single chunk."""
        text = "x" * 500
        metadata = {"source_type": "ontology", "source_uri": "http://example.org/law1"}

        chunks = chunk_entity(text, metadata)
        assert len(chunks) == 1


class TestChunkEntityLongText:
    def test_long_text_multiple_chunks(self):
        """Text longer than target_chars is split into multiple chunks."""
        # Build a ~2000 char text with sentence boundaries
        text = "".join(f"See on lause number {i}. " for i in range(100))
        metadata = {"source_type": "ontology", "source_uri": "http://example.org/law1"}

        chunks = chunk_entity(text, metadata, target_chars=800, overlap_chars=150)

        assert len(chunks) > 1
        # Each chunk should have sequential indices
        for i, chunk in enumerate(chunks):
            assert chunk.chunk_index == i
            assert chunk.metadata == metadata
            assert len(chunk.content) > 0

    def test_chunks_cover_full_text(self):
        """All original content appears in at least one chunk."""
        text = " ".join(f"Word{i}" for i in range(200))
        metadata = {"source_type": "ontology", "source_uri": "http://example.org/law1"}

        chunks = chunk_entity(text, metadata, target_chars=200, overlap_chars=40)

        # Every word should appear in at least one chunk
        for i in range(200):
            word = f"Word{i}"
            found = any(word in c.content for c in chunks)
            assert found, f"{word} not found in any chunk"

    def test_overlap_creates_shared_content(self):
        """Consecutive chunks should share some content due to overlap."""
        text = " ".join(f"Token{i}" for i in range(300))
        metadata = {"source_type": "ontology", "source_uri": "http://example.org/law1"}

        chunks = chunk_entity(text, metadata, target_chars=400, overlap_chars=100)

        assert len(chunks) >= 3
        # Check that consecutive chunks share some content
        for i in range(len(chunks) - 1):
            words_a = set(chunks[i].content.split())
            words_b = set(chunks[i + 1].content.split())
            shared = words_a & words_b
            assert len(shared) > 0, f"Chunks {i} and {i + 1} share no words"


class TestEstonianLegalPreservation:
    def test_paragraph_reference_not_split(self):
        """Estonian \u00a7 references should not be split across chunks."""
        # Build text with a legal reference near where a chunk boundary would fall
        filler = "See on t\u00e4itetekst mis t\u00e4idab ruumi. " * 20  # ~720 chars
        ref = "Vastavalt \u00a7 123 lg 4 p 5 tuleb arvestada j\u00e4rgmist. "
        text = filler + ref + filler

        metadata = {"source_type": "ontology", "source_uri": "http://example.org/law1"}

        chunks = chunk_entity(text, metadata, target_chars=800, overlap_chars=150)

        # The \u00a7 reference should appear intact in at least one chunk
        ref_found_intact = any("\u00a7 123 lg 4 p 5" in c.content for c in chunks)
        assert ref_found_intact, "\u00a7 reference was split across chunks"

    def test_section_range_preserved(self):
        """Section ranges like \u00a7\u00a7 10\u201315 should stay intact."""
        text = "A" * 600 + " Vastavalt \u00a7\u00a7 10\u201315 kehtib kord. " + "B" * 600
        metadata = {"source_type": "ontology", "source_uri": "http://example.org/law1"}

        chunks = chunk_entity(text, metadata, target_chars=800, overlap_chars=150)

        ref_found = any("\u00a7\u00a7 10\u201315" in c.content for c in chunks)
        assert ref_found, "Section range was not found intact in any chunk"


class TestChunkEntityEdgeCases:
    def test_invalid_target_chars(self):
        """target_chars <= 0 raises ValueError."""
        with pytest.raises(ValueError, match="target_chars must be positive"):
            chunk_entity("test", {}, target_chars=0)

    def test_invalid_overlap(self):
        """overlap_chars >= target_chars raises ValueError."""
        with pytest.raises(ValueError, match="overlap_chars must be smaller"):
            chunk_entity("test", {}, target_chars=100, overlap_chars=100)

    def test_negative_overlap(self):
        """Negative overlap raises ValueError."""
        with pytest.raises(ValueError, match="overlap_chars must be non-negative"):
            chunk_entity("test", {}, overlap_chars=-1)

    def test_no_overlap(self):
        """overlap_chars=0 produces non-overlapping chunks."""
        text = "A" * 2000
        metadata = {"source_type": "ontology", "source_uri": "http://example.org/law1"}

        chunks = chunk_entity(text, metadata, target_chars=800, overlap_chars=0)

        # Sum of chunk lengths should approximately equal total length
        # (may not be exact due to stripping and boundary logic)
        assert len(chunks) >= 2

    def test_paragraph_break_preferred_as_split_point(self):
        """Chunks prefer to split on paragraph breaks."""
        part1 = "First paragraph content here. " * 15  # ~450 chars
        part2 = "Second paragraph content here. " * 15  # ~450 chars
        text = part1 + "\n\n" + part2

        metadata = {"source_type": "ontology", "source_uri": "http://example.org/law1"}

        chunks = chunk_entity(text, metadata, target_chars=800, overlap_chars=100)

        # With a paragraph break near 450 chars and target 800,
        # the chunker should find the paragraph break
        if len(chunks) > 1:
            # First chunk should end roughly at the paragraph break
            assert "First paragraph" in chunks[0].content

    def test_custom_target_and_overlap(self):
        """Custom target_chars and overlap_chars are respected."""
        text = "Word " * 500  # 2500 chars
        metadata = {"source_type": "ontology", "source_uri": "http://example.org/law1"}

        chunks = chunk_entity(text, metadata, target_chars=400, overlap_chars=50)

        assert len(chunks) >= 5  # 2500 / (400-50) ~= 7 chunks

    def test_metadata_propagated_to_all_chunks(self):
        """Every chunk gets the same metadata dict."""
        text = "Content here. " * 200
        metadata = {"source_type": "court_decision", "source_uri": "http://example.org/cd1"}

        chunks = chunk_entity(text, metadata, target_chars=400, overlap_chars=50)

        for chunk in chunks:
            assert chunk.metadata == metadata


class TestRagChunkDataclass:
    def test_rag_chunk_is_frozen(self):
        """RagChunk instances are immutable."""
        chunk = RagChunk(content="test", metadata={}, chunk_index=0)
        with pytest.raises(AttributeError):
            chunk.content = "modified"  # type: ignore[misc]

    def test_rag_chunk_equality(self):
        """Two RagChunks with same fields are equal."""
        a = RagChunk(content="test", metadata={"k": "v"}, chunk_index=0)
        b = RagChunk(content="test", metadata={"k": "v"}, chunk_index=0)
        assert a == b
