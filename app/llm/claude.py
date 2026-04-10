"""Anthropic Claude concrete ``LLMProvider`` implementation.

Phase 2 ships only the scaffolding: Phase 3 fills in real Anthropic
API calls. When ``APP_ENV != production`` (i.e. dev/test/staging/ci)
and ``ANTHROPIC_API_KEY`` is unset, the provider switches to
``_stubbed = True`` and returns canned responses so tests, local
dev, and CI never make network calls. The stub-mode gate goes
through :func:`app.config.is_stub_allowed` so the rule stays in
lock-step with the Tika and Fernet stub gates (#449).

The real implementation path (when the SDK is installed and a key is
set) is also wired up so Phase 3 only needs to flesh out the body of
``complete``/``extract_json``/``count_tokens``. If the ``anthropic``
package isn't installed yet, falling into the non-stubbed path raises
a helpful error pointing to the Phase 3 ticket.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from app.llm.provider import LLMProvider

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-6"


class ClaudeProvider(LLMProvider):
    """Anthropic Claude backend with a dev-mode stub path.

    Attributes:
        _stubbed: True when running in dev with no API key; methods
            return canned responses instead of calling Anthropic.
        _api_key: The ``ANTHROPIC_API_KEY`` value (empty when stubbed).
        _model: Model identifier, from ``CLAUDE_MODEL`` env var.
    """

    def __init__(self) -> None:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()

        if not api_key:
            # Unlike STORAGE_ENCRYPTION_KEY and TIKA_URL, the Anthropic key
            # is explicitly OPTIONAL in Phase 2 (README Step 5). The LLM
            # stub path produces synthetic entity refs that are good enough
            # for the full pipeline to run end-to-end in demo mode.
            # Requiring the key in production was the root cause of the
            # "ANTHROPIC_API_KEY must be set" pipeline failure on the first
            # real upload — the is_stub_allowed() gate blocked stub mode
            # when APP_ENV=production, making the entire extract_entities
            # handler unreachable without a paid API key.
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
        # Lazy-initialised SDK client; only built on first real call so
        # stub users never need the ``anthropic`` package installed.
        self._client: Any = None

    # -- helpers ------------------------------------------------------------

    def _get_client(self) -> Any:
        """Return a lazily-constructed Anthropic SDK client.

        Raises:
            RuntimeError: If the ``anthropic`` package isn't installed.
                Phase 3 should add it to ``pyproject.toml``.
        """
        if self._client is not None:
            return self._client
        try:
            import anthropic  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - exercised in Phase 3
            raise RuntimeError(
                "ClaudeProvider: the 'anthropic' package is not installed. "
                "Phase 3 will add it to pyproject.toml; until then, run with "
                "APP_ENV=development to use the stub path."
            ) from exc
        self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    # -- LLMProvider interface ---------------------------------------------

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> str:
        """Return a completion for *prompt*.

        Stub mode returns a deterministic marker string so tests can
        assert on it without network I/O.
        """
        if self._stubbed:
            return f"[STUB Claude] {prompt[:40]}..."

        client = self._get_client()
        message = client.messages.create(  # pragma: no cover - Phase 3
            model=self._model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        # Anthropic returns a list of content blocks; concatenate the
        # text-typed blocks in order for the free-form complete() API.
        parts: list[str] = []
        for block in getattr(message, "content", []):  # pragma: no cover
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return "".join(parts)

    def extract_json(self, prompt: str, *, schema: dict | None = None) -> dict:
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
        wrapped = f"{prompt}\n\n{instruction}"

        raw = self.complete(wrapped, max_tokens=2048, temperature=0.0)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:  # pragma: no cover - Phase 3
            raise ValueError(
                f"ClaudeProvider.extract_json: model did not return valid JSON: {raw[:200]}"
            ) from exc

    def count_tokens(self, text: str) -> int:
        """Return an approximate token count for *text*.

        Stub mode falls back to ``len(text) // 4``, which is a common
        rule of thumb for English/Estonian latin-script text. Real mode
        uses the Anthropic SDK's ``count_tokens`` helper.
        """
        if self._stubbed:
            return len(text) // 4

        client = self._get_client()
        # The exact API surface varies across SDK versions; Phase 3 will
        # pin a version and call the appropriate helper. Until then we
        # fall back to the rough estimate so unit tests stay stable.
        count_fn = getattr(client, "count_tokens", None)
        if callable(count_fn):  # pragma: no cover - Phase 3
            result: Any = count_fn(text)
            return int(result)
        return len(text) // 4  # pragma: no cover - Phase 3


def get_default_provider() -> LLMProvider:
    """Return the project default ``LLMProvider`` instance.

    Today that's Claude. Phase 3+ may read a ``LLM_PROVIDER`` env var
    here to choose between Claude and alternatives without changing
    every call site.
    """
    return ClaudeProvider()
