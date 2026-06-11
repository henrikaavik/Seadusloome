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
- **Regenerate**: a ``{"type": "regenerate", "conversation_id": "<uuid>",
  "pivot_message_id": "<uuid>"?}`` message re-runs generation from the
  existing conversation history *up to and including* the pivot user
  message — **no new user message is inserted** (issues #737 / #738). The
  HTTP regenerate/edit endpoints (``app.chat.handlers``) own the prior
  rewind (delete the stale assistant reply + downstream) so it cannot
  race against an in-flight stream; this WS action only drives the
  replay. It reuses the same active-task registry as ``send_message`` so
  ``stop_generation`` can cancel a regenerate too.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from app.auth.jwt_provider import JWTAuthProvider
from app.auth.ws_auth import WSCookieAuth, close_ws, conn_key, start_heartbeat
from app.chat.orchestrator import ChatOrchestrator
from app.llm.claude import get_default_provider

logger = logging.getLogger(__name__)

_MAX_MESSAGE_LENGTH = 10_000

# Heartbeat plumbing is shared across every WS channel since #856 —
# see app/auth/ws_auth.py. The chat heartbeat is scoped to a single
# ``_ws_handler`` invocation (per-message-handling) rather than to the
# whole WS connection: NAT/proxy idle-cuts are only a concern *during*
# a long server-side operation; an idle connection sends nothing in
# either direction and is functionally unaffected by an absent
# heartbeat (#658 / #684).


# IMPORTANT: do NOT annotate ``send`` on FastHTML connect/disconnect
# hooks. FastHTML's ``_find_p`` only resolves the special WS names
# (``send``, ``scope``, ``ws``) inside its ``if anno is empty:`` branch
# (``fasthtml/core.py:_find_p``). Annotating ``send`` — even with
# ``Any`` — causes the resolver to fall through to the generic
# data/path/cookies/headers/query lookup and raise
# ``ValueError: Missing required field: send`` before the body runs.
# This was the root cause of #802 (chat hang). See
# ``docs/2026-05-18-bugfix-plan.md`` Wave 3.
async def on_connect(send) -> None:
    """Called when a WebSocket client connects to /ws/chat.

    The heartbeat is now spawned inside ``_ws_handler`` per message
    handling (see ``register_chat_ws_routes`` below) so this hook just
    emits the initial ``connected`` event.
    """
    logger.info("Chat WS client connected")
    await send(json.dumps({"type": "connected"}))


async def on_disconnect(send) -> None:
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

        {"type": "send_message",    "conversation_id": "<uuid>", "content": "..."}
        {"type": "regenerate",      "conversation_id": "<uuid>", "pivot_message_id": "<uuid>"?}
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
        so that ``stop_generation`` can cancel an in-flight stream
        (both ``send_message`` and ``regenerate`` register here). When
        omitted the stop handler is a no-op.
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

    # --- regenerate ------------------------------------------------------
    if msg_type == "regenerate":
        await _handle_regenerate(data, send, scope, active_tasks)
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
    auth = _auth_from_scope(scope)
    if not auth:
        await send(json.dumps({"type": "error", "message": "Autentimine nõutav."}))
        return

    await _drive_orchestrator(
        conversation_id,
        auth,
        send,
        active_tasks,
        user_message=content,
    )


def _auth_from_scope(scope: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the auth dict from the ASGI *scope*, or ``None`` if absent."""
    auth: dict[str, Any] = {}
    if scope is not None:
        auth = scope.get("auth") or {}
    if not auth or not auth.get("id"):
        return None
    return auth


async def _drive_orchestrator(
    conversation_id: uuid.UUID,
    auth: dict[str, Any],
    send: Any,
    active_tasks: dict[str, asyncio.Task[Any]] | None,
    *,
    user_message: str | None = None,
    regenerate: bool = False,
    regenerate_pivot_message_id: uuid.UUID | None = None,
) -> None:
    """Run one orchestrator turn (new message *or* regenerate) for a socket.

    Shared by the ``send_message`` and ``regenerate`` paths.

    * ``send_message`` → pass *user_message* (non-empty) and
      ``regenerate=False``; the orchestrator is invoked with the classic
      4-arg signature so existing callers/tests are unaffected.
    * ``regenerate`` → pass ``regenerate=True`` and optionally
      *regenerate_pivot_message_id*; the orchestrator re-runs generation
      from the existing history up to and including that user message and
      persists no new user row (issues #737 / #738).

    Both paths register the task in *active_tasks* so ``stop_generation``
    can cancel an in-flight turn.
    """
    orchestrator = ChatOrchestrator(get_default_provider())

    async def send_event(event: dict[str, Any]) -> None:
        """Serialize event as JSON and push to WS client."""
        await send(json.dumps(event, default=str))

    conv_key = str(conversation_id)

    async def _invoke() -> None:
        if regenerate:
            # Empty ``user_message`` is the orchestrator's regenerate-mode
            # sentinel; the prompt is the persisted pivot turn.
            await orchestrator.handle_message(
                conversation_id,
                "",
                auth,
                send_event,
                regenerate_pivot_message_id=regenerate_pivot_message_id,
            )
        else:
            await orchestrator.handle_message(
                conversation_id,
                user_message or "",
                auth,
                send_event,
            )

    if active_tasks is not None:

        async def _run() -> None:
            try:
                await _invoke()
            finally:
                active_tasks.pop(conv_key, None)

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
        await _invoke()


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


async def _handle_regenerate(
    data: dict[str, Any],
    send: Any,
    scope: dict[str, Any] | None,
    active_tasks: dict[str, asyncio.Task[Any]] | None,
) -> None:
    """Re-run generation from an existing user turn (issues #737 / #738).

    Carries ``conversation_id`` and an optional ``pivot_message_id`` (the
    user message to regenerate from; the HTTP regenerate/edit endpoints
    resolve the clicked assistant bubble to that boundary and have
    already deleted the stale reply + downstream). When ``pivot_message_id``
    is omitted the orchestrator regenerates from the conversation's last
    user message. **No new user message is inserted** — the existing
    history *is* the prompt.
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

    raw_pivot = data.get("pivot_message_id")
    pivot_message_id: uuid.UUID | None = None
    if raw_pivot:
        try:
            pivot_message_id = uuid.UUID(str(raw_pivot))
        except (ValueError, TypeError):
            await send(json.dumps({"type": "error", "message": "Vigane pivot_message_id."}))
            return

    auth = _auth_from_scope(scope)
    if not auth:
        await send(json.dumps({"type": "error", "message": "Autentimine nõutav."}))
        return

    await _drive_orchestrator(
        conversation_id,
        auth,
        send,
        active_tasks,
        regenerate=True,
        regenerate_pivot_message_id=pivot_message_id,
    )


def register_chat_ws_routes(app: Any) -> None:
    """Register the chat WebSocket route on the FastHTML *app*.

    Uses the same ``app.ws()`` pattern as
    :func:`app.explorer.websocket.register_ws_routes`.

    Note: ``/ws/chat`` is deliberately **not** in ``SKIP_PATHS`` because
    the WS handshake does not go through the HTTP Beforeware. Instead,
    authentication is handled by extracting the ``access_token`` cookie
    from the raw ASGI headers inside ``_ws_handler`` below.
    """

    # Shared cookie-JWT authenticator (#856). The provider is lazily
    # created on first use (avoids import-time DB hits). The factory
    # lambda resolves ``JWTAuthProvider`` from THIS module's globals at
    # call time so the established test patch path
    # (``patch("app.chat.websocket.JWTAuthProvider")``) keeps working.
    authenticator = WSCookieAuth("chat", provider_factory=lambda: JWTAuthProvider())

    # Per-connection task registry so ``stop_generation`` can cancel the
    # right socket's stream. Keyed by :func:`app.auth.ws_auth.conn_key`
    # — i.e. ``id(ws)``, the raw Starlette conn, which is the SAME
    # object across every hook dispatch of one connection. ``id(send)``
    # is NOT stable (FastHTML rebuilds ``partial(_send_ws, conn)`` per
    # dispatch), which is exactly the F4 defect this replaces.
    _per_conn_tasks: dict[int, dict[str, asyncio.Task[Any]]] = {}

    # IMPORTANT — FastHTML param resolution (root cause of #802):
    #   * ``send`` / ``scope`` MUST be unannotated. ``_find_p`` only
    #     resolves these WS special names inside its ``if anno is empty:``
    #     branch (``fasthtml/core.py:_find_p``). Annotating them (even
    #     with ``Any``) makes FastHTML fall through to the generic
    #     path/cookies/headers/query/data resolver and raise
    #     ``ValueError: Missing required field: <name>``.
    #   * ``msg`` MUST be the real ``dict`` type at runtime, not a string.
    #     ``_find_p``'s ``if anno is dict: return data`` branch checks
    #     ``isinstance(anno, type)`` AND identity (``anno is dict``). With
    #     ``from __future__ import annotations`` (PEP 563) at the top of
    #     this module, every source-level annotation becomes a string at
    #     runtime — so ``async def _ws_handler(msg: dict, …)`` would give
    #     us ``__annotations__['msg'] == 'dict'`` (a str), and FastHTML's
    #     identity check fails, falling back to the empty-anno branch
    #     which returns ``None`` for non-special names. ``ws_chat`` would
    #     then crash on ``json.loads(None)``.
    #     The fix: declare the annotation in source for readability +
    #     static analysis, THEN override ``__annotations__['msg']`` with
    #     the real ``dict`` type immediately after definition. We
    #     re-serialise the dict to a string at the boundary below so
    #     ``ws_chat``'s existing ``msg: str`` contract — and its unit
    #     tests — stay unchanged.
    # See ``docs/2026-05-18-bugfix-plan.md`` Wave 3.
    async def _ws_handler(msg: dict, send, scope=None, ws=None) -> None:
        """Extract JWT from WS handshake cookies and pass auth scope to ws_chat.

        Mirrors the HTTP middleware's silent-refresh contract (#637) via
        the shared :class:`~app.auth.ws_auth.WSCookieAuth`: if the
        ``access_token`` cookie is missing or invalid but a valid
        ``refresh_token`` is present, verify the refresh token
        (verify-only — the WS upgrade response cannot persist rotated
        cookies) and proceed with the authenticated user. The next HTTP
        request rotates the pair through the Beforeware. This keeps
        long-lived chat sessions alive past the 60-minute access-token
        expiry.

        ``ws`` binds the raw Starlette conn — the ONLY object whose
        identity is stable across hook dispatches, and the only handle
        that can actually close the socket (F1/F4, #856).
        """
        auth_scope: dict[str, Any] = {}

        result = authenticator.resolve_user(scope)
        if result.provider_unavailable:
            # Fail-closed: we cannot verify tokens, so terminate the
            # socket rather than letting the request through as
            # unauthenticated (#594.4). The close MUST go through
            # ``ws.close()`` — pushing an ASGI close dict through
            # FastHTML's wrapped ``send`` yields a text frame and
            # leaves the connection open (F1).
            await close_ws(ws, 1011, "auth provider unavailable", channel="chat")
            return
        if result.user is not None:
            auth_scope["auth"] = result.user

        key = conn_key(send, ws)
        tasks = _per_conn_tasks.setdefault(key, {})
        # Heartbeat scoped to one ``_ws_handler`` invocation (#684):
        # NAT idle timeouts only matter while a long server-side
        # operation is in flight.
        heartbeat = start_heartbeat(send, channel="chat")
        try:
            # ``msg`` arrives as a dict from FastHTML's resolver (see the
            # ``__annotations__['msg'] = dict`` override below). Direct-
            # invocation unit tests pass a JSON string for back-compat.
            # Accept both shapes; ``ws_chat`` itself expects a string.
            msg_str = json.dumps(msg) if isinstance(msg, dict) else msg
            await ws_chat(
                msg_str,
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
            # Always cancel the heartbeat when message handling ends —
            # success, exception, or graceful return — so we never leak
            # background tasks beyond the request lifetime.
            heartbeat.cancel()
            try:
                await heartbeat
            except (asyncio.CancelledError, Exception):
                pass
            # Drop the registry slot once no tasks are running. If a task
            # is still in-flight (normal mid-stream case) we leave it —
            # its own ``_run`` finally-block pops the conversation key
            # and the next handler call will GC the outer slot.
            if not tasks:
                _per_conn_tasks.pop(key, None)

    def _cancel_all_and_drop(key: int) -> None:
        """Cancel every task registered under *key* and drop the slot."""
        tasks = _per_conn_tasks.pop(key, None)
        if not tasks:
            return
        for task in list(tasks.values()):
            if not task.done():
                task.cancel()

    # IMPORTANT: do NOT annotate ``send`` / ``ws``. See _ws_handler
    # above and ``docs/2026-05-18-bugfix-plan.md`` Wave 3 for the
    # FastHTML ``_find_p`` trap that motivates the missing annotations.
    async def _on_disconnect(send, ws=None) -> None:
        """Clear the per-connection task registry when the socket closes.

        Keyed on the stable conn identity (``id(ws)``) — the ``send``
        received here is a DIFFERENT partial than the one any message
        handler saw, so an ``id(send)`` keyed registry could never be
        cleaned up from this hook (F4, #856).
        """
        try:
            _cancel_all_and_drop(conn_key(send, ws))
        finally:
            await on_disconnect(send)

    # Expose registry + disconnect hook on the handler so tests (and
    # instrumentation) can inspect per-connection task state without
    # poking at closure internals.
    _ws_handler._per_conn_tasks = _per_conn_tasks  # type: ignore[attr-defined]
    _ws_handler._on_disconnect = _on_disconnect  # type: ignore[attr-defined]

    # Override the stringified ``msg`` annotation with the real ``dict``
    # type at runtime. ``from __future__ import annotations`` at the top
    # of this module makes ``msg: dict`` resolve to the *string* ``'dict'``
    # in ``__annotations__``, which fails FastHTML's identity check
    # ``if anno is dict: return data``. Setting it explicitly here makes
    # the resolver see the real type. See #802 phase-2.
    _ws_handler.__annotations__["msg"] = dict

    app.ws("/ws/chat", conn=on_connect, disconn=_on_disconnect)(_ws_handler)
