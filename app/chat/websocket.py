"""WebSocket handler for AI Advisory Chat streaming.

Uses the same FastHTML ``@app.ws()`` pattern established by
:mod:`app.explorer.websocket`. The handler parses incoming JSON
messages, delegates to :class:`~app.chat.orchestrator.ChatOrchestrator`,
and streams events back to the client as HTML fragments.

Phase UX polish additions (issue #594):

- **Max message length**: user-supplied ``content`` longer than
  ``_MAX_MESSAGE_LENGTH`` characters is rejected with an error event
  and never reaches the orchestrator.
- **JWT fail-closed**: when the JWT provider cannot be constructed we
  close the WebSocket with code ``1011`` instead of proceeding with an
  empty auth scope.
- **Stop generation**: a ``{"type": "stop_generation", ...}`` message
  cancels the currently-running orchestrator task for the connection.
  The orchestrator catches ``CancelledError`` and persists the partial
  assistant turn with ``is_truncated=True``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from http.cookies import SimpleCookie
from typing import Any

from app.auth.jwt_provider import JWTAuthProvider
from app.chat.orchestrator import ChatOrchestrator
from app.llm.claude import get_default_provider

logger = logging.getLogger(__name__)

_MAX_MESSAGE_LENGTH = 10_000


async def on_connect(send: Any) -> None:
    """Called when a WebSocket client connects to /ws/chat."""
    logger.info("Chat WS client connected")
    await send(json.dumps({"type": "connected"}))


async def on_disconnect(send: Any) -> None:
    """Called when a WebSocket client disconnects."""
    logger.info("Chat WS client disconnected")


async def ws_chat(
    msg: str,
    send: Any,
    scope: dict[str, Any] | None = None,
    *,
    active_tasks: dict[str, asyncio.Task[Any]] | None = None,
) -> None:
    """Handle incoming chat WebSocket messages.

    Expected message formats::

        {"type": "send_message", "conversation_id": "<uuid>", "content": "..."}
        {"type": "stop_generation", "conversation_id": "<uuid>"}

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
    active_tasks:
        Optional per-connection mapping of ``conversation_id -> Task``
        so that ``stop_generation`` can cancel an in-flight stream.
        When omitted the stop handler is a no-op.
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

    # --- stop_generation -------------------------------------------------
    if msg_type == "stop_generation":
        await _handle_stop_generation(data, send, active_tasks)
        return

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

    raw_content = data.get("content", "")
    if not isinstance(raw_content, str):
        await send(json.dumps({"type": "error", "message": "Vigane sõnumi sisu."}))
        return

    # Length-check BEFORE strip so a 10k+ payload of whitespace is still
    # rejected — prevents obvious DoS via the orchestrator.
    if len(raw_content) > _MAX_MESSAGE_LENGTH:
        await send(
            json.dumps(
                {
                    "type": "error",
                    "message": "Sõnum on liiga pikk (max 10 000 märki).",
                }
            )
        )
        return

    content = raw_content.strip()
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

    conv_key = str(conversation_id)

    async def _run() -> None:
        try:
            await orchestrator.handle_message(conversation_id, content, auth, send_event)
        finally:
            if active_tasks is not None:
                active_tasks.pop(conv_key, None)

    if active_tasks is not None:
        task = asyncio.create_task(_run())
        active_tasks[conv_key] = task
        try:
            await task
        except asyncio.CancelledError:
            # Swallow the cancel so the connection stays alive for
            # the next user message. The orchestrator has already
            # emitted a "stopped" event and persisted the partial.
            logger.info("Chat stream cancelled for conversation %s", conv_key)
    else:
        # No task registry (test path) — run inline.
        await orchestrator.handle_message(conversation_id, content, auth, send_event)


async def _handle_stop_generation(
    data: dict[str, Any],
    send: Any,
    active_tasks: dict[str, asyncio.Task[Any]] | None,
) -> None:
    """Cancel the orchestrator task for ``data['conversation_id']``.

    The actual ``stopped`` event is emitted by the orchestrator's
    ``CancelledError`` branch so the server owns the message_id.
    """
    raw_conv_id = data.get("conversation_id")
    if not raw_conv_id:
        await send(json.dumps({"type": "error", "message": "Puudub conversation_id."}))
        return

    try:
        conversation_id = uuid.UUID(str(raw_conv_id))
    except (ValueError, TypeError):
        await send(json.dumps({"type": "error", "message": "Vigane conversation_id."}))
        return

    if active_tasks is None:
        # No registry — nothing to cancel. Surface a minimal ack so the
        # client isn't left hanging.
        await send(
            json.dumps(
                {"type": "stopped", "message_id": None, "conversation_id": str(conversation_id)}
            )
        )
        return

    task = active_tasks.get(str(conversation_id))
    if task is None or task.done():
        await send(
            json.dumps(
                {"type": "stopped", "message_id": None, "conversation_id": str(conversation_id)}
            )
        )
        return

    task.cancel()
    try:
        await asyncio.shield(task)
    except (asyncio.CancelledError, Exception):
        # CancelledError is expected; any other exception has already
        # been logged inside the orchestrator.
        pass


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


async def _ws_close(send: Any, code: int, reason: str) -> None:
    """Attempt to close the WebSocket with *code*/*reason*.

    FastHTML's ``send`` callback for WS is a plain ASGI send. We try the
    ASGI ``websocket.close`` envelope first; if the runtime prefers a
    JSON error event (e.g. in tests) we fall back to that.
    """
    try:
        await send({"type": "websocket.close", "code": code, "reason": reason})
    except Exception:
        try:
            await send(json.dumps({"type": "error", "message": reason, "code": code}))
        except Exception:
            logger.debug("Failed to close WS after provider init error", exc_info=True)


def register_chat_ws_routes(app: Any) -> None:
    """Register the chat WebSocket route on the FastHTML *app*.

    Uses the same ``app.ws()`` pattern as
    :func:`app.explorer.websocket.register_ws_routes`.

    Note: ``/ws/chat`` is deliberately **not** in ``SKIP_PATHS`` because
    the WS handshake does not go through the HTTP Beforeware. Instead,
    authentication is handled by extracting the ``access_token`` cookie
    from the raw ASGI headers inside ``_ws_handler`` below.
    """

    # Shared provider instance for cookie verification; lazily created
    # the first time a WS message arrives (avoids import-time DB hits).
    _jwt_provider: list[JWTAuthProvider | None] = [None]

    def _get_jwt_provider() -> JWTAuthProvider | None:
        """Return the shared JWT provider, constructing it lazily.

        Returns ``None`` when construction fails (missing secret, DB
        unreachable, etc.). Callers MUST treat ``None`` as a
        fail-closed signal and reject the WS with close code ``1011``
        instead of silently proceeding with empty auth (#594.4).
        """
        if _jwt_provider[0] is None:
            try:
                _jwt_provider[0] = JWTAuthProvider()
            except Exception:
                logger.error("Failed to construct JWTAuthProvider", exc_info=True)
                return None
        return _jwt_provider[0]

    # One registry per connection is ideal, but the FastHTML WS hook
    # doesn't give us a per-connection init spot other than ``conn``.
    # The registry is keyed by (id(send), conversation_id) so that
    # cancel targets the right socket's stream.  ``id(send)`` is stable
    # for the lifetime of a single WS connection.
    _per_send_tasks: dict[int, dict[str, asyncio.Task[Any]]] = {}

    async def _ws_handler(msg: str, send: Any, scope: dict[str, Any] | None = None) -> None:
        """Extract JWT from WS handshake cookies and pass auth scope to ws_chat.

        Mirrors the HTTP middleware's silent-refresh contract (#637): if
        the ``access_token`` cookie is missing or invalid but a valid
        ``refresh_token`` is present, verify the refresh token, mint a
        new pair, and proceed with the authenticated user. The new
        cookies cannot be set on the WS upgrade response here (we only
        have the ASGI send callable), so the next HTTP request the
        client makes will go through the Beforeware which will do the
        same rotation and persist the cookies. This keeps long-lived
        chat sessions alive past the 60-minute access-token expiry.
        """
        auth_scope: dict[str, Any] = {}

        if scope is not None:
            # Try to extract the access_token from handshake headers.
            raw_headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
            access_token = _extract_cookie_from_headers(raw_headers, "access_token")
            refresh_token = _extract_cookie_from_headers(raw_headers, "refresh_token")

            if access_token or refresh_token:
                provider = _get_jwt_provider()
                if provider is None:
                    # Fail-closed: we cannot verify tokens, so reject
                    # the socket rather than letting the request
                    # through as unauthenticated.
                    await _ws_close(send, 1011, "auth provider unavailable")
                    return

                user: dict[str, Any] | None = None
                if access_token:
                    verified = provider.get_current_user(access_token)
                    if verified is not None:
                        user = dict(verified)

                if user is None and refresh_token:
                    # #637 — silent refresh for long-lived chat sessions.
                    # The WS upgrade response cannot set replacement
                    # cookies, so we use the verify-only helper here
                    # (see #637, review). Consuming the refresh token
                    # without being able to persist the rotated
                    # cookies would leave the browser with a dead
                    # refresh_token and break the next HTTP request's
                    # silent-refresh. The refresh token still rotates
                    # atomically on the next HTTP call through
                    # ``auth_before``.
                    #
                    # Import here so the chat module has no
                    # side-effect-y dependency on the HTTP middleware
                    # at import time.
                    from app.auth.middleware import verify_refresh_token_user

                    refreshed_user = verify_refresh_token_user(refresh_token, provider=provider)
                    if refreshed_user is not None:
                        user = refreshed_user

                if user is not None:
                    auth_scope["auth"] = user

        key = id(send)
        tasks = _per_send_tasks.setdefault(key, {})
        try:
            await ws_chat(
                msg,
                send,
                auth_scope if auth_scope else None,
                active_tasks=tasks,
            )
        except Exception:
            # The socket is going away (disconnect, cancellation, handler
            # error). Cancel every in-flight orchestrator task for this
            # connection so they don't keep streaming into a dead send
            # callable and so the registry never leaks an entry.
            _cancel_all_and_drop(key)
            raise
        finally:
            # Drop the registry slot once no tasks are running. If a task
            # is still in-flight (normal mid-stream case) we leave it —
            # its own ``_run`` finally-block pops the conversation key
            # and the next handler call will GC the outer slot.
            if not tasks:
                _per_send_tasks.pop(key, None)

    def _cancel_all_and_drop(key: int) -> None:
        """Cancel every task registered under *key* and drop the slot."""
        tasks = _per_send_tasks.pop(key, None)
        if not tasks:
            return
        for task in list(tasks.values()):
            if not task.done():
                task.cancel()

    async def _on_disconnect(send: Any) -> None:
        """Clear the per-send task registry when the socket closes."""
        try:
            _cancel_all_and_drop(id(send))
        finally:
            await on_disconnect(send)

    # Expose registry + disconnect hook on the handler so tests (and
    # instrumentation) can inspect per-connection task state without
    # poking at closure internals.
    _ws_handler._per_send_tasks = _per_send_tasks  # type: ignore[attr-defined]
    _ws_handler._on_disconnect = _on_disconnect  # type: ignore[attr-defined]

    app.ws("/ws/chat", conn=on_connect, disconn=_on_disconnect)(_ws_handler)
