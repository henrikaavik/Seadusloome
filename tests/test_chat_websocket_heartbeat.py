"""WebSocket heartbeat — server emits {'type': 'ping'} every ~25s.

Closes part of #658: NAT idle timeouts and proxy idle-cuts can silently
kill the WS during long RAG/LLM rounds. A periodic ping keeps the path
warm.

Uses ``asyncio.run()`` to drive async test bodies, matching the project
convention established by :mod:`tests.test_chat_orchestrator`.
"""

from __future__ import annotations

import asyncio
import json

from app.chat import websocket as ws_mod
from app.chat.websocket import _start_heartbeat


def test_heartbeat_emits_ping_on_interval(monkeypatch) -> None:
    """The heartbeat task emits at least one ping per interval and
    exits cleanly on cancel."""
    # Override the interval so the test is fast.
    monkeypatch.setattr(ws_mod, "_WS_HEARTBEAT_INTERVAL_SECONDS", 0.05)

    received: list[dict] = []

    async def fake_send(payload: str) -> None:
        received.append(json.loads(payload))

    async def _run() -> None:
        task = _start_heartbeat(fake_send)
        try:
            await asyncio.sleep(0.18)  # allow ~3 ticks
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())

    pings = [e for e in received if e.get("type") == "ping"]
    assert len(pings) >= 2, f"expected at least 2 pings in 180ms, got {len(pings)}: {received}"


def test_heartbeat_swallows_send_errors(monkeypatch) -> None:
    """Heartbeat survives a transient send failure without crashing."""
    monkeypatch.setattr(ws_mod, "_WS_HEARTBEAT_INTERVAL_SECONDS", 0.02)

    calls: list[int] = []

    async def flaky_send(payload: str) -> None:
        calls.append(len(calls))
        if len(calls) == 2:
            raise RuntimeError("transient")

    async def _run() -> None:
        task = _start_heartbeat(flaky_send)
        try:
            await asyncio.sleep(0.1)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())

    assert len(calls) >= 3, "heartbeat must keep ticking after a send error"
