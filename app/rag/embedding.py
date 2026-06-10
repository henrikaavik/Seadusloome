"""Embedding provider abstraction and Voyage AI implementation.

Phase 3C introduces a pluggable ``EmbeddingProvider`` interface following
the same pattern as :class:`app.llm.provider.LLMProvider`. The default
concrete implementation uses Voyage AI's ``voyage-multilingual-2`` model
(1024 dimensions), which excels at multilingual text including Estonian.

When ``VOYAGE_API_KEY`` is unset AND :func:`app.config.is_stub_allowed`
permits it (``APP_ENV`` in development/test/ci/staging), the provider
switches to stub mode and returns random vectors of the correct
dimensionality. This mirrors the ``ClaudeProvider`` pattern. In
production or any unrecognized ``APP_ENV`` (#847) a missing key raises
``RuntimeError`` at construction instead — random stub vectors must
never be persisted over real embeddings (the ingestion pipeline writes
with ``ON CONFLICT DO UPDATE``).
"""

from __future__ import annotations

import logging
import os
import random
import threading
from abc import ABC, abstractmethod
from typing import Any

from app.config import STUB_ALLOWED_ENVS, get_app_env, is_stub_allowed

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "voyage-multilingual-2"
DEFAULT_DIMENSIONS = 1024


class EmbeddingProvider(ABC):
    """Abstract base class for text embedding providers."""

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts into dense vectors.

        Args:
            texts: List of text strings to embed.

        Returns:
            List of embedding vectors, one per input text. Each vector
            has length equal to :attr:`dimensions`.
        """

    @property
    @abstractmethod
    def dimensions(self) -> int:
        """Return the dimensionality of the embedding vectors."""


class VoyageProvider(EmbeddingProvider):
    """Voyage AI embedding backend with a dev-mode stub path.

    Attributes:
        _stubbed: True when running with no API key in a stub-allowed
            environment; methods return random vectors instead of
            calling Voyage AI.
        _api_key: The ``VOYAGE_API_KEY`` value (empty when stubbed).
        _model: Model identifier, from ``VOYAGE_MODEL`` env var.
        _dimensions: Vector dimensionality for the selected model.

    Raises:
        RuntimeError: When ``VOYAGE_API_KEY`` is missing and
            :func:`app.config.is_stub_allowed` is False (production or
            an unrecognized ``APP_ENV``) — the fail-closed gate of #847.
    """

    def __init__(self) -> None:
        api_key = os.environ.get("VOYAGE_API_KEY", "").strip()

        if not api_key:
            if not is_stub_allowed():
                raise RuntimeError(
                    "VOYAGE_API_KEY is not set and stub mode is disabled "
                    f"for APP_ENV={get_app_env()!r} (stubs are only allowed in "
                    f"{sorted(STUB_ALLOWED_ENVS)}). Refusing to generate "
                    "random stub embeddings — they would be persisted over "
                    "real vectors by the ingestion pipeline. Set "
                    "VOYAGE_API_KEY, or fix APP_ENV if this is not a "
                    "production deployment (#847)."
                )
            logger.warning(
                "VOYAGE_API_KEY not set — VoyageProvider running in STUB mode. "
                "All embeddings return random vectors. Set VOYAGE_API_KEY "
                "to enable real embedding generation."
            )
            self._stubbed = True
            self._api_key = ""
        else:
            self._stubbed = False
            self._api_key = api_key

        self._model = os.environ.get("VOYAGE_MODEL", DEFAULT_MODEL)
        self._dimensions = int(os.environ.get("VOYAGE_DIMENSIONS", str(DEFAULT_DIMENSIONS)))
        # Lazy-initialised SDK client; only built on first real call so
        # stub users never need the ``voyageai`` package installed.
        self._client: Any = None

    # -- helpers ------------------------------------------------------------

    def _get_client(self) -> Any:
        """Return a lazily-constructed Voyage AI async client.

        Raises:
            RuntimeError: If the ``voyageai`` package isn't installed.
        """
        if self._client is not None:
            return self._client
        try:
            import voyageai  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "VoyageProvider: the 'voyageai' package is not installed. "
                "Run `uv add voyageai` to add it."
            ) from exc
        self._client = voyageai.AsyncClient(api_key=self._api_key)  # type: ignore[attr-defined]
        return self._client

    def _log_cost(self, token_count: int) -> None:
        """Log embedding usage via cost_tracker."""
        from app.llm.cost_tracker import log_usage

        log_usage(
            user_id=None,
            org_id=None,
            provider="voyage",
            model=self._model,
            feature="embedding",
            tokens_input=token_count,
            tokens_output=0,
        )

    # -- EmbeddingProvider interface ----------------------------------------

    @property
    def dimensions(self) -> int:
        """Return the dimensionality of Voyage embeddings."""
        return self._dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts using Voyage AI.

        Stub mode returns random vectors of the correct dimensionality
        seeded from the text content for reproducibility in tests.
        """
        if not texts:
            return []

        if self._stubbed:
            results: list[list[float]] = []
            for text in texts:
                # Use text hash as seed for reproducible stub vectors
                rng = random.Random(hash(text))
                vec = [rng.uniform(-1.0, 1.0) for _ in range(self._dimensions)]
                results.append(vec)
            return results

        client = self._get_client()

        # #354: retry transient errors (429/5xx/network) with bounded backoff.
        # Voyage AI uses httpx underneath and raises status-code-bearing errors,
        # so the same retry policy applies.
        from app.llm.retry import retry_async

        async def _call() -> Any:
            return await client.embed(texts, model=self._model)

        response = await retry_async(_call, context="voyage-embed")

        # Log cost (Voyage charges per token)
        total_tokens = getattr(response, "total_tokens", 0)
        if total_tokens:
            self._log_cost(total_tokens)

        return response.embeddings  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_default_provider: VoyageProvider | None = None
_default_provider_lock = threading.Lock()


def _reset_default_embedding_provider() -> None:
    """Reset the singleton (for tests only)."""
    global _default_provider
    with _default_provider_lock:
        _default_provider = None


def get_default_embedding_provider() -> EmbeddingProvider:
    """Return the project default ``EmbeddingProvider`` singleton.

    Uses the same lock + singleton pattern as
    :func:`app.llm.claude.get_default_provider`.
    """
    global _default_provider
    if _default_provider is None:
        with _default_provider_lock:
            if _default_provider is None:
                _default_provider = VoyageProvider()
    return _default_provider
