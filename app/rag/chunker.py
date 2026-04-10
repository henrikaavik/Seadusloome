"""RAG-specific text chunking with Estonian legal awareness.

Unlike :mod:`app.docs.chunking` (which splits uploaded drafts into
windows for LLM entity extraction), this module chunks *ontology
entities* for embedding and vector search. The target size is much
smaller (~800 chars vs ~24k) because embedding models work best with
focused, single-topic chunks.

Estonian legal text has specific structures that must not be broken
mid-reference:

    - ``\u00a7 123 lg 4 p 5`` (section/paragraph/point references)
    - ``\u00a7\u00a7 10\u201315`` (section ranges)
    - ``RT I, 2024, 3, 15`` (Riigi Teataja citations)

The chunker prefers sentence boundaries and avoids splitting inside
these patterns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RagChunk:
    """A single chunk ready for embedding and storage.

    Attributes:
        content: The chunk text.
        metadata: Arbitrary metadata dict (source_type, source_uri, etc.).
        chunk_index: Zero-based position within the source entity.
    """

    content: str
    metadata: dict
    chunk_index: int


# Patterns that should not be split across chunk boundaries.
# We look for these near candidate split points and nudge the split
# past them.
_LEGAL_REF_RE = re.compile(
    r"\u00a7\u00a7?\s*\d+"  # \u00a7 123 or \u00a7\u00a7 123
    r"(?:\s*lg\s*\d+)?"  # optional lg N
    r"(?:\s*p\s*\d+)?"  # optional p N
    r"(?:\s*\u2013\s*\d+)?",  # optional range \u2013N
    re.IGNORECASE,
)

# Estonian sentence-ending punctuation followed by whitespace.
_SENTENCE_END_RE = re.compile(r"[.!?]\s+")


def chunk_entity(
    content: str,
    metadata: dict,
    *,
    target_chars: int = 800,
    overlap_chars: int = 150,
) -> list[RagChunk]:
    """Split an ontology entity's text into RAG-sized chunks.

    Args:
        content: Full text of the entity (provision summary, court
            decision text, etc.).
        metadata: Dict to attach to every chunk (typically includes
            ``source_type`` and ``source_uri``).
        target_chars: Desired chunk length in characters.
        overlap_chars: Overlap between consecutive chunks for context
            continuity.

    Returns:
        List of :class:`RagChunk` objects in reading order.
        Short entities (fewer than ``target_chars // 1.6`` characters)
        produce a single chunk.

    Raises:
        ValueError: If ``target_chars <= 0`` or
            ``overlap_chars >= target_chars``.
    """
    if target_chars <= 0:
        raise ValueError("target_chars must be positive")
    if overlap_chars < 0:
        raise ValueError("overlap_chars must be non-negative")
    if overlap_chars >= target_chars:
        raise ValueError("overlap_chars must be smaller than target_chars")

    content = content.strip()
    if not content:
        return []

    # Short entities: single chunk (threshold is ~500 chars for default params)
    if len(content) <= int(target_chars * 0.625):
        return [RagChunk(content=content, metadata=metadata, chunk_index=0)]

    chunks: list[RagChunk] = []
    start = 0
    idx = 0
    total = len(content)

    while start < total:
        desired_end = min(start + target_chars, total)

        if desired_end >= total:
            # Last chunk
            chunks.append(
                RagChunk(
                    content=content[start:total].strip(),
                    metadata=metadata,
                    chunk_index=idx,
                )
            )
            break

        # Find the best split point
        end = _find_split_point(content, start, desired_end)

        chunk_text = content[start:end].strip()
        if chunk_text:
            chunks.append(
                RagChunk(
                    content=chunk_text,
                    metadata=metadata,
                    chunk_index=idx,
                )
            )
            idx += 1

        # Advance with overlap
        next_start = end - overlap_chars
        if next_start <= start:
            next_start = start + 1
        start = next_start

    return chunks


def _find_split_point(text: str, start: int, end: int) -> int:
    """Find the best character offset to split the text.

    Preference order:
        1. Paragraph break (``\\n\\n``) in the second half of the window.
        2. Sentence boundary (``. `` / ``! `` / ``? ``) that doesn't
           fall inside an Estonian legal reference.
        3. Hard cut at ``end`` if nothing better found.

    Returns the absolute index in ``text`` where the split should occur
    (exclusive — the chunk is ``text[start:result]``).
    """
    min_end = start + (end - start) // 2
    window = text[start:end]

    # 1. Paragraph break
    para = window.rfind("\n\n")
    if para != -1:
        absolute = start + para + 2
        if absolute > min_end:
            return absolute

    # 2. Sentence boundary — scan backwards from end
    for match in reversed(list(_SENTENCE_END_RE.finditer(window))):
        absolute = start + match.end()
        if absolute <= min_end:
            break
        # Check that the split doesn't land inside a legal reference
        if not _inside_legal_ref(text, absolute):
            return absolute

    # 3. Fallback: hard cut
    return end


def _inside_legal_ref(text: str, pos: int) -> bool:
    """Return True if ``pos`` falls inside an Estonian legal reference pattern.

    We check a window around the position for legal reference patterns
    and see if any span covers ``pos``.
    """
    # Check a generous window around the position
    check_start = max(0, pos - 40)
    check_end = min(len(text), pos + 40)
    window = text[check_start:check_end]

    for match in _LEGAL_REF_RE.finditer(window):
        ref_start = check_start + match.start()
        ref_end = check_start + match.end()
        if ref_start < pos < ref_end:
            return True
    return False
