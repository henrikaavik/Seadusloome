"""Tests for :mod:`app.chat.title`.

Uses ``asyncio.run()`` to drive the async entrypoint, matching the
convention established by :mod:`tests.test_chat_orchestrator`.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

from app.chat import title as title_mod


class _FakeProvider:
    """Minimal async-provider stub that records the acomplete call."""

    def __init__(self, reply: str | Exception) -> None:
        self._reply = reply
        self.calls: list[dict[str, Any]] = []

    async def acomplete(self, prompt: str, **kwargs: Any) -> str:
        self.calls.append({"prompt": prompt, **kwargs})
        if isinstance(self._reply, Exception):
            raise self._reply
        return self._reply


def test_generate_title_happy_path() -> None:
    provider = _FakeProvider("Mingi teema")
    with (
        patch.object(title_mod, "get_default_provider", return_value=provider),
        patch.object(title_mod, "is_chat_auto_title_enabled", return_value=True),
    ):
        result = asyncio.run(
            title_mod.generate_title("Kuidas muuta seadust?", "Selleks tuleb algatada eelnõu.")
        )

    assert result == "Mingi teema"
    assert len(provider.calls) == 1
    call = provider.calls[0]
    assert call["feature"] == "chat_auto_title"
    assert call["max_tokens"] == 60
    assert "Kasutaja: Kuidas muuta seadust?" in call["prompt"]
    assert "Assistent: Selleks tuleb algatada eelnõu." in call["prompt"]
    assert "pealkiri" in call["system"]


def test_generate_title_feature_flag_off_skips_llm() -> None:
    sentinel = MagicMock()
    with (
        patch.object(title_mod, "is_chat_auto_title_enabled", return_value=False),
        patch.object(title_mod, "get_default_provider", sentinel),
    ):
        result = asyncio.run(title_mod.generate_title("u", "a"))

    assert result is None
    sentinel.assert_not_called()


def test_generate_title_llm_raises_returns_none() -> None:
    provider = _FakeProvider(RuntimeError("boom"))
    with (
        patch.object(title_mod, "get_default_provider", return_value=provider),
        patch.object(title_mod, "is_chat_auto_title_enabled", return_value=True),
    ):
        result = asyncio.run(title_mod.generate_title("u", "a"))

    assert result is None
    assert len(provider.calls) == 1


def test_generate_title_truncates_overlong_response() -> None:
    long_reply = "Väga pikk pealkiri " * 10  # well over 48 chars
    provider = _FakeProvider(long_reply)
    with (
        patch.object(title_mod, "get_default_provider", return_value=provider),
        patch.object(title_mod, "is_chat_auto_title_enabled", return_value=True),
    ):
        result = asyncio.run(title_mod.generate_title("u", "a"))

    assert result is not None
    assert len(result) <= 48


def test_generate_title_strips_wrapping_quotes() -> None:
    provider = _FakeProvider('"Mingi teema"')
    with (
        patch.object(title_mod, "get_default_provider", return_value=provider),
        patch.object(title_mod, "is_chat_auto_title_enabled", return_value=True),
    ):
        result = asyncio.run(title_mod.generate_title("u", "a"))

    assert result == "Mingi teema"


def test_generate_title_strips_typographic_quotes_and_period() -> None:
    provider = _FakeProvider("\u201cMingi teema.\u201d")
    with (
        patch.object(title_mod, "get_default_provider", return_value=provider),
        patch.object(title_mod, "is_chat_auto_title_enabled", return_value=True),
    ):
        result = asyncio.run(title_mod.generate_title("u", "a"))

    assert result == "Mingi teema"


def test_generate_title_empty_response_returns_none() -> None:
    provider = _FakeProvider("   ")
    with (
        patch.object(title_mod, "get_default_provider", return_value=provider),
        patch.object(title_mod, "is_chat_auto_title_enabled", return_value=True),
    ):
        result = asyncio.run(title_mod.generate_title("u", "a"))

    assert result is None
