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
        captured_handler = None

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
        captured_handler = None

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
