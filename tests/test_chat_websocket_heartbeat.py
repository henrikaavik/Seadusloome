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


def test_heartbeat_self_terminates_on_send_error(monkeypatch) -> None:
    """Post-review fix to #684: once ``send`` raises, the socket is
    almost certainly dead and looping forever just spams DEBUG logs and
    leaks a background task. The heartbeat must self-terminate cleanly
    on the first send failure."""
    monkeypatch.setattr(ws_mod, "_WS_HEARTBEAT_INTERVAL_SECONDS", 0.02)

    calls: list[int] = []

    async def always_failing_send(payload: str) -> None:
        calls.append(len(calls))
        raise RuntimeError("socket closed")

    async def _run() -> asyncio.Task[None]:
        task = _start_heartbeat(always_failing_send)
        # Give the loop enough time to attempt several pings IF it were
        # going to retry. With the self-terminate contract it should
        # have exited after the very first failure.
        await asyncio.sleep(0.1)
        return task

    task = asyncio.run(_run())

    # The task must have completed on its own (no cancel needed).
    assert task.done(), "heartbeat must self-terminate after first send error"
    # Only one send call should have happened — the one that raised.
    assert len(calls) == 1, (
        f"expected the heartbeat to abort after the first failure, got {len(calls)} calls"
    )


def test_heartbeat_send_is_bounded_by_timeout(monkeypatch) -> None:
    """Post-review fix to #684: a hung TCP send buffer must not hang
    the heartbeat. The send is wrapped in ``asyncio.wait_for`` with a
    short timeout, after which the heartbeat self-terminates (since the
    timeout indicates the socket is dead)."""
    monkeypatch.setattr(ws_mod, "_WS_HEARTBEAT_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(ws_mod, "_WS_HEARTBEAT_SEND_TIMEOUT_SECONDS", 0.05)

    sends_started: list[float] = []

    async def hanging_send(payload: str) -> None:
        sends_started.append(asyncio.get_event_loop().time())
        await asyncio.sleep(3600)  # would hang forever without wait_for

    async def _run() -> tuple[asyncio.Task[None], float]:
        task = _start_heartbeat(hanging_send)
        # Wait long enough for sleep+send_timeout to fire and the loop
        # to self-terminate via the TimeoutError path.
        started = asyncio.get_event_loop().time()
        await asyncio.sleep(0.2)
        elapsed = asyncio.get_event_loop().time() - started
        return task, elapsed

    task, elapsed = asyncio.run(_run())

    # Heartbeat self-terminated via the wait_for timeout path.
    assert task.done(), "heartbeat must self-terminate when send hangs past the timeout"
    # Real-time elapsed bounded; the test isn't hung.
    assert elapsed < 1.0, "test must not hang waiting on the send"
    # send was attempted at least once — the timeout cancelled it
    # mid-flight which counts as a send-error for our self-terminate
    # logic.
    assert len(sends_started) >= 1
