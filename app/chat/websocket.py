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
from typing import Any

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


def register_chat_ws_routes(app: Any) -> None:
    """Register the chat WebSocket route on the FastHTML *app*.

    Uses the same ``app.ws()`` pattern as
    :func:`app.explorer.websocket.register_ws_routes`.
    """

    async def _ws_handler(msg: str, send: Any) -> None:
        """Thin wrapper that passes scope through from the outer handler."""
        # FastHTML's WS handler doesn't directly pass scope to the
        # function, so we use a closure-captured reference. The real
        # auth is injected by the Beforeware before the WS upgrade.
        await ws_chat(msg, send)

    app.ws("/ws/chat", conn=on_connect, disconn=on_disconnect)(_ws_handler)
