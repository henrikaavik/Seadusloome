"""Tests for ``app.auth.ws_auth`` — the shared WS auth/heartbeat helpers (#856).

Consolidates the per-channel coverage that previously lived in
``tests/test_chat_websocket_heartbeat.py`` (heartbeat cadence,
self-termination, bounded send) and the duplicated cookie-extraction
classes in the chat/notifications test modules, plus new coverage for
the close helper, the cookie-JWT resolution contract, the heartbeat
``on_fail`` callback (F3) and the :class:`HeartbeatRegistry`.

Uses ``asyncio.run()`` to drive async test bodies, matching the project
convention established by :mod:`tests.test_chat_orchestrator`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock, patch

from app.auth import ws_auth
from app.auth.ws_auth import (
    HeartbeatRegistry,
    WSCookieAuth,
    close_ws,
    conn_key,
    extract_cookie_from_headers,
    start_heartbeat,
)

_USER = {
    "id": "11111111-1111-1111-1111-111111111111",
    "email": "test@riik.ee",
    "full_name": "Test Kasutaja",
    "role": "drafter",
    "org_id": "22222222-2222-2222-2222-222222222222",
}


class _FakeWs:
    """Minimal stand-in for the Starlette WebSocket conn FastHTML hands
    to WS hooks via the unannotated ``ws`` parameter."""

    def __init__(self) -> None:
        self.close_calls: list[tuple[int, str]] = []
        self.close_raises: BaseException | None = None

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.close_raises is not None:
            raise self.close_raises
        self.close_calls.append((code, reason))


# ---------------------------------------------------------------------------
# extract_cookie_from_headers
# ---------------------------------------------------------------------------


class TestExtractCookieFromHeaders:
    def test_extracts_named_cookie(self):
        headers = [(b"cookie", b"access_token=my-jwt-value; other=abc")]
        assert extract_cookie_from_headers(headers, "access_token") == "my-jwt-value"

    def test_returns_none_when_cookie_absent(self):
        headers = [(b"cookie", b"other=abc")]
        assert extract_cookie_from_headers(headers, "access_token") is None

    def test_returns_none_when_no_cookie_header(self):
        headers = [(b"host", b"localhost")]
        assert extract_cookie_from_headers(headers, "access_token") is None


# ---------------------------------------------------------------------------
# conn_key — stable identity rule
# ---------------------------------------------------------------------------


class TestConnKey:
    def test_prefers_ws_identity(self):
        ws = _FakeWs()

        async def send_a(_: Any) -> None: ...

        async def send_b(_: Any) -> None: ...

        # Different send partials, same conn → same key. This is the
        # exact production shape: FastHTML rebuilds send per dispatch.
        assert conn_key(send_a, ws) == conn_key(send_b, ws) == id(ws)

    def test_falls_back_to_send_identity_without_ws(self):
        async def send(_: Any) -> None: ...

        assert conn_key(send, None) == id(send)


# ---------------------------------------------------------------------------
# Heartbeat — cadence, self-termination, bounded send, on_fail (#658/#684/F3)
# ---------------------------------------------------------------------------


def test_heartbeat_emits_ping_on_interval(monkeypatch) -> None:
    """The heartbeat task emits at least one ping per interval and
    exits cleanly on cancel."""
    monkeypatch.setattr(ws_auth, "WS_HEARTBEAT_INTERVAL_SECONDS", 0.05)

    received: list[dict] = []

    async def fake_send(payload: str) -> None:
        received.append(json.loads(payload))

    async def _run() -> None:
        task = start_heartbeat(fake_send)
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
    monkeypatch.setattr(ws_auth, "WS_HEARTBEAT_INTERVAL_SECONDS", 0.02)

    calls: list[int] = []

    async def always_failing_send(payload: str) -> None:
        calls.append(len(calls))
        raise RuntimeError("socket closed")

    async def _run() -> asyncio.Task[None]:
        task = start_heartbeat(always_failing_send)
        # Give the loop enough time to attempt several pings IF it were
        # going to retry. With the self-terminate contract it should
        # have exited after the very first failure.
        await asyncio.sleep(0.1)
        return task

    task = asyncio.run(_run())

    assert task.done(), "heartbeat must self-terminate after first send error"
    assert len(calls) == 1, (
        f"expected the heartbeat to abort after the first failure, got {len(calls)} calls"
    )


def test_heartbeat_send_is_bounded_by_timeout(monkeypatch) -> None:
    """Post-review fix to #684: a hung TCP send buffer must not hang
    the heartbeat. The send is wrapped in ``asyncio.wait_for`` with a
    short timeout, after which the heartbeat self-terminates (since the
    timeout indicates the socket is dead)."""
    monkeypatch.setattr(ws_auth, "WS_HEARTBEAT_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(ws_auth, "WS_HEARTBEAT_SEND_TIMEOUT_SECONDS", 0.05)

    sends_started: list[float] = []

    async def hanging_send(payload: str) -> None:
        sends_started.append(asyncio.get_event_loop().time())
        await asyncio.sleep(3600)  # would hang forever without wait_for

    async def _run() -> tuple[asyncio.Task[None], float]:
        task = start_heartbeat(hanging_send)
        started = asyncio.get_event_loop().time()
        await asyncio.sleep(0.2)
        elapsed = asyncio.get_event_loop().time() - started
        return task, elapsed

    task, elapsed = asyncio.run(_run())

    assert task.done(), "heartbeat must self-terminate when send hangs past the timeout"
    assert elapsed < 1.0, "test must not hang waiting on the send"
    assert len(sends_started) >= 1


def test_heartbeat_on_fail_runs_exactly_once_on_send_failure(monkeypatch) -> None:
    """F3 contract: push-only channels deregister the dead connection
    the moment the heartbeat detects it."""
    monkeypatch.setattr(ws_auth, "WS_HEARTBEAT_INTERVAL_SECONDS", 0.02)

    fail_calls: list[int] = []

    async def failing_send(payload: str) -> None:
        raise RuntimeError("socket dead")

    async def _run() -> asyncio.Task[None]:
        task = start_heartbeat(failing_send, on_fail=lambda: fail_calls.append(1))
        await asyncio.sleep(0.1)
        return task

    task = asyncio.run(_run())

    assert task.done()
    assert len(fail_calls) == 1


def test_heartbeat_on_fail_not_called_on_cancel(monkeypatch) -> None:
    """A normal teardown (cancel) is not a connection failure — the
    deregistration hook must not fire for it."""
    monkeypatch.setattr(ws_auth, "WS_HEARTBEAT_INTERVAL_SECONDS", 60.0)

    fail_calls: list[int] = []

    async def fine_send(payload: str) -> None: ...

    async def _run() -> None:
        task = start_heartbeat(fine_send, on_fail=lambda: fail_calls.append(1))
        await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())
    assert fail_calls == []


def test_heartbeat_on_fail_exception_is_swallowed(monkeypatch) -> None:
    """A broken on_fail callback must not propagate out of the task."""
    monkeypatch.setattr(ws_auth, "WS_HEARTBEAT_INTERVAL_SECONDS", 0.02)

    async def failing_send(payload: str) -> None:
        raise RuntimeError("socket dead")

    def broken_on_fail() -> None:
        raise RuntimeError("registry exploded")

    async def _run() -> asyncio.Task[None]:
        task = start_heartbeat(failing_send, on_fail=broken_on_fail)
        await asyncio.sleep(0.1)
        return task

    task = asyncio.run(_run())
    assert task.done()
    assert task.exception() is None


# ---------------------------------------------------------------------------
# close_ws — must close via the raw conn (F1)
# ---------------------------------------------------------------------------


class TestCloseWs:
    def test_closes_via_ws_close(self):
        ws = _FakeWs()
        asyncio.run(close_ws(ws, 1011, "auth provider unavailable", channel="test"))
        assert ws.close_calls == [(1011, "auth provider unavailable")]

    def test_none_ws_is_a_logged_noop(self):
        # Direct-invocation tests have no conn; must not raise.
        asyncio.run(close_ws(None, 1008, "authentication required"))

    def test_close_exception_is_swallowed(self):
        ws = _FakeWs()
        ws.close_raises = RuntimeError("already closed")
        asyncio.run(close_ws(ws, 1000, "bye"))
        assert ws.close_calls == []


# ---------------------------------------------------------------------------
# WSCookieAuth — cookie-JWT resolution with silent refresh (#637)
# ---------------------------------------------------------------------------


class TestWSCookieAuth:
    def test_none_scope_is_unauthenticated_not_failure(self):
        auth = WSCookieAuth("test", provider_factory=lambda: MagicMock())
        result = auth.resolve_user(None)
        assert result.user is None
        assert result.provider_unavailable is False

    def test_no_cookies_is_unauthenticated_without_provider_construction(self):
        factory = MagicMock()
        auth = WSCookieAuth("test", provider_factory=factory)
        result = auth.resolve_user({"headers": [(b"host", b"localhost")]})
        assert result.user is None
        assert result.provider_unavailable is False
        # No tokens → the provider must not even be constructed.
        factory.assert_not_called()

    def test_valid_access_token_resolves_user(self):
        provider = MagicMock()
        provider.get_current_user.return_value = _USER
        auth = WSCookieAuth("test", provider_factory=lambda: provider)

        scope = {"headers": [(b"cookie", b"access_token=good-token")]}
        result = auth.resolve_user(scope)

        provider.get_current_user.assert_called_once_with("good-token")
        assert result.user is not None
        assert result.user["id"] == _USER["id"]
        assert result.provider_unavailable is False

    def test_expired_access_falls_back_to_verify_only_refresh(self):
        """Silent refresh (#637): verify-only — the session row must not
        be consumed because the upgrade response cannot persist rotated
        cookies."""
        provider = MagicMock()
        provider.get_current_user.return_value = None
        auth = WSCookieAuth("test", provider_factory=lambda: provider)

        scope = {"headers": [(b"cookie", b"access_token=expired; refresh_token=fresh")]}
        with patch(
            "app.auth.middleware.verify_refresh_token_user",
            return_value=dict(_USER),
        ) as verify:
            result = auth.resolve_user(scope)

        verify.assert_called_once_with("fresh", provider=provider)
        assert result.user is not None
        assert result.user["id"] == _USER["id"]
        # Verify-only contract: no rotation calls on the provider.
        provider.delete_refresh_token.assert_not_called()
        provider.create_tokens.assert_not_called()

    def test_invalid_both_tokens_is_unauthenticated(self):
        provider = MagicMock()
        provider.get_current_user.return_value = None
        auth = WSCookieAuth("test", provider_factory=lambda: provider)

        scope = {"headers": [(b"cookie", b"access_token=bad; refresh_token=dead")]}
        with patch("app.auth.middleware.verify_refresh_token_user", return_value=None):
            result = auth.resolve_user(scope)

        assert result.user is None
        assert result.provider_unavailable is False

    def test_provider_construction_failure_is_fail_closed_signal(self):
        def exploding_factory() -> Any:
            raise RuntimeError("cannot init JWT provider")

        auth = WSCookieAuth("test", provider_factory=exploding_factory)
        scope = {"headers": [(b"cookie", b"access_token=anything")]}
        result = auth.resolve_user(scope)
        assert result.user is None
        assert result.provider_unavailable is True

    def test_provider_construction_failure_is_not_cached(self):
        """A transient outage at first connect must not poison every
        later handshake — the factory is retried on the next call."""
        attempts: list[int] = []
        provider = MagicMock()
        provider.get_current_user.return_value = _USER

        def flaky_factory() -> Any:
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("transient DB outage")
            return provider

        auth = WSCookieAuth("test", provider_factory=flaky_factory)
        scope = {"headers": [(b"cookie", b"access_token=good")]}

        first = auth.resolve_user(scope)
        assert first.provider_unavailable is True

        second = auth.resolve_user(scope)
        assert second.provider_unavailable is False
        assert second.user is not None
        assert len(attempts) == 2

    def test_provider_constructed_once_and_reused(self):
        constructed: list[int] = []
        provider = MagicMock()
        provider.get_current_user.return_value = _USER

        def factory() -> Any:
            constructed.append(1)
            return provider

        auth = WSCookieAuth("test", provider_factory=factory)
        scope = {"headers": [(b"cookie", b"access_token=good")]}
        auth.resolve_user(scope)
        auth.resolve_user(scope)
        assert len(constructed) == 1


# ---------------------------------------------------------------------------
# HeartbeatRegistry
# ---------------------------------------------------------------------------


class TestHeartbeatRegistry:
    def test_register_pop_roundtrip(self):
        async def _run() -> None:
            reg = HeartbeatRegistry()
            task = asyncio.create_task(asyncio.sleep(10))
            reg.register(1, task)
            assert 1 in reg
            assert len(reg) == 1
            popped = reg.pop(1)
            assert popped is task
            assert 1 not in reg
            assert reg.pop(1) is None
            task.cancel()

        asyncio.run(_run())

    def test_reregister_cancels_stale_task(self):
        async def _run() -> tuple[asyncio.Task[None], asyncio.Task[None]]:
            reg = HeartbeatRegistry()
            stale = asyncio.create_task(asyncio.sleep(10))
            fresh = asyncio.create_task(asyncio.sleep(10))
            reg.register(7, stale)
            reg.register(7, fresh)
            await asyncio.sleep(0)  # let the cancel propagate
            assert reg.get(7) is fresh
            fresh.cancel()
            return stale, fresh

        stale, _fresh = asyncio.run(_run())
        assert stale.cancelled()

    def test_cancel_clear_empties_registry(self):
        async def _run() -> list[asyncio.Task[None]]:
            reg = HeartbeatRegistry()
            tasks = [asyncio.create_task(asyncio.sleep(10)) for _ in range(3)]
            for i, task in enumerate(tasks):
                reg.register(i, task)
            reg.cancel_clear()
            assert len(reg) == 0
            await asyncio.sleep(0)
            return tasks

        tasks = asyncio.run(_run())
        assert all(t.cancelled() for t in tasks)
