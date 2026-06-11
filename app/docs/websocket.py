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
from typing import Any

from app.auth.jwt_provider import JWTAuthProvider
from app.auth.policy import can_view_draft
from app.auth.ws_auth import WSCookieAuth, close_ws, start_heartbeat
from app.docs import status_events
from app.docs.draft_model import fetch_draft

logger = logging.getLogger(__name__)


# Heartbeat / cookie-auth / close plumbing is shared across every WS
# channel since #856 — see app/auth/ws_auth.py. The heartbeat keeps
# NAT idle timeouts from dropping the socket during long pipeline
# phases (analyze can run several minutes for large drafts).


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
    # Shared cookie-JWT authenticator (#856). The factory lambda
    # resolves ``JWTAuthProvider`` from THIS module's globals at call
    # time so tests can patch ``app.docs.websocket.JWTAuthProvider``.
    authenticator = WSCookieAuth("draft-status", provider_factory=lambda: JWTAuthProvider())

    # IMPORTANT — FastHTML param resolution (root cause of #802):
    #   * ``send`` / ``scope`` / ``ws`` MUST be unannotated. ``_find_p``
    #     only resolves these WS special names inside its ``if anno is
    #     empty:`` branch (``fasthtml/core.py:_find_p``). Annotating
    #     them (even with ``Any``) makes FastHTML fall through to the
    #     generic path/cookies/headers/query/data resolver and raise
    #     ``ValueError: Missing required field: <name>``.
    #   * ``msg`` MUST be annotated ``dict``. The ``if anno is dict:``
    #     branch of ``_find_p`` returns the parsed JSON ``data`` payload.
    #     Without an annotation the empty-anno branch returns ``None``
    #     (``msg`` is not a FastHTML special name); ``ws_draft_status``
    #     would then crash on ``json.loads(None)``. We re-serialise the
    #     dict at the boundary below so the inner handler's existing
    #     ``msg: str`` contract — and its unit tests — stay unchanged.
    # See ``docs/2026-05-18-bugfix-plan.md`` Wave 3.
    async def _ws_handler(msg: dict, send, scope=None, ws=None) -> None:
        auth_scope: dict[str, Any] = {}

        result = authenticator.resolve_user(scope)
        if result.provider_unavailable:
            # Fail-closed (#594.4): terminate via the raw conn — a
            # close dict pushed through FastHTML's wrapped ``send``
            # would leave the socket open (F1, #856).
            await close_ws(ws, 1011, "auth provider unavailable", channel="draft-status")
            return
        if result.user is not None:
            auth_scope["auth"] = result.user

        # Heartbeat scoped to this handler invocation, mirroring the
        # chat pattern (post-review fix to #684).
        heartbeat = start_heartbeat(send, channel="draft-status")
        try:
            # See app/chat/websocket.py: accept both dict (FastHTML resolver)
            # and string (legacy direct-call tests).
            msg_str = json.dumps(msg) if isinstance(msg, dict) else msg
            await ws_draft_status(msg_str, send, auth_scope if auth_scope else None)
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except (asyncio.CancelledError, Exception):
                pass

    # See app/chat/websocket.py for the rationale: PEP-563 stringifies
    # the ``msg: dict`` annotation, which fails FastHTML's identity
    # check in ``_find_p``. Override with the real type at runtime so
    # the resolver injects the parsed WS payload. #802 phase-2.
    _ws_handler.__annotations__["msg"] = dict

    app.ws("/ws/drafts/status")(_ws_handler)
