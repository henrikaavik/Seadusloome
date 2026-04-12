"""PII / secret scrubbing for LLM prompt payloads.

NFR §7.1 requires emails, phone numbers, UUIDs, and secret-like tokens
to be redacted *before* prompts are sent to third-party LLM providers
such as Anthropic. This module is the single source of truth for that
regex set — ``app.observability._scrub_pii`` (Sentry ``before_send``)
also consumes :data:`SCRUB_PATTERNS` so we never drift between the two
egress paths.

Public surface
==============

* :data:`SCRUB_PATTERNS` — list of ``(compiled_pattern, placeholder)``
  tuples that define what gets redacted.
* :func:`scrub_prompt` — apply the patterns to a single string.
* :func:`scrub_messages` — apply the patterns to every message's
  ``content`` field in an Anthropic-style messages list, preserving
  ordering, role, and block shape.

Both functions accept ``allow_raw=True`` to opt out. This is reserved
for draft-analysis entry points that legitimately need to see verbatim
legal text — e.g. the entity extractor running over a user's draft
body. User-supplied free-form fields (chat turns, drafter intent) must
NOT opt out.
"""

from __future__ import annotations

import re

__all__ = [
    "SCRUB_PATTERNS",
    "scrub_prompt",
    "scrub_messages",
]


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------
#
# Order matters: PEM blocks are matched first so the inner base64/header
# lines don't get individually redacted by a narrower pattern. JWT-style
# ``eyJ...`` tokens and ``sk-``/``pk_`` prefixed secrets come before the
# generic UUID pattern because an API key can embed a UUID-looking
# segment. Emails and phones come last — they are the most constrained.

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# E.164 (+ country code, up to 15 digits total) OR Estonian national
# format (``+372`` / ``372`` / ``00372`` prefix with 7-8 digits). We
# keep the pattern conservative to avoid clobbering random numeric
# strings such as paragraph numbers.
_PHONE_RE = re.compile(
    r"(?<![\w.+])"
    r"(?:"
    r"\+[1-9]\d{7,14}"  # generic E.164
    r"|(?:\+?372|00372)[\s-]?\d{7,8}"  # Estonia explicit
    r")"
    r"(?![\w.])"
)

_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

# PEM-encoded private/public keys and certificates. Match the whole
# block non-greedily so a document with multiple blocks is handled.
_PEM_RE = re.compile(r"-----BEGIN [A-Z0-9 ]+-----[\s\S]+?-----END [A-Z0-9 ]+-----")

# JWT-style tokens (``eyJ`` base64 header + 2 more segments) and common
# secret prefixes (``sk-``, ``sk_``, ``pk-``, ``pk_``) followed by a
# sufficiently long opaque string. We require at least 16 chars after
# the prefix so short variable names like ``sk_id`` don't match.
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_TOKEN_RE = re.compile(r"\b(?:sk|pk)[-_][A-Za-z0-9_-]{16,}\b")


SCRUB_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # PEM first so inner lines aren't nibbled by other patterns.
    (_PEM_RE, "[REDACTED_KEY]"),
    (_JWT_RE, "[REDACTED_TOKEN]"),
    (_TOKEN_RE, "[REDACTED_TOKEN]"),
    (_EMAIL_RE, "[REDACTED_EMAIL]"),
    (_PHONE_RE, "[REDACTED_PHONE]"),
    (_UUID_RE, "[REDACTED_UUID]"),
]


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def scrub_prompt(text: str, *, allow_raw: bool = False) -> str:
    """Return *text* with PII / secret patterns replaced by placeholders.

    Args:
        text: The raw prompt string.
        allow_raw: When ``True``, return *text* unchanged. Reserved for
            callers whose whole purpose is to analyse verbatim draft
            content (e.g. legal reference extraction). User-supplied
            free-form fields must NOT set this.

    Returns:
        The scrubbed string (or the input unchanged when ``allow_raw``).
    """
    if allow_raw:
        return text
    if not text:
        return text
    scrubbed = text
    for pattern, placeholder in SCRUB_PATTERNS:
        scrubbed = pattern.sub(placeholder, scrubbed)
    return scrubbed


def scrub_messages(
    messages: list[dict],
    *,
    allow_raw: bool = False,
) -> list[dict]:
    """Return a copy of *messages* with every ``content`` field scrubbed.

    Supports both Anthropic Messages API shapes:

    - ``{"role": ..., "content": "plain string"}``
    - ``{"role": ..., "content": [{"type": "text", "text": "..."}, ...]}``

    Ordering, roles, and block metadata (``type``, ``tool_use_id`` …)
    are preserved verbatim; only free-form text is rewritten.

    Args:
        messages: Anthropic-style message list.
        allow_raw: When ``True``, return *messages* unchanged.

    Returns:
        A new list with scrubbed ``content`` values. The input is not
        mutated.
    """
    if allow_raw:
        return messages

    out: list[dict] = []
    for msg in messages:
        if not isinstance(msg, dict):
            out.append(msg)
            continue
        new_msg = dict(msg)
        content = new_msg.get("content")
        if isinstance(content, str):
            new_msg["content"] = scrub_prompt(content)
        elif isinstance(content, list):
            new_blocks: list[object] = []
            for block in content:
                if isinstance(block, dict):
                    new_block = dict(block)
                    # Anthropic text blocks use ``text``; tool_result
                    # blocks may nest content under ``content``.
                    if isinstance(new_block.get("text"), str):
                        new_block["text"] = scrub_prompt(new_block["text"])
                    inner = new_block.get("content")
                    if isinstance(inner, str):
                        new_block["content"] = scrub_prompt(inner)
                    new_blocks.append(new_block)
                else:
                    new_blocks.append(block)
            new_msg["content"] = new_blocks
        out.append(new_msg)
    return out
