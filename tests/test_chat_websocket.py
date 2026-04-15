"""Tests for ``app.chat.websocket``.

Tests the WebSocket message parsing, validation, and auth checks.
The orchestrator is mocked out so these tests focus on the WS handler layer.

Uses ``asyncio.run()`` to run async functions, matching the convention
in ``tests/test_chat_tools.py``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from app.chat.websocket import (
    _extract_cookie_from_headers,
    on_connect,
    on_disconnect,
    ws_chat,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_ID = "11111111-1111-1111-1111-111111111111"
_ORG_ID = "22222222-2222-2222-2222-222222222222"
_CONV_ID = "33333333-3333-3333-3333-333333333333"


def _auth_scope() -> dict[str, Any]:
    return {"auth": {"id": _USER_ID, "org_id": _ORG_ID}}


class _Collector:
    """Async-compatible send collector."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def __call__(self, data: str) -> None:
        self.sent.append(data)


# ---------------------------------------------------------------------------
# on_connect / on_disconnect
# ---------------------------------------------------------------------------


class TestOnConnect:
    def test_sends_connected_event(self):
        collector = _Collector()
        asyncio.run(on_connect(collector))
        assert len(collector.sent) == 1
        parsed = json.loads(collector.sent[0])
        assert parsed["type"] == "connected"


class TestOnDisconnect:
    def test_disconnect_does_not_raise(self):
        collector = _Collector()
        asyncio.run(on_disconnect(collector))


# ---------------------------------------------------------------------------
# ws_chat — parsing
# ---------------------------------------------------------------------------


class TestWsChatParsing:
    def test_invalid_json_sends_error(self):
        collector = _Collector()
        asyncio.run(ws_chat("not json at all", collector, _auth_scope()))
        assert len(collector.sent) == 1
        parsed = json.loads(collector.sent[0])
        assert parsed["type"] == "error"

    def test_non_dict_json_sends_error(self):
        collector = _Collector()
        asyncio.run(ws_chat('"just a string"', collector, _auth_scope()))
        assert len(collector.sent) == 1
        parsed = json.loads(collector.sent[0])
        assert parsed["type"] == "error"

    def test_unknown_type_ignored(self):
        collector = _Collector()
        asyncio.run(ws_chat(json.dumps({"type": "ping"}), collector, _auth_scope()))
        # Unknown types are silently ignored
        assert len(collector.sent) == 0


# ---------------------------------------------------------------------------
# ws_chat — validation
# ---------------------------------------------------------------------------


class TestWsChatValidation:
    def test_missing_conversation_id_sends_error(self):
        collector = _Collector()
        msg = json.dumps({"type": "send_message", "content": "Tere"})
        asyncio.run(ws_chat(msg, collector, _auth_scope()))
        assert len(collector.sent) == 1
        parsed = json.loads(collector.sent[0])
        assert parsed["type"] == "error"
        assert "conversation_id" in parsed["message"].lower()

    def test_invalid_conversation_id_sends_error(self):
        collector = _Collector()
        msg = json.dumps(
            {
                "type": "send_message",
                "conversation_id": "not-a-uuid",
                "content": "Tere",
            }
        )
        asyncio.run(ws_chat(msg, collector, _auth_scope()))
        assert len(collector.sent) == 1
        parsed = json.loads(collector.sent[0])
        assert parsed["type"] == "error"

    def test_empty_content_sends_error(self):
        collector = _Collector()
        msg = json.dumps(
            {
                "type": "send_message",
                "conversation_id": _CONV_ID,
                "content": "   ",
            }
        )
        asyncio.run(ws_chat(msg, collector, _auth_scope()))
        assert len(collector.sent) == 1
        parsed = json.loads(collector.sent[0])
        assert parsed["type"] == "error"


# ---------------------------------------------------------------------------
# ws_chat — auth
# ---------------------------------------------------------------------------


class TestWsChatAuth:
    def test_unauthenticated_sends_error(self):
        collector = _Collector()
        msg = json.dumps(
            {
                "type": "send_message",
                "conversation_id": _CONV_ID,
                "content": "Tere",
            }
        )
        asyncio.run(ws_chat(msg, collector, scope={}))
        assert len(collector.sent) == 1
        parsed = json.loads(collector.sent[0])
        assert parsed["type"] == "error"
        assert "autentimine" in parsed["message"].lower()

    def test_none_scope_sends_error(self):
        collector = _Collector()
        msg = json.dumps(
            {
                "type": "send_message",
                "conversation_id": _CONV_ID,
                "content": "Tere",
            }
        )
        asyncio.run(ws_chat(msg, collector, scope=None))
        assert len(collector.sent) == 1
        parsed = json.loads(collector.sent[0])
        assert parsed["type"] == "error"


# ---------------------------------------------------------------------------
# ws_chat — delegates to orchestrator
# ---------------------------------------------------------------------------


class TestWsChatDelegatesToOrchestrator:
    @patch("app.chat.websocket.ChatOrchestrator")
    @patch("app.chat.websocket.get_default_provider")
    def test_valid_message_calls_orchestrator(self, mock_provider, mock_orch_cls):
        mock_instance = MagicMock()
        mock_instance.handle_message = AsyncMock()
        mock_orch_cls.return_value = mock_instance

        collector = _Collector()
        msg = json.dumps(
            {
                "type": "send_message",
                "conversation_id": _CONV_ID,
                "content": "Kuidas see seadus moju avaldab?",
            }
        )
        asyncio.run(ws_chat(msg, collector, _auth_scope()))

        mock_instance.handle_message.assert_called_once()
        call_args = mock_instance.handle_message.call_args
        assert str(call_args[0][0]) == _CONV_ID  # conversation_id
        assert call_args[0][1] == "Kuidas see seadus moju avaldab?"  # content
        assert call_args[0][2]["id"] == _USER_ID  # auth


# ---------------------------------------------------------------------------
# C1: WebSocket auth via JWT cookie extraction
# ---------------------------------------------------------------------------


class TestExtractCookieFromHeaders:
    def test_extracts_access_token_from_cookie_header(self):
        headers = [
            (b"cookie", b"access_token=my-jwt-value; other=abc"),
        ]
        result = _extract_cookie_from_headers(headers, "access_token")
        assert result == "my-jwt-value"

    def test_returns_none_when_cookie_absent(self):
        headers = [
            (b"cookie", b"other=abc"),
        ]
        result = _extract_cookie_from_headers(headers, "access_token")
        assert result is None

    def test_returns_none_when_no_cookie_header(self):
        headers = [
            (b"host", b"localhost"),
        ]
        result = _extract_cookie_from_headers(headers, "access_token")
        assert result is None


class TestWsHandlerAuthExtraction:
    """Test _ws_handler extracts JWT from WS scope and passes auth to ws_chat."""

    @patch("app.chat.websocket.ChatOrchestrator")
    @patch("app.chat.websocket.get_default_provider")
    @patch("app.chat.websocket.JWTAuthProvider")
    def test_ws_handler_with_valid_jwt_cookie(self, mock_jwt_cls, mock_provider, mock_orch_cls):
        """A valid JWT cookie in the handshake headers populates auth."""
        mock_jwt_instance = MagicMock()
        mock_jwt_instance.get_current_user.return_value = {
            "id": _USER_ID,
            "email": "test@test.ee",
            "full_name": "Test User",
            "role": "drafter",
            "org_id": _ORG_ID,
        }
        mock_jwt_cls.return_value = mock_jwt_instance

        mock_orch_instance = MagicMock()
        mock_orch_instance.handle_message = AsyncMock()
        mock_orch_cls.return_value = mock_orch_instance

        from app.chat.websocket import register_chat_ws_routes

        # Capture the registered handler
        mock_app = MagicMock()
        captured_handler: Any = None

        def capture_ws(path, conn=None, disconn=None):
            def decorator(fn):
                nonlocal captured_handler
                captured_handler = fn
                return fn

            return decorator

        mock_app.ws = capture_ws
        register_chat_ws_routes(mock_app)

        assert captured_handler is not None

        # Build a scope with Cookie header containing a JWT token
        scope = {
            "headers": [
                (b"cookie", b"access_token=valid-jwt-token-here"),
            ],
        }

        collector = _Collector()
        msg = json.dumps(
            {
                "type": "send_message",
                "conversation_id": _CONV_ID,
                "content": "Tere!",
            }
        )

        asyncio.run(captured_handler(msg, collector, scope))

        # JWT provider was called with the token
        mock_jwt_instance.get_current_user.assert_called_once_with("valid-jwt-token-here")

        # Orchestrator was called (auth was populated so ws_chat proceeded)
        mock_orch_instance.handle_message.assert_called_once()
        call_args = mock_orch_instance.handle_message.call_args
        assert call_args[0][2]["id"] == _USER_ID

    @patch("app.chat.websocket.JWTAuthProvider")
    def test_ws_handler_without_cookie_sends_error(self, mock_jwt_cls):
        """No JWT cookie in the handshake headers -> auth error."""
        mock_jwt_instance = MagicMock()
        mock_jwt_cls.return_value = mock_jwt_instance

        from app.chat.websocket import register_chat_ws_routes

        mock_app = MagicMock()
        captured_handler: Any = None

        def capture_ws(path, conn=None, disconn=None):
            def decorator(fn):
                nonlocal captured_handler
                captured_handler = fn
                return fn

            return decorator

        mock_app.ws = capture_ws
        register_chat_ws_routes(mock_app)

        assert captured_handler is not None

        # Scope with no cookie header
        scope = {
            "headers": [
                (b"host", b"localhost"),
            ],
        }

        collector = _Collector()
        msg = json.dumps(
            {
                "type": "send_message",
                "conversation_id": _CONV_ID,
                "content": "Tere!",
            }
        )

        asyncio.run(captured_handler(msg, collector, scope))

        # Should send error event (auth required)
        assert len(collector.sent) == 1
        parsed = json.loads(collector.sent[0])
        assert parsed["type"] == "error"
        assert "autentimine" in parsed["message"].lower()


# ---------------------------------------------------------------------------
# Phase UX polish tests (issue #594)
# ---------------------------------------------------------------------------


class TestWsChatMaxLength:
    """Content longer than 10_000 characters is rejected before the orchestrator."""

    @patch("app.chat.websocket.ChatOrchestrator")
    @patch("app.chat.websocket.get_default_provider")
    def test_over_length_rejected_with_error(self, mock_provider, mock_orch_cls):
        mock_instance = MagicMock()
        mock_instance.handle_message = AsyncMock()
        mock_orch_cls.return_value = mock_instance

        collector = _Collector()
        msg = json.dumps(
            {
                "type": "send_message",
                "conversation_id": _CONV_ID,
                "content": "x" * 10_001,
            }
        )
        asyncio.run(ws_chat(msg, collector, _auth_scope()))

        assert len(collector.sent) == 1
        parsed = json.loads(collector.sent[0])
        assert parsed["type"] == "error"
        assert "liiga pikk" in parsed["message"].lower()
        mock_instance.handle_message.assert_not_called()

    @patch("app.chat.websocket.ChatOrchestrator")
    @patch("app.chat.websocket.get_default_provider")
    def test_exactly_limit_accepted(self, mock_provider, mock_orch_cls):
        mock_instance = MagicMock()
        mock_instance.handle_message = AsyncMock()
        mock_orch_cls.return_value = mock_instance

        collector = _Collector()
        msg = json.dumps(
            {
                "type": "send_message",
                "conversation_id": _CONV_ID,
                "content": "x" * 10_000,
            }
        )
        asyncio.run(ws_chat(msg, collector, _auth_scope()))

        mock_instance.handle_message.assert_called_once()


class TestWsChatJwtFailClosed:
    """When JWTAuthProvider construction fails the socket must be closed with 1011."""

    @patch("app.chat.websocket.JWTAuthProvider")
    def test_jwt_provider_construction_failure_closes_socket(self, mock_jwt_cls):
        mock_jwt_cls.side_effect = RuntimeError("cannot init JWT provider")

        from app.chat.websocket import register_chat_ws_routes

        mock_app = MagicMock()
        captured_handler: Any = None

        def capture_ws(path, conn=None, disconn=None):
            def decorator(fn):
                nonlocal captured_handler
                captured_handler = fn
                return fn

            return decorator

        mock_app.ws = capture_ws
        register_chat_ws_routes(mock_app)
        assert captured_handler is not None

        scope = {
            "headers": [
                (b"cookie", b"access_token=irrelevant"),
            ],
        }

        sent_raw: list[Any] = []

        async def raw_send(data: Any) -> None:
            sent_raw.append(data)

        msg = json.dumps(
            {
                "type": "send_message",
                "conversation_id": _CONV_ID,
                "content": "Tere",
            }
        )

        asyncio.run(captured_handler(msg, raw_send, scope))

        # We expect a close frame with code 1011 to have been attempted.
        close_attempts = [
            d for d in sent_raw if isinstance(d, dict) and d.get("type") == "websocket.close"
        ]
        assert len(close_attempts) == 1
        assert close_attempts[0]["code"] == 1011


class TestWsChatStopGeneration:
    """``stop_generation`` cancels an in-flight orchestrator task."""

    def test_stop_cancels_running_task_and_emits_stopped(self):
        cancel_observed: dict[str, bool] = {"called": False}

        async def slow_handle(conv_id, content, auth, send_event):
            try:
                await send_event({"type": "content_delta", "delta": "partial"})
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancel_observed["called"] = True
                # orchestrator normally emits stopped; mimic that.
                await send_event({"type": "stopped", "message_id": None})
                raise

        mock_instance = MagicMock()
        mock_instance.handle_message = AsyncMock(side_effect=slow_handle)

        collector = _Collector()

        async def scenario():
            with (
                patch("app.chat.websocket.ChatOrchestrator", return_value=mock_instance),
                patch("app.chat.websocket.get_default_provider"),
            ):
                tasks: dict[str, Any] = {}

                send_msg = json.dumps(
                    {
                        "type": "send_message",
                        "conversation_id": _CONV_ID,
                        "content": "Tere",
                    }
                )
                stop_msg = json.dumps(
                    {
                        "type": "stop_generation",
                        "conversation_id": _CONV_ID,
                    }
                )

                send_task = asyncio.create_task(
                    ws_chat(send_msg, collector, _auth_scope(), active_tasks=tasks)
                )
                # Give the orchestrator a tick to start.
                await asyncio.sleep(0.05)
                await ws_chat(stop_msg, collector, _auth_scope(), active_tasks=tasks)
                await send_task

        asyncio.run(scenario())

        assert cancel_observed["called"] is True
        stopped = [s for s in collector.sent if json.loads(s).get("type") == "stopped"]
        assert len(stopped) >= 1

    def test_stop_without_registry_still_acks(self):
        collector = _Collector()
        msg = json.dumps(
            {
                "type": "stop_generation",
                "conversation_id": _CONV_ID,
            }
        )
        asyncio.run(ws_chat(msg, collector, _auth_scope()))
        assert len(collector.sent) == 1
        parsed = json.loads(collector.sent[0])
        assert parsed["type"] == "stopped"


class TestWsHandlerDisconnectCleanup:
    """Disconnect mid-stream must cancel pending tasks and drop the registry key."""

    @patch("app.chat.websocket.ChatOrchestrator")
    @patch("app.chat.websocket.get_default_provider")
    @patch("app.chat.websocket.JWTAuthProvider")
    def test_disconnect_drops_registry_key(self, mock_jwt_cls, mock_provider, mock_orch_cls):
        mock_jwt_instance = MagicMock()
        mock_jwt_instance.get_current_user.return_value = {
            "id": _USER_ID,
            "org_id": _ORG_ID,
            "role": "drafter",
            "email": "t@t.ee",
            "full_name": "T",
        }
        mock_jwt_cls.return_value = mock_jwt_instance

        # Orchestrator stub that blocks so we have a pending task when
        # the disconnect fires.
        async def slow_handle(conv_id, content, auth, send_event):
            try:
                await send_event({"type": "content_delta", "delta": "x"})
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise

        mock_instance = MagicMock()
        mock_instance.handle_message = AsyncMock(side_effect=slow_handle)
        mock_orch_cls.return_value = mock_instance

        from app.chat.websocket import register_chat_ws_routes

        mock_app = MagicMock()
        captured_handler: Any = None

        def capture_ws(path, conn=None, disconn=None):
            def decorator(fn):
                nonlocal captured_handler
                captured_handler = fn
                return fn

            return decorator

        mock_app.ws = capture_ws
        register_chat_ws_routes(mock_app)
        assert captured_handler is not None

        registry = captured_handler._per_send_tasks  # type: ignore[attr-defined]
        on_disconnect_hook = captured_handler._on_disconnect  # type: ignore[attr-defined]

        scope = {"headers": [(b"cookie", b"access_token=irrelevant")]}

        async def send(data: Any) -> None:
            # no-op; just needs to be a stable callable so id(send) is
            # consistent across the start and the disconnect.
            return

        send_msg = json.dumps(
            {
                "type": "send_message",
                "conversation_id": _CONV_ID,
                "content": "Tere",
            }
        )

        handler_fn = captured_handler  # re-bind so pyright narrows through the closure
        assert handler_fn is not None
        disconnect_fn = on_disconnect_hook
        assert disconnect_fn is not None

        async def scenario() -> None:
            handler_task = asyncio.create_task(handler_fn(send_msg, send, scope))
            # Let the orchestrator register its per-conv task.
            await asyncio.sleep(0.05)
            # Exactly one connection is registered.
            assert id(send) in registry
            assert len(registry[id(send)]) == 1
            # Simulate the socket going away.
            await disconnect_fn(send)
            # Registry slot gone, tasks cancelled.
            assert id(send) not in registry
            # Let the handler coroutine drain the CancelledError.
            try:
                await handler_task
            except Exception:
                pass

        asyncio.run(scenario())

        # After teardown, zero keys remain for this connection.
        assert id(send) not in registry
        assert all(k != id(send) for k in registry)
