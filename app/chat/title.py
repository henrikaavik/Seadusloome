"""Generate short conversation titles from the opening chat exchange.

Called once per conversation, right after the first assistant reply
has been persisted, to produce a compact Estonian label used in the
sidebar/history list. The call is best-effort: on any error (feature
flag off, LLM failure, timeout) we return ``None`` and let the caller
fall back to a generic placeholder. Pricing-wise we lean on Claude's
cheapest Haiku tier via a small ``max_tokens`` budget.
"""

from __future__ import annotations

import logging
from uuid import UUID

from app.config import is_chat_auto_title_enabled
from app.llm import get_default_provider

logger = logging.getLogger(__name__)

_MAX_TITLE_CHARS = 48
_MAX_PROMPT_CHARS = 2000
_MAX_OUTPUT_TOKENS = 60

_SYSTEM_PROMPT = (
    "Genereeri lühike (kuni 48 märki) pealkiri järgnevale vestlusele. "
    "Vasta AINULT pealkirjaga, ilma jutumärkideta, ilma punktita lõpus."
)


def _clean_title(raw: str) -> str:
    """Strip whitespace, wrapping quotes, and trailing punctuation."""
    text = raw.strip()
    # Strip matching wrapping quotes (straight or typographic) repeatedly
    # in case the model layered them.
    quote_pairs = {
        '"': '"',
        "'": "'",
        "\u201c": "\u201d",  # “ ”
        "\u2018": "\u2019",  # ‘ ’
        "\u00ab": "\u00bb",  # « »
        "\u201e": "\u201c",  # „ “ (Estonian)
    }
    changed = True
    while changed and len(text) >= 2:
        changed = False
        for opener, closer in quote_pairs.items():
            if text.startswith(opener) and text.endswith(closer):
                text = text[len(opener) : -len(closer)].strip()
                changed = True
                break
    # Drop trailing punctuation the prompt told the model to avoid.
    text = text.rstrip(" .\t\n\r")
    if len(text) > _MAX_TITLE_CHARS:
        text = text[:_MAX_TITLE_CHARS].rstrip()
    return text


async def generate_title(
    user_message: str,
    assistant_reply: str,
    *,
    user_id: UUID | str | None = None,
    org_id: UUID | str | None = None,
) -> str | None:
    """Return a <=48 char Estonian title, or ``None`` on failure.

    Args:
        user_message: First user message in the conversation.
        assistant_reply: First assistant reply in the conversation.
        user_id: Optional user id for cost tracking attribution.
        org_id: Optional org id for cost tracking attribution.

    Returns:
        A cleaned title string no longer than 48 characters, or
        ``None`` if the feature is disabled or the LLM call fails.
    """
    if not is_chat_auto_title_enabled():
        return None

    prompt = f"Kasutaja: {user_message}\n\nAssistent: {assistant_reply}"
    if len(prompt) > _MAX_PROMPT_CHARS:
        prompt = prompt[:_MAX_PROMPT_CHARS]

    try:
        provider = get_default_provider()
        raw = await provider.acomplete(
            prompt,
            max_tokens=_MAX_OUTPUT_TOKENS,
            temperature=0.0,
            system=_SYSTEM_PROMPT,
            feature="chat_auto_title",
            user_id=user_id,
            org_id=org_id,
        )
    except Exception:
        logger.exception("generate_title: LLM call failed")
        return None

    if not raw:
        return None

    title = _clean_title(raw)
    return title or None
