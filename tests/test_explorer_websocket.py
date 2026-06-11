"""Tests for ``app.explorer.websocket`` (#856, review finding F2).

``/ws/explorer`` used to be the only WS channel with no handshake
authentication, no heartbeat, and an unbounded connection set keyed on
``send`` identity (which FastHTML rebuilds per dispatch, so disconnect
cleanup relied on broadcast-failure pruning). These tests pin the
fixed behaviour:

* unauthenticated handshakes are closed with 1008 via ``ws.close()``;
* a JWT-provider outage fails closed with 1011;
* authenticated connects register the socket (keyed on ``id(ws)``)
  and start a connection-lifetime heartbeat;
* the connection set is bounded — excess handshakes get 1013;
* disconnect cleanup works even though the ``send`` object differs
  between the connect and disconnect hooks (the F3-class trap);
* the heartbeat-failure path deregisters the connection.

Same direct-handler style as ``tests/test_notifications_websocket.py``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.auth import ws_auth
from app.explorer import websocket as explorer_ws

_USER_ID = "11111111-1111-1111-1111-111111111111"


class _Collector:
    """Async-compatible send collector."""

    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def __call__(self, data: Any) -> None:
        self.sent.append(data)


class _FakeWs:
    """Minimal stand-in for the Starlette WebSocket conn."""

    def __init__(self) -> None:
        self.close_calls: list[tuple[int, str]] = []

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_calls.append((code, reason))


def _build_user(user_id: str = _USER_ID) -> dict[str, Any]:
    return {
        "id": user_id,
        "email": "test@riik.ee",
        "full_name": "Test Kasutaja",
        "role": "drafter",
        "org_id": "33333333-3333-3333-3333-333333333333",
    }


def _capture_handlers() -> tuple[Any, Any, Any]:
    """Register against a fake app; capture conn/disconn/message handlers."""
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
    explorer_ws.register_ws_routes(mock_app)
    return captured["conn"], captured["disconn"], captured["handler"]


def _scope_with_cookie(token: str = "access-token-value") -> dict[str, Any]:
    return {"headers": [(b"cookie", f"access_token={token}".encode())]}


@pytest.fixture(autouse=True)
def _reset_registries():
    """Wipe the module-level connection + heartbeat registries between tests."""
    with explorer_ws._clients_lock:
        explorer_ws._connected_clients.clear()
    explorer_ws._heartbeats.cancel_clear()
    yield
    with explorer_ws._clients_lock:
        explorer_ws._connected_clients.clear()
    explorer_ws._heartbeats.cancel_clear()


# ---------------------------------------------------------------------------
# Handshake auth (F2)
# ---------------------------------------------------------------------------


class TestConnectAuth:
    def test_unauthenticated_connect_closes_with_1008_and_skips_registration(self):
        on_conn, _disconn, _handler = _capture_handlers()
        ws = _FakeWs()
        collector = _Collector()

        # No cookie header at all → no auth.
        asyncio.run(on_conn(collector, {"headers": []}, ws))

        assert ws.close_calls == [(1008, "authentication required")]
        # The close must NOT have been pushed through ``send`` as a frame.
        assert collector.sent == []
        with explorer_ws._clients_lock:
            assert explorer_ws._connected_clients == {}
        assert id(ws) not in explorer_ws._heartbeats

    def test_invalid_jwt_closes_with_1008(self):
        on_conn, _disconn, _handler = _capture_handlers()
        ws = _FakeWs()

        with patch("app.explorer.websocket.JWTAuthProvider") as mock_jwt_cls:
            mock_jwt = MagicMock()
            mock_jwt.get_current_user.return_value = None  # invalid token
            mock_jwt.verify_refresh_token.return_value = None
            mock_jwt_cls.return_value = mock_jwt

            asyncio.run(on_conn(_Collector(), _scope_with_cookie("bad-token"), ws))

        assert ws.close_calls == [(1008, "authentication required")]
        with explorer_ws._clients_lock:
            assert explorer_ws._connected_clients == {}

    def test_provider_outage_fails_closed_with_1011(self):
        on_conn, _disconn, _handler = _capture_handlers()
        ws = _FakeWs()

        with patch(
            "app.explorer.websocket.JWTAuthProvider",
            side_effect=RuntimeError("cannot init JWT provider"),
        ):
            asyncio.run(on_conn(_Collector(), _scope_with_cookie(), ws))

        assert ws.close_calls == [(1011, "auth provider unavailable")]
        with explorer_ws._clients_lock:
            assert explorer_ws._connected_clients == {}

    def test_authenticated_connect_registers_and_starts_heartbeat(self):
        on_conn, _disconn, _handler = _capture_handlers()
        ws = _FakeWs()
        collector = _Collector()

        captured: dict[str, Any] = {}

        async def _run() -> None:
            await on_conn(collector, _scope_with_cookie(), ws)
            # Inspect inside the loop — asyncio.run cancels leftovers
            # on close, which would make done() checks misleading.
            captured["registered"] = id(ws) in explorer_ws._heartbeats
            captured["task"] = explorer_ws._heartbeats.get(id(ws))

        with patch("app.explorer.websocket.JWTAuthProvider") as mock_jwt_cls:
            mock_jwt = MagicMock()
            mock_jwt.get_current_user.return_value = _build_user()
            mock_jwt_cls.return_value = mock_jwt

            asyncio.run(_run())

        assert ws.close_calls == []
        with explorer_ws._clients_lock:
            assert list(explorer_ws._connected_clients.keys()) == [id(ws)]
            assert explorer_ws._connected_clients[id(ws)] is collector
        assert captured["registered"] is True
        assert isinstance(captured["task"], asyncio.Task)


# ---------------------------------------------------------------------------
# Bounded connection set (F2)
# ---------------------------------------------------------------------------


class TestConnectionBound:
    def test_connect_beyond_limit_closes_with_1013(self, monkeypatch):
        monkeypatch.setattr(explorer_ws, "_MAX_CONNECTIONS", 2)
        on_conn, _disconn, _handler = _capture_handlers()

        with patch("app.explorer.websocket.JWTAuthProvider") as mock_jwt_cls:
            mock_jwt = MagicMock()
            mock_jwt.get_current_user.return_value = _build_user()
            mock_jwt_cls.return_value = mock_jwt

            ws1, ws2, ws3 = _FakeWs(), _FakeWs(), _FakeWs()

            async def _run() -> None:
                await on_conn(_Collector(), _scope_with_cookie(), ws1)
                await on_conn(_Collector(), _scope_with_cookie(), ws2)
                await on_conn(_Collector(), _scope_with_cookie(), ws3)

            asyncio.run(_run())

        assert ws1.close_calls == []
        assert ws2.close_calls == []
        assert ws3.close_calls == [(1013, "too many connections")]
        with explorer_ws._clients_lock:
            assert len(explorer_ws._connected_clients) == 2
            assert id(ws3) not in explorer_ws._connected_clients
        # No heartbeat leaked for the rejected socket.
        assert id(ws3) not in explorer_ws._heartbeats


# ---------------------------------------------------------------------------
# Disconnect cleanup — keyed on id(ws), NOT on send identity (F3-class)
# ---------------------------------------------------------------------------


class TestDisconnectCleanup:
    def test_disconnect_with_different_send_object_still_cleans_up(self):
        """FastHTML hands the disconnect hook a freshly-built ``send``
        partial — never the object the connect hook saw. Cleanup must
        therefore key on the stable conn identity. We deliberately
        pass two DIFFERENT send objects here; the old send-identity
        registry would leak the slot forever under this test."""
        on_conn, on_disconn, _handler = _capture_handlers()
        ws = _FakeWs()
        connect_send = _Collector()
        disconnect_send = _Collector()  # different object, same conn
        assert connect_send is not disconnect_send

        with patch("app.explorer.websocket.JWTAuthProvider") as mock_jwt_cls:
            mock_jwt = MagicMock()
            mock_jwt.get_current_user.return_value = _build_user()
            mock_jwt_cls.return_value = mock_jwt

            async def _run() -> None:
                await on_conn(connect_send, _scope_with_cookie(), ws)
                with explorer_ws._clients_lock:
                    assert len(explorer_ws._connected_clients) == 1
                await on_disconn(disconnect_send, ws)

            asyncio.run(_run())

        # Registry size after disconnect: zero — no leak.
        with explorer_ws._clients_lock:
            assert explorer_ws._connected_clients == {}
        assert id(ws) not in explorer_ws._heartbeats

    def test_disconnect_cancels_heartbeat_task(self):
        on_conn, on_disconn, _handler = _capture_handlers()
        ws = _FakeWs()

        with patch("app.explorer.websocket.JWTAuthProvider") as mock_jwt_cls:
            mock_jwt = MagicMock()
            mock_jwt.get_current_user.return_value = _build_user()
            mock_jwt_cls.return_value = mock_jwt

            async def _run() -> asyncio.Task[None]:
                await on_conn(_Collector(), _scope_with_cookie(), ws)
                task = explorer_ws._heartbeats.get(id(ws))
                assert task is not None
                await on_disconn(_Collector(), ws)
                return task

            task = asyncio.run(_run())

        assert task.cancelled() or task.done()
        assert id(ws) not in explorer_ws._heartbeats


# ---------------------------------------------------------------------------
# Heartbeat-failure deregistration (F3 contract)
# ---------------------------------------------------------------------------


class TestHeartbeatFailureDeregisters:
    def test_dead_socket_is_dropped_when_heartbeat_send_fails(self, monkeypatch):
        monkeypatch.setattr(ws_auth, "WS_HEARTBEAT_INTERVAL_SECONDS", 0.01)
        on_conn, _disconn, _handler = _capture_handlers()
        ws = _FakeWs()

        class _DyingSend:
            """Send that works for the greet, then starts failing."""

            def __init__(self) -> None:
                self.calls = 0

            async def __call__(self, data: Any) -> None:
                self.calls += 1
                raise RuntimeError("socket dead")

        dying = _DyingSend()

        with patch("app.explorer.websocket.JWTAuthProvider") as mock_jwt_cls:
            mock_jwt = MagicMock()
            mock_jwt.get_current_user.return_value = _build_user()
            mock_jwt_cls.return_value = mock_jwt

            async def _run() -> None:
                await on_conn(dying, _scope_with_cookie(), ws)
                with explorer_ws._clients_lock:
                    assert len(explorer_ws._connected_clients) == 1
                # Let the heartbeat tick, fail, and deregister.
                await asyncio.sleep(0.1)

            asyncio.run(_run())

        with explorer_ws._clients_lock:
            assert explorer_ws._connected_clients == {}
        assert id(ws) not in explorer_ws._heartbeats


# ---------------------------------------------------------------------------
# Broadcast
# ---------------------------------------------------------------------------


class TestBroadcast:
    def test_notify_sync_complete_reaches_registered_clients(self):
        c1, c2 = _Collector(), _Collector()
        explorer_ws._register_client(101, c1)
        explorer_ws._register_client(102, c2)

        asyncio.run(explorer_ws.notify_sync_complete())

        for collector in (c1, c2):
            assert len(collector.sent) == 1
            payload = json.loads(collector.sent[0])
            assert payload["event"] == "sync_complete"

    def test_failing_client_is_pruned_from_registry(self):
        class _Bomb:
            async def __call__(self, data: Any) -> None:
                raise RuntimeError("socket dead")

        good = _Collector()
        explorer_ws._register_client(201, _Bomb())
        explorer_ws._register_client(202, good)

        asyncio.run(explorer_ws.notify_sync_complete())

        with explorer_ws._clients_lock:
            assert 201 not in explorer_ws._connected_clients
            assert 202 in explorer_ws._connected_clients
        assert len(good.sent) == 1

    def test_broadcast_with_no_clients_is_noop(self):
        asyncio.run(explorer_ws.notify_sync_complete())


# ---------------------------------------------------------------------------
# Integration — real FastHTML resolver (the #802-trap canary for this channel)
# ---------------------------------------------------------------------------


class TestExplorerWsViaFastHTMLResolver:
    """Drive the real ``_find_p`` path: hooks declare unannotated
    ``send``/``scope``/``ws``, and the registry keys on the conn object
    FastHTML actually injects (whose ``send`` partner differs between
    the connect and disconnect dispatches)."""

    def test_unauthenticated_handshake_is_closed_1008(self):
        from fasthtml.common import FastHTML
        from starlette.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        app = FastHTML()
        explorer_ws.register_ws_routes(app)
        client = TestClient(app)

        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect("/ws/explorer") as conn:
                conn.receive_text()

        assert excinfo.value.code == 1008
        with explorer_ws._clients_lock:
            assert explorer_ws._connected_clients == {}

    def test_authenticated_connect_then_disconnect_leaves_no_registry_entry(self):
        from fasthtml.common import FastHTML
        from starlette.testclient import TestClient

        app = FastHTML()
        explorer_ws.register_ws_routes(app)
        client = TestClient(app)

        with patch("app.explorer.websocket.JWTAuthProvider") as mock_jwt_cls:
            mock_jwt = MagicMock()
            mock_jwt.get_current_user.return_value = _build_user()
            mock_jwt_cls.return_value = mock_jwt

            with client.websocket_connect("/ws/explorer", headers={"cookie": "access_token=tok"}):
                with explorer_ws._clients_lock:
                    assert len(explorer_ws._connected_clients) == 1

        # Registry size after a REAL disconnect dispatch (different
        # ``send`` partial than connect) must be zero — the F3-class
        # leak this issue fixes.
        with explorer_ws._clients_lock:
            assert explorer_ws._connected_clients == {}


# ---------------------------------------------------------------------------
# Route registration smoke
# ---------------------------------------------------------------------------


class TestRouteRegistration:
    def test_registers_explorer_path_with_lifecycle_hooks(self):
        recorded: dict[str, Any] = {}

        class _StubApp:
            def ws(self, path: str, conn=None, disconn=None):
                recorded["path"] = path
                recorded["conn"] = conn
                recorded["disconn"] = disconn

                def _decorator(handler: Any) -> Any:
                    recorded["handler"] = handler
                    return handler

                return _decorator

        explorer_ws.register_ws_routes(_StubApp())
        assert recorded["path"] == "/ws/explorer"
        assert recorded["conn"] is not None
        assert recorded["disconn"] is not None
        assert recorded["handler"] is explorer_ws.ws_explorer
