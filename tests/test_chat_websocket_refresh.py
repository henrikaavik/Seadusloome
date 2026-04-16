"""Regression tests for /ws/chat silent refresh (#637).

The HTTP Beforeware rotates tokens when the access_token is expired but
the refresh_token is still valid. The WebSocket handshake bypasses the
Beforeware, so pre-fix it would reject chats for up to 60 minutes past
access-token expiry even when the user still had a valid refresh cookie.

These tests pin the behaviour:

- When only ``refresh_token`` (no valid access_token) is present, the WS
  handshake must verify the refresh token, mint a new pair, and proceed
  with the authenticated user.
- When neither cookie is usable, the handshake must fail-closed (existing
  behaviour, preserved).

The JWT provider is mocked so no real DB is needed.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

_USER_ID = "11111111-1111-1111-1111-111111111111"
_ORG_ID = "22222222-2222-2222-2222-222222222222"
_CONV_ID = "33333333-3333-3333-3333-333333333333"


class _FreshSend:
    """A stable send callable that records every ASGI message sent."""

    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def __call__(self, data: Any) -> None:
        self.sent.append(data)


def _capture_handler():
    from app.chat.websocket import register_chat_ws_routes

    mock_app = MagicMock()
    captured: dict[str, Any] = {"handler": None}

    def capture_ws(path: str, conn: Any = None, disconn: Any = None) -> Any:
        def decorator(fn: Any) -> Any:
            captured["handler"] = fn
            return fn

        return decorator

    mock_app.ws = capture_ws
    register_chat_ws_routes(mock_app)
    return captured["handler"]


class TestWsChatSilentRefresh:
    """Only a refresh_token cookie — handshake must refresh and proceed."""

    @patch("app.chat.websocket.ChatOrchestrator")
    @patch("app.chat.websocket.get_default_provider")
    @patch("app.chat.websocket.JWTAuthProvider")
    def test_expired_access_valid_refresh_rotates_and_authenticates(
        self, mock_jwt_cls, mock_provider, mock_orch_cls
    ):
        """Expired access + valid refresh → WS authenticates."""
        user_payload: dict[str, Any] = {
            "id": _USER_ID,
            "email": "a@b.ee",
            "full_name": "A B",
            "role": "drafter",
            "org_id": _ORG_ID,
        }

        mock_jwt_instance = MagicMock()
        # Access token is present but invalid (expired or stale).
        mock_jwt_instance.get_current_user.return_value = None
        # Refresh token verifies cleanly.
        mock_jwt_instance.verify_refresh_token.return_value = user_payload
        mock_jwt_instance.create_tokens.return_value = (
            "new-access-xyz",
            "new-refresh-xyz",
        )
        mock_jwt_cls.return_value = mock_jwt_instance

        mock_orch_instance = MagicMock()
        mock_orch_instance.handle_message = AsyncMock()
        mock_orch_cls.return_value = mock_orch_instance

        handler = _capture_handler()
        assert handler is not None

        scope = {
            "headers": [
                (b"cookie", b"access_token=expired; refresh_token=valid-refresh"),
            ],
        }
        send = _FreshSend()
        msg = json.dumps(
            {
                "type": "send_message",
                "conversation_id": _CONV_ID,
                "content": "Tere!",
            }
        )

        asyncio.run(handler(msg, send, scope))

        # Refresh verified and new tokens minted.
        mock_jwt_instance.verify_refresh_token.assert_called_once_with("valid-refresh")
        mock_jwt_instance.create_tokens.assert_called_once_with(user_payload)
        # Old refresh removed so it cannot be reused.
        mock_jwt_instance.delete_refresh_token.assert_called_once_with("valid-refresh")
        # Orchestrator received the authenticated auth dict.
        mock_orch_instance.handle_message.assert_called_once()
        call_args = mock_orch_instance.handle_message.call_args
        assert call_args[0][2]["id"] == _USER_ID

    @patch("app.chat.websocket.ChatOrchestrator")
    @patch("app.chat.websocket.get_default_provider")
    @patch("app.chat.websocket.JWTAuthProvider")
    def test_no_access_cookie_only_refresh_works(self, mock_jwt_cls, mock_provider, mock_orch_cls):
        """Even without an access_token cookie at all, a valid refresh
        cookie must authenticate the WS handshake."""
        user_payload: dict[str, Any] = {
            "id": _USER_ID,
            "email": "a@b.ee",
            "full_name": "A B",
            "role": "drafter",
            "org_id": _ORG_ID,
        }

        mock_jwt_instance = MagicMock()
        mock_jwt_instance.verify_refresh_token.return_value = user_payload
        mock_jwt_instance.create_tokens.return_value = ("a", "r")
        mock_jwt_cls.return_value = mock_jwt_instance

        mock_orch_instance = MagicMock()
        mock_orch_instance.handle_message = AsyncMock()
        mock_orch_cls.return_value = mock_orch_instance

        handler = _capture_handler()
        assert handler is not None

        scope = {
            "headers": [
                (b"cookie", b"refresh_token=only-refresh"),
            ],
        }
        send = _FreshSend()
        msg = json.dumps(
            {
                "type": "send_message",
                "conversation_id": _CONV_ID,
                "content": "Hei",
            }
        )

        asyncio.run(handler(msg, send, scope))

        mock_jwt_instance.verify_refresh_token.assert_called_once_with("only-refresh")
        mock_orch_instance.handle_message.assert_called_once()

    @patch("app.chat.websocket.ChatOrchestrator")
    @patch("app.chat.websocket.get_default_provider")
    @patch("app.chat.websocket.JWTAuthProvider")
    def test_neither_cookie_rejects_with_auth_error(
        self, mock_jwt_cls, mock_provider, mock_orch_cls
    ):
        mock_jwt_instance = MagicMock()
        mock_jwt_cls.return_value = mock_jwt_instance

        handler = _capture_handler()
        assert handler is not None

        scope = {"headers": [(b"host", b"localhost")]}
        send = _FreshSend()
        msg = json.dumps(
            {
                "type": "send_message",
                "conversation_id": _CONV_ID,
                "content": "Hei",
            }
        )

        asyncio.run(handler(msg, send, scope))

        # verify_refresh_token must not even be attempted with no cookie.
        mock_jwt_instance.verify_refresh_token.assert_not_called()
        # The existing "autentimine nõutav" error path is preserved.
        error_events = [json.loads(m) for m in send.sent if isinstance(m, str)]
        assert any(
            e.get("type") == "error" and "autentimine" in e.get("message", "").lower()
            for e in error_events
        )

    @patch("app.chat.websocket.ChatOrchestrator")
    @patch("app.chat.websocket.get_default_provider")
    @patch("app.chat.websocket.JWTAuthProvider")
    def test_invalid_refresh_rejects(self, mock_jwt_cls, mock_provider, mock_orch_cls):
        mock_jwt_instance = MagicMock()
        mock_jwt_instance.get_current_user.return_value = None
        mock_jwt_instance.verify_refresh_token.return_value = None  # invalid
        mock_jwt_cls.return_value = mock_jwt_instance

        handler = _capture_handler()
        assert handler is not None

        scope = {"headers": [(b"cookie", b"refresh_token=dead")]}
        send = _FreshSend()
        msg = json.dumps(
            {
                "type": "send_message",
                "conversation_id": _CONV_ID,
                "content": "Hei",
            }
        )

        asyncio.run(handler(msg, send, scope))

        mock_jwt_instance.verify_refresh_token.assert_called_once_with("dead")
        # Must not touch delete/create on invalid refresh.
        mock_jwt_instance.delete_refresh_token.assert_not_called()
        mock_jwt_instance.create_tokens.assert_not_called()
        # And must emit an auth-required error.
        error_events = [json.loads(m) for m in send.sent if isinstance(m, str)]
        assert any(
            e.get("type") == "error" and "autentimine" in e.get("message", "").lower()
            for e in error_events
        )
