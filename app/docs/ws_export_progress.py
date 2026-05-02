"""WebSocket endpoint for live export job progress (#610).

Same FastHTML ``app.ws()`` pattern as :mod:`app.docs.websocket` (draft
status, post-#694 review). Handshake auth comes from the JWT
access-token cookie with refresh fallback. Path is
``/ws/drafts/export-progress``; clients send a single
``{"type": "subscribe", "draft_id": "<uuid>", "job_id": <int>}``
message after the ``connected`` event to start receiving progress
events for that export job.

Subscription model
------------------

One connection, one job. The export-status fragment renders a small
client script that opens this WS, sends the subscribe message for the
in-flight job, and updates the ``<progress>`` element + numeric label
on every ``{"type": "progress", "current": N, "total": M}`` push. The
WS closes itself once the job reaches a terminal status
(``success`` / ``failed``).

Cross-org safety
----------------

Before subscribing the handler runs the same authorisation check used
by the HTTP export-status fragment (:func:`app.auth.policy.can_view_draft`)
plus a defensive check that the requested ``job_id`` belongs to the
``draft_id`` in the payload. A user from another org hitting this
endpoint with a leaked job id receives a single ``error`` frame and
the socket closes — they never get to read another org's progress.

Polling instead of LISTEN/NOTIFY
--------------------------------

The progress column is updated by the worker thread (synchronous,
inside ``app/docs/export_handler.py::_publish_progress``). Rather than
wire a Postgres ``LISTEN`` channel for one column on one job, this
handler polls ``background_jobs`` every ``_POLL_INTERVAL_SECONDS`` and
pushes the current ``{current, total}`` if it changed since the last
push. The worst-case observable lag is ``_POLL_INTERVAL_SECONDS``
which is well under the polling fallback's 2-10s tick. If we ever need
sub-second pushes we can swap the poll loop for a Postgres LISTEN on a
``progress_published`` channel without changing the WS frame format.
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
from app.db import get_connection
from app.docs.draft_model import fetch_draft

logger = logging.getLogger(__name__)


# Heartbeat — same shape as the chat module's per-handler heartbeat
# (post-review fix to #684). Keeps NAT idle timeouts from dropping the
# socket during long .docx renders (large reports can spend a minute
# rendering tables without a status change).
_WS_HEARTBEAT_INTERVAL_SECONDS = 25.0
_WS_HEARTBEAT_SEND_TIMEOUT_SECONDS = 5.0

# Poll cadence for the worker-side progress column. Fast enough to feel
# live in the browser without hammering Postgres; the column itself is
# tiny (a JSONB blob with two ints) so the SELECT cost is negligible.
_POLL_INTERVAL_SECONDS = 0.5

# Total budget on the watcher loop. The polling layer in the HTTP
# fragment caps its own retries at 300s (see ``_EXPORT_POLLING_TIMEOUT_SECONDS``
# in ``app/docs/report_routes.py``); we mirror that here so a
# permanently-stuck worker can't keep a WebSocket alive forever.
_WS_MAX_LIFETIME_SECONDS = 360.0

# Background-job statuses that mean the export is done (success or
# permanent failure). Reaching one of these closes the WS cleanly.
_TERMINAL_STATUSES = frozenset({"success", "failed"})


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
                    "export-progress WS heartbeat send failed; terminating",
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
            logger.debug("export-progress WS close fallback failed", exc_info=True)


def _read_job_progress(job_id: int) -> tuple[str | None, dict[str, Any] | None]:
    """Return ``(status, progress)`` for *job_id*, or ``(None, None)`` on miss.

    Sync DB call — the WS handler offloads it via
    :func:`asyncio.to_thread` so the event loop is never blocked. The
    return signature explicitly carries the status so the caller can
    detect terminal jobs and close the socket without an extra round
    trip to ``JobQueue.get``.
    """
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT status, progress, payload FROM background_jobs WHERE id = %s",
                (job_id,),
            ).fetchone()
    except Exception:
        logger.debug("export-progress WS: DB read failed for job=%s", job_id, exc_info=True)
        return None, None
    if row is None:
        return None, None
    status, raw_progress, _payload = row
    progress: dict[str, Any] | None
    if raw_progress is None:
        progress = None
    elif isinstance(raw_progress, dict):
        progress = raw_progress
    elif isinstance(raw_progress, (bytes, bytearray)):
        try:
            progress = json.loads(raw_progress.decode())
        except (TypeError, ValueError, UnicodeDecodeError):
            logger.debug("export-progress WS: unparseable bytes job=%s", job_id)
            progress = None
    elif isinstance(raw_progress, str):
        try:
            progress = json.loads(raw_progress)
        except (TypeError, ValueError):
            logger.debug("export-progress WS: unparseable string job=%s", job_id)
            progress = None
    else:
        logger.debug(
            "export-progress WS: unexpected type for progress job=%s type=%s",
            job_id,
            type(raw_progress).__name__,
        )
        progress = None
    return status, progress


def _validate_job_belongs_to_draft(job_id: int, draft_id: uuid.UUID) -> bool:
    """Return ``True`` iff *job_id* is an ``export_report`` job for *draft_id*.

    Defensive cross-org guard: even after the draft-level auth check
    has passed, a leaked job id from another org must not let the
    caller pull progress for a job that isn't theirs.
    """
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM background_jobs
                WHERE id = %s
                  AND job_type = 'export_report'
                  AND payload->>'draft_id' = %s
                """,
                (job_id, str(draft_id)),
            ).fetchone()
    except Exception:
        logger.debug(
            "export-progress WS: ownership check failed job=%s draft=%s",
            job_id,
            draft_id,
            exc_info=True,
        )
        return False
    return row is not None


async def ws_export_progress(
    msg: str,
    send: Any,
    scope: dict[str, Any] | None = None,
) -> None:
    """Handle one client message on ``/ws/drafts/export-progress``.

    Expected envelope::

        {"type": "subscribe", "draft_id": "<uuid>", "job_id": <int>}

    On a valid subscribe the handler:

    1. Authorises the caller against the draft via
       :func:`can_view_draft` (drops the connection on miss).
    2. Confirms the requested ``job_id`` actually belongs to that draft
       via :func:`_validate_job_belongs_to_draft`.
    3. Pushes the current ``progress`` payload as a one-shot
       ``initial`` event so the client doesn't have to wait for the
       first per-N-row publish.
    4. Polls ``background_jobs.progress`` every
       ``_POLL_INTERVAL_SECONDS``; pushes a ``{"type": "progress",
       "current": N, "total": M}`` event whenever the value changed
       since the last push.
    5. Closes the socket when the job reaches a terminal status
       (``success`` / ``failed``) or when ``_WS_MAX_LIFETIME_SECONDS``
       elapses (defence-in-depth against a stuck worker).
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
        # Forward-compat: ignore unknown message types silently.
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

    raw_job_id = data.get("job_id")
    if raw_job_id is None:
        await send(json.dumps({"type": "error", "message": "Puudub job_id."}))
        return

    try:
        job_id = int(raw_job_id)
    except (TypeError, ValueError):
        await send(json.dumps({"type": "error", "message": "Vigane job_id."}))
        return

    auth = (scope or {}).get("auth") or {}
    if not auth.get("id"):
        await send(json.dumps({"type": "error", "message": "Autentimine nõutav."}))
        return

    # Authorisation: same gate as the HTTP export-status fragment. We
    # drop on a denial without leaking whether the draft exists.
    draft = await asyncio.to_thread(fetch_draft, draft_id)
    if draft is None or not can_view_draft(auth, draft):
        await send(json.dumps({"type": "error", "message": "Eelnõu ei leitud."}))
        return

    # Cross-org defence: even after the draft check, the job id has to
    # belong to this draft. A leaked job id from another export must
    # not return progress.
    job_owns_draft = await asyncio.to_thread(_validate_job_belongs_to_draft, job_id, draft_id)
    if not job_owns_draft:
        await send(json.dumps({"type": "error", "message": "Eksporditööd ei leitud."}))
        return

    # Emit the initial state immediately so the client paints the bar
    # without waiting for the first poll tick.
    status, progress = await asyncio.to_thread(_read_job_progress, job_id)
    last_pushed: tuple[Any, Any] | None = None
    initial_payload: dict[str, Any] = {
        "type": "initial",
        "job_id": job_id,
        "draft_id": str(draft_id),
        "status": status,
    }
    if progress is not None:
        initial_payload["current"] = progress.get("current")
        initial_payload["total"] = progress.get("total")
        last_pushed = (progress.get("current"), progress.get("total"))
    await send(json.dumps(initial_payload, default=str))

    # Already terminal? Close immediately.
    if status in _TERMINAL_STATUSES:
        await _ws_close(send, 1000, "terminal-status")
        return

    # Poll loop. Each iteration is offloaded via ``asyncio.to_thread``
    # so the event loop stays responsive to other connections.
    elapsed = 0.0
    while elapsed < _WS_MAX_LIFETIME_SECONDS:
        try:
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            # The wrapper is tearing us down (client disconnect).
            raise
        elapsed += _POLL_INTERVAL_SECONDS

        try:
            status, progress = await asyncio.to_thread(_read_job_progress, job_id)
        except Exception:
            logger.debug("export-progress WS: poll iteration failed", exc_info=True)
            continue

        # Push only when the (current,total) tuple changes. Avoids
        # spamming idle frames while a long table is rendering.
        if progress is not None:
            current = progress.get("current")
            total = progress.get("total")
            if last_pushed != (current, total):
                try:
                    await send(
                        json.dumps(
                            {
                                "type": "progress",
                                "job_id": job_id,
                                "current": current,
                                "total": total,
                            },
                            default=str,
                        )
                    )
                    last_pushed = (current, total)
                except Exception:
                    logger.debug(
                        "export-progress WS: progress send failed; closing",
                        exc_info=True,
                    )
                    return

        if status in _TERMINAL_STATUSES:
            # Send a final terminal event so the client can render the
            # success/error UI without an extra HTTP round trip.
            try:
                await send(
                    json.dumps(
                        {
                            "type": "terminal",
                            "job_id": job_id,
                            "status": status,
                        },
                        default=str,
                    )
                )
            except Exception:
                logger.debug(
                    "export-progress WS: terminal send failed",
                    exc_info=True,
                )
            await _ws_close(send, 1000, "terminal-status")
            return

    # Lifetime budget exhausted. Tell the client politely; the HTTP
    # polling fallback will surface the "Vajab tähelepanu" warning.
    try:
        await send(
            json.dumps(
                {
                    "type": "timeout",
                    "job_id": job_id,
                    "message": "Eksport venib, jätkake HTTP-päringutega.",
                }
            )
        )
    except Exception:
        logger.debug("export-progress WS: timeout send failed", exc_info=True)
    await _ws_close(send, 1000, "lifetime-exceeded")


def register_export_progress_ws_routes(app: Any) -> None:
    """Mount the export-progress WS at ``/ws/drafts/export-progress``.

    Mirrors :func:`app.docs.websocket.register_draft_ws_routes`: a
    cookie-based JWT auth wrapper is installed per invocation, then
    the message body is delegated to :func:`ws_export_progress`. The
    heartbeat scope is also per invocation (post-#684 pattern).
    """
    _jwt_provider: list[JWTAuthProvider | None] = [None]

    def _get_jwt_provider() -> JWTAuthProvider | None:
        if _jwt_provider[0] is None:
            try:
                _jwt_provider[0] = JWTAuthProvider()
            except Exception:
                logger.error(
                    "export-progress WS: failed to construct JWTAuthProvider",
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

        # Heartbeat scoped to this handler invocation (post-#684 pattern).
        heartbeat = _start_heartbeat(send)
        try:
            await ws_export_progress(msg, send, auth_scope if auth_scope else None)
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except (asyncio.CancelledError, Exception):
                pass

    app.ws("/ws/drafts/export-progress")(_ws_handler)
