"""WebSocket handler for real-time notification delivery (#180).

Same FastHTML ``app.ws()`` pattern as ``app/chat/websocket.py``,
``app/explorer/websocket.py``, and ``app/docs/websocket.py``. The
handshake is authenticated via the ``access_token`` JWT cookie (with
silent-refresh fallback to ``refresh_token`` — see chat module for the
full pattern). The path is ``/ws/notifications``.

Push model
----------

This is primarily a **push-only** channel: the server emits a
``{"type": "notification", ...}`` event whenever
:func:`app.notifications.notify.notify` inserts a new row for the
connected user. Multiple browser tabs translate into multiple sockets
per user (the same physical user receives one push per tab), so the
connection registry is a ``dict[user_id, dict[conn_key, send]]`` keyed
on the stable conn identity (``id(ws)`` — see
:func:`app.auth.ws_auth.conn_key` and review finding F3 in #856).

Client messages are silently ignored other than the ``ping`` →
``pong`` keepalive hook (kept for symmetry with the other modules);
notifications themselves are minted by domain code, not by the WS
client.

Reconnect / heartbeat
---------------------

The server emits a periodic ``{"type": "ping"}`` so NAT / proxy idle
timeouts cannot silently kill the socket. On the client side, the
bell UI in :mod:`app.ui.layout.top_bar` reconnects with a 5-second
backoff on close. The existing 30 s polling endpoint
(``/api/notifications/unread-count``) is retained as a fallback so a
WS-disconnect-storm does not leave the bell stale.

FastHTML param-resolver discipline (root cause of #802)
-------------------------------------------------------

* ``send`` / ``scope`` MUST NOT be annotated on FastHTML WS
  connect/disconnect/receive hooks. ``_find_p`` only resolves these
  special WS names in its ``if anno is empty:`` branch.
* ``msg`` is declared ``dict`` in source for static analysis, and the
  ``__annotations__['msg'] = dict`` override below makes FastHTML's
  identity-based ``if anno is dict`` check pass at runtime (PEP-563
  would otherwise stringify the annotation and break the resolver).

See ``docs/2026-05-18-bugfix-plan.md`` Wave 3 for the full story.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

from app.auth.jwt_provider import JWTAuthProvider
from app.auth.ws_auth import (
    HeartbeatRegistry,
    WSCookieAuth,
    close_ws,
    conn_key,
    start_heartbeat,
)

logger = logging.getLogger(__name__)


# Heartbeat / cookie-auth / close plumbing is shared across every WS
# channel since #856 — see app/auth/ws_auth.py.


# ---------------------------------------------------------------------------
# Connection registry — module-level so notify() can push from anywhere
# ---------------------------------------------------------------------------

# ``user_id (str)`` → ``{conn_key: send}``. Multiple tabs for the same
# user produce multiple entries. Keyed on the stable conn identity
# (:func:`app.auth.ws_auth.conn_key` — ``id(ws)``) rather than on the
# ``send`` object: FastHTML rebuilds ``partial(_send_ws, conn)`` on
# every hook dispatch, so the ``send`` the disconnect hook receives is
# NEVER the one the connect hook registered and identity-based cleanup
# could not remove anything (review finding F3, #856). The registry is
# protected by ``_registry_lock`` because :func:`push_to_user` may be
# invoked from a background worker thread (notify is called
# fire-and-forget from job handlers and synchronous domain code).
_connections: dict[str, dict[int, Any]] = {}
_registry_lock = threading.Lock()

# The event loop running the FastHTML ASGI app. Captured by
# :func:`register_event_loop` from the lifespan hook so background-thread
# callers of :func:`push_to_user` can schedule a coroutine on the right
# loop via ``asyncio.run_coroutine_threadsafe``.
_event_loop: asyncio.AbstractEventLoop | None = None


def register_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Remember the FastHTML ASGI event loop.

    Called once from the lifespan hook so :func:`push_to_user` can
    schedule pushes from a non-async background thread (notify() is
    fire-and-forget and may run inside the job worker).
    """
    global _event_loop
    _event_loop = loop


def _add_connection(user_id: str, send: Any, key: int | None = None) -> None:
    """Register *send* as an active socket for *user_id* under *key*.

    *key* is the stable conn identity (``id(ws)``); it defaults to
    ``id(send)`` for direct-invocation tests that have no conn object.
    """
    if key is None:
        key = id(send)
    with _registry_lock:
        _connections.setdefault(user_id, {})[key] = send


def _remove_connection(user_id: str, key: int) -> None:
    """Drop conn *key* from *user_id*'s socket pool; drop the entry when empty."""
    with _registry_lock:
        conns = _connections.get(user_id)
        if conns is None:
            return
        conns.pop(key, None)
        if not conns:
            _connections.pop(user_id, None)


def _remove_connection_any_user(key: int) -> None:
    """Drop conn *key* from whichever user pool holds it.

    The disconnect hook and the heartbeat-failure path know the conn
    identity but not the user id; with at most a few hundred concurrent
    users the scan is negligible.
    """
    with _registry_lock:
        empty_users: list[str] = []
        for user_id, conns in _connections.items():
            if key in conns:
                conns.pop(key, None)
                if not conns:
                    empty_users.append(user_id)
        for user_id in empty_users:
            _connections.pop(user_id, None)


def _snapshot_connections(user_id: str) -> list[tuple[int, Any]]:
    """Return a ``(key, send)`` snapshot of *user_id*'s current sockets.

    Snapshotting under the lock lets the caller iterate without
    blocking other connect/disconnect mutations.
    """
    with _registry_lock:
        conns = _connections.get(user_id)
        if not conns:
            return []
        return list(conns.items())


async def _broadcast_async(user_id: str, payload: dict[str, Any]) -> None:
    """Send *payload* to every live socket for *user_id*.

    Dead sockets are removed from the registry on send failure so the
    next broadcast does not re-attempt them.
    """
    conns = _snapshot_connections(user_id)
    if not conns:
        return

    serialised = json.dumps(payload, default=str)
    for key, send in conns:
        try:
            await send(serialised)
        except Exception:
            logger.debug(
                "notifications WS send failed for user=%s; dropping socket",
                user_id,
                exc_info=True,
            )
            _remove_connection(user_id, key)


def push_to_user(user_id: Any, payload: dict[str, Any]) -> None:
    """Fire-and-forget push of *payload* to every socket owned by *user_id*.

    Safe to call from sync code (e.g. ``notify()`` running in a
    background worker thread) — if the FastHTML event loop has been
    registered via :func:`register_event_loop` we schedule the
    broadcast coroutine on it via
    ``asyncio.run_coroutine_threadsafe``. If we are already inside an
    async task on that loop we use ``create_task`` instead.

    Never raises: errors are logged at DEBUG so notification creation
    stays unobtrusive. The DB row has already been written by the
    time the caller reaches this function — the push is a courtesy
    on top of the durable record, not a substitute for it.
    """
    user_id_str = str(user_id)

    # Fast path: nothing to do if nobody is connected.
    if not _snapshot_connections(user_id_str):
        return

    try:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if running is not None:
            # We are on an event loop already — schedule directly.
            running.create_task(_broadcast_async(user_id_str, payload))
            return

        # We are on a thread with no running loop. Hop onto the
        # registered FastHTML loop if we have one.
        if _event_loop is not None and not _event_loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                _broadcast_async(user_id_str, payload),
                _event_loop,
            )
            return

        # Nothing to schedule against — log and drop. The DB row is
        # still there, so the next poll / page load will pick it up.
        logger.debug(
            "notifications WS push: no event loop registered; "
            "user=%s will rely on polling fallback",
            user_id_str,
        )
    except Exception:
        logger.debug(
            "notifications WS push failed for user=%s",
            user_id_str,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Per-connection heartbeat registry
# ---------------------------------------------------------------------------
# Keyed by ``conn_key`` (``id(ws)`` — the Starlette WebSocket conn)
# rather than ``id(send)`` because FastHTML rebuilds the ``send``
# partial on every WS hook invocation (``partial(_send_ws, conn)``
# inside ``_find_p``), so ``id(send)`` differs between ``_on_connect``
# and ``_on_disconnect`` even though the underlying physical connection
# is the same. The ``ws`` (conn) IS the same object across all hooks
# for one socket (see Starlette's ``WebSocketEndpoint.dispatch``).

_heartbeats = HeartbeatRegistry()


# ---------------------------------------------------------------------------
# Inner message handler — exposed for unit tests
# ---------------------------------------------------------------------------


async def ws_notifications(
    msg: str,
    send: Any,
    scope: dict[str, Any] | None = None,
) -> None:
    """Handle one client message on ``/ws/notifications``.

    This is largely a push-only channel: the server emits events when
    :func:`push_to_user` runs. The only meaningful client message is
    an optional ``{"type": "ping"}`` keepalive that the handler replies
    to with ``{"type": "pong"}``. Any other (or invalid) message is
    silently ignored — keeping the channel forwards-compatible if we
    later add ``ack`` or ``mark_read`` events.

    Auth is extracted by the wrapper in
    :func:`register_notifications_ws_routes`; this handler trusts
    ``scope['auth']`` to be set on success.
    """
    try:
        data = json.loads(msg)
    except (json.JSONDecodeError, TypeError):
        # Silently ignore malformed input — push-only channel; client
        # never *needs* to send anything.
        return

    if not isinstance(data, dict):
        return

    if data.get("type") == "ping":
        try:
            await send(json.dumps({"type": "pong"}))
        except Exception:
            logger.debug("notifications WS pong send failed", exc_info=True)
        return

    # Unknown message types are silently ignored.


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_notifications_ws_routes(app: Any) -> None:
    """Mount the notifications WS at ``/ws/notifications``.

    Mirrors :func:`app.chat.websocket.register_chat_ws_routes`: a
    wrapper does cookie-based JWT auth on each invocation, then
    delegates the message body to :func:`ws_notifications`. The
    per-connection registry entry is added in the ``conn`` hook and
    removed in the ``disconn`` hook so that pushes from
    :func:`push_to_user` reach every live tab the user has open.

    Note: ``/ws/notifications`` is deliberately **not** added to
    ``SKIP_PATHS`` — the WS handshake does not go through the HTTP
    Beforeware. Auth is handled inline by extracting the
    ``access_token`` cookie from the raw ASGI headers below.
    """
    # Shared cookie-JWT authenticator (#856). The factory lambda
    # resolves ``JWTAuthProvider`` from THIS module's globals at call
    # time so the established test patch path
    # (``patch("app.notifications.websocket.JWTAuthProvider")``) keeps
    # working.
    authenticator = WSCookieAuth("notifications", provider_factory=lambda: JWTAuthProvider())

    # IMPORTANT: do NOT annotate ``send``/``scope``/``ws``. See #802
    # / chat module for the FastHTML ``_find_p`` trap — only the
    # empty-annotation branch resolves these special WS parameter
    # names. ``ws`` binds to the raw Starlette WebSocket conn (see
    # ``fasthtml/core.py``: ``if arg.lower()=='ws' … return conn``).
    async def _on_connect(send, scope=None, ws=None) -> None:
        """Authenticate the handshake, register the socket, send the
        greet event, and start the heartbeat.

        Auth + heartbeat live HERE rather than in ``_ws_handler``
        because this is a **push-only** channel: the top-bar bell
        only opens the socket and listens for server-side
        ``notification`` events, so ``_ws_handler`` (called on
        inbound messages) never runs in production. If we deferred
        auth or heartbeat to that path, neither would ever execute.

        An unauthenticated handshake is closed with code 1008; a
        provider outage fails closed with 1011. The close MUST go
        through ``ws.close()`` (not ``send`` — see
        :func:`app.auth.ws_auth.close_ws` for why) so the underlying
        ASGI socket is actually terminated rather than receiving a
        stray text frame.
        """
        result = authenticator.resolve_user(scope)
        if result.provider_unavailable:
            await close_ws(ws, 1011, "auth provider unavailable", channel="notifications")
            return
        user = result.user
        if user is None or not user.get("id"):
            await close_ws(ws, 1008, "authentication required", channel="notifications")
            return

        user_id = str(user["id"])
        key = conn_key(send, ws)
        _add_connection(user_id, send, key)
        try:
            await send(json.dumps({"type": "connected"}))
        except Exception:
            # Failed to emit the greet event — drop the freshly-added
            # socket so we don't leak it in the registry.
            _remove_connection(user_id, key)
            logger.debug("notifications WS greet send failed", exc_info=True)
            return

        # Heartbeat scoped to the WS lifetime. Push-only channel, so
        # ``_ws_handler`` (the only place a per-message heartbeat
        # could live) never runs — without spawning here the 25 s
        # NAT-keepalive contract documented in the module header
        # would silently never fire. On send failure the heartbeat
        # self-terminates AND deregisters this connection so a dead
        # socket can't linger in the pool until the next broadcast
        # trips over it (F3 contract, #856).
        def _heartbeat_failed() -> None:
            _remove_connection(user_id, key)
            _heartbeats.pop(key)

        task = start_heartbeat(send, channel="notifications", on_fail=_heartbeat_failed)
        _heartbeats.register(key, task)

    async def _on_disconnect(send, ws=None) -> None:
        """Cancel the heartbeat and remove the socket from every
        user pool it might belong to.

        Keyed on the stable conn identity (``id(ws)``): the ``send``
        this hook receives is a freshly-built partial, never the
        object ``_on_connect`` registered, so identity-matching on
        ``send`` could not clean anything up (F3, #856). We don't
        have direct access to the user_id here, so we scan the
        registry for the conn key. With at most a few hundred
        concurrent users this is negligible.
        """
        key = conn_key(send, ws)
        task = _heartbeats.pop(key)
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        _remove_connection_any_user(key)

    # IMPORTANT — FastHTML param resolution:
    #   * ``send`` / ``scope`` MUST be unannotated (see chat module).
    #   * ``msg`` is declared ``dict`` for readability + static
    #     analysis; the ``__annotations__['msg'] = dict`` override
    #     below makes FastHTML's identity-based resolver inject the
    #     parsed payload (PEP-563 otherwise stringifies the
    #     annotation and breaks the check).
    async def _ws_handler(msg: dict, send, scope=None) -> None:
        # NOTE: heartbeat lives in ``_on_connect`` for the lifetime
        # of the WS, NOT here. This handler only runs when the
        # client sends a message (e.g. a ``ping`` keepalive), which
        # is the rare case for this push-only channel.
        msg_str = json.dumps(msg) if isinstance(msg, dict) else msg
        await ws_notifications(msg_str, send, scope)

    # See chat module: PEP-563 stringifies ``msg: dict`` at runtime;
    # FastHTML's ``_find_p`` does ``if anno is dict`` (identity), so
    # we have to set the *real* type at runtime.
    _ws_handler.__annotations__["msg"] = dict

    app.ws("/ws/notifications", conn=_on_connect, disconn=_on_disconnect)(_ws_handler)
