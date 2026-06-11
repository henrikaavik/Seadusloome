"""WebSocket handler for explorer real-time notifications.

Push-only channel: the client opens ``/ws/explorer`` and listens for
``{"event": "sync_complete", ...}`` broadcasts emitted by the sync
orchestrator after a successful data refresh. Clients never send
meaningful messages.

Since #856 the handshake is **authenticated** (review finding F2): the
ontology data behind the explorer is org-scoped and every other WS
channel already requires the JWT cookie, so an anonymous socket here
was an inconsistency, not a feature. Auth + heartbeat live in the
``conn`` hook because this is a push-only channel — the receive
handler (:func:`ws_explorer`) never runs in production, so anything
deferred to it would never execute (same shape as
:mod:`app.notifications.websocket`, post-#684).

The connection registry is keyed on the stable conn identity
(:func:`app.auth.ws_auth.conn_key` — ``id(ws)``), NOT on ``send``:
FastHTML rebuilds the ``send`` partial on every hook dispatch, so a
``send``-keyed set could never be cleaned up from the disconnect hook.
The registry is also bounded (:data:`_MAX_CONNECTIONS`) so a
reconnect storm cannot grow it without limit.

Origin allowlisting for the handshake is enforced upstream by
:class:`app.auth.perimeter.OriginCheckMiddleware` (#851/#871).

FastHTML param-resolver discipline (root cause of #802): ``send`` /
``scope`` / ``ws`` MUST stay unannotated on the hooks below. See
``docs/2026-05-18-bugfix-plan.md`` Wave 3.
"""

from __future__ import annotations

import asyncio
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

# Upper bound on concurrent explorer sockets (F2: bound the connection
# set). The product targets 5-50 concurrent officials; 200 leaves
# generous multi-tab headroom while capping what a reconnect loop or a
# cookie-bearing script can pin in memory. Excess handshakes are closed
# with 1013 (Try Again Later) — the client's existing backoff retries.
_MAX_CONNECTIONS = 200

# ``conn_key`` (id(ws)) → bound ``send`` callable. Guarded by a
# threading.Lock because ``notify_sync_complete_sync`` may run from the
# sync orchestrator's worker thread while the event loop mutates the
# registry (same discipline as the notifications registry).
_connected_clients: dict[int, Any] = {}
_clients_lock = threading.Lock()

# Per-connection heartbeat tasks, keyed identically.
_heartbeats = HeartbeatRegistry()


def _register_client(key: int, send: Any) -> None:
    with _clients_lock:
        _connected_clients[key] = send


def _remove_client(key: int) -> None:
    with _clients_lock:
        _connected_clients.pop(key, None)


def _client_count() -> int:
    with _clients_lock:
        return len(_connected_clients)


def _snapshot_clients() -> list[tuple[int, Any]]:
    with _clients_lock:
        return list(_connected_clients.items())


def _drop_connection(key: int) -> None:
    """Deregister *key* entirely: connection slot + heartbeat task.

    Used by the disconnect hook and by the heartbeat-failure path (the
    heartbeat self-terminates, so its task only needs popping, not
    cancelling — and cancelling a task from within itself would be a
    no-op anyway).
    """
    _remove_client(key)
    _heartbeats.pop(key)


async def ws_explorer(msg, send) -> None:
    """Handle incoming messages from the client (currently unused)."""
    # Clients don't send meaningful messages; this is a push-only channel.
    pass


async def notify_sync_complete() -> None:
    """Broadcast a sync-complete notification to all connected explorer clients.

    Called from the sync orchestrator after a successful data refresh.
    Dead sockets are dropped from the registry on send failure.
    """
    clients = _snapshot_clients()
    if not clients:
        logger.debug("No connected WS clients to notify")
        return

    payload = '{"event":"sync_complete","message":"Andmebaas uuendatud"}'
    failed = 0

    for key, send in clients:
        try:
            await send(payload)
        except Exception:
            logger.debug("Failed to send WS notification, marking client for removal")
            _drop_connection(key)
            failed += 1

    logger.info(
        "Notified %d WS clients of sync completion",
        len(clients) - failed,
    )


def notify_sync_complete_sync() -> None:
    """Synchronous wrapper for notify_sync_complete.

    Can be called from synchronous code (e.g., the sync orchestrator).
    Schedules the async notification on the running event loop if available,
    otherwise creates a new one.
    """
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(notify_sync_complete())
    except RuntimeError:
        # No running event loop — run in a new one.
        asyncio.run(notify_sync_complete())


def register_ws_routes(app: Any) -> None:
    """Register the explorer WebSocket route on the FastHTML *app*.

    The ``conn`` hook authenticates the handshake (cookie JWT with
    silent-refresh fallback — same contract as ``/ws/chat`` /
    ``/ws/notifications``), bounds the connection set, registers the
    socket for broadcasts, and starts the connection-lifetime
    heartbeat. ``/ws/explorer`` is deliberately NOT in ``SKIP_PATHS``
    (the WS handshake never goes through the HTTP Beforeware; auth
    happens here).
    """
    # The factory lambda resolves ``JWTAuthProvider`` from THIS
    # module's globals at call time so tests can patch
    # ``app.explorer.websocket.JWTAuthProvider``.
    authenticator = WSCookieAuth("explorer", provider_factory=lambda: JWTAuthProvider())

    # IMPORTANT: do NOT annotate ``send`` / ``scope`` / ``ws`` — see
    # the module docstring (#802 resolver trap).
    async def _on_connect(send, scope=None, ws=None) -> None:
        """Authenticate, bound, register, and start the heartbeat.

        An unauthenticated handshake is closed with 1008; a provider
        outage fails closed with 1011; a full registry closes with
        1013 (Try Again Later). All closes go through ``ws.close()``
        on the raw conn — never through FastHTML's wrapped ``send``.
        """
        result = authenticator.resolve_user(scope)
        if result.provider_unavailable:
            await close_ws(ws, 1011, "auth provider unavailable", channel="explorer")
            return
        if result.user is None or not result.user.get("id"):
            await close_ws(ws, 1008, "authentication required", channel="explorer")
            return

        if _client_count() >= _MAX_CONNECTIONS:
            logger.warning(
                "Explorer WS connection limit (%d) reached; rejecting handshake",
                _MAX_CONNECTIONS,
            )
            await close_ws(ws, 1013, "too many connections", channel="explorer")
            return

        key = conn_key(send, ws)
        _register_client(key, send)
        logger.info("Explorer WS client connected (total: %d)", _client_count())

        # Connection-lifetime heartbeat: push-only channel, so the
        # receive handler never runs and a per-message heartbeat would
        # never fire. On send failure the heartbeat self-terminates
        # and deregisters this connection immediately (F3 contract).
        task = start_heartbeat(
            send,
            channel="explorer",
            on_fail=lambda: _drop_connection(key),
        )
        _heartbeats.register(key, task)

    async def _on_disconnect(send, ws=None) -> None:
        """Cancel the heartbeat and drop the registry slot.

        Keyed on ``id(ws)`` — the ``send`` here is a different partial
        than the one ``_on_connect`` registered, so identity-matching
        on ``send`` would leak the slot forever.
        """
        key = conn_key(send, ws)
        task = _heartbeats.pop(key)
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        _remove_client(key)
        logger.info("Explorer WS client disconnected (total: %d)", _client_count())

    app.ws("/ws/explorer", conn=_on_connect, disconn=_on_disconnect)(ws_explorer)
