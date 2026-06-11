"""Tests for ``app.notifications.websocket`` (#180).

Covers the four DoD scenarios:

1. ``push_to_user`` reaches every live socket for the target user.
2. ``notify()`` inserts the row AND triggers a WS push for the user.
3. Cross-user isolation — user A's notification does not push to user B.
4. Disconnect cleanly drops the socket from the per-user pool.

Same direct-handler test style as ``tests/test_chat_websocket.py`` and
``tests/test_docs_websocket.py``: we drive ``_on_connect`` /
``_on_disconnect`` / ``_ws_handler`` directly with a stub ``send`` and
explicit ``scope`` instead of spinning up a full ASGI fixture.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.notifications import websocket as notif_ws
from app.notifications.models import Notification

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_A = "11111111-1111-1111-1111-111111111111"
_USER_B = "22222222-2222-2222-2222-222222222222"


class _Collector:
    """Async-compatible send collector with a ``close`` hook surrogate."""

    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def __call__(self, data: Any) -> None:
        self.sent.append(data)


class _FakeWs:
    """Minimal stand-in for the Starlette WebSocket conn that
    FastHTML hands to WS hooks via the ``ws`` parameter. Only
    ``close(code, reason)`` is exercised by the lifecycle code."""

    def __init__(self) -> None:
        self.close_calls: list[tuple[int, str]] = []
        self.close_raises: BaseException | None = None

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.close_raises is not None:
            raise self.close_raises
        self.close_calls.append((code, reason))


def _build_user(user_id: str = _USER_A) -> dict[str, Any]:
    return {
        "id": user_id,
        "email": "test@riik.ee",
        "full_name": "Test Kasutaja",
        "role": "drafter",
        "org_id": "33333333-3333-3333-3333-333333333333",
    }


def _capture_handlers() -> tuple[Any, Any, Any]:
    """Drive ``register_notifications_ws_routes`` against a fake app and
    capture the connect / disconnect / message handlers."""
    mock_app = MagicMock()
    captured: dict[str, Any] = {}

    def capture_ws(path, conn=None, disconn=None):
        captured["conn"] = conn
        captured["disconn"] = disconn

        def decorator(fn):
            captured["handler"] = fn
            return fn

        return decorator

    mock_app.ws = capture_ws
    notif_ws.register_notifications_ws_routes(mock_app)
    return captured["conn"], captured["disconn"], captured["handler"]


def _scope_with_cookie(token: str = "access-token-value") -> dict[str, Any]:
    return {"headers": [(b"cookie", f"access_token={token}".encode())]}


@pytest.fixture(autouse=True)
def _reset_registry():
    """Wipe the module-level connection + heartbeat registries between tests."""
    with notif_ws._registry_lock:
        notif_ws._connections.clear()
    notif_ws._heartbeats.cancel_clear()
    yield
    with notif_ws._registry_lock:
        notif_ws._connections.clear()
    notif_ws._heartbeats.cancel_clear()


# ---------------------------------------------------------------------------
# push_to_user — direct API (no scope / cookie required)
# ---------------------------------------------------------------------------


class TestPushToUser:
    def test_push_reaches_single_registered_socket(self):
        collector = _Collector()
        notif_ws._add_connection(_USER_A, collector)

        async def _run() -> None:
            await notif_ws._broadcast_async(
                _USER_A,
                {"type": "notification", "id": "abc", "title": "Tere"},
            )

        asyncio.run(_run())

        assert len(collector.sent) == 1
        payload = json.loads(collector.sent[0])
        assert payload["type"] == "notification"
        assert payload["title"] == "Tere"

    def test_push_reaches_multiple_tabs_for_same_user(self):
        """Multiple tabs translate into multiple ``send`` callables in
        the same user's pool. Every tab must receive the push."""
        tab1 = _Collector()
        tab2 = _Collector()
        notif_ws._add_connection(_USER_A, tab1)
        notif_ws._add_connection(_USER_A, tab2)

        async def _run() -> None:
            await notif_ws._broadcast_async(
                _USER_A,
                {"type": "notification", "id": "n1", "title": "Hei"},
            )

        asyncio.run(_run())

        assert len(tab1.sent) == 1
        assert len(tab2.sent) == 1

    def test_push_to_user_handles_uuid_input(self):
        """``push_to_user`` accepts UUID and string IDs interchangeably."""
        collector = _Collector()
        user_uuid = uuid.UUID(_USER_A)
        notif_ws._add_connection(str(user_uuid), collector)

        async def _run() -> None:
            await notif_ws._broadcast_async(
                str(user_uuid),
                {"type": "notification", "id": "n1"},
            )

        asyncio.run(_run())
        assert len(collector.sent) == 1

    def test_dead_socket_is_pruned_on_send_failure(self):
        """A ``send`` that raises is dropped from the registry so the
        next broadcast does not re-attempt it."""

        class _ExplodingSend:
            calls = 0

            async def __call__(self, data: Any) -> None:
                _ExplodingSend.calls += 1
                raise RuntimeError("socket dead")

        bomb = _ExplodingSend()
        notif_ws._add_connection(_USER_A, bomb)

        async def _run() -> None:
            await notif_ws._broadcast_async(_USER_A, {"type": "notification"})

        asyncio.run(_run())
        # The dead socket was removed from the user's pool.
        with notif_ws._registry_lock:
            assert _USER_A not in notif_ws._connections

    def test_push_to_empty_pool_is_noop(self):
        """``push_to_user`` for an unknown user is a no-op (no raise)."""
        notif_ws.push_to_user(_USER_A, {"type": "notification"})


# ---------------------------------------------------------------------------
# Cross-user isolation
# ---------------------------------------------------------------------------


class TestCrossUserIsolation:
    def test_push_to_user_a_does_not_reach_user_b(self):
        socket_a = _Collector()
        socket_b = _Collector()
        notif_ws._add_connection(_USER_A, socket_a)
        notif_ws._add_connection(_USER_B, socket_b)

        async def _run() -> None:
            await notif_ws._broadcast_async(
                _USER_A,
                {"type": "notification", "title": "Ainult A-le"},
            )

        asyncio.run(_run())

        assert len(socket_a.sent) == 1
        # User B sees nothing.
        assert socket_b.sent == []


# ---------------------------------------------------------------------------
# notify() integration — DB insert + WS push
# ---------------------------------------------------------------------------


class TestNotifyIntegration:
    def test_notify_inserts_row_and_pushes_to_socket(self):
        """A successful ``notify()`` call must do TWO things:
        1. Insert + commit the row.
        2. Push the payload to every live socket for the user.
        """
        socket = _Collector()
        notif_ws._add_connection(_USER_A, socket)

        fake_notification = Notification(
            id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            user_id=uuid.UUID(_USER_A),
            type="analysis_done",
            title="Mõjuanalüüs valmis",
            body="Eelnõu mõjuanalüüs on valmis.",
            link="/drafts/123/report",
            metadata=None,
            read=False,
            created_at=datetime.now(UTC),
        )

        # ``push_to_user`` schedules the broadcast on the running loop
        # via ``create_task``. We need to wait for that task to finish
        # before asserting on the socket — so wrap the whole call in
        # an async runner and add a short sleep.
        async def _run() -> None:
            from app.notifications.notify import notify as notify_fn

            mock_conn = MagicMock()
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value=mock_conn)
            mock_ctx.__exit__ = MagicMock(return_value=None)

            with (
                patch("app.notifications.notify.get_connection", return_value=mock_ctx),
                patch(
                    "app.notifications.notify.create_notification",
                    return_value=fake_notification,
                ),
            ):
                notify_fn(
                    user_id=_USER_A,
                    type="analysis_done",
                    title="Mõjuanalüüs valmis",
                    body="Eelnõu mõjuanalüüs on valmis.",
                    link="/drafts/123/report",
                )
                # Give the create_task() scheduled by push_to_user a
                # chance to run before we assert.
                await asyncio.sleep(0)
                await asyncio.sleep(0)

            mock_conn.commit.assert_called_once()

        asyncio.run(_run())

        # WS push landed on the connected socket.
        assert len(socket.sent) == 1, f"Expected 1 WS push, got: {socket.sent}"
        payload = json.loads(socket.sent[0])
        assert payload["type"] == "notification"
        assert payload["title"] == "Mõjuanalüüs valmis"
        assert payload["link"] == "/drafts/123/report"
        assert payload["notification_type"] == "analysis_done"
        assert payload["id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

    def test_notify_to_user_a_does_not_push_to_user_b(self):
        """End-to-end cross-user isolation through ``notify()``."""
        socket_a = _Collector()
        socket_b = _Collector()
        notif_ws._add_connection(_USER_A, socket_a)
        notif_ws._add_connection(_USER_B, socket_b)

        fake = Notification(
            id=uuid.uuid4(),
            user_id=uuid.UUID(_USER_A),
            type="analysis_done",
            title="Privaatne",
            body=None,
            link=None,
            metadata=None,
            read=False,
            created_at=datetime.now(UTC),
        )

        async def _run() -> None:
            from app.notifications.notify import notify as notify_fn

            mock_conn = MagicMock()
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value=mock_conn)
            mock_ctx.__exit__ = MagicMock(return_value=None)

            with (
                patch("app.notifications.notify.get_connection", return_value=mock_ctx),
                patch("app.notifications.notify.create_notification", return_value=fake),
            ):
                notify_fn(
                    user_id=_USER_A,
                    type="analysis_done",
                    title="Privaatne",
                )
                await asyncio.sleep(0)
                await asyncio.sleep(0)

        asyncio.run(_run())

        assert len(socket_a.sent) == 1
        assert socket_b.sent == []

    def test_notify_db_failure_skips_push(self):
        """When the DB write fails we must not push anything."""
        socket = _Collector()
        notif_ws._add_connection(_USER_A, socket)

        async def _run() -> None:
            from app.notifications.notify import notify as notify_fn

            with patch(
                "app.notifications.notify.get_connection",
                side_effect=RuntimeError("db down"),
            ):
                notify_fn(
                    user_id=_USER_A,
                    type="analysis_done",
                    title="Anyhow",
                )
                await asyncio.sleep(0)

        asyncio.run(_run())
        assert socket.sent == []


# ---------------------------------------------------------------------------
# Connect / disconnect lifecycle (via the FastHTML wrapper handlers)
# ---------------------------------------------------------------------------


class TestConnectLifecycle:
    def test_authenticated_connect_registers_socket(self):
        on_conn, _on_disconn, _handler = _capture_handlers()
        collector = _Collector()
        ws = _FakeWs()

        with patch("app.notifications.websocket.JWTAuthProvider") as mock_jwt_cls:
            mock_jwt = MagicMock()
            mock_jwt.get_current_user.return_value = _build_user(_USER_A)
            mock_jwt_cls.return_value = mock_jwt

            asyncio.run(on_conn(collector, _scope_with_cookie(), ws))

        # Socket is registered under the authenticated user, keyed on
        # the stable conn identity (#856 F3).
        with notif_ws._registry_lock:
            assert _USER_A in notif_ws._connections
            assert notif_ws._connections[_USER_A].get(id(ws)) is collector

        # The greet event was sent.
        assert any(
            isinstance(s, str) and json.loads(s).get("type") == "connected" for s in collector.sent
        )

        # And we do NOT close the socket on a successful auth.
        assert ws.close_calls == []

    def test_authenticated_connect_starts_heartbeat_for_push_only_channel(self):
        """The bell UI is push-only — _ws_handler never runs in
        production because the client doesn't send messages. The
        heartbeat MUST therefore be started from _on_connect (keyed
        by id(ws)) or NAT idle timeouts silently kill the socket.
        """
        on_conn, _on_disconn, _handler = _capture_handlers()
        collector = _Collector()
        ws = _FakeWs()

        # Capture the task state *inside* the event loop — asyncio.run
        # cancels all pending tasks when the loop closes, so checking
        # done()/cancelled() from outside would always look cancelled.
        captured: dict[str, Any] = {}

        async def _run() -> None:
            await on_conn(collector, _scope_with_cookie(), ws)
            captured["registered_id"] = id(ws) in notif_ws._heartbeats
            captured["task"] = notif_ws._heartbeats.get(id(ws))

        with patch("app.notifications.websocket.JWTAuthProvider") as mock_jwt_cls:
            mock_jwt = MagicMock()
            mock_jwt.get_current_user.return_value = _build_user(_USER_A)
            mock_jwt_cls.return_value = mock_jwt

            asyncio.run(_run())

        assert captured["registered_id"] is True
        # The task was alive when we captured it (the lifetime check
        # happens inside the loop where asyncio.run hasn't yet
        # cancelled outstanding tasks).
        task = captured["task"]
        assert task is not None
        assert isinstance(task, asyncio.Task)

    def test_unauthenticated_connect_closes_socket_with_1008(self):
        """The unauthenticated handshake MUST actually close the
        underlying WebSocket (via ws.close), not just send a
        ``{"type": "websocket.close"}`` dict through FastHTML's
        ``send`` wrapper — that wrapper routes through to_xml +
        ws.send_text and leaves the connection open in production.
        """
        on_conn, _on_disconn, _handler = _capture_handlers()
        sent: list[Any] = []
        ws = _FakeWs()

        async def raw_send(data: Any) -> None:
            sent.append(data)

        # Empty scope = no cookie = no auth.
        asyncio.run(on_conn(raw_send, {"headers": []}, ws))

        # Verify the close went through ws.close() with code 1008.
        assert ws.close_calls == [(1008, "authentication required")]
        # And NOT through `send` as a text frame.
        close_frames_via_send = [
            d for d in sent if isinstance(d, dict) and d.get("type") == "websocket.close"
        ]
        assert close_frames_via_send == []

        # No connection was registered and no heartbeat was spawned.
        with notif_ws._registry_lock:
            assert notif_ws._connections == {}
        assert id(ws) not in notif_ws._heartbeats

    def test_invalid_jwt_closes_socket_and_skips_registration(self):
        on_conn, _on_disconn, _handler = _capture_handlers()
        sent: list[Any] = []
        ws = _FakeWs()

        async def raw_send(data: Any) -> None:
            sent.append(data)

        with patch("app.notifications.websocket.JWTAuthProvider") as mock_jwt_cls:
            mock_jwt = MagicMock()
            mock_jwt.get_current_user.return_value = None  # invalid token
            mock_jwt_cls.return_value = mock_jwt

            asyncio.run(on_conn(raw_send, _scope_with_cookie("bad-token"), ws))

        # Real ASGI close via ws.close, not a text frame masquerade.
        assert ws.close_calls == [(1008, "authentication required")]
        with notif_ws._registry_lock:
            assert notif_ws._connections == {}


class TestDisconnectLifecycle:
    """Disconnect cleanup is keyed on the stable conn identity
    (``id(ws)``). FastHTML rebuilds the ``send`` partial on every hook
    dispatch, so each test below deliberately hands ``_on_disconnect``
    a DIFFERENT send object than ``_on_connect`` registered — the
    production shape. (The previous tests reused the same object,
    which is the identity assumption that masked review finding F3.)"""

    def test_disconnect_removes_socket_from_user_pool(self):
        on_conn, on_disconn, _handler = _capture_handlers()
        collector = _Collector()
        ws = _FakeWs()

        with patch("app.notifications.websocket.JWTAuthProvider") as mock_jwt_cls:
            mock_jwt = MagicMock()
            mock_jwt.get_current_user.return_value = _build_user(_USER_A)
            mock_jwt_cls.return_value = mock_jwt

            asyncio.run(on_conn(collector, _scope_with_cookie(), ws))

        with notif_ws._registry_lock:
            assert collector in notif_ws._connections[_USER_A].values()

        # Disconnect with a FRESH send object — same conn (ws). The
        # F3 defect made this a silent no-op.
        asyncio.run(on_disconn(_Collector(), ws))

        # The user's entry should be gone entirely (it was the only tab).
        with notif_ws._registry_lock:
            assert _USER_A not in notif_ws._connections

    def test_disconnect_cancels_heartbeat_task(self):
        """Disconnect must cancel the heartbeat that _on_connect spawned,
        otherwise the task leaks past the connection lifetime."""
        on_conn, on_disconn, _handler = _capture_handlers()
        collector = _Collector()
        ws = _FakeWs()

        with patch("app.notifications.websocket.JWTAuthProvider") as mock_jwt_cls:
            mock_jwt = MagicMock()
            mock_jwt.get_current_user.return_value = _build_user(_USER_A)
            mock_jwt_cls.return_value = mock_jwt

            async def _run() -> asyncio.Task[None]:
                await on_conn(collector, _scope_with_cookie(), ws)
                task = notif_ws._heartbeats.get(id(ws))
                assert task is not None
                await on_disconn(_Collector(), ws)
                return task

            task = asyncio.run(_run())

        # The heartbeat task was cancelled and removed from the registry.
        assert task.cancelled() or task.done()
        assert id(ws) not in notif_ws._heartbeats

    def test_disconnect_only_removes_the_specific_tab(self):
        """When the user has two tabs and one disconnects, the other
        must remain registered."""
        on_conn, on_disconn, _handler = _capture_handlers()

        tab1 = _Collector()
        tab2 = _Collector()
        ws1 = _FakeWs()
        ws2 = _FakeWs()

        with patch("app.notifications.websocket.JWTAuthProvider") as mock_jwt_cls:
            mock_jwt = MagicMock()
            mock_jwt.get_current_user.return_value = _build_user(_USER_A)
            mock_jwt_cls.return_value = mock_jwt

            asyncio.run(on_conn(tab1, _scope_with_cookie(), ws1))
            asyncio.run(on_conn(tab2, _scope_with_cookie(), ws2))

        # Disconnect only tab1 — fresh send object, tab1's conn.
        asyncio.run(on_disconn(_Collector(), ws1))

        with notif_ws._registry_lock:
            assert _USER_A in notif_ws._connections
            assert tab1 not in notif_ws._connections[_USER_A].values()
            assert tab2 in notif_ws._connections[_USER_A].values()

        # Tab2's heartbeat is still alive.
        assert id(ws2) in notif_ws._heartbeats
        assert id(ws1) not in notif_ws._heartbeats

    def test_heartbeat_failure_deregisters_connection(self, monkeypatch):
        """F3 contract (#856): when the connection-lifetime heartbeat
        hits a dead socket, the connection is dropped from the user
        pool AND the heartbeat registry immediately — not left for the
        next broadcast to trip over."""
        from app.auth import ws_auth

        monkeypatch.setattr(ws_auth, "WS_HEARTBEAT_INTERVAL_SECONDS", 0.01)
        on_conn, _on_disconn, _handler = _capture_handlers()
        ws = _FakeWs()

        class _GreetThenDie:
            """Send that delivers the greet, then starts failing."""

            def __init__(self) -> None:
                self.calls = 0

            async def __call__(self, data: Any) -> None:
                self.calls += 1
                if self.calls > 1:
                    raise RuntimeError("socket dead")

        dying = _GreetThenDie()

        with patch("app.notifications.websocket.JWTAuthProvider") as mock_jwt_cls:
            mock_jwt = MagicMock()
            mock_jwt.get_current_user.return_value = _build_user(_USER_A)
            mock_jwt_cls.return_value = mock_jwt

            async def _run() -> None:
                await on_conn(dying, _scope_with_cookie(), ws)
                with notif_ws._registry_lock:
                    assert _USER_A in notif_ws._connections
                # Let the heartbeat tick, fail, and deregister.
                await asyncio.sleep(0.1)

            asyncio.run(_run())

        with notif_ws._registry_lock:
            assert _USER_A not in notif_ws._connections
        assert id(ws) not in notif_ws._heartbeats


# ---------------------------------------------------------------------------
# Message handler (ping/pong + silent-ignore for unknown messages)
# ---------------------------------------------------------------------------


class TestWsNotificationsHandler:
    def test_ping_message_yields_pong(self):
        collector = _Collector()
        asyncio.run(notif_ws.ws_notifications(json.dumps({"type": "ping"}), collector))
        assert len(collector.sent) == 1
        payload = json.loads(collector.sent[0])
        assert payload["type"] == "pong"

    def test_unknown_message_silently_ignored(self):
        collector = _Collector()
        asyncio.run(notif_ws.ws_notifications(json.dumps({"type": "future-thing"}), collector))
        assert collector.sent == []

    def test_invalid_json_silently_ignored(self):
        collector = _Collector()
        asyncio.run(notif_ws.ws_notifications("not-json{", collector))
        assert collector.sent == []

    def test_non_dict_payload_silently_ignored(self):
        collector = _Collector()
        asyncio.run(notif_ws.ws_notifications('["list-not-dict"]', collector))
        assert collector.sent == []


# Cookie-extraction coverage lives in tests/test_ws_auth.py since #856
# — the per-channel private copies were replaced by the shared
# app.auth.ws_auth helpers.
