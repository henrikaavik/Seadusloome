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
connection registry is a ``dict[user_id, set[send]]``.

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
from http.cookies import SimpleCookie
from typing import Any

from app.auth.jwt_provider import JWTAuthProvider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Heartbeat (same shape as chat/draft-status WS handlers — see #684 review)
# ---------------------------------------------------------------------------

_WS_HEARTBEAT_INTERVAL_SECONDS = 25.0
_WS_HEARTBEAT_SEND_TIMEOUT_SECONDS = 5.0


def _start_heartbeat(send: Any) -> asyncio.Task[None]:
    """Spawn a periodic ``{"type": "ping"}`` task for the lifetime of the
    WS handler invocation. Self-terminates on the first send failure."""

    async def _beat() -> None:
        while True:
            try:
                await asyncio.sleep(_WS_HEARTBEAT_INTERVAL_SECONDS)
                await asyncio.wait_for(
                    send(json.dumps({"type": "ping"})),
                    timeout=_WS_HEARTBEAT_SEND_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug(
                    "notifications WS heartbeat send failed; terminating",
                    exc_info=True,
                )
                return

    return asyncio.create_task(_beat())


# ---------------------------------------------------------------------------
# Connection registry — module-level so notify() can push from anywhere
# ---------------------------------------------------------------------------

# ``user_id (str)`` → set of bound ``send`` callables. Multiple tabs for
# the same user produce multiple entries. The registry is protected by
# ``_registry_lock`` because :func:`push_to_user` may be invoked from a
# background worker thread (notify is called fire-and-forget from job
# handlers and synchronous domain code), so we need thread-safe access
# to the set.
_connections: dict[str, set[Any]] = {}
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


def _add_connection(user_id: str, send: Any) -> None:
    """Register *send* as an active socket for *user_id*."""
    with _registry_lock:
        _connections.setdefault(user_id, set()).add(send)


def _remove_connection(user_id: str, send: Any) -> None:
    """Drop *send* from *user_id*'s socket set; drop the entry when empty."""
    with _registry_lock:
        sends = _connections.get(user_id)
        if sends is None:
            return
        sends.discard(send)
        if not sends:
            _connections.pop(user_id, None)


def _snapshot_connections(user_id: str) -> list[Any]:
    """Return a snapshot of *user_id*'s current sockets.

    Snapshotting under the lock lets the caller iterate without
    blocking other connect/disconnect mutations.
    """
    with _registry_lock:
        sends = _connections.get(user_id)
        if not sends:
            return []
        return list(sends)


async def _broadcast_async(user_id: str, payload: dict[str, Any]) -> None:
    """Send *payload* to every live socket for *user_id*.

    Dead sockets are removed from the registry on send failure so the
    next broadcast does not re-attempt them.
    """
    sends = _snapshot_connections(user_id)
    if not sends:
        return

    serialised = json.dumps(payload, default=str)
    for send in sends:
        try:
            await send(serialised)
        except Exception:
            logger.debug(
                "notifications WS send failed for user=%s; dropping socket",
                user_id,
                exc_info=True,
            )
            _remove_connection(user_id, send)


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
# Cookie helpers (same shape as chat/draft-status WS)
# ---------------------------------------------------------------------------


def _extract_cookie_from_headers(headers: list[tuple[bytes, bytes]], name: str) -> str | None:
    """Parse the ``Cookie`` header from raw ASGI *headers* and return the value for *name*."""
    for hdr_name, hdr_value in headers:
        if hdr_name.lower() == b"cookie":
            cookie: SimpleCookie = SimpleCookie()
            cookie.load(hdr_value.decode("latin-1"))
            morsel = cookie.get(name)
            if morsel is not None:
                return morsel.value
    return None


async def _ws_close(ws: Any, code: int, reason: str) -> None:
    """Best-effort WS close that actually closes the underlying ASGI socket.

    FastHTML's ``send`` parameter exposed to WS hooks is
    ``partial(_send_ws, conn)`` — i.e. the dict gets fed through
    ``to_xml`` and ultimately ``ws.send_text``. Trying to close by
    sending ``{"type": "websocket.close", ...}`` through ``send``
    therefore produces a regular text frame and leaves the connection
    open. We have to call ``ws.close()`` on the raw Starlette
    WebSocket conn (exposed by FastHTML via the unannotated ``ws``
    parameter on WS hooks).
    """
    if ws is None:
        # Defensive: the caller forgot to pass the conn. Nothing we can
        # do here other than log so a future regression is visible.
        logger.debug("notifications WS close requested without ws conn")
        return
    try:
        await ws.close(code=code, reason=reason)
    except Exception:
        logger.debug("notifications WS close failed", exc_info=True)


# ---------------------------------------------------------------------------
# Per-connection heartbeat registry
# ---------------------------------------------------------------------------
# We key by ``id(ws)`` (the Starlette WebSocket conn) rather than
# ``id(send)`` because FastHTML rebuilds the ``send`` partial on every
# WS hook invocation (``partial(_send_ws, conn)`` inside ``_find_p``),
# so ``id(send)`` differs between ``_on_connect`` and ``_on_disconnect``
# even though the underlying physical connection is the same. The
# ``ws`` (conn) IS the same object across all hooks for one socket
# (see Starlette's ``WebSocketEndpoint.dispatch`` which forwards the
# same ``WebSocket`` to ``on_connect``/``on_disconnect``/``on_receive``).

_heartbeats: dict[int, asyncio.Task[None]] = {}
_heartbeats_lock = threading.Lock()


def _register_heartbeat(ws_id: int, task: asyncio.Task[None]) -> None:
    with _heartbeats_lock:
        # Cancel any pre-existing task under this key (defensive — a
        # mis-ordered conn/disconn could leave a stale task).
        old = _heartbeats.pop(ws_id, None)
    if old is not None and not old.done():
        old.cancel()
    with _heartbeats_lock:
        _heartbeats[ws_id] = task


def _pop_heartbeat(ws_id: int) -> asyncio.Task[None] | None:
    with _heartbeats_lock:
        return _heartbeats.pop(ws_id, None)


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
    _jwt_provider: list[JWTAuthProvider | None] = [None]

    def _get_jwt_provider() -> JWTAuthProvider | None:
        """Lazily construct the shared JWT provider.

        Returns ``None`` on construction failure so callers can
        fail-closed (#594.4).
        """
        if _jwt_provider[0] is None:
            try:
                _jwt_provider[0] = JWTAuthProvider()
            except Exception:
                logger.error(
                    "notifications WS: failed to construct JWTAuthProvider",
                    exc_info=True,
                )
                return None
        return _jwt_provider[0]

    def _auth_from_scope(scope: dict[str, Any] | None) -> dict[str, Any] | None:
        """Verify the JWT cookie carried on the WS handshake.

        Returns the user dict on success, ``None`` when the request
        is unauthenticated. Mirrors the silent-refresh contract used
        by ``/ws/chat`` (see #637).
        """
        if scope is None:
            return None
        raw_headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
        access_token = _extract_cookie_from_headers(raw_headers, "access_token")
        refresh_token = _extract_cookie_from_headers(raw_headers, "refresh_token")

        if not (access_token or refresh_token):
            return None

        provider = _get_jwt_provider()
        if provider is None:
            return None

        user: dict[str, Any] | None = None
        if access_token:
            verified = provider.get_current_user(access_token)
            if verified is not None:
                user = dict(verified)

        if user is None and refresh_token:
            # Verify-only refresh — same shape as chat/draft-status
            # (#637). The WS upgrade response cannot persist rotated
            # cookies; the next HTTP request will rotate atomically.
            from app.auth.middleware import verify_refresh_token_user

            refreshed_user = verify_refresh_token_user(refresh_token, provider=provider)
            if refreshed_user is not None:
                user = refreshed_user

        return user

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

        An unauthenticated handshake is closed with code 1008. The
        close MUST go through ``ws.close()`` (not ``send`` — see
        :func:`_ws_close` for why) so the underlying ASGI socket is
        actually terminated rather than receiving a stray text frame.
        """
        user = _auth_from_scope(scope)
        if user is None or not user.get("id"):
            await _ws_close(ws, 1008, "authentication required")
            return

        user_id = str(user["id"])
        _add_connection(user_id, send)
        try:
            await send(json.dumps({"type": "connected"}))
        except Exception:
            # Failed to emit the greet event — drop the freshly-added
            # socket so we don't leak it in the registry.
            _remove_connection(user_id, send)
            logger.debug("notifications WS greet send failed", exc_info=True)
            return

        # Heartbeat scoped to the WS lifetime. Push-only channel, so
        # ``_ws_handler`` (the only place a per-message heartbeat
        # could live) never runs — without spawning here the 25 s
        # NAT-keepalive contract documented in the module header
        # would silently never fire.
        if ws is not None:
            task = _start_heartbeat(send)
            _register_heartbeat(id(ws), task)

    async def _on_disconnect(send, ws=None) -> None:
        """Cancel the heartbeat and remove the socket from every
        user pool it might belong to.

        We don't have direct access to the user_id at disconnect
        time (FastHTML's disconnect hook only receives ``send`` and
        ``ws``), so we scan the registry for any entry containing
        this ``send``. With at most a few hundred concurrent users
        this is negligible; if the user count grows we can swap in
        a reverse index (``send -> user_id``) without changing the
        wire protocol.
        """
        if ws is not None:
            task = _pop_heartbeat(id(ws))
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        with _registry_lock:
            empty_users: list[str] = []
            for user_id, sends in _connections.items():
                if send in sends:
                    sends.discard(send)
                    if not sends:
                        empty_users.append(user_id)
            for user_id in empty_users:
                _connections.pop(user_id, None)

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
