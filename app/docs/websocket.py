"""WebSocket endpoint for live draft pipeline status updates (#608).

Same FastHTML ``app.ws()`` pattern as ``app/chat/websocket.py`` and
``app/explorer/websocket.py``. Handshake auth comes from the JWT
access-token cookie (refresh-rotated transparently — see chat module
for the full pattern). Path is ``/ws/drafts/status``; clients send a
``{"type": "subscribe", "draft_id": "<uuid>"}`` message after the
``connected`` event to start receiving status events for that draft.

Subscription model
------------------

One connection, one draft. The detail page renders a small client
script that opens this WS, sends the subscribe message for the draft
on screen, and swaps the status tracker fragment via HTMX whenever a
``{"type": "status", ...}`` event arrives. Cross-tab updates fall out
of the in-memory subscriber set automatically: if user A has the
draft open in two tabs, both connections subscribe and both receive.

Cross-org safety
----------------

Before adding a connection to the subscriber registry the handler
runs the same authorisation check used by the HTTP detail page
(:func:`app.auth.policy.can_view_draft`). A user from another org
hitting this endpoint with an arbitrary draft id receives a single
``error`` frame and the socket closes — they never get added to the
broadcast list.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from http.cookies import SimpleCookie
from typing import Any

from app.auth.jwt_provider import JWTAuthProvider
from app.auth.policy import can_view_draft
from app.docs import status_events
from app.docs.draft_model import fetch_draft

logger = logging.getLogger(__name__)


# Heartbeat — same shape as the chat module's per-handler heartbeat
# (post-review fix to #684). Keeps NAT idle timeouts from dropping the
# socket during long pipeline phases (analyze can run several
# minutes for large drafts).
_WS_HEARTBEAT_INTERVAL_SECONDS = 25.0
_WS_HEARTBEAT_SEND_TIMEOUT_SECONDS = 5.0


def _start_heartbeat(send: Any) -> asyncio.Task[None]:
    """Spawn a periodic ``{"type": "ping"}`` task for the lifetime of
    the WS handler invocation. Self-terminates on first send error."""

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
                    "draft-status WS heartbeat send failed; terminating",
                    exc_info=True,
                )
                return

    return asyncio.create_task(_beat())


def _extract_cookie_from_headers(headers: list[tuple[bytes, bytes]], name: str) -> str | None:
    """Parse a single named cookie out of raw ASGI ``Cookie`` headers."""
    for hdr_name, hdr_value in headers:
        if hdr_name.lower() == b"cookie":
            cookie: SimpleCookie = SimpleCookie()
            cookie.load(hdr_value.decode("latin-1"))
            morsel = cookie.get(name)
            if morsel is not None:
                return morsel.value
    return None


async def _ws_close(send: Any, code: int, reason: str) -> None:
    """Best-effort WS close. Mirrors the chat module's helper.

    The ASGI ``websocket.close`` envelope is the canonical close path;
    if the runtime prefers a different shape we fall back to sending a
    JSON error event before exiting.
    """
    try:
        await send({"type": "websocket.close", "code": code, "reason": reason})
    except Exception:
        try:
            await send(json.dumps({"type": "error", "message": reason, "code": code}))
        except Exception:
            logger.debug("draft-status WS close fallback failed", exc_info=True)


async def ws_draft_status(
    msg: str,
    send: Any,
    scope: dict[str, Any] | None = None,
) -> None:
    """Handle one client message on ``/ws/drafts/status``.

    Expected envelope::

        {"type": "subscribe", "draft_id": "<uuid>"}

    On a valid subscribe the handler:
    1. Authorises the caller against the draft via
       :func:`can_view_draft` (404-style: drop the connection on miss
       so we don't leak existence of out-of-scope drafts).
    2. Registers the ``send`` callable in :mod:`app.docs.status_events`.
    3. Pushes the current status as a one-shot ``initial`` event so
       the client doesn't have to wait for the next transition.
    4. Holds the connection open until the FastHTML wrapper tears it
       down (handler return / cancellation / disconnect).

    Auth is extracted by the wrapper in
    :func:`register_draft_ws_routes`; this handler trusts ``scope['auth']``.
    """
    try:
        data = json.loads(msg)
    except (json.JSONDecodeError, TypeError):
        await send(json.dumps({"type": "error", "message": "Vigane JSON."}))
        return

    if not isinstance(data, dict):
        await send(json.dumps({"type": "error", "message": "Vigane sõnum."}))
        return

    if data.get("type") != "subscribe":
        # Other message types are silently ignored — keeps the API
        # forwards-compatible with future operations like "unsubscribe".
        return

    raw_draft_id = data.get("draft_id")
    if not raw_draft_id:
        await send(json.dumps({"type": "error", "message": "Puudub draft_id."}))
        return

    try:
        draft_id = uuid.UUID(str(raw_draft_id))
    except (ValueError, TypeError):
        await send(json.dumps({"type": "error", "message": "Vigane draft_id."}))
        return

    auth = (scope or {}).get("auth") or {}
    if not auth.get("id"):
        await send(json.dumps({"type": "error", "message": "Autentimine nõutav."}))
        return

    # Authorisation: same gate as the HTTP detail page. We drop the
    # subscription request on a denial without leaking whether the
    # draft exists.
    draft = fetch_draft(draft_id)
    if draft is None or not can_view_draft(auth, draft):
        await send(json.dumps({"type": "error", "message": "Eelnõu ei leitud."}))
        return

    # Subscribe + send initial state. The client uses ``initial`` to
    # paint the tracker without waiting for the next transition.
    await status_events.subscribe(draft_id, send)
    await send(
        json.dumps(
            {
                "type": "initial",
                "draft_id": str(draft_id),
                "status": draft.status,
                "error_message": getattr(draft, "error_message", None),
            },
            default=str,
        )
    )

    # Hold the handler open until cancellation. The FastHTML WS
    # wrapper invokes us per-message; subsequent messages from the
    # client (or disconnect) reach us through new invocations or via
    # the wrapper's exception/finally path. Without this hold the
    # handler returns immediately and our subscription is torn down
    # by the caller's finally block — the client would never receive
    # a status event because nobody is keeping the slot alive.
    #
    # In practice: this awaits forever; the caller's exception/cancel
    # path runs the finally block which unsubscribes us cleanly.
    try:
        await asyncio.Event().wait()
    finally:
        await status_events.unsubscribe(draft_id, send)


def register_draft_ws_routes(app: Any) -> None:
    """Mount the draft-status WS at ``/ws/drafts/status``.

    Mirrors :func:`app.chat.websocket.register_chat_ws_routes`: we
    install a wrapper that does cookie-based JWT auth on each
    invocation, then delegates the message body to
    :func:`ws_draft_status`. Connection-level lifecycle hooks
    (``conn``, ``disconn``) are no-ops here because all per-connection
    state lives in the subscriber registry, which is keyed by the
    closure-local ``send`` and torn down in
    :func:`ws_draft_status`'s finally block.
    """
    _jwt_provider: list[JWTAuthProvider | None] = [None]

    def _get_jwt_provider() -> JWTAuthProvider | None:
        if _jwt_provider[0] is None:
            try:
                _jwt_provider[0] = JWTAuthProvider()
            except Exception:
                logger.error(
                    "draft-status WS: failed to construct JWTAuthProvider",
                    exc_info=True,
                )
                return None
        return _jwt_provider[0]

    async def _ws_handler(msg: str, send: Any, scope: dict[str, Any] | None = None) -> None:
        auth_scope: dict[str, Any] = {}

        if scope is not None:
            raw_headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
            access_token = _extract_cookie_from_headers(raw_headers, "access_token")
            refresh_token = _extract_cookie_from_headers(raw_headers, "refresh_token")

            if access_token or refresh_token:
                provider = _get_jwt_provider()
                if provider is None:
                    await _ws_close(send, 1011, "auth provider unavailable")
                    return

                user: dict[str, Any] | None = None
                if access_token:
                    verified = provider.get_current_user(access_token)
                    if verified is not None:
                        user = dict(verified)

                if user is None and refresh_token:
                    # Same silent-refresh shape as the chat WS (#637).
                    from app.auth.middleware import verify_refresh_token_user

                    refreshed_user = verify_refresh_token_user(refresh_token, provider=provider)
                    if refreshed_user is not None:
                        user = refreshed_user

                if user is not None:
                    auth_scope["auth"] = user

        # Heartbeat scoped to this handler invocation, mirroring the
        # chat pattern (post-review fix to #684).
        heartbeat = _start_heartbeat(send)
        try:
            await ws_draft_status(msg, send, auth_scope if auth_scope else None)
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except (asyncio.CancelledError, Exception):
                pass

    app.ws("/ws/drafts/status")(_ws_handler)
