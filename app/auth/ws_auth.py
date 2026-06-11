"""Shared WebSocket auth + lifecycle helpers (#856).

Five WS channels (chat, draft status, export progress, notifications,
explorer) used to hand-roll byte-identical copies of the same four
pieces of plumbing, with real drift bugs between them (a close path
that never closed, an unauthenticated channel, a registry that could
never clean up). This module is the single home for:

* :func:`extract_cookie_from_headers` — parse one named cookie out of
  the raw ASGI handshake headers.
* :class:`WSCookieAuth` — per-channel lazily-constructed
  :class:`~app.auth.jwt_provider.JWTAuthProvider` plus the
  access-token-with-silent-refresh resolution contract (#637).
* :func:`close_ws` — fail-closed close that actually terminates the
  socket via the raw Starlette conn (FastHTML's ``send`` wraps
  everything in ``to_xml`` + ``ws.send_text``, so pushing an ASGI
  ``websocket.close`` dict through it produces a garbage text frame
  and leaves the connection open — review finding F1).
* :func:`start_heartbeat` — the periodic ``{"type": "ping"}`` task
  that keeps NAT / proxy idle timeouts from silently killing the
  socket (#658 / #684), with an optional ``on_fail`` callback so
  push-only channels can deregister a dead connection the moment the
  heartbeat detects it (review finding F3).
* :class:`HeartbeatRegistry` — thread-safe per-connection heartbeat
  task registry for push-only channels whose heartbeat outlives a
  single handler invocation (notifications, explorer).

Connection identity (the F3 / F4 trap)
--------------------------------------

FastHTML rebuilds ``send`` as ``partial(_send_ws, conn)`` on EVERY
hook dispatch (``fasthtml/core.py::_find_p``), so ``id(send)`` is NOT
stable across the connect hook, each message, and the disconnect hook
of one physical connection. The unannotated ``ws`` parameter binds the
raw Starlette ``WebSocket`` conn, which IS the same object for the
connection's whole lifetime — registries must key on ``id(ws)``.
:func:`conn_key` encodes that rule (with an ``id(send)`` fallback for
direct-invocation unit tests that don't construct a conn).

Origin checking is deliberately NOT here: ``/ws/*`` handshake Origin
allowlisting is enforced for every channel by
:class:`app.auth.perimeter.OriginCheckMiddleware` (#851/#871) before
the app ever accepts the socket.

FastHTML param-resolver discipline still applies in the channel
modules: ``send`` / ``scope`` / ``ws`` MUST stay unannotated on the
registered hooks (#802 — see ``docs/2026-05-18-bugfix-plan.md`` Wave 3).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable
from http.cookies import SimpleCookie
from typing import Any, NamedTuple

from app.auth.jwt_provider import JWTAuthProvider

logger = logging.getLogger(__name__)


# Heartbeat cadence shared by every channel (#658 / #684). The send is
# bounded so a hung TCP buffer (the exact failure the heartbeat is
# meant to detect) can't hang the heartbeat task itself. Module-level
# names (not function defaults) so tests can monkeypatch them.
WS_HEARTBEAT_INTERVAL_SECONDS = 25.0
WS_HEARTBEAT_SEND_TIMEOUT_SECONDS = 5.0


def extract_cookie_from_headers(headers: list[tuple[bytes, bytes]], name: str) -> str | None:
    """Parse the ``Cookie`` header from raw ASGI *headers*; return *name*'s value.

    Returns ``None`` when the cookie is absent or the headers carry no
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


def conn_key(send: Any, ws: Any = None) -> int:
    """Stable per-connection registry key.

    ``id(ws)`` when the raw Starlette conn is available (always, in
    production — FastHTML resolves the unannotated ``ws`` param to the
    same conn object on every hook dispatch). Falls back to
    ``id(send)`` only for direct-invocation unit tests that drive a
    handler without constructing a conn.
    """
    return id(ws) if ws is not None else id(send)


class WSAuthResult(NamedTuple):
    """Outcome of resolving the handshake cookies to a user.

    ``user``
        The verified user dict, or ``None`` when the handshake is
        unauthenticated (no usable cookie).
    ``provider_unavailable``
        ``True`` when credentials WERE presented but the JWT provider
        could not be constructed (missing secret, DB unreachable, …).
        Callers MUST treat this as fail-closed and terminate the
        socket with close code 1011 instead of proceeding (#594.4).
    """

    user: dict[str, Any] | None
    provider_unavailable: bool


class WSCookieAuth:
    """Per-channel JWT cookie authentication for WS handshakes.

    Mirrors the HTTP middleware's silent-refresh contract (#637): when
    the ``access_token`` cookie is missing/invalid but a valid
    ``refresh_token`` is present, the user is verified via the
    *verify-only* helper — the WS upgrade response cannot persist
    rotated cookies, so consuming the refresh token here would strand
    the browser with a dead cookie. Rotation happens atomically on the
    next HTTP request through the Beforeware.

    ``provider_factory`` exists so each channel module can pass
    ``lambda: JWTAuthProvider()`` referencing ITS OWN module-level
    import — keeping the established per-channel test patch paths
    (``patch("app.chat.websocket.JWTAuthProvider")`` etc.) working.
    Construction failure is NOT cached: a transient DB outage at
    first-connect must not poison every later handshake.
    """

    def __init__(
        self,
        channel: str,
        provider_factory: Callable[[], JWTAuthProvider] | None = None,
    ) -> None:
        self.channel = channel
        self._provider_factory = provider_factory
        self._provider: JWTAuthProvider | None = None

    def get_provider(self) -> JWTAuthProvider | None:
        """Return the shared JWT provider, constructing it lazily.

        Returns ``None`` when construction fails. Callers MUST treat
        ``None`` as a fail-closed signal (close 1011), never as
        "proceed unauthenticated".
        """
        if self._provider is None:
            factory: Callable[[], JWTAuthProvider] = self._provider_factory or JWTAuthProvider
            try:
                self._provider = factory()
            except Exception:
                logger.error(
                    "%s WS: failed to construct JWTAuthProvider",
                    self.channel,
                    exc_info=True,
                )
                return None
        return self._provider

    def resolve_user(self, scope: dict[str, Any] | None) -> WSAuthResult:
        """Resolve the handshake cookies in *scope* to a verified user.

        Access token first; silent-refresh (verify-only, #637) as the
        fallback. ``WSAuthResult(None, False)`` means "no/invalid
        credentials"; ``WSAuthResult(None, True)`` means "credentials
        presented but the provider is down" (fail-closed, 1011).
        """
        if scope is None:
            return WSAuthResult(None, False)

        raw_headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
        access_token = extract_cookie_from_headers(raw_headers, "access_token")
        refresh_token = extract_cookie_from_headers(raw_headers, "refresh_token")

        if not (access_token or refresh_token):
            return WSAuthResult(None, False)

        provider = self.get_provider()
        if provider is None:
            return WSAuthResult(None, True)

        user: dict[str, Any] | None = None
        if access_token:
            verified = provider.get_current_user(access_token)
            if verified is not None:
                user = dict(verified)

        if user is None and refresh_token:
            # Verify-only refresh (#637, review): the upgrade response
            # has no Set-Cookie hook, so the refresh token must NOT be
            # consumed here. Imported lazily so channel modules carry
            # no import-time dependency on the HTTP middleware.
            from app.auth.middleware import verify_refresh_token_user

            refreshed_user = verify_refresh_token_user(refresh_token, provider=provider)
            if refreshed_user is not None:
                user = refreshed_user

        return WSAuthResult(user, False)


async def close_ws(ws: Any, code: int, reason: str, *, channel: str = "ws") -> None:
    """Best-effort WS close that actually closes the underlying socket.

    FastHTML's ``send`` parameter is ``partial(_send_ws, conn)`` — the
    payload is fed through ``to_xml`` and ``ws.send_text``, so a
    ``{"type": "websocket.close", ...}`` dict pushed through ``send``
    becomes a regular text frame and the connection stays open (review
    finding F1). The only correct close path is ``ws.close()`` on the
    raw Starlette conn, which FastHTML exposes via the unannotated
    ``ws`` parameter on WS hooks.
    """
    if ws is None:
        # Defensive: the caller had no conn (direct-invocation test or
        # a hook that forgot to declare ``ws``). Log so a production
        # regression is visible instead of silently not closing.
        logger.debug("%s WS close(code=%s) requested without ws conn", channel, code)
        return
    try:
        await ws.close(code=code, reason=reason)
    except Exception:
        logger.debug("%s WS close failed", channel, exc_info=True)


def start_heartbeat(
    send: Any,
    *,
    channel: str = "ws",
    on_fail: Callable[[], None] | None = None,
) -> asyncio.Task[None]:
    """Spawn the periodic ``{"type": "ping"}`` heartbeat task.

    Emits every :data:`WS_HEARTBEAT_INTERVAL_SECONDS`; each send is
    bounded by :data:`WS_HEARTBEAT_SEND_TIMEOUT_SECONDS`. Self-
    terminates on the first send failure (post-#684): once the socket
    is dead every subsequent send fails forever, so looping would only
    accumulate noise. When *on_fail* is given it runs exactly once on
    that failure path — push-only channels use it to deregister the
    dead connection immediately instead of waiting for the next
    broadcast to trip over it (review finding F3).

    Returns the task so the caller can cancel it when the scope that
    owns it (handler invocation or connection) ends.
    """

    async def _beat() -> None:
        while True:
            try:
                # Re-read the module-level constants every tick so test
                # monkeypatching takes effect mid-task.
                await asyncio.sleep(WS_HEARTBEAT_INTERVAL_SECONDS)
                await asyncio.wait_for(
                    send(json.dumps({"type": "ping"})),
                    timeout=WS_HEARTBEAT_SEND_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug(
                    "%s WS heartbeat send failed; terminating",
                    channel,
                    exc_info=True,
                )
                if on_fail is not None:
                    try:
                        on_fail()
                    except Exception:
                        logger.debug(
                            "%s WS heartbeat on_fail callback failed",
                            channel,
                            exc_info=True,
                        )
                return

    return asyncio.create_task(_beat())


class HeartbeatRegistry:
    """Thread-safe per-connection heartbeat task registry.

    Used by push-only channels (notifications, explorer) whose
    heartbeat is scoped to the whole connection rather than to one
    handler invocation. Keys are :func:`conn_key` values (``id(ws)``).
    The lock is a ``threading.Lock`` because registries may be
    inspected from sync code (tests, diagnostics) while the event loop
    mutates them.
    """

    def __init__(self) -> None:
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._lock = threading.Lock()

    def register(self, key: int, task: asyncio.Task[None]) -> None:
        """Track *task* under *key*, cancelling any stale predecessor."""
        with self._lock:
            old = self._tasks.pop(key, None)
        if old is not None and not old.done():
            old.cancel()
        with self._lock:
            self._tasks[key] = task

    def pop(self, key: int) -> asyncio.Task[None] | None:
        """Remove and return the task under *key* (``None`` on miss)."""
        with self._lock:
            return self._tasks.pop(key, None)

    def get(self, key: int) -> asyncio.Task[None] | None:
        with self._lock:
            return self._tasks.get(key)

    def cancel_clear(self) -> None:
        """Cancel every tracked task and empty the registry (test hook)."""
        with self._lock:
            tasks = list(self._tasks.values())
            self._tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()

    def __contains__(self, key: int) -> bool:
        with self._lock:
            return key in self._tasks

    def __len__(self) -> int:
        with self._lock:
            return len(self._tasks)
