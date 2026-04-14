"""Safe markdown rendering for chat content.

Renders assistant markdown (and plaintext user messages) to sanitised HTML
with auto-linked URLs and auto-linked Estonian/EU-style legal citations
(for example ``KarS § 113``, ``TsUS § 5 lg 2``, ``Art. 5 loige 2``).

Pipeline for :func:`render_markdown_safe`:

1. Render markdown to HTML via :mod:`mistune` (zero-config, fast).
2. Sanitise with :mod:`bleach` using a strict tag/attribute allowlist and
   force safe link attributes (``target="_blank"``,
   ``rel="noopener noreferrer"``).
3. Linkify bare ``http(s)://`` URLs.
4. Post-process with BeautifulSoup to wrap legal citations in
   ``<a class="citation-link" href="/explorer?q=...">`` -- but only
   inside plain text nodes, never inside existing ``<a>`` or ``<code>``
   elements.

This module replaces the ad-hoc regex-only approach used previously in
``app.chat.routes._format_assistant_content``.
"""

from __future__ import annotations

import html
import logging
import re
from urllib.parse import quote_plus

import bleach
import mistune
from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Allowlist configuration
# ---------------------------------------------------------------------------

ALLOWED_TAGS: list[str] = [
    # Block
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "blockquote",
    "ul",
    "ol",
    "li",
    "pre",
    "code",
    "hr",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "br",
    # Inline
    "strong",
    "em",
    "del",
    "a",
    "span",
]

ALLOWED_ATTRIBUTES: dict[str, list[str]] = {
    "a": ["href", "title", "target", "rel"],
    "span": ["class"],
    "code": ["class"],
    "pre": ["class"],
    "th": ["align"],
    "td": ["align"],
}

ALLOWED_PROTOCOLS: list[str] = ["http", "https", "mailto"]


# ---------------------------------------------------------------------------
# Citation patterns (Estonian + EU style)
# ---------------------------------------------------------------------------

# Matches Estonian law abbreviations / names followed by section marker:
#   "KarS § 113", "TsUS § 5 lg 2", "PS § 13", "Pohiseadus § 13 lg 1 p 2"
_CITATION_PARAGRAPH_RE = re.compile(
    r"\b[A-ZÕÄÖÜ][A-Za-zÕÄÖÜõäöü]{1,10}\s*§\s*\d+"
    r"(?:\s*lg\s*\d+)?"
    r"(?:\s*p\s*\d+)?\b"
)

# Matches EU / international Article style: "Art. 5", "Art. 5 loige 2"
_CITATION_ARTICLE_RE = re.compile(r"\bArt\.\s*\d+(?:\s*l[õo]ige\s*\d+)?\b")

_CITATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    _CITATION_PARAGRAPH_RE,
    _CITATION_ARTICLE_RE,
)

# Tags whose descendant text must NOT be rewritten (already a link, or
# inside code / pre blocks).
_SKIP_CITATION_PARENTS: frozenset[str] = frozenset({"a", "code", "pre"})


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------

# Single module-level markdown instance. ``mistune.create_markdown`` is
# thread-safe for rendering; escape=False is fine because we run every
# output through bleach afterwards.
_markdown = mistune.create_markdown(
    escape=False,
    plugins=["strikethrough", "table", "url"],
)


def _force_safe_link_attrs(attrs: dict, new: bool = False) -> dict:
    """bleach linkify/cleaner callback -- force safe external-link attrs."""
    # Keys in bleach callbacks are (namespace, name) tuples.
    attrs[(None, "target")] = "_blank"
    attrs[(None, "rel")] = "noopener noreferrer"
    return attrs


def _sanitize_html(raw_html: str) -> str:
    """Run bleach clean + linkify on rendered markdown HTML."""
    cleaned = bleach.clean(
        raw_html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )
    # Auto-link bare URLs and normalise link attributes on all anchors.
    # bleach's stubs type callbacks narrowly; the runtime signature we use
    # matches the documented API, so we cast via ``type: ignore``.
    linkified = bleach.linkify(
        cleaned,
        callbacks=[_force_safe_link_attrs],  # type: ignore[list-item]
        skip_tags=["pre", "code"],
        parse_email=False,
    )
    return linkified


def _wrap_citations_in_text(soup: BeautifulSoup) -> None:
    """Wrap legal citations in text nodes with ``<a class="citation-link">``.

    Skips any text inside ``<a>``, ``<code>``, or ``<pre>`` elements so
    that existing links are preserved and code samples remain untouched.
    """
    # Collect nodes first; mutating during iteration confuses the tree walker.
    candidates: list[NavigableString] = []
    for text_node in soup.find_all(string=True):
        # Walk up ancestors; skip if any is in the skip set.
        skip = False
        parent = text_node.parent
        while parent is not None and parent.name is not None:
            if parent.name in _SKIP_CITATION_PARENTS:
                skip = True
                break
            parent = parent.parent
        if skip:
            continue
        candidates.append(text_node)

    for text_node in candidates:
        original = str(text_node)
        # Find all matches across all citation patterns, then splice.
        matches: list[tuple[int, int, str]] = []
        for pattern in _CITATION_PATTERNS:
            for m in pattern.finditer(original):
                matches.append((m.start(), m.end(), m.group(0)))
        if not matches:
            continue

        # Sort by start position; drop overlaps (keep earliest/longest).
        matches.sort(key=lambda t: (t[0], -(t[1] - t[0])))
        non_overlapping: list[tuple[int, int, str]] = []
        last_end = -1
        for start, end, text in matches:
            if start < last_end:
                continue
            non_overlapping.append((start, end, text))
            last_end = end

        # Build replacement fragments.
        new_nodes: list[NavigableString | Tag] = []
        cursor = 0
        for start, end, citation_text in non_overlapping:
            if start > cursor:
                new_nodes.append(NavigableString(original[cursor:start]))
            anchor = soup.new_tag(
                "a",
                href=f"/explorer?q={quote_plus(citation_text)}",
            )
            # Multi-valued class attribute: bs4 accepts a list at runtime but
            # its stubs only type ``str``. The list form is preferred because
            # it preserves whitespace handling across serialisation.
            anchor["class"] = ["citation-link"]  # type: ignore[assignment]
            anchor.string = citation_text
            new_nodes.append(anchor)
            cursor = end
        if cursor < len(original):
            new_nodes.append(NavigableString(original[cursor:]))

        # Replace the original text node with the expanded sequence.
        first, *rest = new_nodes
        text_node.replace_with(first)
        anchor_point: NavigableString | Tag = first
        for node in rest:
            anchor_point.insert_after(node)
            anchor_point = node


def _apply_citation_links(html_fragment: str) -> str:
    """Post-process HTML to wrap Estonian/EU legal citations in anchors."""
    if not html_fragment:
        return html_fragment
    soup = BeautifulSoup(html_fragment, "html.parser")
    _wrap_citations_in_text(soup)
    return str(soup)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_markdown_safe(content: str) -> str:
    """Render user/LLM markdown to sanitised HTML.

    Auto-links bare URLs and Estonian/EU-style legal citations. Strips
    any tag or attribute not in the allowlist, defeating XSS attempts
    such as ``<script>``, ``<img onerror=...>``, and ``javascript:`` URLs.
    """
    if not content:
        return ""
    try:
        rendered = _markdown(content)
        if not isinstance(rendered, str):  # defensive: mistune can return state
            rendered = str(rendered)
        cleaned = _sanitize_html(rendered)
        return _apply_citation_links(cleaned)
    except Exception:  # pragma: no cover - defensive
        logger.exception("Failed to render markdown; falling back to escape")
        return render_plaintext_safe(content)


def render_plaintext_safe(content: str) -> str:
    """Escape plain text and convert newlines to ``<br>`` tags.

    Used for user messages where markdown should NOT be interpreted --
    the user's literal text is displayed verbatim, with HTML special
    characters escaped so the output cannot inject markup.
    """
    if not content:
        return ""
    escaped = html.escape(content, quote=False)
    return escaped.replace("\r\n", "\n").replace("\n", "<br>")
