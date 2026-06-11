"""Mid-stream cost metering for ``ClaudeProvider.astream`` (#854).

Before #854, ``_log_cost`` ran in a ``finally`` attached to the trailing
``yield stop`` — so a mid-stream upstream error or a client-disconnect
cancel (GeneratorExit via ``aclose()``) skipped it entirely and
already-billed tokens never reached ``llm_usage``. These tests pin the
new contract: once the stream has *opened*, exactly one usage row is
recorded no matter how the stream ends; if the open itself fails, no
row is recorded.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from app.llm.claude import ClaudeProvider
from app.llm.provider import StreamEvent


def _make_provider(monkeypatch: pytest.MonkeyPatch) -> ClaudeProvider:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    return ClaudeProvider()


def _message_start(input_tokens: int) -> SimpleNamespace:
    return SimpleNamespace(
        type="message_start",
        message=SimpleNamespace(usage=SimpleNamespace(input_tokens=input_tokens)),
    )


def _text_delta(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(text=text),
    )


def _message_delta(output_tokens: int) -> SimpleNamespace:
    return SimpleNamespace(
        type="message_delta",
        usage=SimpleNamespace(output_tokens=output_tokens),
    )


class _ScriptedStream:
    """Async iterator that replays events; an Exception entry is raised."""

    def __init__(self, events: list[Any]) -> None:
        self._events = list(events)

    def __aiter__(self) -> _ScriptedStream:
        return self

    async def __anext__(self) -> Any:
        if not self._events:
            raise StopAsyncIteration
        item = self._events.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _Ctx:
    """Context manager wrapper mimicking ``client.messages.stream(...)``."""

    def __init__(self, stream: _ScriptedStream) -> None:
        self._stream = stream
        self.exits = 0

    async def __aenter__(self) -> _ScriptedStream:
        return self._stream

    async def __aexit__(self, *args: Any) -> None:
        self.exits += 1


def _wire(provider: ClaudeProvider, ctx: _Ctx) -> MagicMock:
    mock_client = MagicMock()
    mock_client.messages.stream = MagicMock(return_value=ctx)
    provider._async_client = mock_client
    return mock_client


class TestMidStreamUpstreamError:
    def test_usage_row_recorded_on_mid_stream_error(self, monkeypatch: pytest.MonkeyPatch):
        """Upstream dies after message_start — the billed input tokens
        must still reach ``llm_usage``."""
        provider = _make_provider(monkeypatch)
        ctx = _Ctx(
            _ScriptedStream(
                [
                    _message_start(120),
                    _text_delta("Tere"),
                    ConnectionError("upstream died mid-stream"),
                ]
            )
        )
        _wire(provider, ctx)

        with patch.object(ClaudeProvider, "_log_cost") as mock_log_cost:

            async def _drain() -> list[Any]:
                got = []
                async for evt in provider.astream("hi", feature="chat"):
                    got.append(evt)
                return got

            with pytest.raises(ConnectionError):
                asyncio.run(_drain())

        mock_log_cost.assert_called_once_with(
            feature="chat",
            tokens_input=120,
            tokens_output=0,
            user_id=None,
            org_id=None,
        )
        assert ctx.exits == 1  # httpx stream released exactly once

    def test_no_usage_row_when_open_never_succeeds(self, monkeypatch: pytest.MonkeyPatch):
        """A permanent 401 on the open call bills nothing — no row."""
        provider = _make_provider(monkeypatch)

        class _AuthError(Exception):
            status_code = 401

        class _BadCtx:
            async def __aenter__(self) -> Any:
                raise _AuthError("bad key")

            async def __aexit__(self, *args: Any) -> None:
                return None

        mock_client = MagicMock()
        mock_client.messages.stream = MagicMock(return_value=_BadCtx())
        provider._async_client = mock_client

        with patch.object(ClaudeProvider, "_log_cost") as mock_log_cost:

            async def _drain() -> None:
                async for _ in provider.astream("hi"):
                    pass

            with pytest.raises(_AuthError):
                asyncio.run(_drain())

        mock_log_cost.assert_not_called()


class TestConsumerCancel:
    def test_usage_row_recorded_on_generator_exit(self, monkeypatch: pytest.MonkeyPatch):
        """Client disconnect → orchestrator ``aclose()`` → GeneratorExit.

        The partial usage captured so far (here the message_start input
        tokens) must still be metered.
        """
        provider = _make_provider(monkeypatch)
        ctx = _Ctx(
            _ScriptedStream(
                [
                    _message_start(250),
                    _text_delta("Tere "),
                    _text_delta("tulemast"),
                    _message_delta(99),
                ]
            )
        )
        _wire(provider, ctx)

        with patch.object(ClaudeProvider, "_log_cost") as mock_log_cost:

            async def _cancel_mid_stream() -> None:
                # ``astream`` is declared as AsyncIterator but is an async
                # generator at runtime — cast so we can drive aclose().
                agen = cast(
                    AsyncGenerator[StreamEvent],
                    provider.astream("hi", feature="chat", user_id="u-1", org_id="o-1"),
                )
                first = await agen.__anext__()
                assert first.type == "content"
                # Suspended at the first content yield — close like the
                # WebSocket layer does when the client disconnects.
                await agen.aclose()

            asyncio.run(_cancel_mid_stream())

        mock_log_cost.assert_called_once_with(
            feature="chat",
            tokens_input=250,
            tokens_output=0,
            user_id="u-1",
            org_id="o-1",
        )
        assert ctx.exits == 1

    def test_usage_row_recorded_on_cancel_after_stop_frame(self, monkeypatch: pytest.MonkeyPatch):
        """The #662 scenario: consumer takes the stop frame then closes.
        Still exactly one usage row, now with full token counts."""
        provider = _make_provider(monkeypatch)
        ctx = _Ctx(
            _ScriptedStream(
                [
                    _message_start(80),
                    _text_delta("Vastus"),
                    _message_delta(7),
                ]
            )
        )
        _wire(provider, ctx)

        with patch.object(ClaudeProvider, "_log_cost") as mock_log_cost:

            async def _take_stop_then_close() -> None:
                agen = cast(
                    AsyncGenerator[StreamEvent],
                    provider.astream("hi", feature="chat"),
                )
                stop_seen = False
                while True:
                    evt = await agen.__anext__()
                    if evt.type == "stop":
                        stop_seen = True
                        break
                assert stop_seen
                # Generator is suspended at the stop yield — close it the
                # way the orchestrator does after a client disconnect.
                await agen.aclose()

            asyncio.run(_take_stop_then_close())

        mock_log_cost.assert_called_once_with(
            feature="chat",
            tokens_input=80,
            tokens_output=7,
            user_id=None,
            org_id=None,
        )


class TestCleanCompletion:
    def test_exactly_one_usage_row_on_clean_stop(self, monkeypatch: pytest.MonkeyPatch):
        """The restructure must not double-log the happy path."""
        provider = _make_provider(monkeypatch)
        ctx = _Ctx(
            _ScriptedStream(
                [
                    _message_start(60),
                    _text_delta("Tere!"),
                    _message_delta(11),
                ]
            )
        )
        _wire(provider, ctx)

        with patch.object(ClaudeProvider, "_log_cost") as mock_log_cost:

            async def _drain() -> list[Any]:
                return [evt async for evt in provider.astream("hi", feature="chat")]

            events = asyncio.run(_drain())

        stop_events = [e for e in events if e.type == "stop"]
        assert len(stop_events) == 1
        assert stop_events[0].tokens_input == 60
        assert stop_events[0].tokens_output == 11
        mock_log_cost.assert_called_once_with(
            feature="chat",
            tokens_input=60,
            tokens_output=11,
            user_id=None,
            org_id=None,
        )
        assert ctx.exits == 1
