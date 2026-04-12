"""Unit tests for ``app.llm.provider`` and ``app.llm.claude``.

None of these tests make real network calls: they exercise the abstract
base class contract and the stubbed dev-mode path of ``ClaudeProvider``.
The real-mode tests mock the ``anthropic`` SDK.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.llm import (
    ClaudeProvider,
    LLMProvider,
    StreamEvent,
    _reset_default_provider,
    get_default_provider,
)


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
    def test_complete_scrubs_pii_before_send(
        self,
        mock_log_cost: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """NFR §7.1: emails in the prompt must not reach Anthropic.

        The captured ``messages`` payload must contain the placeholder,
        never the raw address.
        """
        provider = self._make_provider(monkeypatch)
        mock_client = MagicMock()
        provider._client = mock_client

        mock_client.messages.create.return_value = self._make_response("ok")

        provider.complete(
            "please email foo@example.com about UUID 550e8400-e29b-41d4-a716-446655440000"
        )

        kw = mock_client.messages.create.call_args[1]
        sent = kw["messages"][0]["content"]
        assert "foo@example.com" not in sent
        assert "550e8400-e29b-41d4-a716-446655440000" not in sent
        assert "[REDACTED_EMAIL]" in sent
        assert "[REDACTED_UUID]" in sent

    @patch("app.llm.claude.ClaudeProvider._log_cost")
    def test_complete_allow_raw_preserves_prompt(
        self,
        mock_log_cost: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """allow_raw=True bypasses the scrubber (draft-analysis path)."""
        provider = self._make_provider(monkeypatch)
        mock_client = MagicMock()
        provider._client = mock_client

        mock_client.messages.create.return_value = self._make_response("ok")

        prompt = "verbatim draft references foo@example.com"
        provider.complete(prompt, allow_raw=True)

        kw = mock_client.messages.create.call_args[1]
        assert kw["messages"][0]["content"] == prompt

    @patch("app.llm.claude.ClaudeProvider._log_cost")
    def test_complete_scrubs_system_prompt(
        self,
        mock_log_cost: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """PII in the system prompt is scrubbed too."""
        provider = self._make_provider(monkeypatch)
        mock_client = MagicMock()
        provider._client = mock_client

        mock_client.messages.create.return_value = self._make_response("ok")

        provider.complete("hi", system="operator: ops@example.ee")

        kw = mock_client.messages.create.call_args[1]
        assert "ops@example.ee" not in kw["system"]
        assert "[REDACTED_EMAIL]" in kw["system"]

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
        _reset_default_provider()
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        provider = get_default_provider()
        assert isinstance(provider, ClaudeProvider)
        assert isinstance(provider, LLMProvider)

    def test_get_default_provider_returns_singleton(self, monkeypatch: pytest.MonkeyPatch):
        """H3: Two calls to get_default_provider return the same instance."""
        _reset_default_provider()
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        p1 = get_default_provider()
        p2 = get_default_provider()
        assert p1 is p2


# ---------------------------------------------------------------------------
# Async methods — stub mode
# ---------------------------------------------------------------------------


class TestAsyncStubMode:
    def test_acomplete_stub_mode(self, monkeypatch: pytest.MonkeyPatch):
        """Dev + no API key -> stubbed acomplete() returns a marker."""
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        provider = ClaudeProvider()
        assert provider._stubbed is True

        result = asyncio.run(provider.acomplete("Mis on tsiviilseadustik?"))
        assert result.startswith("[STUB Claude async]")
        assert "Mis on tsiviilseadustik" in result

    def test_astream_stub_yields_events(self, monkeypatch: pytest.MonkeyPatch):
        """Stub astream yields content events then a stop event."""
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        provider = ClaudeProvider()

        async def _collect():
            events = []
            async for event in provider.astream("test prompt"):
                events.append(event)
            return events

        events = asyncio.run(_collect())

        # Should have content events followed by a stop event
        assert len(events) >= 2
        content_events = [e for e in events if e.type == "content"]
        stop_events = [e for e in events if e.type == "stop"]
        assert len(content_events) >= 1
        assert len(stop_events) == 1
        assert stop_events[0] == StreamEvent(type="stop")

        # Content deltas should contain stub text
        full_text = "".join(e.delta or "" for e in content_events)
        assert "[STUB]" in full_text


# ---------------------------------------------------------------------------
# Async methods — real mode (mocked)
# ---------------------------------------------------------------------------


class TestAsyncRealMode:
    """Tests for non-stubbed async code paths. SDK calls are mocked."""

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
    def test_acomplete_real_mode(
        self,
        mock_log_cost: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Real mode acomplete calls messages.create on the async client."""
        provider = self._make_provider(monkeypatch)
        mock_client = MagicMock()
        provider._async_client = mock_client

        mock_client.messages.create = AsyncMock(return_value=self._make_response("Tere!"))

        result = asyncio.run(
            provider.acomplete(
                "Mis on tsiviilseadustik?",
                max_tokens=512,
                temperature=0.3,
            )
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
            feature="acomplete", tokens_input=10, tokens_output=5, user_id=None, org_id=None
        )

    @patch("app.llm.claude.ClaudeProvider._log_cost")
    def test_astream_real_mode(
        self,
        mock_log_cost: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Real mode astream yields events from the streaming API."""
        provider = self._make_provider(monkeypatch)
        mock_client = MagicMock()
        provider._async_client = mock_client

        # Build mock streaming events
        event_content = SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(text="Tere maailm"),
        )
        event_msg_start = SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                usage=SimpleNamespace(input_tokens=15),
            ),
        )
        event_msg_delta = SimpleNamespace(
            type="message_delta",
            usage=SimpleNamespace(output_tokens=8),
        )

        # Create an async iterator for the mock stream
        class _MockStream:
            def __init__(self):
                self._events = [event_msg_start, event_content, event_msg_delta]
                self._idx = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self._idx >= len(self._events):
                    raise StopAsyncIteration
                evt = self._events[self._idx]
                self._idx += 1
                return evt

        mock_stream = _MockStream()

        # Mock the async context manager
        class _MockStreamCtx:
            async def __aenter__(self):
                return mock_stream

            async def __aexit__(self, *args):
                pass

        mock_client.messages.stream = MagicMock(return_value=_MockStreamCtx())

        async def _collect():
            events = []
            async for event in provider.astream("test prompt"):
                events.append(event)
            return events

        events = asyncio.run(_collect())

        # Should have content + stop events
        content_events = [e for e in events if e.type == "content"]
        stop_events = [e for e in events if e.type == "stop"]
        assert len(content_events) == 1
        assert content_events[0].delta == "Tere maailm"
        assert len(stop_events) == 1

        # Cost tracking was called with captured usage
        mock_log_cost.assert_called_once_with(
            feature="astream",
            tokens_input=15,
            tokens_output=8,
            user_id=None,
            org_id=None,
        )
