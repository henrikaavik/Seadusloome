"""Abstract LLM provider interface.

The concrete implementations (Claude today, potentially Codex or
EstBERT tomorrow) all satisfy this same narrow surface so that
advisory chat, the law drafter, and any ad-hoc tooling can swap
providers via configuration alone.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class LLMProvider(ABC):
    """Narrow abstract base class every LLM backend must implement."""

    @abstractmethod
    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> str:
        """Return a free-form completion for *prompt*.

        Args:
            prompt: Fully formatted prompt string.
            max_tokens: Upper bound on generated tokens.
            temperature: Sampling temperature (0.0 = deterministic).
        """

    @abstractmethod
    def extract_json(self, prompt: str, *, schema: dict | None = None) -> dict:
        """Run *prompt* through the model and parse the reply as JSON.

        Args:
            prompt: Prompt instructing the model to emit JSON.
            schema: Optional JSON schema the reply should conform to.
                Implementations may use this for constrained decoding
                or just validation; callers must not assume enforcement.
        """

    @abstractmethod
    def count_tokens(self, text: str) -> int:
        """Return the token count of *text* under this provider's tokenizer.

        Accuracy depends on the backend — stubbed providers are allowed
        to return a rough character-based estimate.
        """
