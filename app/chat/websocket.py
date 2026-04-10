"""WebSocket handler for AI Advisory Chat streaming.

Uses the same FastHTML ``@app.ws()`` pattern established by
:mod:`app.explorer.websocket`. The handler parses incoming JSON
messages, delegates to :class:`~app.chat.orchestrator.ChatOrchestrator`,
and streams events back to the client as HTML fragments.
"""

from __future__ import annotations

import json
import logging
import uuid
from http.cookies import SimpleCookie
from typing import Any

from app.auth.jwt_provider import JWTAuthProvider
from app.chat.orchestrator import ChatOrchestrator
from app.llm.claude import get_default_provider

logger = logging.getLogger(__name__)


async def on_connect(send: Any) -> None:
    """Called when a WebSocket client connects to /ws/chat."""
    logger.info("Chat WS client connected")
    await send(json.dumps({"type": "connected"}))


async def on_disconnect(send: Any) -> None:
    """Called when a WebSocket client disconnects."""
    logger.info("Chat WS client disconnected")


async def ws_chat(msg: str, send: Any, scope: dict[str, Any] | None = None) -> None:
    """Handle incoming chat WebSocket messages.

    Expected message format::

        {"type": "send_message", "conversation_id": "<uuid>", "content": "..."}

    All other message types are silently ignored.

    Parameters
    ----------
    msg:
        Raw message string from the client.
    send:
        Async callback for pushing responses to the client.
    scope:
        Optional ASGI scope dict for auth extraction (injected by
        test harness or passed from the WS handler wrapper).
    """
    # Parse JSON
    try:
        data = json.loads(msg)
    except (json.JSONDecodeError, TypeError):
        await send(json.dumps({"type": "error", "message": "Vigane JSON."}))
        return

    if not isinstance(data, dict):
        await send(json.dumps({"type": "error", "message": "Vigane sonum."}))
        return

    msg_type = data.get("type")
    if msg_type != "send_message":
        # Silently ignore unknown message types
        return

    # Validate conversation_id
    raw_conv_id = data.get("conversation_id")
    if not raw_conv_id:
        await send(json.dumps({"type": "error", "message": "Puudub conversation_id."}))
        return

    try:
        conversation_id = uuid.UUID(str(raw_conv_id))
    except (ValueError, TypeError):
        await send(json.dumps({"type": "error", "message": "Vigane conversation_id."}))
        return

    content = data.get("content", "").strip()
    if not content:
        await send(json.dumps({"type": "error", "message": "Tühi sõnum."}))
        return

    # Extract auth from scope
    auth: dict[str, Any] = {}
    if scope is not None:
        auth = scope.get("auth") or {}

    if not auth or not auth.get("id"):
        await send(json.dumps({"type": "error", "message": "Autentimine nõutav."}))
        return

    # Create orchestrator and handle message
    orchestrator = ChatOrchestrator(get_default_provider())

    async def send_event(event: dict[str, Any]) -> None:
        """Serialize event as JSON and push to WS client."""
        await send(json.dumps(event, default=str))

    await orchestrator.handle_message(conversation_id, content, auth, send_event)


def _extract_cookie_from_headers(headers: list[tuple[bytes, bytes]], name: str) -> str | None:
    """Parse the ``Cookie`` header from raw ASGI *headers* and return the value for *name*.

    Returns ``None`` when the cookie is absent or the headers contain no
    ``Cookie`` entry.
    """
    for hdr_name, hdr_value in headers:
        if hdr_name.lower() == b"cookie":
            cookie: SimpleCookie = SimpleCookie()
            cookie.load(hdr_value.decode("latin-1"))
            morsel = cookie.get(name)
            if morsel is not None:
                return morsel.value
    return None


def register_chat_ws_routes(app: Any) -> None:
    """Register the chat WebSocket route on the FastHTML *app*.

    Uses the same ``app.ws()`` pattern as
    :func:`app.explorer.websocket.register_ws_routes`.
    """

    # Shared provider instance for cookie verification; lazily created
    # the first time a WS message arrives (avoids import-time DB hits).
    _jwt_provider: list[JWTAuthProvider | None] = [None]

    def _get_jwt_provider() -> JWTAuthProvider:
        if _jwt_provider[0] is None:
            _jwt_provider[0] = JWTAuthProvider()
        return _jwt_provider[0]

    async def _ws_handler(msg: str, send: Any, scope: dict[str, Any] | None = None) -> None:
        """Extract JWT from WS handshake cookies and pass auth scope to ws_chat."""
        auth_scope: dict[str, Any] = {}

        if scope is not None:
            # Try to extract the access_token from handshake headers.
            raw_headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
            access_token = _extract_cookie_from_headers(raw_headers, "access_token")

            if access_token:
                provider = _get_jwt_provider()
                user = provider.get_current_user(access_token)
                if user is not None:
                    auth_scope["auth"] = dict(user)

        await ws_chat(msg, send, auth_scope if auth_scope else None)

    app.ws("/ws/chat", conn=on_connect, disconn=on_disconnect)(_ws_handler)
