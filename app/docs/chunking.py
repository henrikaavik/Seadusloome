"""Text chunking for LLM-based entity extraction.

The ``parse_draft`` pipeline lands a blob of plain text in
``drafts.parsed_text`` and the next stage (``extract_entities``)
has to fan it out to the LLM. We can't send the whole blob: Claude
4.6's context is huge but latency and token cost both scale with
input length, and the entity extractor gets called once per draft
so the overhead adds up.

Instead we split the text into overlapping windows sized for fast,
cheap extraction calls.

Strategy:
    - Approximate tokens as ``chars / 4`` (common rule of thumb for
      latin-script text; Estonian is close enough). No ``tiktoken``
      dependency — Phase 3 can add real tokenisation if it matters.
    - Target ``24_000`` characters per chunk (~6000 tokens) with a
      ``500`` character overlap so references that straddle a chunk
      boundary show up in both windows.
    - Split preferentially on paragraph breaks (``\\n\\n``); fall
      back to sentence breaks (``". "``); hard-cut if neither fits.
    - The last chunk may be shorter than ``target_chars``.

Callers get a list of :class:`ChunkSpan` objects so downstream code
(entity extractor, dedupe, location recording) knows where each
window came from in the original document.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChunkSpan:
    """A contiguous slice of the source document.

    Attributes:
        start: Inclusive character offset in the source text.
        end: Exclusive character offset in the source text.
        text: The slice itself — ``source_text[start:end]``.
    """

    start: int
    end: int
    text: str


def chunk_text(
    text: str,
    *,
    target_chars: int = 24_000,
    overlap_chars: int = 500,
) -> list[ChunkSpan]:
    """Split *text* into overlapping chunks for LLM extraction.

    Args:
        text: The full document body.
        target_chars: Desired chunk length in characters.
        overlap_chars: Number of characters every chunk overlaps with
            the previous one. Must be smaller than ``target_chars``;
            ``0`` disables overlap entirely.

    Returns:
        A list of :class:`ChunkSpan` objects covering the whole input
        in reading order. Empty input returns an empty list.

    Raises:
        ValueError: If ``target_chars <= 0`` or
            ``overlap_chars >= target_chars`` (an overlap that eats
            the whole window would loop forever).
    """
    if target_chars <= 0:
        raise ValueError("target_chars must be positive")
    if overlap_chars < 0:
        raise ValueError("overlap_chars must be non-negative")
    if overlap_chars >= target_chars:
        raise ValueError("overlap_chars must be smaller than target_chars")

    if not text:
        return []

    total = len(text)
    if total <= target_chars:
        return [ChunkSpan(start=0, end=total, text=text)]

    chunks: list[ChunkSpan] = []
    start = 0
    while start < total:
        # Initial end position is either the hard target or end-of-text.
        desired_end = min(start + target_chars, total)

        if desired_end >= total:
            # Last chunk — just take whatever's left.
            chunks.append(ChunkSpan(start=start, end=total, text=text[start:total]))
            break

        # Look for a clean break inside [start, desired_end] so the chunk
        # ends on a paragraph or sentence boundary. We scan *backwards*
        # from desired_end toward start so we pick the break closest to
        # the target size rather than the very first one we find.
        boundary = _find_boundary(text, start, desired_end)
        end = boundary if boundary is not None else desired_end
        chunks.append(ChunkSpan(start=start, end=end, text=text[start:end]))

        # Advance start, accounting for overlap. Guard against zero-width
        # steps when the boundary search returned a position so close to
        # ``start`` that overlap would send us backwards.
        next_start = end - overlap_chars
        if next_start <= start:
            next_start = start + 1
        start = next_start

    return chunks


def _find_boundary(text: str, start: int, end: int) -> int | None:
    """Return the best split point inside ``text[start:end]`` or None.

    Preference order:
        1. Last ``"\\n\\n"`` (paragraph break) — returns the index just
           past the break so the next chunk doesn't start with blank
           lines.
        2. Last ``". "`` (sentence break) — same idea, returns the
           index just after the space.
        3. ``None`` if neither appears in the window; caller falls back
           to a hard cut at ``end``.

    The search is bounded to half the window ``[start, end)`` to avoid
    shrinking chunks too aggressively — if the last paragraph break
    is very close to ``start`` the chunk becomes tiny and we pay for
    an extra LLM call for almost no content. In that case we accept
    the hard cut instead.
    """
    # Only accept a boundary in the second half of the window so chunks
    # stay reasonably sized. This biases splits toward the desired
    # target length.
    min_end = start + (end - start) // 2

    # rfind on a slice is O(n) but the slice itself is only ``end-start``
    # chars, i.e. ``target_chars`` at most — fast enough.
    window = text[start:end]

    para = window.rfind("\n\n")
    if para != -1:
        absolute = start + para + 2  # skip past the "\n\n"
        if absolute > min_end:
            return absolute

    sentence = window.rfind(". ")
    if sentence != -1:
        absolute = start + sentence + 2  # skip past the ". "
        if absolute > min_end:
            return absolute

    return None
