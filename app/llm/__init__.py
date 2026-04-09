"""Pluggable LLM provider abstraction.

Phase 2 only scaffolds the interface; real API calls land in Phase 3
(AI Advisory Chat + AI Law Drafter). Downstream code should depend on
the abstract ``LLMProvider`` so swapping Claude for Codex or a local
model later doesn't require touching call sites.

Typical usage:

    from app.llm import get_default_provider

    provider = get_default_provider()
    answer = provider.complete("Mis on Tsiviilseadustiku üldosa?")
"""

from app.llm.claude import ClaudeProvider, get_default_provider
from app.llm.provider import LLMProvider

__all__ = ["ClaudeProvider", "LLMProvider", "get_default_provider"]
