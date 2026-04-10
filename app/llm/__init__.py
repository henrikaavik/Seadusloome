"""Pluggable LLM provider abstraction.

Downstream code should depend on the abstract ``LLMProvider`` so
swapping Claude for Codex or a local model later doesn't require
touching call sites.

Typical usage:

    from app.llm import get_default_provider

    provider = get_default_provider()
    answer = provider.complete("Mis on Tsiviilseadustiku üldosa?")
"""

from app.llm.claude import ClaudeProvider, _reset_default_provider, get_default_provider
from app.llm.provider import LLMProvider, StreamEvent

__all__ = [
    "ClaudeProvider",
    "LLMProvider",
    "StreamEvent",
    "_reset_default_provider",
    "get_default_provider",
]
