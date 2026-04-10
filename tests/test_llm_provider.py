"""Unit tests for ``app.llm.provider`` and ``app.llm.claude``.

None of these tests make real network calls: they exercise the abstract
base class contract and the stubbed dev-mode path of ``ClaudeProvider``.
The real-mode tests mock the ``anthropic`` SDK.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.llm import ClaudeProvider, LLMProvider, get_default_provider


class TestLLMProviderAbstract:
    def test_provider_is_abstract(self):
        """Instantiating the bare abstract class must raise TypeError."""
        with pytest.raises(TypeError):
            LLMProvider()  # type: ignore[abstract]


class TestClaudeStubMode:
    def test_claude_stubbed_in_dev(self, monkeypatch: pytest.MonkeyPatch):
        """Dev + no API key -> stubbed complete() returns a marker."""
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
        """Stub count_tokens uses len(text) // 4."""
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        provider = ClaudeProvider()
        # 40 characters -> 10 tokens under the rough estimate.
        text = "x" * 40
        assert provider.count_tokens(text) == 10


class TestClaudeProdMode:
    def test_claude_stubs_in_prod_without_key(self, monkeypatch: pytest.MonkeyPatch):
        """APP_ENV=production + no key -> stub mode (NOT RuntimeError).

        Unlike STORAGE_ENCRYPTION_KEY (data-loss risk) and TIKA_URL
        (hard dep for parsing), the Anthropic key is explicitly optional
        per README Phase 2 Step 5.
        """
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        provider = ClaudeProvider()
        assert provider._stubbed is True
        assert provider.complete("test").startswith("[STUB Claude]")

    def test_claude_stubbed_in_staging(self, monkeypatch: pytest.MonkeyPatch):
        """#449: APP_ENV=staging is now explicitly stub-mode-eligible."""
        monkeypatch.setenv("APP_ENV", "staging")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        provider = ClaudeProvider()
        assert provider._stubbed is True


class TestClaudeRealMode:
    """Tests for non-stubbed code paths. SDK calls are mocked."""

    def _make_provider(self, monkeypatch: pytest.MonkeyPatch) -> ClaudeProvider:
        """Create a non-stubbed ClaudeProvider with a fake key."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
        monkeypatch.setenv("CLAUDE_MODEL", "claude-sonnet-4-6")
        return ClaudeProvider()

    def _make_response(
        self,
        text: str = "Hello",
        input_tokens: int = 10,
        output_tokens: int = 5,
    ) -> SimpleNamespace:
        """Build a mock Anthropic response object."""
        block = SimpleNamespace(text=text)
        usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
        return SimpleNamespace(content=[block], usage=usage)

    @patch("app.llm.claude.ClaudeProvider._log_cost")
    def test_complete_real_mode_calls_anthropic(
        self,
        mock_log_cost: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Real mode calls messages.create with correct params."""
        provider = self._make_provider(monkeypatch)
        mock_client = MagicMock()
        provider._client = mock_client

        mock_client.messages.create.return_value = self._make_response("Tere!")

        result = provider.complete(
            "Mis on tsiviilseadustik?",
            max_tokens=512,
            temperature=0.3,
        )

        assert result == "Tere!"
        mock_client.messages.create.assert_called_once()
        kw = mock_client.messages.create.call_args[1]
        assert kw["model"] == "claude-sonnet-4-6"
        assert kw["max_tokens"] == 512
        assert kw["temperature"] == 0.3
        assert kw["messages"] == [{"role": "user", "content": "Mis on tsiviilseadustik?"}]

        # Cost tracking was called
        mock_log_cost.assert_called_once_with(
            feature="complete", tokens_input=10, tokens_output=5, user_id=None, org_id=None
        )

    @patch("app.llm.claude.ClaudeProvider._log_cost")
    def test_complete_with_system_prompt(
        self,
        mock_log_cost: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """System prompt is passed through to the API."""
        provider = self._make_provider(monkeypatch)
        mock_client = MagicMock()
        provider._client = mock_client

        mock_client.messages.create.return_value = self._make_response("OK")

        provider.complete("test", system="You are a legal expert.")

        kw = mock_client.messages.create.call_args[1]
        assert kw["system"] == "You are a legal expert."

    @patch("app.llm.claude.ClaudeProvider._log_cost")
    def test_complete_without_system_prompt(
        self,
        mock_log_cost: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """When system is None, 'system' key is absent."""
        provider = self._make_provider(monkeypatch)
        mock_client = MagicMock()
        provider._client = mock_client

        mock_client.messages.create.return_value = self._make_response("OK")

        provider.complete("test")

        kw = mock_client.messages.create.call_args[1]
        assert "system" not in kw

    @patch("app.llm.claude.ClaudeProvider._log_cost")
    def test_complete_passes_feature_to_log(
        self,
        mock_log_cost: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Custom feature label flows through to cost tracking."""
        provider = self._make_provider(monkeypatch)
        mock_client = MagicMock()
        provider._client = mock_client

        mock_client.messages.create.return_value = self._make_response()

        provider.complete("test", feature="drafter_clarify")

        mock_log_cost.assert_called_once_with(
            feature="drafter_clarify",
            tokens_input=10,
            tokens_output=5,
            user_id=None,
            org_id=None,
        )

    @patch("app.llm.claude.ClaudeProvider._log_cost")
    def test_extract_json_parses_response(
        self,
        mock_log_cost: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """extract_json returns a parsed dict for valid JSON."""
        provider = self._make_provider(monkeypatch)
        mock_client = MagicMock()
        provider._client = mock_client

        mock_client.messages.create.return_value = self._make_response('{"key": "value"}')

        result = provider.extract_json("extract entities")

        assert isinstance(result, dict)
        assert result == {"key": "value"}

    @patch("app.llm.claude.ClaudeProvider._log_cost")
    def test_extract_json_handles_markdown_code_block(
        self,
        mock_log_cost: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """extract_json extracts JSON from markdown code fences."""
        provider = self._make_provider(monkeypatch)
        mock_client = MagicMock()
        provider._client = mock_client

        text = '```json\n{"entities": ["TsMS", "TsUS"]}\n```'
        mock_client.messages.create.return_value = self._make_response(text)

        result = provider.extract_json("extract entities")

        assert isinstance(result, dict)
        assert result == {"entities": ["TsMS", "TsUS"]}

    @patch("app.llm.claude.ClaudeProvider._log_cost")
    def test_extract_json_returns_error_on_unparseable(
        self,
        mock_log_cost: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """extract_json returns error dict for non-JSON response."""
        provider = self._make_provider(monkeypatch)
        mock_client = MagicMock()
        provider._client = mock_client

        mock_client.messages.create.return_value = self._make_response("I cannot provide JSON.")

        result = provider.extract_json("extract entities")

        assert isinstance(result, dict)
        assert result == {"error": "failed to parse"}

    @patch("app.llm.claude.time.sleep")
    @patch("app.llm.claude.ClaudeProvider._log_cost")
    def test_complete_rate_limit_retries(
        self,
        mock_log_cost: MagicMock,
        mock_sleep: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """RateLimitError triggers a 10s sleep + retry."""
        import anthropic

        provider = self._make_provider(monkeypatch)
        mock_client = MagicMock()
        provider._client = mock_client

        rate_err = anthropic.RateLimitError(
            message="rate limited",
            response=MagicMock(status_code=429),
            body=None,
        )
        mock_client.messages.create.side_effect = [
            rate_err,
            self._make_response("Success after retry"),
        ]

        result = provider.complete("test prompt")

        assert result == "Success after retry"
        mock_sleep.assert_called_once_with(10)
        assert mock_client.messages.create.call_count == 2

    @patch("app.llm.claude.ClaudeProvider._log_cost")
    def test_complete_api_error_raises(
        self,
        mock_log_cost: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Non-retryable APIError is logged and re-raised."""
        import anthropic

        provider = self._make_provider(monkeypatch)
        mock_client = MagicMock()
        provider._client = mock_client

        api_error = anthropic.APIError(
            message="internal error",
            request=MagicMock(),
            body=None,
        )
        mock_client.messages.create.side_effect = api_error

        with pytest.raises(anthropic.APIError):
            provider.complete("test prompt")


class TestFactory:
    def test_get_default_provider_returns_claude(self, monkeypatch: pytest.MonkeyPatch):
        """The factory currently returns a ClaudeProvider instance."""
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        provider = get_default_provider()
        assert isinstance(provider, ClaudeProvider)
        assert isinstance(provider, LLMProvider)
