"""Anthropic Claude concrete ``LLMProvider`` implementation.

Phase 2 shipped only the scaffolding; Phase 3A fills in real Anthropic
API calls. When ``ANTHROPIC_API_KEY`` is unset, the provider switches to
``_stubbed = True`` and returns canned responses so tests, local dev,
and CI never make network calls.

The real implementation path (when the SDK is installed and a key is
set) calls the Anthropic Messages API and logs token usage via
:mod:`app.llm.cost_tracker`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import AsyncIterator
from typing import Any

from app.llm.provider import LLMProvider, StreamEvent

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-6"


class ClaudeProvider(LLMProvider):
    """Anthropic Claude backend with a dev-mode stub path.

    Attributes:
        _stubbed: True when running with no API key; methods return
            canned responses instead of calling Anthropic.
        _api_key: The ``ANTHROPIC_API_KEY`` value (empty when stubbed).
        _model: Model identifier, from ``CLAUDE_MODEL`` env var.
    """

    def __init__(self) -> None:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()

        if not api_key:
            logger.warning(
                "ANTHROPIC_API_KEY not set — ClaudeProvider running in STUB mode. "
                "All completions return canned responses. Set ANTHROPIC_API_KEY "
                "in Coolify to enable real LLM extraction."
            )
            self._stubbed = True
            self._api_key = ""
        else:
            self._stubbed = False
            self._api_key = api_key

        self._model = os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL)
        # Lazy-initialised SDK clients; only built on first real call so
        # stub users never need the ``anthropic`` package installed.
        self._client: Any = None
        self._async_client: Any = None

    # -- helpers ------------------------------------------------------------

    def _get_client(self) -> Any:
        """Return a lazily-constructed Anthropic SDK client.

        Raises:
            RuntimeError: If the ``anthropic`` package isn't installed.
        """
        if self._client is not None:
            return self._client
        try:
            import anthropic  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "ClaudeProvider: the 'anthropic' package is not installed. "
                "Run `uv add anthropic` to add it."
            ) from exc
        self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def _log_cost(
        self,
        feature: str,
        tokens_input: int,
        tokens_output: int,
        *,
        user_id: Any = None,
        org_id: Any = None,
    ) -> None:
        """Log usage via cost_tracker. Import deferred to avoid circular deps."""
        from app.llm.cost_tracker import log_usage

        log_usage(
            user_id=user_id,
            org_id=org_id,
            provider="claude",
            model=self._model,
            feature=feature,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
        )

    # -- LLMProvider interface ---------------------------------------------

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        system: str | None = None,
        feature: str = "complete",
        user_id: Any = None,
        org_id: Any = None,
    ) -> str:
        """Return a completion for *prompt*.

        Stub mode returns a deterministic marker string so tests can
        assert on it without network I/O.
        """
        if self._stubbed:
            return f"[STUB Claude] {prompt[:40]}..."

        import anthropic as _anthropic

        client = self._get_client()

        create_kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            create_kwargs["system"] = system

        try:
            response = client.messages.create(**create_kwargs)
        except _anthropic.RateLimitError:
            logger.warning(
                "Anthropic rate limit hit (prompt length=%d). Retrying in 10s...",
                len(prompt),
            )
            time.sleep(10)
            try:
                response = client.messages.create(**create_kwargs)
            except Exception:
                logger.exception(
                    "Anthropic retry failed after rate limit (prompt length=%d)",
                    len(prompt),
                )
                raise
        except _anthropic.APITimeoutError:
            logger.warning(
                "Anthropic timeout (prompt length=%d). Retrying...",
                len(prompt),
            )
            try:
                response = client.messages.create(**create_kwargs)
            except Exception:
                logger.exception(
                    "Anthropic retry failed after timeout (prompt length=%d)",
                    len(prompt),
                )
                raise
        except _anthropic.APIError:
            logger.exception(
                "Anthropic API error (prompt length=%d)",
                len(prompt),
            )
            raise

        content = response.content[0].text if response.content else ""

        # Cost tracking
        self._log_cost(
            feature=feature,
            tokens_input=response.usage.input_tokens,
            tokens_output=response.usage.output_tokens,
            user_id=user_id,
            org_id=org_id,
        )

        return content

    def extract_json(
        self,
        prompt: str,
        *,
        schema: dict | None = None,
        feature: str = "extract_json",
        user_id: Any = None,
        org_id: Any = None,
    ) -> dict:
        """Run *prompt* through the model and parse the reply as JSON.

        Stub mode returns a deterministic dict; real mode wraps the
        prompt in a "respond with valid JSON" instruction and json-loads
        the reply.
        """
        if self._stubbed:
            return {"stub": True, "prompt": prompt[:40]}

        instruction = "Respond with a single valid JSON object and no surrounding prose."
        if schema is not None:
            instruction += f" The object must conform to this schema: {json.dumps(schema)}"

        raw = self.complete(
            prompt,
            max_tokens=2048,
            temperature=0.0,
            system=instruction,
            feature=feature,
            user_id=user_id,
            org_id=org_id,
        )
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # Try extracting JSON from markdown code blocks
            match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", raw, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(1))
                except json.JSONDecodeError:
                    pass

            logger.warning(
                "ClaudeProvider.extract_json: model did not return valid JSON "
                "(response length=%d)",
                len(raw),
            )
            return {"error": "failed to parse"}

    def count_tokens(self, text: str) -> int:
        """Return an approximate token count for *text*.

        Stub mode falls back to ``len(text) // 4``, which is a common
        rule of thumb for English/Estonian latin-script text. Real mode
        uses the Anthropic SDK's ``count_tokens`` helper if available.
        """
        if self._stubbed:
            return len(text) // 4

        client = self._get_client()
        count_fn = getattr(client, "count_tokens", None)
        if callable(count_fn):  # pragma: no cover
            result: Any = count_fn(text)
            return int(result)
        return len(text) // 4

    # -- async helpers ---------------------------------------------------------

    def _get_async_client(self) -> Any:
        """Return a lazily-constructed Anthropic AsyncAnthropic client.

        Raises:
            RuntimeError: If the ``anthropic`` package isn't installed.
        """
        if self._async_client is not None:
            return self._async_client
        try:
            import anthropic  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "ClaudeProvider: the 'anthropic' package is not installed. "
                "Run `uv add anthropic` to add it."
            ) from exc
        self._async_client = anthropic.AsyncAnthropic(api_key=self._api_key)
        return self._async_client

    # -- async LLMProvider interface -------------------------------------------

    async def acomplete(
        self,
        prompt: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        system: str | None = None,
        feature: str = "acomplete",
        user_id: Any = None,
        org_id: Any = None,
    ) -> str:
        """Async variant of :meth:`complete`.

        Stub mode returns a deterministic marker string so tests can
        assert on it without network I/O.
        """
        if self._stubbed:
            return f"[STUB Claude async] {prompt[:40]}..."

        import anthropic as _anthropic

        client = self._get_async_client()

        create_kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            create_kwargs["system"] = system

        try:
            response = await client.messages.create(**create_kwargs)
        except _anthropic.RateLimitError:
            logger.warning(
                "Anthropic async rate limit hit (prompt length=%d). Retrying...",
                len(prompt),
            )
            try:
                response = await client.messages.create(**create_kwargs)
            except Exception:
                logger.exception(
                    "Anthropic async retry failed after rate limit (prompt length=%d)",
                    len(prompt),
                )
                raise
        except _anthropic.APITimeoutError:
            logger.warning(
                "Anthropic async timeout (prompt length=%d). Retrying...",
                len(prompt),
            )
            try:
                response = await client.messages.create(**create_kwargs)
            except Exception:
                logger.exception(
                    "Anthropic async retry failed after timeout (prompt length=%d)",
                    len(prompt),
                )
                raise
        except _anthropic.APIError:
            logger.exception(
                "Anthropic async API error (prompt length=%d)",
                len(prompt),
            )
            raise

        content = response.content[0].text if response.content else ""

        self._log_cost(
            feature=feature,
            tokens_input=response.usage.input_tokens,
            tokens_output=response.usage.output_tokens,
            user_id=user_id,
            org_id=org_id,
        )

        return content

    async def astream(
        self,
        prompt: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        system: str | None = None,
        feature: str = "astream",
        user_id: Any = None,
        org_id: Any = None,
    ) -> AsyncIterator[StreamEvent]:
        """Async streaming completion yielding :class:`StreamEvent` objects.

        Stub mode yields a few canned events then ``StreamEvent(type="stop")``.
        Real mode wraps ``AsyncAnthropic.messages.stream()``.
        """
        if self._stubbed:
            yield StreamEvent(type="content", delta="[STUB] ")
            yield StreamEvent(type="content", delta="Tere! ")
            yield StreamEvent(type="content", delta="See on stub-vastus.")
            yield StreamEvent(type="stop")
            return

        client = self._get_async_client()

        create_kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            create_kwargs["system"] = system

        tokens_input = 0
        tokens_output = 0

        async with client.messages.stream(**create_kwargs) as stream:
            async for event in stream:
                if hasattr(event, "type"):
                    if event.type == "content_block_delta":
                        delta_obj = event.delta
                        if hasattr(delta_obj, "text"):
                            yield StreamEvent(type="content", delta=delta_obj.text)
                    elif event.type == "message_delta":
                        # End of message — capture final usage
                        usage = getattr(event, "usage", None)
                        if usage:
                            tokens_output = getattr(usage, "output_tokens", 0)
                    elif event.type == "message_start":
                        msg = getattr(event, "message", None)
                        if msg:
                            usage = getattr(msg, "usage", None)
                            if usage:
                                tokens_input = getattr(usage, "input_tokens", 0)

        self._log_cost(
            feature=feature,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            user_id=user_id,
            org_id=org_id,
        )

        yield StreamEvent(type="stop")


_default_provider: ClaudeProvider | None = None
_default_provider_lock = threading.Lock()


def _reset_default_provider() -> None:
    """Reset the singleton (for tests only)."""
    global _default_provider
    with _default_provider_lock:
        _default_provider = None


def get_default_provider() -> LLMProvider:
    """Return the project default ``LLMProvider`` singleton.

    Uses a module-level lock + singleton pattern (same approach as the
    Phase 2 #453 fix) to avoid creating a new ``ClaudeProvider`` on
    every call.

    Today that's Claude. Phase 3+ may read a ``LLM_PROVIDER`` env var
    here to choose between Claude and alternatives without changing
    every call site.
    """
    global _default_provider
    if _default_provider is None:
        with _default_provider_lock:
            if _default_provider is None:
                _default_provider = ClaudeProvider()
    return _default_provider
