"""Unit tests for ``app.llm.provider`` and ``app.llm.claude``.

None of these tests make real network calls: they exercise the abstract
base class contract and the stubbed dev-mode path of ``ClaudeProvider``.
Phase 3 will add integration tests behind a network marker.
"""

from __future__ import annotations

import pytest

from app.llm import ClaudeProvider, LLMProvider, get_default_provider


class TestLLMProviderAbstract:
    def test_provider_is_abstract(self):
        """Instantiating the bare abstract class must raise TypeError."""
        with pytest.raises(TypeError):
            LLMProvider()  # type: ignore[abstract]


class TestClaudeStubMode:
    def test_claude_stubbed_in_dev(self, monkeypatch: pytest.MonkeyPatch):
        """Dev + no API key → stubbed complete() returns a marker string."""
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        provider = ClaudeProvider()
        assert provider._stubbed is True

        result = provider.complete("Mis on tsiviilseadustik?")
        assert result.startswith("[STUB Claude]")
        assert "Mis on tsiviilseadustik" in result

    def test_extract_json_returns_dict_when_stubbed(self, monkeypatch: pytest.MonkeyPatch):
        """Stubbed extract_json returns a deterministic dict."""
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        provider = ClaudeProvider()
        result = provider.extract_json("summarise this draft")

        assert isinstance(result, dict)
        assert result["stub"] is True
        assert "summarise" in result["prompt"]

    def test_count_tokens_rough_estimate(self, monkeypatch: pytest.MonkeyPatch):
        """Stub count_tokens uses len(text) // 4 as a rough estimate."""
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        provider = ClaudeProvider()
        # 40 characters → 10 tokens under the rough estimate.
        text = "x" * 40
        assert provider.count_tokens(text) == 10


class TestClaudeProdMode:
    def test_claude_raises_in_prod_without_key(self, monkeypatch: pytest.MonkeyPatch):
        """Off-dev + no key must fail ClaudeProvider __init__."""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            ClaudeProvider()


class TestFactory:
    def test_get_default_provider_returns_claude(self, monkeypatch: pytest.MonkeyPatch):
        """The factory currently returns a ClaudeProvider instance."""
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        provider = get_default_provider()
        assert isinstance(provider, ClaudeProvider)
        assert isinstance(provider, LLMProvider)
