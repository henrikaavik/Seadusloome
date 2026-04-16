"""Regression tests for /ws/chat silent refresh (#637).

The HTTP Beforeware rotates tokens when the access_token is expired but
the refresh_token is still valid. The WebSocket handshake bypasses the
Beforeware, so pre-fix it would reject chats for up to 60 minutes past
access-token expiry even when the user still had a valid refresh cookie.

Design note (#637, review)
--------------------------

The WS upgrade response has no Set-Cookie hook, so the WS handshake
MUST NOT consume the refresh token (i.e. delete the old session row
and mint a new pair). If it did, the browser would keep sending the
now-dead refresh cookie, the next HTTP request's Beforeware silent-
refresh would fail, and the user would be redirected to /auth/login
on the very next navigation or quota poll.

The WS handshake therefore uses the verify-only helper
``verify_refresh_token_user`` which checks signature + DB session
row + user active, but leaves session state untouched. The refresh
token still gets rotated atomically on the next HTTP request that
goes through ``auth_before``.

These tests pin the behaviour:

- When only ``refresh_token`` (no valid access_token) is present, the WS
  handshake must verify the refresh token and proceed with the
  authenticated user, WITHOUT deleting the session row or minting a
  new pair.
- A subsequent HTTP request with the SAME cookies must still be able
  to rotate via the normal Beforeware path (regression for the P1
  finding on PR #648).
- When neither cookie is usable, the handshake must fail-closed
  (existing behaviour, preserved).

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
    def test_expired_access_valid_refresh_authenticates_without_consuming(
        self, mock_jwt_cls, mock_provider, mock_orch_cls
    ):
        """Expired access + valid refresh -> WS authenticates, session preserved.

        The WS handshake must verify the refresh token but MUST NOT
        delete the old session or mint new tokens (#637, review). That
        work happens later, on the next HTTP request through the
        Beforeware.
        """
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

        # Refresh token verified.
        mock_jwt_instance.verify_refresh_token.assert_called_once_with("valid-refresh")
        # CRITICAL: session row NOT deleted and NO new tokens minted.
        # The HTTP Beforeware owns rotation — the WS handshake must not
        # consume the refresh token because it cannot persist the
        # replacement cookies.
        mock_jwt_instance.delete_refresh_token.assert_not_called()
        mock_jwt_instance.create_tokens.assert_not_called()
        # Orchestrator received the authenticated auth dict.
        mock_orch_instance.handle_message.assert_called_once()
        call_args = mock_orch_instance.handle_message.call_args
        assert call_args[0][2]["id"] == _USER_ID

    @patch("app.chat.websocket.ChatOrchestrator")
    @patch("app.chat.websocket.get_default_provider")
    @patch("app.chat.websocket.JWTAuthProvider")
    def test_no_access_cookie_only_refresh_works(self, mock_jwt_cls, mock_provider, mock_orch_cls):
        """Even without an access_token cookie at all, a valid refresh
        cookie must authenticate the WS handshake — and still not
        consume the refresh session."""
        user_payload: dict[str, Any] = {
            "id": _USER_ID,
            "email": "a@b.ee",
            "full_name": "A B",
            "role": "drafter",
            "org_id": _ORG_ID,
        }

        mock_jwt_instance = MagicMock()
        mock_jwt_instance.verify_refresh_token.return_value = user_payload
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
        mock_jwt_instance.delete_refresh_token.assert_not_called()
        mock_jwt_instance.create_tokens.assert_not_called()
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


class TestWsFollowedByHttpStillRotates:
    """Regression test for the P1 review finding on PR #648.

    Before the fix, the WS handshake called ``try_refresh_access_token``
    which *consumed* the refresh token: the old session row was deleted
    and a new pair was minted — but the new tokens were thrown away
    because the upgrade response could not set them as cookies. The
    browser kept the stale refresh cookie, and the next HTTP request's
    ``auth_before`` could not rotate it (session row gone), so the user
    was redirected to /auth/login.

    This test drives both code paths against the same stub provider and
    asserts that after the WS handshake, the follow-up HTTP request can
    still complete the silent refresh.
    """

    @patch("app.chat.websocket.ChatOrchestrator")
    @patch("app.chat.websocket.get_default_provider")
    @patch("app.chat.websocket.JWTAuthProvider")
    def test_ws_handshake_then_http_request_same_refresh_still_rotates(
        self, mock_jwt_cls, mock_provider, mock_orch_cls
    ):
        from app.auth import middleware as mw
        from app.auth.middleware import auth_before

        user_payload: dict[str, Any] = {
            "id": _USER_ID,
            "email": "a@b.ee",
            "full_name": "A B",
            "role": "drafter",
            "org_id": _ORG_ID,
        }

        # Shared mock provider used by both transports. Both
        # ``get_current_user`` (access-token validator) returns None
        # (expired access token); ``verify_refresh_token`` returns a
        # valid user both times the refresh cookie is presented — which
        # is what a real provider would do because the WS handshake
        # must NOT delete the session row.
        mock_jwt_instance = MagicMock()
        mock_jwt_instance.get_current_user.return_value = None
        mock_jwt_instance.verify_refresh_token.return_value = user_payload
        mock_jwt_instance.create_tokens.return_value = ("new-access", "new-refresh")
        mock_jwt_cls.return_value = mock_jwt_instance

        mock_orch_instance = MagicMock()
        mock_orch_instance.handle_message = AsyncMock()
        mock_orch_cls.return_value = mock_orch_instance

        # --- step 1: WS handshake with expired access + valid refresh ---
        handler = _capture_handler()
        assert handler is not None

        scope = {
            "headers": [
                (b"cookie", b"access_token=expired; refresh_token=shared-refresh"),
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

        # WS authenticated (handle_message called).
        mock_orch_instance.handle_message.assert_called_once()
        # And — the important part — the WS handshake did NOT consume
        # the refresh session.  ``verify_refresh_token`` runs, but
        # ``delete_refresh_token`` and ``create_tokens`` do NOT.
        assert mock_jwt_instance.verify_refresh_token.call_count == 1
        mock_jwt_instance.delete_refresh_token.assert_not_called()
        mock_jwt_instance.create_tokens.assert_not_called()

        # --- step 2: follow-up HTTP request with the SAME cookies ----
        # ``auth_before`` must still be able to rotate the refresh
        # token because the WS handshake left the session row intact.
        mw._provider = mock_jwt_instance  # inject stub into middleware
        try:
            req = MagicMock()
            req.cookies = {"access_token": "expired", "refresh_token": "shared-refresh"}
            req.url = "http://testserver/dashboard"
            req.scope = {}

            result = auth_before(req)
        finally:
            mw._provider = None

        # HTTP path rotated the refresh cookie (307 redirect with
        # Set-Cookie headers for both new tokens).
        assert result is not None
        assert getattr(result, "status_code", None) == 307
        mock_jwt_instance.delete_refresh_token.assert_called_once_with("shared-refresh")
        mock_jwt_instance.create_tokens.assert_called_once_with(user_payload)

        set_cookies = [
            h for h in getattr(result, "raw_headers", []) if h[0].lower() == b"set-cookie"
        ]
        cookie_blob = b"\n".join(v for _, v in set_cookies).decode()
        assert "access_token=new-access" in cookie_blob
        assert "refresh_token=new-refresh" in cookie_blob
