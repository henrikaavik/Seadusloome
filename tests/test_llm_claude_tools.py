# pyright: reportArgumentType=false
"""Provider-level tests for Claude streaming tool_use translation (#636).

Covers the real-mode path of :meth:`app.llm.claude.ClaudeProvider.astream`:

- ``tools=`` is forwarded to the Anthropic streaming API when supplied.
- Anthropic's streaming tool-use block shape
  (``content_block_start`` -> ``input_json_delta`` -> ``content_block_stop``)
  is translated into a single :class:`StreamEvent` with ``type="tool_use"``,
  ``tool_name``, ``tool_input``, and ``tool_use_id``.

All SDK calls are mocked — no network I/O.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.chat.tools import CHAT_TOOLS
from app.llm.claude import ClaudeProvider


def _make_provider(monkeypatch: pytest.MonkeyPatch) -> ClaudeProvider:
    """Construct a non-stubbed provider with a fake API key."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-sonnet-4-6")
    return ClaudeProvider()


class _MockStream:
    """Minimal async iterator over a canned list of Anthropic stream events."""

    def __init__(self, events: list[Any]) -> None:
        self._events = events
        self._idx = 0

    def __aiter__(self) -> _MockStream:
        return self

    async def __anext__(self) -> Any:
        if self._idx >= len(self._events):
            raise StopAsyncIteration
        evt = self._events[self._idx]
        self._idx += 1
        return evt


class _MockStreamCtx:
    """Async context manager that yields a pre-built :class:`_MockStream`."""

    def __init__(self, stream: _MockStream) -> None:
        self._stream = stream

    async def __aenter__(self) -> _MockStream:
        return self._stream

    async def __aexit__(self, *args: Any) -> None:
        return None


def _tool_use_events(
    tool_name: str,
    tool_use_id: str,
    input_fragments: list[str],
) -> list[Any]:
    """Build a canned Anthropic stream covering a single tool_use block."""
    return [
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(usage=SimpleNamespace(input_tokens=12)),
        ),
        SimpleNamespace(
            type="content_block_start",
            index=0,
            content_block=SimpleNamespace(
                type="tool_use",
                id=tool_use_id,
                name=tool_name,
                input={},
            ),
        ),
        *[
            SimpleNamespace(
                type="content_block_delta",
                index=0,
                delta=SimpleNamespace(
                    type="input_json_delta",
                    partial_json=fragment,
                ),
            )
            for fragment in input_fragments
        ],
        SimpleNamespace(type="content_block_stop", index=0),
        SimpleNamespace(
            type="message_delta",
            usage=SimpleNamespace(output_tokens=7),
        ),
    ]


class TestAstreamForwardsTools:
    """``tools=`` must reach ``client.messages.stream(**kwargs)``."""

    @patch("app.llm.claude.ClaudeProvider._log_cost")
    def test_tools_passed_to_anthropic(
        self,
        mock_log_cost: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        provider = _make_provider(monkeypatch)
        mock_client = MagicMock()
        provider._async_client = mock_client

        # Trivial stream: message_start -> message_delta (no tool_use, no text)
        mock_stream = _MockStream(
            [
                SimpleNamespace(
                    type="message_start",
                    message=SimpleNamespace(usage=SimpleNamespace(input_tokens=5)),
                ),
                SimpleNamespace(
                    type="message_delta",
                    usage=SimpleNamespace(output_tokens=3),
                ),
            ]
        )
        mock_client.messages.stream = MagicMock(return_value=_MockStreamCtx(mock_stream))

        async def _drain() -> None:
            async for _ in provider.astream("Mis on tsiviilseadustik?", tools=CHAT_TOOLS):
                pass

        asyncio.run(_drain())

        mock_client.messages.stream.assert_called_once()
        kw = mock_client.messages.stream.call_args[1]
        assert kw["tools"] == CHAT_TOOLS
        # Sanity: other required kwargs are still there.
        assert kw["model"] == "claude-sonnet-4-6"
        assert "messages" in kw

    @patch("app.llm.claude.ClaudeProvider._log_cost")
    def test_tools_omitted_when_none(
        self,
        mock_log_cost: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When no tools are supplied, ``tools`` must not be in kwargs."""
        provider = _make_provider(monkeypatch)
        mock_client = MagicMock()
        provider._async_client = mock_client

        mock_stream = _MockStream(
            [
                SimpleNamespace(
                    type="message_start",
                    message=SimpleNamespace(usage=SimpleNamespace(input_tokens=1)),
                ),
                SimpleNamespace(
                    type="message_delta",
                    usage=SimpleNamespace(output_tokens=1),
                ),
            ]
        )
        mock_client.messages.stream = MagicMock(return_value=_MockStreamCtx(mock_stream))

        async def _drain() -> None:
            async for _ in provider.astream("hei"):
                pass

        asyncio.run(_drain())

        kw = mock_client.messages.stream.call_args[1]
        assert "tools" not in kw


class TestAstreamEmitsToolUse:
    """Anthropic tool-use blocks become :class:`StreamEvent` objects."""

    @patch("app.llm.claude.ClaudeProvider._log_cost")
    def test_tool_use_block_translated_to_stream_event(
        self,
        mock_log_cost: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        provider = _make_provider(monkeypatch)
        mock_client = MagicMock()
        provider._async_client = mock_client

        # partial_json deltas must be concatenated and parsed as JSON.
        events = _tool_use_events(
            tool_name="query_ontology",
            tool_use_id="toolu_01ABC",
            input_fragments=['{"query": ', '"SELECT *"}'],
        )
        mock_client.messages.stream = MagicMock(return_value=_MockStreamCtx(_MockStream(events)))

        async def _collect() -> list[Any]:
            out: list[Any] = []
            async for ev in provider.astream("Leia sättega seotud normid", tools=CHAT_TOOLS):
                out.append(ev)
            return out

        collected = asyncio.run(_collect())

        tool_events = [e for e in collected if e.type == "tool_use"]
        assert len(tool_events) == 1
        (ev,) = tool_events
        assert ev.tool_name == "query_ontology"
        assert ev.tool_input == {"query": "SELECT *"}
        assert ev.tool_use_id == "toolu_01ABC"

        # A trailing stop event is still emitted.
        assert collected[-1].type == "stop"

    @patch("app.llm.claude.ClaudeProvider._log_cost")
    def test_tool_use_empty_input_parses_as_dict(
        self,
        mock_log_cost: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Anthropic emits no input_json_delta for zero-arg tools — treat as ``{}``."""
        provider = _make_provider(monkeypatch)
        mock_client = MagicMock()
        provider._async_client = mock_client

        events = _tool_use_events(
            tool_name="get_provision_details",
            tool_use_id="toolu_02XYZ",
            input_fragments=[],  # no deltas
        )
        mock_client.messages.stream = MagicMock(return_value=_MockStreamCtx(_MockStream(events)))

        async def _collect() -> list[Any]:
            out: list[Any] = []
            async for ev in provider.astream("test", tools=CHAT_TOOLS):
                out.append(ev)
            return out

        collected = asyncio.run(_collect())

        tool_events = [e for e in collected if e.type == "tool_use"]
        assert len(tool_events) == 1
        assert tool_events[0].tool_input == {}
        assert tool_events[0].tool_name == "get_provision_details"

    @patch("app.llm.claude.ClaudeProvider._log_cost")
    def test_mixed_text_and_tool_use_stream(
        self,
        mock_log_cost: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Text deltas and a tool_use block in the same stream coexist cleanly."""
        provider = _make_provider(monkeypatch)
        mock_client = MagicMock()
        provider._async_client = mock_client

        events: list[Any] = [
            SimpleNamespace(
                type="message_start",
                message=SimpleNamespace(usage=SimpleNamespace(input_tokens=20)),
            ),
            # Text block
            SimpleNamespace(
                type="content_block_start",
                index=0,
                content_block=SimpleNamespace(type="text", text=""),
            ),
            SimpleNamespace(
                type="content_block_delta",
                index=0,
                delta=SimpleNamespace(type="text_delta", text="Otsin vastust... "),
            ),
            SimpleNamespace(type="content_block_stop", index=0),
            # Tool use block
            SimpleNamespace(
                type="content_block_start",
                index=1,
                content_block=SimpleNamespace(
                    type="tool_use",
                    id="toolu_03",
                    name="search_provisions",
                    input={},
                ),
            ),
            SimpleNamespace(
                type="content_block_delta",
                index=1,
                delta=SimpleNamespace(
                    type="input_json_delta",
                    partial_json='{"keywords": "võlaõigus"}',
                ),
            ),
            SimpleNamespace(type="content_block_stop", index=1),
            SimpleNamespace(
                type="message_delta",
                usage=SimpleNamespace(output_tokens=12),
            ),
        ]
        mock_client.messages.stream = MagicMock(return_value=_MockStreamCtx(_MockStream(events)))

        async def _collect() -> list[Any]:
            out: list[Any] = []
            async for ev in provider.astream("Otsi norme", tools=CHAT_TOOLS):
                out.append(ev)
            return out

        collected = asyncio.run(_collect())

        content_events = [e for e in collected if e.type == "content"]
        tool_events = [e for e in collected if e.type == "tool_use"]

        assert "".join(e.delta or "" for e in content_events) == "Otsin vastust... "
        assert len(tool_events) == 1
        assert tool_events[0].tool_name == "search_provisions"
        assert tool_events[0].tool_input == {"keywords": "võlaõigus"}

        # Usage still logged
        mock_log_cost.assert_called_once_with(
            feature="astream",
            tokens_input=20,
            tokens_output=12,
            user_id=None,
            org_id=None,
        )
