"""Extra HTTP handlers for the Vestlus polish sweep (issue #17 chat UX).

This module houses the handlers that complement the main conversation
view defined in :mod:`app.chat.routes`. The split keeps ``routes.py``
focused on the page-rendering paths and concentrates the chat mutation
+ export surface area here:

    POST   /chat/{conv_id}/pin
    POST   /chat/{conv_id}/archive
    POST   /chat/{conv_id}/rename
    POST   /chat/{conv_id}/fork
    POST   /chat/{conv_id}/messages/{msg_id}/pin
    POST   /chat/{conv_id}/messages/{msg_id}/feedback
    DELETE /chat/{conv_id}/messages/{msg_id}/feedback
    POST   /chat/{conv_id}/messages/{msg_id}/regenerate
    POST   /chat/{conv_id}/messages/{msg_id}/edit
    GET    /chat/{conv_id}/export.md
    GET    /chat/{conv_id}/export.docx
    GET    /chat/search
    GET    /api/me/usage

The handlers follow the same shape as ``app/chat/routes.py``:

    - :func:`require_auth` short-circuit
    - ``_parse_uuid`` + ``get_conversation`` + ``can_access_conversation``
      for cross-org rejection (404, never 403)
    - 204 + ``HX-Trigger`` for successful mutations
    - 303 / ``HX-Redirect`` for navigations
    - fire-and-forget audit via ``app.auth.audit.log_action``

CSRF note
---------
The app uses JWT-in-cookie auth; ``routes.py`` does **not** install a
CSRF middleware and neither does this module. If CSRF protection is
added later it will ship as beforeware across all mutating routes, at
which point these handlers pick it up automatically.

Models shim
-----------
Several helpers expected to live in :mod:`app.chat.models` (``set_conversation_pinned``,
``set_conversation_archived``, ``update_conversation_title``,
``set_message_pinned``, ``delete_messages_after``, ``fork_conversation``,
``list_conversations_for_user(search=...)``) are being authored in a
parallel wave. To avoid a hard coupling we try the import and fall back
to inline SQL that matches migration 017. Once the models module lands
the shims become no-ops via the import path.
"""

from __future__ import annotations

import logging
import re
import unicodedata
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from fasthtml.common import A, Div, Li, P, Ul
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from app.auth.audit import log_action
from app.auth.helpers import require_auth as _require_auth
from app.auth.policy import can_access_conversation
from app.chat.export import conversation_to_docx_bytes, conversation_to_markdown
from app.chat.models import (
    Conversation,
    Message,
    delete_messages_after,
    fork_conversation,
    get_conversation,
    list_conversations_for_user,
    list_messages,
    set_conversation_archived,
    set_conversation_pinned,
    set_message_pinned,
    update_conversation_title,
)
from app.chat.rate_limiter import get_user_quota, seconds_until_hourly_reset
from app.db import get_connection as _connect

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Small helpers duplicated from routes.py so we don't introduce a cycle
# ---------------------------------------------------------------------------


def _parse_uuid(raw: str) -> uuid.UUID | None:
    """Return a UUID parsed from *raw*, or ``None`` if invalid."""
    try:
        return uuid.UUID(raw)
    except (ValueError, TypeError):
        return None


def _is_htmx(req: Request) -> bool:
    return req.headers.get("HX-Request") == "true"


def _not_found_response() -> Response:
    """Generic 404 body for invalid/cross-org access.

    Handlers in this module answer machine targets (HTMX fragments, JSON,
    file downloads) rather than a full page so a terse plaintext 404 is
    the appropriate response. ``routes.py`` uses a themed page because
    those URLs are direct navigation targets.
    """
    return Response("Vestlust ei leitud", status_code=404, media_type="text/plain")


def _hx_trigger_response(event: str, status_code: int = 204) -> Response:
    """204 + ``HX-Trigger: <event>`` response used by most mutations."""
    return Response(status_code=status_code, headers={"HX-Trigger": event})


def _load_conversation_or_404(
    req: Request, conv_id: str
) -> tuple[uuid.UUID, Conversation] | Response:
    """Parse *conv_id*, load the row, and verify ownership.

    Returns ``(parsed_id, conversation)`` on success or a 404 Response
    otherwise. Any exception from the DB layer collapses into 404 because
    we deliberately don't leak existence to the caller.
    """
    parsed = _parse_uuid(conv_id)
    if parsed is None:
        return _not_found_response()

    auth = req.scope.get("auth")
    try:
        with _connect() as conn:
            conversation = get_conversation(conn, parsed)
    except Exception:
        logger.exception("Failed to load conversation %s", conv_id)
        return _not_found_response()

    if conversation is None:
        return _not_found_response()
    if not can_access_conversation(auth, conversation):
        return _not_found_response()
    return parsed, conversation


def _slugify(value: str, fallback: str = "chat") -> str:
    """Lowercase, ASCII-only slug for use in filenames.

    Drops diacritics via NFKD decomposition, collapses any run of
    non-alphanumeric characters to ``-``, and trims leading/trailing
    separators. Returns *fallback* if the result is empty.
    """
    if not value:
        return fallback
    normalised = unicodedata.normalize("NFKD", value)
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii")
    ascii_only = ascii_only.lower()
    ascii_only = re.sub(r"[^a-z0-9]+", "-", ascii_only)
    ascii_only = ascii_only.strip("-")
    return ascii_only or fallback


async def _form(req: Request) -> dict[str, str]:
    """Read the request's form payload into a plain dict of first values.

    ``starlette`` returns a :class:`FormData` multimap; for the single-
    value fields this module deals with a flat ``dict[str, str]`` is
    easier to work with. Missing fields are simply absent from the dict.
    """
    out: dict[str, str] = {}
    try:
        form = await req.form()
    except Exception:
        logger.exception("Failed to read form data")
        return out
    for key in form.keys():
        value = form.get(key)
        if isinstance(value, str):
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# Thin wrappers around ``app.chat.models`` helpers
# ---------------------------------------------------------------------------
#
# These wrappers exist purely as patch targets for the tests and to keep
# the call sites small. They delegate to the real helpers so the
# migration 017 contracts are the single source of truth.


def _set_conversation_pinned(conn: Any, conv_id: uuid.UUID, pinned: bool) -> None:
    set_conversation_pinned(conn, conv_id, pinned)


def _set_conversation_archived(conn: Any, conv_id: uuid.UUID, archived: bool) -> None:
    set_conversation_archived(conn, conv_id, archived)


def _update_conversation_title(conn: Any, conv_id: uuid.UUID, title: str) -> None:
    # Always pass ``is_custom=True`` here — the contract is "user renamed
    # the conversation", which the auto-title job must not overwrite.
    update_conversation_title(conn, conv_id, title, is_custom=True)


def _set_message_pinned(conn: Any, msg_id: uuid.UUID, pinned: bool) -> None:
    set_message_pinned(conn, msg_id, pinned)


def _delete_messages_after_pivot(conn: Any, conv_id: uuid.UUID, pivot_msg: Message) -> int:
    """Drop every row newer than the pivot in *conv_id*.

    The real :func:`delete_messages_after` takes a ``datetime`` boundary
    (that's the schema-level truth — there is no "delete after message X"
    primitive). The pivot ``Message`` is the anchor for both edit (the
    user message whose reply should be regenerated) and regenerate (the
    assistant reply's preceding user message); we look up its
    ``created_at`` and delegate.
    """
    return int(delete_messages_after(conn, conv_id, pivot_msg.created_at) or 0)


def _update_message_content(conn: Any, msg_id: uuid.UUID, new_content: str) -> None:
    """Update a user message's content.

    Writes to ``content_encrypted`` when :func:`app.storage.encrypt_text`
    is available (production) and falls back to the plaintext column
    otherwise (dev environments without an encryption key). The read
    path in :func:`_row_to_message` prefers the encrypted column, so the
    dual-write keeps decryption working after edits.

    Production safety: in ``APP_ENV=production`` (where
    :func:`app.config.is_stub_allowed` returns ``False``) an encryption
    failure re-raises instead of silently writing plaintext. Drafts are
    politically sensitive — a plaintext fallback in prod would leak the
    very data the encryption-at-rest policy exists to protect. In dev /
    test the plaintext fallback keeps the app runnable without a key.
    """
    from app.config import is_stub_allowed

    try:
        from app.storage import encrypt_text

        ciphertext = encrypt_text(new_content or "")
    except Exception:
        if not is_stub_allowed():
            logger.exception("encrypt_text failed in production — refusing plaintext fallback")
            raise
        logger.exception("encrypt_text failed — writing plaintext content (dev fallback)")
        conn.execute(
            "UPDATE messages SET content = %s, content_encrypted = NULL WHERE id = %s",
            (new_content, str(msg_id)),
        )
        return

    conn.execute(
        "UPDATE messages SET content = NULL, content_encrypted = %s WHERE id = %s",
        (ciphertext, str(msg_id)),
    )


def _fork_conversation(
    conn: Any,
    source_conv: Conversation,
    pivot_msg_id: uuid.UUID,
    user_id: uuid.UUID | str,
    org_id: uuid.UUID | str,
) -> uuid.UUID:
    """Wrapper around :func:`app.chat.models.fork_conversation`.

    Returns the new conversation's UUID. The models-level helper returns
    a :class:`Conversation` but the handler only needs the id for the
    redirect target.
    """
    new_conv = fork_conversation(
        conn,
        source_conv.id,
        pivot_msg_id,
        user_id=user_id,
        org_id=org_id,
    )
    return new_conv.id


# ---------------------------------------------------------------------------
# Conversation mutations
# ---------------------------------------------------------------------------


def _bool_from_form(raw: str | None, default: bool) -> bool:
    """Parse a string form field into a bool; ``"1"``/``"true"`` truthy."""
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


async def pin_conversation_handler(req: Request, conv_id: str):
    """POST /chat/{conv_id}/pin — toggle the conversation's is_pinned flag."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    loaded = _load_conversation_or_404(req, conv_id)
    if isinstance(loaded, Response):
        return loaded
    parsed, conversation = loaded

    # Optional "pinned=1|0" form field; default is toggle.
    form = await _form(req)
    raw_pinned = form.get("pinned")
    if raw_pinned is None:
        new_pinned = not bool(getattr(conversation, "is_pinned", False))
    else:
        new_pinned = _bool_from_form(raw_pinned, True)

    try:
        with _connect() as conn:
            _set_conversation_pinned(conn, parsed, new_pinned)
            conn.commit()
    except Exception:
        logger.exception("Failed to set pinned=%s on conversation %s", new_pinned, conv_id)
        return Response(status_code=500)

    log_action(
        auth.get("id"),
        "chat.conversation.pin",
        {"conversation_id": str(parsed), "pinned": new_pinned},
    )
    return _hx_trigger_response("chat:conversation-updated")


async def archive_conversation_handler(req: Request, conv_id: str):
    """POST /chat/{conv_id}/archive — toggle is_archived."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    loaded = _load_conversation_or_404(req, conv_id)
    if isinstance(loaded, Response):
        return loaded
    parsed, conversation = loaded

    form = await _form(req)
    raw_archived = form.get("archived")
    if raw_archived is None:
        new_archived = not bool(getattr(conversation, "is_archived", False))
    else:
        new_archived = _bool_from_form(raw_archived, True)

    try:
        with _connect() as conn:
            _set_conversation_archived(conn, parsed, new_archived)
            conn.commit()
    except Exception:
        logger.exception("Failed to set archived=%s on conversation %s", new_archived, conv_id)
        return Response(status_code=500)

    log_action(
        auth.get("id"),
        "chat.conversation.archive",
        {"conversation_id": str(parsed), "archived": new_archived},
    )

    if _is_htmx(req):
        return _hx_trigger_response("chat:conversation-updated")
    return RedirectResponse(url="/chat", status_code=303)


async def rename_conversation_handler(req: Request, conv_id: str):
    """POST /chat/{conv_id}/rename — set a user-chosen title."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    loaded = _load_conversation_or_404(req, conv_id)
    if isinstance(loaded, Response):
        return loaded
    parsed, _ = loaded

    form = await _form(req)
    title = (form.get("title") or "").strip()
    if not title:
        return Response(
            "Pealkiri ei saa olla tuhi.",
            status_code=400,
            media_type="text/plain",
        )
    title = title[:200]

    try:
        with _connect() as conn:
            _update_conversation_title(conn, parsed, title)
            conn.commit()
    except Exception:
        logger.exception("Failed to rename conversation %s", conv_id)
        return Response(status_code=500)

    log_action(
        auth.get("id"),
        "chat.conversation.rename",
        {"conversation_id": str(parsed), "title": title},
    )
    return _hx_trigger_response("chat:conversation-updated")


async def fork_conversation_handler(req: Request, conv_id: str):
    """POST /chat/{conv_id}/fork — branch a new conversation at a message.

    The caller supplies ``message_id`` in the form body: the new
    conversation gets every message up to and including that pivot, plus
    a ``(haru)`` suffix on the title.
    """
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    loaded = _load_conversation_or_404(req, conv_id)
    if isinstance(loaded, Response):
        return loaded
    parsed_conv, conversation = loaded

    form = await _form(req)
    pivot_raw = form.get("message_id", "")
    pivot = _parse_uuid(pivot_raw)
    if pivot is None:
        return Response("Puudub message_id.", status_code=400, media_type="text/plain")

    user_id = auth.get("id")
    if not user_id:
        return Response(status_code=400)

    try:
        with _connect() as conn:
            new_conv_id = _fork_conversation(
                conn, conversation, pivot, user_id, conversation.org_id
            )
            conn.commit()
    except Exception:
        logger.exception("Failed to fork conversation %s at %s", conv_id, pivot)
        return Response(status_code=500)

    log_action(
        auth.get("id"),
        "chat.conversation.fork",
        {
            "source_conversation_id": str(parsed_conv),
            "new_conversation_id": str(new_conv_id),
            "pivot_message_id": str(pivot),
        },
    )

    target = f"/chat/{new_conv_id}"
    if _is_htmx(req):
        return Response(status_code=204, headers={"HX-Redirect": target})
    return RedirectResponse(url=target, status_code=303)


# ---------------------------------------------------------------------------
# Message pin / feedback / edit / regenerate
# ---------------------------------------------------------------------------


def _load_message_in_conversation(
    conn: Any, conv_id: uuid.UUID, msg_id: uuid.UUID
) -> Message | None:
    """Fetch a specific message and verify it belongs to *conv_id*.

    Using ``list_messages`` + linear scan keeps the decryption logic
    centralised; chat transcripts are capped at O(100) messages so this
    is cheaper than a bespoke SELECT that would need to duplicate the
    encrypted-column handling.
    """
    for msg in list_messages(conn, conv_id):
        if msg.id == msg_id:
            return msg
    return None


async def pin_message_handler(req: Request, conv_id: str, msg_id: str):
    """POST /chat/{conv_id}/messages/{msg_id}/pin — toggle is_pinned."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    loaded = _load_conversation_or_404(req, conv_id)
    if isinstance(loaded, Response):
        return loaded
    parsed_conv, _ = loaded

    parsed_msg = _parse_uuid(msg_id)
    if parsed_msg is None:
        return _not_found_response()

    form = await _form(req)
    raw_pinned = form.get("pinned")

    try:
        with _connect() as conn:
            msg = _load_message_in_conversation(conn, parsed_conv, parsed_msg)
            if msg is None:
                return _not_found_response()
            if raw_pinned is None:
                new_pinned = not bool(getattr(msg, "is_pinned", False))
            else:
                new_pinned = _bool_from_form(raw_pinned, True)
            _set_message_pinned(conn, parsed_msg, new_pinned)
            conn.commit()
    except Exception:
        logger.exception("Failed to set pinned=%s on message %s", raw_pinned, msg_id)
        return Response(status_code=500)

    log_action(
        auth.get("id"),
        "chat.message.pin",
        {
            "conversation_id": str(parsed_conv),
            "message_id": str(parsed_msg),
            "pinned": new_pinned,
        },
    )
    return _hx_trigger_response("chat:message-updated")


# ---------------------------------------------------------------------------
# Feedback (POST upsert / DELETE)
# ---------------------------------------------------------------------------


def _feedback_counts(conn: Any, msg_id: uuid.UUID) -> tuple[int, int]:
    """Return ``(up, down)`` counts for a message."""
    from app.chat.feedback import feedback_counts

    try:
        counts = feedback_counts(conn, msg_id)
    except Exception:
        logger.exception("feedback_counts failed for msg_id=%s", msg_id)
        return 0, 0
    return int(counts[0]), int(counts[1])


def _upsert_feedback(
    conn: Any, msg_id: uuid.UUID, user_id: str, rating: int, comment: str | None
) -> None:
    from app.chat.feedback import upsert_feedback

    upsert_feedback(
        conn,
        message_id=msg_id,
        user_id=user_id,
        rating=rating,
        comment=comment,
    )


def _delete_feedback_row(conn: Any, msg_id: uuid.UUID, user_id: str) -> None:
    from app.chat.feedback import delete_feedback

    delete_feedback(conn, msg_id, user_id)


async def _feedback_post(req: Request, parsed_conv: uuid.UUID, parsed_msg: uuid.UUID, auth: Any):
    form = await _form(req)
    raw_rating = (form.get("rating") or "").strip()
    try:
        rating = int(raw_rating)
    except ValueError:
        return JSONResponse({"error": "invalid_rating"}, status_code=400)
    if rating not in (1, -1):
        return JSONResponse({"error": "invalid_rating"}, status_code=400)

    comment = (form.get("comment") or "").strip() or None
    user_id = str(auth.get("id") or "")
    if not user_id:
        return Response(status_code=400)

    try:
        with _connect() as conn:
            # Make sure the message belongs to the conversation we just
            # authz-checked — otherwise a caller with a known message id
            # from another user's chat could leave feedback on it.
            if _load_message_in_conversation(conn, parsed_conv, parsed_msg) is None:
                return _not_found_response()
            _upsert_feedback(conn, parsed_msg, user_id, rating, comment)
            conn.commit()
            up, down = _feedback_counts(conn, parsed_msg)
    except Exception:
        logger.exception("Failed to upsert feedback for %s", parsed_msg)
        return Response(status_code=500)

    log_action(
        user_id,
        "chat.message.feedback",
        {
            "conversation_id": str(parsed_conv),
            "message_id": str(parsed_msg),
            "rating": rating,
            "has_comment": bool(comment),
        },
    )
    return JSONResponse({"up": up, "down": down, "user_rating": rating})


def _feedback_delete(parsed_conv: uuid.UUID, parsed_msg: uuid.UUID, auth: Any):
    user_id = str(auth.get("id") or "")
    if not user_id:
        return Response(status_code=400)
    try:
        with _connect() as conn:
            if _load_message_in_conversation(conn, parsed_conv, parsed_msg) is None:
                return _not_found_response()
            _delete_feedback_row(conn, parsed_msg, user_id)
            conn.commit()
            up, down = _feedback_counts(conn, parsed_msg)
    except Exception:
        logger.exception("Failed to delete feedback for %s", parsed_msg)
        return Response(status_code=500)

    log_action(
        user_id,
        "chat.message.feedback.delete",
        {"conversation_id": str(parsed_conv), "message_id": str(parsed_msg)},
    )
    return JSONResponse({"up": up, "down": down, "user_rating": None})


async def feedback_handler(req: Request, conv_id: str, msg_id: str):
    """POST / DELETE /chat/{conv_id}/messages/{msg_id}/feedback.

    Dispatches on ``req.method`` so a single handler registration covers
    both verbs (some FastHTML/Starlette versions require this pattern).
    """
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    loaded = _load_conversation_or_404(req, conv_id)
    if isinstance(loaded, Response):
        return loaded
    parsed_conv, _ = loaded

    parsed_msg = _parse_uuid(msg_id)
    if parsed_msg is None:
        return _not_found_response()

    if req.method.upper() == "DELETE":
        return _feedback_delete(parsed_conv, parsed_msg, auth)
    return await _feedback_post(req, parsed_conv, parsed_msg, auth)


async def regenerate_handler(req: Request, conv_id: str, msg_id: str):
    """POST /chat/{conv_id}/messages/{msg_id}/regenerate.

    Discards every message *after* the pivot and returns 204. The actual
    replay is driven by the client: after this call succeeds the page
    re-sends the preceding user message through the existing chat
    WebSocket, which re-enters :mod:`app.chat.orchestrator` and streams a
    fresh assistant turn. Keeping the regenerate trigger out of the WS
    protocol means the HTTP transaction — delete + audit — cannot race
    against an in-flight stream.
    """
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    loaded = _load_conversation_or_404(req, conv_id)
    if isinstance(loaded, Response):
        return loaded
    parsed_conv, _ = loaded

    parsed_msg = _parse_uuid(msg_id)
    if parsed_msg is None:
        return _not_found_response()

    try:
        with _connect() as conn:
            pivot = _load_message_in_conversation(conn, parsed_conv, parsed_msg)
            if pivot is None:
                return _not_found_response()
            deleted = _delete_messages_after_pivot(conn, parsed_conv, pivot)
            conn.commit()
    except Exception:
        logger.exception("Failed to regenerate from message %s", msg_id)
        return Response(status_code=500)

    log_action(
        auth.get("id"),
        "chat.message.regenerate",
        {
            "conversation_id": str(parsed_conv),
            "pivot_message_id": str(parsed_msg),
            "deleted_count": deleted,
        },
    )
    return _hx_trigger_response("chat:message-regenerate")


async def edit_message_handler(req: Request, conv_id: str, msg_id: str):
    """POST /chat/{conv_id}/messages/{msg_id}/edit.

    Only ``role='user'`` messages can be edited. Downstream messages
    (every message strictly after the edited one) are dropped so the
    assistant can regenerate a response to the new prompt.
    """
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    loaded = _load_conversation_or_404(req, conv_id)
    if isinstance(loaded, Response):
        return loaded
    parsed_conv, _ = loaded

    parsed_msg = _parse_uuid(msg_id)
    if parsed_msg is None:
        return _not_found_response()

    form = await _form(req)
    new_content = (form.get("content") or "").strip()
    if not new_content:
        return Response("Sisu ei saa olla tuhi.", status_code=400, media_type="text/plain")

    try:
        with _connect() as conn:
            msg = _load_message_in_conversation(conn, parsed_conv, parsed_msg)
            if msg is None:
                return _not_found_response()
            if msg.role != "user":
                return Response(
                    "Vaid kasutaja sonumeid saab muuta.",
                    status_code=400,
                    media_type="text/plain",
                )
            _update_message_content(conn, parsed_msg, new_content)
            _delete_messages_after_pivot(conn, parsed_conv, msg)
            conn.commit()
    except Exception:
        logger.exception("Failed to edit message %s", msg_id)
        return Response(status_code=500)

    log_action(
        auth.get("id"),
        "chat.message.edit",
        {
            "conversation_id": str(parsed_conv),
            "message_id": str(parsed_msg),
        },
    )
    return _hx_trigger_response("chat:message-edited")


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _export_filename(conversation: Conversation, extension: str) -> str:
    date_str = ""
    created = conversation.created_at
    if isinstance(created, datetime):
        date_str = created.strftime("%Y-%m-%d")
    slug = _slugify(conversation.title)
    prefix = f"vestlus-{slug}"
    if date_str:
        prefix = f"{prefix}-{date_str}"
    return f"{prefix}.{extension}"


def _content_disposition(filename: str) -> str:
    """Build a Content-Disposition header that handles non-ASCII titles.

    ``filename`` is the *already-slugified* filename (e.g.
    ``"vestlus-pealkiri-2026-04-14.md"``) — :func:`_export_filename`
    runs it through :func:`_slugify` on the stem before appending the
    extension, so re-slugifying here would collapse the dot separator
    into a hyphen (``"vestlus-pealkiri-2026-04-14-md"``). The ASCII
    variant therefore uses *filename* as-is; only the RFC 5987 UTF-8
    variant needs percent-encoding for transport.
    """
    # RFC 5987 escaping for the UTF-8 variant.
    from urllib.parse import quote

    return f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"


def export_md_handler(req: Request, conv_id: str):
    """GET /chat/{conv_id}/export.md — download the transcript as Markdown."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect

    loaded = _load_conversation_or_404(req, conv_id)
    if isinstance(loaded, Response):
        return loaded
    parsed_conv, conversation = loaded

    try:
        with _connect() as conn:
            messages = list_messages(conn, parsed_conv)
    except Exception:
        logger.exception("Failed to load messages for export %s", conv_id)
        return Response(status_code=500)

    markdown = conversation_to_markdown(conversation, messages)
    filename = _export_filename(conversation, "md")
    return Response(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": _content_disposition(filename)},
    )


def export_docx_handler(req: Request, conv_id: str):
    """GET /chat/{conv_id}/export.docx — download the transcript as .docx."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect

    loaded = _load_conversation_or_404(req, conv_id)
    if isinstance(loaded, Response):
        return loaded
    parsed_conv, conversation = loaded

    try:
        with _connect() as conn:
            messages = list_messages(conn, parsed_conv)
    except Exception:
        logger.exception("Failed to load messages for docx export %s", conv_id)
        return Response(status_code=500)

    try:
        payload = conversation_to_docx_bytes(conversation, messages)
    except Exception:
        logger.exception("Failed to render docx for conversation %s", conv_id)
        return Response(status_code=500)

    filename = _export_filename(conversation, "docx")
    return Response(
        content=payload,
        media_type=("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        headers={"Content-Disposition": _content_disposition(filename)},
    )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def _search_conversations_impl(
    conn: Any, user_id: str, term: str, limit: int = 25
) -> list[Conversation]:
    """Return conversations whose title matches *term* (case-insensitive).

    Delegates to :func:`list_conversations_for_user` with the ``search``
    kwarg which performs an ILIKE substring match against ``title``.
    Full-text search on message bodies is out of scope for this sweep;
    that would need a tsvector column migration.
    """
    try:
        return list_conversations_for_user(conn, user_id, limit=limit, search=term)
    except Exception:
        logger.exception("Search failed for term=%r user=%s", term, user_id)
        return []


def _render_search_results(conversations: list[Conversation], term: str) -> Any:
    """Render an HTML fragment suitable for ``hx-target`` replacement."""
    if not conversations:
        return Div(
            P(f'Otsingule "{term}" ei vastanud ukski vestlus.', cls="muted-text"),
            id="chat-search-results",
            cls="chat-search-results chat-search-empty",
        )
    items = []
    for conv in conversations:
        items.append(
            Li(
                A(
                    conv.title or "Vestlus",
                    href=f"/chat/{conv.id}",
                    cls="chat-search-result-link",
                ),
                cls="chat-search-result-item",
            )
        )
    return Div(
        Ul(*items, cls="chat-search-result-list"),
        id="chat-search-results",
        cls="chat-search-results",
    )


def search_conversations_handler(req: Request):
    """GET /chat/search?q=<term>.

    HTMX callers get an HTML fragment keyed on ``#chat-search-results``;
    plain browsers are redirected to ``/chat?q=<term>`` so the main list
    renderer (owned by ``routes.py``) can handle the query parameter.
    """
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    term = (req.query_params.get("q") or "").strip()
    if not _is_htmx(req):
        target = f"/chat?q={term}" if term else "/chat"
        return RedirectResponse(url=target, status_code=303)

    if not term:
        return _render_search_results([], "")

    user_id = auth.get("id")
    if not user_id:
        return _render_search_results([], term)

    try:
        with _connect() as conn:
            conversations = _search_conversations_impl(conn, str(user_id), term)
    except Exception:
        logger.exception("Failed to search conversations for term=%r", term)
        conversations = []

    return _render_search_results(conversations, term)


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


def _decimal_to_str(value: Decimal) -> str:
    """Serialize a :class:`Decimal` for JSON output with 2dp precision.

    Uses ``ROUND_HALF_UP`` rather than Decimal's default ``ROUND_HALF_EVEN``
    so the rendered values match how humans read financial amounts
    (``12.345`` → ``12.35``).
    """
    from decimal import ROUND_HALF_UP

    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _percentage(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    pct = (numerator / denominator) * 100.0
    if pct < 0:
        pct = 0.0
    return round(pct, 1)


def user_usage_handler(req: Request):
    """GET /api/me/usage — JSON snapshot of per-user quota state."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    user_id = auth.get("id")
    org_id = auth.get("org_id")
    if not user_id or not org_id:
        return JSONResponse({"error": "missing_identity"}, status_code=400)

    quota = get_user_quota(user_id, org_id)
    seconds_until_reset = seconds_until_hourly_reset(user_id)

    messages_remaining = max(0, quota.message_limit_per_hour - quota.messages_this_hour)
    cost_remaining = quota.cost_limit_per_month_usd - quota.cost_this_month_usd
    if cost_remaining < Decimal("0"):
        cost_remaining = Decimal("0")

    messages_pct = _percentage(
        float(quota.messages_this_hour), float(quota.message_limit_per_hour)
    )
    cost_pct = _percentage(float(quota.cost_this_month_usd), float(quota.cost_limit_per_month_usd))

    payload = {
        "messages_this_hour": quota.messages_this_hour,
        "message_limit_per_hour": quota.message_limit_per_hour,
        "messages_remaining": messages_remaining,
        "cost_this_month_usd": _decimal_to_str(quota.cost_this_month_usd),
        "cost_limit_per_month_usd": _decimal_to_str(quota.cost_limit_per_month_usd),
        "cost_remaining_usd": _decimal_to_str(cost_remaining),
        "cost_alert_threshold_usd": _decimal_to_str(quota.cost_alert_threshold_usd),
        "seconds_until_reset": seconds_until_reset,
        "percentages": {
            "messages": messages_pct,
            "cost": cost_pct,
        },
    }
    # Every Decimal has already been stringified via ``_decimal_to_str``;
    # the payload is now JSON-native so no dump/load round trip is needed.
    return JSONResponse(payload)


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_chat_handler_routes(rt) -> None:  # type: ignore[no-untyped-def]
    """Mount every handler in this module on the FastHTML decorator *rt*.

    ``app.chat.routes.register_chat_routes`` calls this after registering
    its own page-level routes. Keeping the registrations split means the
    handlers module has no transitive dependency on the UI primitives
    imported by ``routes.py``.
    """
    rt("/chat/{conv_id}/pin", methods=["POST"])(pin_conversation_handler)
    rt("/chat/{conv_id}/archive", methods=["POST"])(archive_conversation_handler)
    rt("/chat/{conv_id}/rename", methods=["POST"])(rename_conversation_handler)
    rt("/chat/{conv_id}/fork", methods=["POST"])(fork_conversation_handler)

    rt("/chat/{conv_id}/messages/{msg_id}/pin", methods=["POST"])(pin_message_handler)
    rt(
        "/chat/{conv_id}/messages/{msg_id}/feedback",
        methods=["POST", "DELETE"],
    )(feedback_handler)
    rt("/chat/{conv_id}/messages/{msg_id}/regenerate", methods=["POST"])(regenerate_handler)
    rt("/chat/{conv_id}/messages/{msg_id}/edit", methods=["POST"])(edit_message_handler)

    rt("/chat/{conv_id}/export.md", methods=["GET"])(export_md_handler)
    rt("/chat/{conv_id}/export.docx", methods=["GET"])(export_docx_handler)

    rt("/chat/search", methods=["GET"])(search_conversations_handler)
    rt("/api/me/usage", methods=["GET"])(user_usage_handler)
