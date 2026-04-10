"""Abstract LLM provider interface.

The concrete implementations (Claude today, potentially Codex or
EstBERT tomorrow) all satisfy this same narrow surface so that
advisory chat, the law drafter, and any ad-hoc tooling can swap
providers via configuration alone.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from uuid import UUID


@dataclass(frozen=True)
class StreamEvent:
    """A single event emitted during a streaming LLM completion.

    Attributes:
        type: One of ``"content"``, ``"tool_use"``, or ``"stop"``.
        delta: Text delta for ``"content"`` events; ``None`` otherwise.
        tool_name: Tool name for ``"tool_use"`` events; ``None`` otherwise.
        tool_input: Tool input dict for ``"tool_use"`` events; ``None`` otherwise.
    """

    type: str  # "content", "tool_use", "stop"
    delta: str | None = None
    tool_name: str | None = None
    tool_input: dict | None = field(default=None)


class LLMProvider(ABC):
    """Narrow abstract base class every LLM backend must implement."""

    # -- Synchronous methods ---------------------------------------------------

    @abstractmethod
    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        system: str | None = None,
        feature: str = "complete",
        user_id: UUID | str | None = None,
        org_id: UUID | str | None = None,
    ) -> str:
        """Return a free-form completion for *prompt*.

        Args:
            prompt: Fully formatted prompt string.
            max_tokens: Upper bound on generated tokens.
            temperature: Sampling temperature (0.0 = deterministic).
            system: Optional system prompt for the model.
            feature: Cost-tracking feature label (e.g. ``"drafter_clarify"``).
            user_id: Optional user id for cost tracking attribution.
            org_id: Optional org id for cost tracking attribution.
        """

    @abstractmethod
    def extract_json(
        self,
        prompt: str,
        *,
        schema: dict | None = None,
        feature: str = "extract_json",
        user_id: UUID | str | None = None,
        org_id: UUID | str | None = None,
    ) -> dict:
        """Run *prompt* through the model and parse the reply as JSON.

        Args:
            prompt: Prompt instructing the model to emit JSON.
            schema: Optional JSON schema the reply should conform to.
                Implementations may use this for constrained decoding
                or just validation; callers must not assume enforcement.
            user_id: Optional user id for cost tracking attribution.
            org_id: Optional org id for cost tracking attribution.
        """

    @abstractmethod
    def count_tokens(self, text: str) -> int:
        """Return the token count of *text* under this provider's tokenizer.

        Accuracy depends on the backend — stubbed providers are allowed
        to return a rough character-based estimate.
        """

    # -- Asynchronous methods --------------------------------------------------

    @abstractmethod
    async def acomplete(
        self,
        prompt: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        system: str | None = None,
        feature: str = "acomplete",
        user_id: UUID | str | None = None,
        org_id: UUID | str | None = None,
    ) -> str:
        """Async variant of :meth:`complete`.

        Args:
            prompt: Fully formatted prompt string.
            max_tokens: Upper bound on generated tokens.
            temperature: Sampling temperature (0.0 = deterministic).
            system: Optional system prompt for the model.
            feature: Cost-tracking feature label.
            user_id: Optional user id for cost tracking attribution.
            org_id: Optional org id for cost tracking attribution.
        """

    @abstractmethod
    async def astream(
        self,
        prompt: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        system: str | None = None,
        feature: str = "astream",
        user_id: UUID | str | None = None,
        org_id: UUID | str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Async streaming completion that yields :class:`StreamEvent` objects.

        Yields ``StreamEvent(type="content", delta=...)`` for text deltas,
        ``StreamEvent(type="tool_use", ...)`` for tool-use blocks, and
        ``StreamEvent(type="stop")`` when the stream is finished.

        Args:
            prompt: Fully formatted prompt string.
            max_tokens: Upper bound on generated tokens.
            temperature: Sampling temperature (0.0 = deterministic).
            system: Optional system prompt for the model.
            feature: Cost-tracking feature label.
            user_id: Optional user id for cost tracking attribution.
            org_id: Optional org id for cost tracking attribution.
        """
        # yield is needed to make this an async generator at the type level
        yield  # type: ignore[misc]  # pragma: no cover
