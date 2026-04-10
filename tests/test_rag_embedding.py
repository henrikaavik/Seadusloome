"""Tests for ``app.rag.embedding`` — EmbeddingProvider and VoyageProvider.

All tests exercise the stub-mode path (no VOYAGE_API_KEY). The real-mode
tests mock the ``voyageai`` SDK to avoid network calls.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.rag.embedding import (
    EmbeddingProvider,
    VoyageProvider,
    _reset_default_embedding_provider,
    get_default_embedding_provider,
)


class TestEmbeddingProviderAbstract:
    def test_provider_is_abstract(self):
        """Instantiating the bare abstract class must raise TypeError."""
        with pytest.raises(TypeError):
            EmbeddingProvider()  # type: ignore[abstract]


class TestVoyageStubMode:
    def test_voyage_stubbed_when_no_key(self, monkeypatch: pytest.MonkeyPatch):
        """No VOYAGE_API_KEY -> stubbed mode."""
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)

        provider = VoyageProvider()
        assert provider._stubbed is True

    def test_dimensions_property(self, monkeypatch: pytest.MonkeyPatch):
        """dimensions returns 1024 by default."""
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)

        provider = VoyageProvider()
        assert provider.dimensions == 1024

    def test_custom_dimensions(self, monkeypatch: pytest.MonkeyPatch):
        """VOYAGE_DIMENSIONS env var overrides default."""
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
        monkeypatch.setenv("VOYAGE_DIMENSIONS", "768")

        provider = VoyageProvider()
        assert provider.dimensions == 768

    def test_embed_returns_correct_shape(self, monkeypatch: pytest.MonkeyPatch):
        """Stub embed returns list of vectors with correct dimensions."""
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)

        provider = VoyageProvider()
        texts = ["Tere maailm", "Tsiviilseadustik"]

        result = asyncio.run(provider.embed(texts))

        assert len(result) == 2
        assert len(result[0]) == 1024
        assert len(result[1]) == 1024
        assert all(isinstance(v, float) for v in result[0])

    def test_embed_empty_list(self, monkeypatch: pytest.MonkeyPatch):
        """Stub embed with empty input returns empty list."""
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)

        provider = VoyageProvider()
        result = asyncio.run(provider.embed([]))
        assert result == []

    def test_embed_deterministic_for_same_text(self, monkeypatch: pytest.MonkeyPatch):
        """Stub mode produces reproducible vectors for the same input."""
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)

        provider = VoyageProvider()
        r1 = asyncio.run(provider.embed(["test"]))
        r2 = asyncio.run(provider.embed(["test"]))

        assert r1[0] == r2[0]

    def test_embed_different_texts_different_vectors(self, monkeypatch: pytest.MonkeyPatch):
        """Stub mode produces different vectors for different inputs."""
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)

        provider = VoyageProvider()
        result = asyncio.run(provider.embed(["hello", "world"]))

        assert result[0] != result[1]


class TestVoyageRealMode:
    """Tests for non-stubbed code paths. SDK calls are mocked."""

    def _make_provider(self, monkeypatch: pytest.MonkeyPatch) -> VoyageProvider:
        monkeypatch.setenv("VOYAGE_API_KEY", "pa-test-key")
        return VoyageProvider()

    @patch("app.rag.embedding.VoyageProvider._log_cost")
    def test_embed_real_mode_calls_voyage(
        self,
        mock_log_cost: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Real mode calls the Voyage API with correct params."""
        provider = self._make_provider(monkeypatch)
        mock_client = MagicMock()
        provider._client = mock_client

        mock_response = SimpleNamespace(
            embeddings=[[0.1] * 1024, [0.2] * 1024],
            total_tokens=100,
        )
        mock_client.embed = AsyncMock(return_value=mock_response)

        result = asyncio.run(provider.embed(["hello", "world"]))

        assert len(result) == 2
        assert result[0] == [0.1] * 1024
        mock_client.embed.assert_called_once_with(
            ["hello", "world"],
            model="voyage-multilingual-2",
        )
        mock_log_cost.assert_called_once_with(100)

    @patch("app.rag.embedding.VoyageProvider._log_cost")
    def test_embed_no_total_tokens_skips_cost_log(
        self,
        mock_log_cost: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """If response has no total_tokens, cost logging is skipped."""
        provider = self._make_provider(monkeypatch)
        mock_client = MagicMock()
        provider._client = mock_client

        mock_response = SimpleNamespace(
            embeddings=[[0.1] * 1024],
            total_tokens=0,
        )
        mock_client.embed = AsyncMock(return_value=mock_response)

        asyncio.run(provider.embed(["test"]))

        mock_log_cost.assert_not_called()


class TestFactory:
    def test_get_default_returns_voyage(self, monkeypatch: pytest.MonkeyPatch):
        """Factory returns a VoyageProvider instance."""
        _reset_default_embedding_provider()
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)

        provider = get_default_embedding_provider()
        assert isinstance(provider, VoyageProvider)
        assert isinstance(provider, EmbeddingProvider)

    def test_get_default_returns_singleton(self, monkeypatch: pytest.MonkeyPatch):
        """Two calls return the same instance."""
        _reset_default_embedding_provider()
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)

        p1 = get_default_embedding_provider()
        p2 = get_default_embedding_provider()
        assert p1 is p2

        # Cleanup
        _reset_default_embedding_provider()
