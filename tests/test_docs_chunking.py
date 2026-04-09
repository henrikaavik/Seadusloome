"""Unit tests for ``app.docs.chunking``.

The chunker is deterministic and has no external dependencies, so
these tests are pure function-level checks on the output shape and
boundary behaviour.
"""

from __future__ import annotations

import pytest

from app.docs.chunking import ChunkSpan, chunk_text


class TestChunkText:
    def test_short_text_single_chunk(self):
        """Input smaller than ``target_chars`` returns exactly one span."""
        text = "§ 1. Üldsätted\n\nSee on lühike tekst."
        result = chunk_text(text, target_chars=1000, overlap_chars=100)
        assert len(result) == 1
        assert isinstance(result[0], ChunkSpan)
        assert result[0].start == 0
        assert result[0].end == len(text)
        assert result[0].text == text

    def test_long_text_multiple_chunks(self):
        """Large input splits into several overlapping chunks that cover it."""
        paragraph = "See on lõik, mis sisaldab olulist õiguslikku teavet. " * 10
        # ~540 chars per paragraph; build ~20 paragraphs.
        text = "\n\n".join(paragraph for _ in range(20))
        result = chunk_text(text, target_chars=1000, overlap_chars=100)
        assert len(result) > 1
        # First chunk starts at 0; last chunk ends at the full length.
        assert result[0].start == 0
        assert result[-1].end == len(text)
        # Every chunk's ``text`` matches the slice it claims to be.
        for span in result:
            assert span.text == text[span.start : span.end]

    def test_overlap_preserved(self):
        """Consecutive chunks must share at least ``overlap_chars`` of text."""
        # Build text with no paragraph or sentence boundaries so the
        # chunker is forced into hard-cut mode and the overlap is exact.
        text = "x" * 2500
        result = chunk_text(text, target_chars=1000, overlap_chars=200)
        assert len(result) >= 2
        # Between chunk 0 and chunk 1: the first chunk ends at 1000
        # (hard-cut) and the next starts at 1000 - 200 = 800.
        assert result[0].end == 1000
        assert result[1].start == 800
        # And the actual text overlaps.
        overlap = text[result[1].start : result[0].end]
        assert len(overlap) == 200

    def test_empty_input_returns_empty(self):
        """Empty input short-circuits to an empty list."""
        assert chunk_text("") == []

    def test_splits_on_paragraph_boundary(self):
        """When a ``\\n\\n`` exists in the second half of the window, split there."""
        # 500 chars of filler + paragraph break + 500 chars of filler
        # = first chunk ends at the paragraph break, not at the hard
        # cut, because the break sits past the midpoint of the window.
        text = "a" * 500 + "\n\n" + "b" * 500
        result = chunk_text(text, target_chars=700, overlap_chars=50)
        assert len(result) >= 2
        # First chunk should end right after the paragraph break so
        # the split is clean.
        first_end = result[0].end
        assert text[first_end - 2 : first_end] == "\n\n"
        # And it doesn't contain any 'b' characters — everything
        # after the break belongs to chunk 2.
        assert "b" not in result[0].text

    def test_hard_cut_when_no_boundary(self):
        """Pure filler with no punctuation falls back to hard cuts at target_chars."""
        text = "x" * 3000  # no "\n\n", no ". ", no boundaries at all
        result = chunk_text(text, target_chars=1000, overlap_chars=0)
        # With no overlap, 3000 chars / 1000 per chunk = exactly 3 chunks.
        assert len(result) == 3
        assert result[0].start == 0
        assert result[0].end == 1000
        assert result[1].start == 1000
        assert result[1].end == 2000
        assert result[2].start == 2000
        assert result[2].end == 3000


class TestChunkTextValidation:
    def test_rejects_nonpositive_target(self):
        with pytest.raises(ValueError, match="target_chars"):
            chunk_text("hi", target_chars=0)

    def test_rejects_negative_overlap(self):
        with pytest.raises(ValueError, match="overlap_chars"):
            chunk_text("hi", target_chars=100, overlap_chars=-1)

    def test_rejects_overlap_larger_than_target(self):
        with pytest.raises(ValueError, match="overlap_chars"):
            chunk_text("hi", target_chars=100, overlap_chars=100)
