"""Drafter-specific exceptions."""


class DrafterNotAvailableError(Exception):
    """Raised when the AI drafter cannot run — typically because
    ANTHROPIC_API_KEY is not set and ClaudeProvider is in stub mode.

    The drafter requires real LLM calls to generate legal text. Unlike
    the chat (Phase 3B) which can gracefully show "AI not available",
    the drafter's core functionality is impossible without a real
    provider — so we block session creation upfront rather than let the
    user go through 7 steps with useless output.
    """
