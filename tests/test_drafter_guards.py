"""Tests for drafter pre-condition guards."""

import pytest

from app.drafter.errors import DrafterNotAvailableError
from app.drafter.guards import require_real_llm
from app.llm.claude import _reset_default_provider


class TestRequireRealLlm:
    def test_stubbed_provider_raises(self, monkeypatch: pytest.MonkeyPatch):
        """When Claude is in stub mode, drafter session creation must be blocked."""
        _reset_default_provider()
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("APP_ENV", "development")

        with pytest.raises(DrafterNotAvailableError, match="ANTHROPIC_API_KEY"):
            require_real_llm()

    def test_stubbed_provider_in_prod_raises(self, monkeypatch: pytest.MonkeyPatch):
        """Even in production, if the key is missing, drafter must block.

        Since #847 the provider itself fails closed: ``ClaudeProvider``
        refuses to instantiate without a key when stubs are disallowed,
        so the guard surfaces a ``RuntimeError`` from provider
        construction instead of a ``DrafterNotAvailableError`` from the
        ``_stubbed`` check. Either way the drafter cannot run on canned
        responses in production.
        """
        _reset_default_provider()
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            require_real_llm()

    def test_real_provider_passes(self, monkeypatch: pytest.MonkeyPatch):
        """When ANTHROPIC_API_KEY is set, the guard passes silently."""
        _reset_default_provider()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key-for-unit-test")
        monkeypatch.setenv("APP_ENV", "production")

        # Should not raise. The provider will be non-stubbed because
        # the key is set. We don't actually call Claude in this test —
        # the guard only checks the _stubbed flag.
        require_real_llm()

    def test_error_message_is_estonian(self, monkeypatch: pytest.MonkeyPatch):
        """The error message should be in Estonian for end-user display."""
        _reset_default_provider()
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        with pytest.raises(DrafterNotAvailableError) as exc_info:
            require_real_llm()

        assert "ANTHROPIC_API_KEY" in str(exc_info.value)
        assert "Coolify" in str(exc_info.value)
        assert "koostaja" in str(exc_info.value)  # "drafter" in Estonian
