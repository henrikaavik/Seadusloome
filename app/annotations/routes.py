"""FastHTML routes for the Phase 4 Annotation system.

Route map:

    POST   /api/annotations                — create annotation
    GET    /api/annotations                — list annotations for a target
    POST   /api/annotations/{id}/reply     — create reply on annotation
    POST   /api/annotations/{id}/resolve   — mark annotation resolved
    DELETE /api/annotations/{id}           — delete annotation

All routes require authentication and are org-scoped. Cross-org access
returns 404 to avoid leaking annotation existence.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request
from starlette.responses import Response

from app.annotations.audit import (
    log_annotation_create,
    log_annotation_delete,
    log_annotation_reply,
    log_annotation_resolve,
    log_row_annotation_create,
    log_row_annotation_message,
    log_row_annotation_reopen,
    log_row_annotation_resolve,
)
from app.annotations.models import (
    VALID_ROW_KINDS,
    Annotation,
    create_annotation,
    create_reply,
    create_row_annotation,
    delete_annotation,
    get_annotation,
    list_annotations_for_target,
    list_annotations_for_version_row,
    list_replies,
    reopen_row_thread,
    resolve_annotation,
    resolve_row_thread,
)
from app.annotations.row_keys import decode_row_key, safe_row_key
from app.auth.helpers import require_auth as _require_auth
from app.auth.provider import UserDict
from app.auth.users import get_user
from app.db import get_connection as _connect
from app.notifications.wire import notify_annotation_reply
from app.ui.primitives.badge import Badge
from app.ui.primitives.button import Button as UiButton
from app.ui.surfaces.alert import Alert
from app.ui.surfaces.annotation_popover import AnnotationPopover
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.time import format_tallinn

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_uuid(raw: str) -> uuid.UUID | None:
    """Return a ``UUID`` parsed from *raw*, or ``None`` if invalid."""
    try:
        return uuid.UUID(raw)
    except (ValueError, TypeError):
        return None


def _format_timestamp(value: Any) -> str:
    """Render a ``datetime`` in Europe/Tallinn (see app.ui.time)."""
    return format_tallinn(value)


def _user_display_name(user_id: uuid.UUID) -> str:
    """Return the full name for a user, or a fallback string."""
    user = get_user(str(user_id))
    if user and user.get("full_name"):
        return user["full_name"]
    return "Tundmatu kasutaja"


def _load_annotations_with_replies(
    target_type: str,
    target_id: str,
    org_id: str,
) -> list[dict[str, Any]]:
    """Load annotations + their replies, enriched with user display names.

    Returns a list of dicts with keys:
        annotation: Annotation
        user_name: str
        replies: list[dict] (each with reply: AnnotationReply, user_name: str)
    """
    try:
        with _connect() as conn:
            annotations = list_annotations_for_target(conn, target_type, target_id, org_id)
            result: list[dict[str, Any]] = []
            for ann in annotations:
                replies_raw = list_replies(conn, ann.id)
                enriched_replies = [
                    {
                        "reply": r,
                        "user_name": _user_display_name(r.user_id),
                    }
                    for r in replies_raw
                ]
                result.append(
                    {
                        "annotation": ann,
                        "user_name": _user_display_name(ann.user_id),
                        "replies": enriched_replies,
                    }
                )
            return result
    except Exception:
        logger.exception(
            "Failed to load annotations for target_type=%s target_id=%s",
            target_type,
            target_id,
        )
        return []


# ---------------------------------------------------------------------------
# Fragment renderers
# ---------------------------------------------------------------------------


def _annotation_item(
    ann: Annotation,
    user_name: str,
    replies: list[dict[str, Any]],
    auth: UserDict,
) -> Any:
    """Render a single annotation with its reply thread."""
    resolved_cls = " annotation-resolved" if ann.resolved else ""

    # Header: user name + timestamp
    header = Div(  # noqa: F405
        Strong(user_name, cls="annotation-author"),  # noqa: F405
        Span(  # noqa: F405
            _format_timestamp(ann.created_at),
            cls="annotation-timestamp",
        ),
        cls="annotation-header",
    )

    # Content
    content = P(ann.content, cls="annotation-content")  # noqa: F405

    # Resolution status
    status_el = None
    if ann.resolved:
        resolved_name = _user_display_name(ann.resolved_by) if ann.resolved_by else "—"
        status_el = Div(  # noqa: F405
            Span(  # noqa: F405
                f"Lahendatud: {resolved_name} ({_format_timestamp(ann.resolved_at)})",
                cls="annotation-resolved-info",
            ),
            cls="annotation-status",
        )

    # Actions (resolve + delete)
    actions: list[Any] = []
    if not ann.resolved:
        actions.append(
            Button(  # noqa: F405
                "Lahenda",
                hx_post=f"/api/annotations/{ann.id}/resolve",
                hx_target=f"#annotation-thread-{ann.id}",
                hx_swap="outerHTML",
                cls="btn btn-ghost btn-sm annotation-resolve-btn",
            )
        )
    # Only the author or an admin can delete
    if str(ann.user_id) == str(auth.get("id")) or auth.get("role") == "admin":
        actions.append(
            Button(  # noqa: F405
                "Kustuta",
                hx_delete=f"/api/annotations/{ann.id}",
                hx_target=f"#annotation-thread-{ann.id}",
                hx_swap="outerHTML",
                hx_confirm="Kas olete kindel, et soovite selle märkuse kustutada?",
                cls="btn btn-ghost btn-sm annotation-delete-btn",
            )
        )

    actions_div = Div(*actions, cls="annotation-actions") if actions else None  # noqa: F405

    # Replies
    reply_items: list[Any] = []
    for r in replies:
        reply = r["reply"]
        reply_items.append(
            Div(  # noqa: F405
                Div(  # noqa: F405
                    Strong(r["user_name"], cls="annotation-author"),  # noqa: F405
                    Span(  # noqa: F405
                        _format_timestamp(reply.created_at),
                        cls="annotation-timestamp",
                    ),
                    cls="annotation-header",
                ),
                P(reply.content, cls="annotation-content"),  # noqa: F405
                cls="annotation-reply",
            )
        )

    # Reply form
    reply_form = Form(  # noqa: F405
        Textarea(  # noqa: F405
            name="content",
            placeholder="Kirjutage vastus...",
            rows="2",
            cls="annotation-reply-input",
            required=True,
        ),
        Button(  # noqa: F405
            "Vasta",
            type="submit",
            cls="btn btn-secondary btn-sm",
        ),
        hx_post=f"/api/annotations/{ann.id}/reply",
        hx_target=f"#annotation-thread-{ann.id}",
        hx_swap="outerHTML",
        cls="annotation-reply-form",
    )

    children: list[Any] = [header, content]
    if status_el:
        children.append(status_el)
    if actions_div:
        children.append(actions_div)
    if reply_items:
        children.append(Div(*reply_items, cls="annotation-replies"))  # noqa: F405
    children.append(reply_form)

    return Div(  # noqa: F405
        *children,
        id=f"annotation-thread-{ann.id}",
        cls=f"annotation-thread{resolved_cls}",
    )


def _annotation_list_fragment(
    target_type: str,
    target_id: str,
    auth: UserDict,
) -> Any:
    """Render the full annotation popover content for a target."""
    org_id = auth.get("org_id") or ""
    enriched = _load_annotations_with_replies(target_type, target_id, org_id)

    items: list[Any] = []
    for entry in enriched:
        items.append(
            _annotation_item(
                entry["annotation"],
                entry["user_name"],
                entry["replies"],
                auth,
            )
        )

    return AnnotationPopover(
        target_type=target_type,
        target_id=target_id,
        annotations=items,
        auth=auth,
    )


# ---------------------------------------------------------------------------
# GET /api/annotations
# ---------------------------------------------------------------------------


def list_annotations_handler(req: Request):
    """GET /api/annotations?target_type=X&target_id=Y — annotation list fragment."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    target_type = req.query_params.get("target_type", "")
    target_id = req.query_params.get("target_id", "")

    if not target_type or not target_id:
        return Div(  # noqa: F405
            P("Puuduvad parameetrid.", cls="muted-text"),  # noqa: F405
            cls="annotation-popover",
        )

    return _annotation_list_fragment(target_type, target_id, auth)


# ---------------------------------------------------------------------------
# POST /api/annotations
# ---------------------------------------------------------------------------


async def create_annotation_handler(req: Request):
    """POST /api/annotations — create a new annotation.

    Accepts JSON body: {target_type, target_id, content, target_metadata?}.
    Returns the updated annotation list fragment for the target.
    """
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    org_id = auth.get("org_id")
    if not org_id:
        return Response(status_code=403)

    # Accept both JSON and form data
    content_type = req.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = await req.json()
        except Exception:
            return Div(  # noqa: F405
                P("Vigane JSON.", cls="muted-text"),  # noqa: F405
                cls="annotation-popover",
            )
    else:
        form = await req.form()
        body = {
            "target_type": form.get("target_type", ""),
            "target_id": form.get("target_id", ""),
            "content": form.get("content", ""),
            "target_metadata": None,
        }

    target_type = str(body.get("target_type", ""))
    target_id = str(body.get("target_id", ""))
    content = str(body.get("content", "")).strip()
    target_metadata = body.get("target_metadata")

    if not target_type or not target_id or not content:
        return Div(  # noqa: F405
            P("Kõik väljad on kohustuslikud.", cls="muted-text"),  # noqa: F405
            cls="annotation-popover",
        )

    try:
        with _connect() as conn:
            annotation = create_annotation(
                conn,
                user_id=auth["id"],
                org_id=org_id,
                target_type=target_type,
                target_id=target_id,
                content=content,
                target_metadata=target_metadata if isinstance(target_metadata, dict) else None,
            )
            conn.commit()
    except ValueError as exc:
        return Div(  # noqa: F405
            P(str(exc), cls="muted-text"),  # noqa: F405
            cls="annotation-popover",
        )
    except Exception:
        logger.exception("Failed to create annotation")
        return Div(  # noqa: F405
            P("Märkuse loomine ebaõnnestus.", cls="muted-text"),  # noqa: F405
            cls="annotation-popover",
        )

    log_annotation_create(auth["id"], annotation.id, target_type, target_id)

    return _annotation_list_fragment(target_type, target_id, auth)


# ---------------------------------------------------------------------------
# POST /api/annotations/{id}/reply
# ---------------------------------------------------------------------------


async def reply_annotation_handler(req: Request, id: str):
    """POST /api/annotations/{id}/reply — add a reply to an annotation."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    parsed = _parse_uuid(id)
    if parsed is None:
        return Response(status_code=404)

    # Load the annotation for org-scoping check
    try:
        with _connect() as conn:
            annotation = get_annotation(conn, parsed)
    except Exception:
        logger.exception("Failed to load annotation %s for reply", id)
        return Response(status_code=404)

    if annotation is None:
        return Response(status_code=404)

    if str(annotation.org_id) != str(auth.get("org_id")):
        return Response(status_code=404)

    # Parse body
    content_type = req.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = await req.json()
        except Exception:
            return Response(status_code=400)
        content = str(body.get("content", "")).strip()
    else:
        form = await req.form()
        content = str(form.get("content", "")).strip()

    if not content:
        return Response(status_code=400)

    try:
        with _connect() as conn:
            reply = create_reply(conn, parsed, auth["id"], content)
            conn.commit()
    except Exception:
        logger.exception("Failed to create reply for annotation %s", id)
        return Response(status_code=500)

    log_annotation_reply(auth["id"], annotation.id, reply.id)
    notify_annotation_reply(annotation, reply)

    # Return just the updated thread item so the outerHTML swap on
    # #annotation-thread-{id} replaces only that thread, not the whole popover.
    with _connect() as conn:
        updated_ann = get_annotation(conn, parsed)
        if updated_ann is None:
            return Response(status_code=404)
        replies_raw = list_replies(conn, updated_ann.id)
    enriched_replies = [
        {"reply": r, "user_name": _user_display_name(r.user_id)} for r in replies_raw
    ]
    return _annotation_item(
        updated_ann, _user_display_name(updated_ann.user_id), enriched_replies, auth
    )


# ---------------------------------------------------------------------------
# POST /api/annotations/{id}/resolve
# ---------------------------------------------------------------------------


def resolve_annotation_handler(req: Request, id: str):
    """POST /api/annotations/{id}/resolve — mark annotation as resolved."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    parsed = _parse_uuid(id)
    if parsed is None:
        return Response(status_code=404)

    try:
        with _connect() as conn:
            annotation = get_annotation(conn, parsed)
    except Exception:
        logger.exception("Failed to load annotation %s for resolve", id)
        return Response(status_code=404)

    if annotation is None:
        return Response(status_code=404)

    if str(annotation.org_id) != str(auth.get("org_id")):
        return Response(status_code=404)

    # Only the author or an admin can resolve
    if str(annotation.user_id) != str(auth.get("id")) and auth.get("role") != "admin":
        return Response(status_code=403)

    try:
        with _connect() as conn:
            updated = resolve_annotation(conn, parsed, auth["id"])
            conn.commit()
    except Exception:
        logger.exception("Failed to resolve annotation %s", id)
        return Response(status_code=500)

    if updated is None:
        return Response(status_code=404)

    log_annotation_resolve(auth["id"], annotation.id)

    # Return just the updated thread item so the outerHTML swap on
    # #annotation-thread-{id} replaces only that thread, not the whole popover.
    with _connect() as conn:
        updated_ann = get_annotation(conn, parsed)
        if updated_ann is None:
            return Response(status_code=404)
        replies_raw = list_replies(conn, updated_ann.id)
    enriched_replies = [
        {"reply": r, "user_name": _user_display_name(r.user_id)} for r in replies_raw
    ]
    return _annotation_item(
        updated_ann, _user_display_name(updated_ann.user_id), enriched_replies, auth
    )


# ---------------------------------------------------------------------------
# DELETE /api/annotations/{id}
# ---------------------------------------------------------------------------


def delete_annotation_handler(req: Request, id: str):
    """DELETE /api/annotations/{id} — delete an annotation."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    parsed = _parse_uuid(id)
    if parsed is None:
        return Response(status_code=404)

    try:
        with _connect() as conn:
            annotation = get_annotation(conn, parsed)
    except Exception:
        logger.exception("Failed to load annotation %s for delete", id)
        return Response(status_code=404)

    if annotation is None:
        return Response(status_code=404)

    if str(annotation.org_id) != str(auth.get("org_id")):
        return Response(status_code=404)

    # Only the author or an admin can delete
    if str(annotation.user_id) != str(auth.get("id")) and auth.get("role") != "admin":
        return Response(status_code=403)

    try:
        with _connect() as conn:
            delete_annotation(conn, parsed)
            conn.commit()
    except Exception:
        logger.exception("Failed to delete annotation %s", id)
        return Response(status_code=500)

    log_annotation_delete(auth["id"], annotation.id)

    # Return empty 200 with HX-Trigger so the client can refresh if needed.
    # The annotation thread div will be removed by HTMX's hx_swap="outerHTML"
    # with an empty response body.
    return Response(
        content="",
        status_code=200,
        headers={"HX-Trigger": "annotationDeleted"},
    )


# ---------------------------------------------------------------------------
# §9.4 Version-scoped row-annotation routes (PR-B)
# ---------------------------------------------------------------------------
#
# Route map:
#   GET    /annotations/version/{draft_version_id}/{row_kind}/{row_key}
#   POST   /annotations/version/{draft_version_id}/{row_kind}/{row_key}/messages
#   POST   /annotations/version/{draft_version_id}/{row_kind}/{row_key}/resolve
#   POST   /annotations/version/{draft_version_id}/{row_kind}/{row_key}/reopen
#
# All four handlers share an ACL preamble enforced by
# ``_load_draft_version_or_404``: parse the ``draft_version_id`` UUID, JOIN
# ``draft_versions`` → ``drafts`` to read the owning org_id, and assert it
# matches the caller's ``auth['org_id']``. Any mismatch returns 404 (NOT
# 403) so cross-org probing cannot enumerate version IDs.
#
# row_kind is validated against ``VALID_ROW_KINDS`` at the handler boundary
# so a malformed value short-circuits before any DB call.
#
# #773 / #781 follow-up: row_key arrives base64url-encoded for every
# row kind, including entity / EU rows whose raw values are ontology URIs
# (and sometimes contain literal ``%XX`` substrings, e.g. CELEX). The
# route uses Starlette's ``:path`` converter so the matcher accepts the
# whole opaque segment, and each handler calls :func:`decode_row_key` to
# recover the raw row key before any DB / renderer work. The encoder /
# decoder pair is the only encode/decode the app performs — base64url
# uses ``[A-Za-z0-9_-]`` so no transport layer (httpx, Starlette
# TestClient, uvicorn, reverse proxies) mutates the value in flight.
# ---------------------------------------------------------------------------


# Side-panel fragment container id; PR-C will render this once on the report
# page and the routes below all swap into it.
_SIDE_PANEL_ID = "annotation-side-panel"

# Estonian labels for the four row_kind types — used in the panel header
# and the PR-C wiring.
_ROW_KIND_LABELS: dict[str, str] = {
    "entity": "Olem",
    "conflict": "Vastuolu",
    "eu": "EL-i õigusakt",
    "gap": "Õiguslünk",
}


def _validate_row_kind(row_kind: str) -> bool:
    """Return True iff *row_kind* is one of the §9.4 whitelist values."""
    return row_kind in VALID_ROW_KINDS


def _load_draft_version_or_404(
    draft_version_id: str,
    auth: UserDict,
) -> tuple[uuid.UUID, str] | Response:
    """Resolve a draft_version_id to (uuid, org_id) or return a 404 Response.

    Joins ``draft_versions`` → ``drafts`` to read ``drafts.org_id`` and
    asserts equality with ``auth['org_id']``. The ACL pattern matches
    ``app.docs._helpers.resolve_draft``: cross-org accesses return 404 so a
    caller cannot probe for the existence of out-of-org versions.

    Returns:
        On success: ``(version_uuid, owning_org_id_str)``.
        On any failure (parse error, missing row, cross-org): a 404
        ``Response`` ready to return verbatim from the route handler.
    """
    parsed = _parse_uuid(draft_version_id)
    if parsed is None:
        return Response(status_code=404)

    caller_org = str(auth.get("org_id") or "")
    if not caller_org:
        return Response(status_code=404)

    try:
        with _connect() as conn:
            row = conn.execute(
                """
                SELECT d.org_id
                FROM draft_versions dv
                JOIN drafts d ON d.id = dv.draft_id
                WHERE dv.id = %s
                """,
                (str(parsed),),
            ).fetchone()
    except Exception:
        logger.exception("Failed to load draft_version=%s for ACL check", draft_version_id)
        return Response(status_code=404)

    if row is None:
        return Response(status_code=404)

    owning_org = str(row[0]) if row[0] is not None else ""
    if owning_org != caller_org:
        # 404 not 403 — never leak that the version exists.
        return Response(status_code=404)

    return parsed, owning_org


async def _read_content(req: Request) -> tuple[str, str | None]:
    """Pull the ``content`` field from JSON or form POST body.

    Returns ``(content, error)`` where ``error`` is None on success and an
    Estonian error string on parse failure. The content is returned with
    surrounding whitespace preserved so the caller can decide whether
    empty-after-strip is allowed.
    """
    content_type = req.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = await req.json()
        except Exception:
            return "", "Vigane JSON."
        return str(body.get("content", "")), None
    form = await req.form()
    return str(form.get("content", "")), None


# ---------------------------------------------------------------------------
# Side-panel fragment renderer
# ---------------------------------------------------------------------------


def _row_message_item(
    ann: Annotation,
    user_name: str,
    mentions_resolved: dict[str, str],
) -> Any:
    """Render a single message inside the side-panel thread."""
    # @mention badges — one per resolved user UUID.
    mention_badges: list[Any] = []
    for mention_uuid in ann.mentions:
        display = mentions_resolved.get(str(mention_uuid), "—")
        mention_badges.append(
            Badge(  # noqa: F405
                f"@{display}",
                variant="primary",
                cls="annotation-mention-badge",
            )
        )

    header = Div(  # noqa: F405
        Strong(user_name, cls="annotation-author"),  # noqa: F405
        Span(  # noqa: F405
            _format_timestamp(ann.created_at),
            cls="annotation-timestamp",
        ),
        cls="annotation-header",
    )

    body_children: list[Any] = [P(ann.content, cls="annotation-content")]  # noqa: F405
    if mention_badges:
        body_children.append(
            Div(*mention_badges, cls="annotation-mentions")  # noqa: F405
        )

    return Card(
        CardHeader(header),
        CardBody(*body_children),
        variant="bordered",
        cls="annotation-message",
    )


def _row_panel_fragment(
    draft_version_id: uuid.UUID | str,
    row_kind: str,
    row_key: str,
    messages: list[Annotation],
    auth: UserDict,
) -> Any:
    """Render the full side-panel fragment for one row thread.

    Layout (top to bottom):
      - "Aegunud" warning banner if any message in the thread is stale
      - Thread header with row_kind label + resolve/reopen toggle
      - Reverse-chronological list of messages
      - Compose form pinned to the bottom

    The fragment carries its own outer ``id`` so HTMX swaps into the
    side-panel container with ``hx_swap="innerHTML"`` (or replace the
    whole inner fragment via ``outerHTML`` from the resolve/reopen
    handlers).
    """
    # #773: the URLs the fragment's buttons / form POST to must carry
    # the SAME percent-encoded form the report renderer mints, otherwise
    # the resolve/reopen/compose actions submit to a path containing raw
    # ``/`` and ``#`` (the keys here are the already-decoded raw URIs).
    encoded_row_key = safe_row_key(row_key)
    base = f"/annotations/version/{draft_version_id}/{row_kind}/{encoded_row_key}"

    # Resolve mention UUIDs → display names once per fragment so each
    # message item can render badges without re-querying.
    mention_uuids: set[str] = set()
    for m in messages:
        for u in m.mentions:
            mention_uuids.add(str(u))
    mentions_resolved = {u: _user_display_name(uuid.UUID(u)) for u in mention_uuids}

    # Thread state: any row marks the thread resolved → all rows are
    # resolved (mirror semantics in resolve_row_thread / reopen_row_thread).
    is_resolved = bool(messages and all(m.resolved for m in messages))

    # Stale banner if any row in the thread is flagged stale.
    is_stale = any(m.stale for m in messages)
    stale_banner = None
    if is_stale:
        stale_banner = Alert(
            "See rida on aegunud — viimane analüüs ei leidnud enam vastavat sisu eelnõust.",
            variant="warning",
            cls="annotation-stale-banner",
        )

    # Header with toggle button.
    if is_resolved:
        toggle = UiButton(
            "Ava uuesti",
            variant="secondary",
            size="sm",
            hx_post=f"{base}/reopen",
            hx_target=f"#{_SIDE_PANEL_ID}",
            hx_swap="innerHTML",
            cls="annotation-toggle-btn",
        )
    else:
        toggle = UiButton(
            "Lahenda",
            variant="primary",
            size="sm",
            hx_post=f"{base}/resolve",
            hx_target=f"#{_SIDE_PANEL_ID}",
            hx_swap="innerHTML",
            cls="annotation-toggle-btn",
        )

    kind_label = _ROW_KIND_LABELS.get(row_kind, row_kind)
    header = Div(  # noqa: F405
        Div(  # noqa: F405
            H3(  # noqa: F405
                f"{kind_label} märkused",
                cls="annotation-panel-title",
            ),
            Badge(  # noqa: F405
                str(len(messages)),
                variant="primary" if not is_resolved else "default",
                cls="annotation-count-badge",
            ),
            cls="annotation-panel-title-row",
        ),
        toggle,
        cls="annotation-panel-header",
    )

    # Message list — newest first (matches the model query ORDER BY DESC).
    if messages:
        message_items = [
            _row_message_item(
                m,
                _user_display_name(m.user_id),
                mentions_resolved,
            )
            for m in messages
        ]
        message_list = Div(*message_items, cls="annotation-message-list")  # noqa: F405
    else:
        message_list = Div(  # noqa: F405
            P(  # noqa: F405
                "Märkuseid ei ole veel lisatud.",
                cls="muted-text",
            ),
            cls="annotation-message-list annotation-message-list-empty",
        )

    # Compose form — disabled (visually) when the thread is resolved so
    # the user has to reopen first.
    compose_form = Form(  # noqa: F405
        Textarea(  # noqa: F405
            name="content",
            placeholder=(
                "Kirjuta märkus... Kasuta @nimi mainimiseks."
                if not is_resolved
                else "Lahendatud lõim — ava uuesti vastamiseks."
            ),
            rows="3",
            cls="annotation-compose-input",
            required=True,
            disabled=is_resolved,
        ),
        UiButton(
            "Saada",
            type="submit",
            variant="primary",
            size="sm",
            disabled=is_resolved,
        ),
        hx_post=f"{base}/messages",
        hx_target=f"#{_SIDE_PANEL_ID}",
        hx_swap="innerHTML",
        cls="annotation-compose-form",
    )

    children: list[Any] = []
    if stale_banner is not None:
        children.append(stale_banner)
    children.append(header)
    children.append(message_list)
    children.append(Hr(cls="annotation-divider"))  # noqa: F405
    children.append(compose_form)

    return Div(  # noqa: F405
        *children,
        # data attrs: useful for the PR-C JS when wiring AnnotationButton →
        # side panel without a full page reload.
        data_draft_version_id=str(draft_version_id),
        data_row_kind=row_kind,
        data_row_key=row_key,
        cls="annotation-side-panel-fragment",
    )


def _load_panel_messages(
    draft_version_id: uuid.UUID | str,
    row_kind: str,
    row_key: str,
) -> list[Annotation]:
    """Load every message in a row thread, swallowing DB errors.

    Wrapper around :func:`list_annotations_for_version_row` so the route
    handlers do not have to repeat the connection-context boilerplate.
    Returns an empty list on any failure — the rendered panel will simply
    show the "no messages yet" empty state, which is the correct UI even
    if the thread does have messages but the DB transiently failed.
    """
    try:
        with _connect() as conn:
            return list_annotations_for_version_row(conn, draft_version_id, row_kind, row_key)
    except Exception:
        logger.exception(
            "Failed to load row-annotation thread version=%s row_kind=%s row_key=%s",
            draft_version_id,
            row_kind,
            row_key,
        )
        return []


# ---------------------------------------------------------------------------
# Handler: GET /annotations/version/{draft_version_id}/{row_kind}/{row_key}
# ---------------------------------------------------------------------------


def get_row_panel_handler(
    req: Request,
    draft_version_id: str,
    row_kind: str,
    row_key: str,
):
    """Return the side-panel fragment for one impact-report row thread."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    if not _validate_row_kind(row_kind):
        return Response(status_code=400)

    resolved = _load_draft_version_or_404(draft_version_id, auth)
    if isinstance(resolved, Response):
        return resolved
    version_uuid, _org_id = resolved

    # #781 follow-up: base64url → raw row key. Malformed input → 400.
    try:
        row_key = decode_row_key(row_key)
    except (ValueError, UnicodeDecodeError):
        return Response(status_code=400)

    messages = _load_panel_messages(version_uuid, row_kind, row_key)
    return _row_panel_fragment(version_uuid, row_kind, row_key, messages, auth)


# ---------------------------------------------------------------------------
# Handler: POST /annotations/version/{...}/messages
# ---------------------------------------------------------------------------


async def post_row_message_handler(
    req: Request,
    draft_version_id: str,
    row_kind: str,
    row_key: str,
):
    """Append a message to a row thread (or create the thread if empty).

    Encrypts the body via Fernet, resolves @mentions to in-org user UUIDs,
    and writes a new ``annotations`` row with ``target_type='impact_report_item'``
    and ``target_id='{row_kind}:{row_key}'``. Returns the refreshed panel
    fragment so the HTMX swap shows the new message at the top.
    """
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    if not _validate_row_kind(row_kind):
        return Response(status_code=400)

    resolved = _load_draft_version_or_404(draft_version_id, auth)
    if isinstance(resolved, Response):
        return resolved
    version_uuid, org_id = resolved

    # #781 follow-up: base64url → raw row key. Malformed input → 400.
    try:
        row_key = decode_row_key(row_key)
    except (ValueError, UnicodeDecodeError):
        return Response(status_code=400)

    raw_content, parse_error = await _read_content(req)
    if parse_error is not None:
        return Response(content=parse_error, status_code=400)

    content = raw_content.strip()
    if not content:
        return Response(status_code=400)

    # Pre-count to discriminate "first message in thread" (create audit)
    # from "follow-up message" (message audit). The thread-level resolve
    # state is read here so we can refuse a write into a resolved thread
    # before touching the DB.
    existing = _load_panel_messages(version_uuid, row_kind, row_key)
    if existing and all(m.resolved for m in existing):
        return Response(
            content="Lõim on lahendatud — ava esmalt uuesti.",
            status_code=409,
        )

    is_first_message = not existing

    try:
        with _connect() as conn:
            annotation = create_row_annotation(
                conn,
                user_id=auth["id"],
                org_id=org_id,
                draft_version_id=version_uuid,
                row_kind=row_kind,
                row_key=row_key,
                content=content,
            )
            conn.commit()
    except ValueError as exc:
        logger.warning("create_row_annotation rejected input: %s", exc)
        return Response(content=str(exc), status_code=400)
    except Exception:
        logger.exception("Failed to create row annotation")
        return Response(content="Märkuse loomine ebaõnnestus.", status_code=500)

    if is_first_message:
        log_row_annotation_create(auth["id"], annotation.id, version_uuid, row_kind, row_key)
    else:
        log_row_annotation_message(auth["id"], annotation.id, version_uuid, row_kind, row_key)

    # Reload the thread so the fragment shows the message we just inserted
    # plus everything that was already there.
    messages = _load_panel_messages(version_uuid, row_kind, row_key)
    return _row_panel_fragment(version_uuid, row_kind, row_key, messages, auth)


# ---------------------------------------------------------------------------
# Handler: POST /annotations/version/{...}/resolve
# ---------------------------------------------------------------------------


def post_row_resolve_handler(
    req: Request,
    draft_version_id: str,
    row_kind: str,
    row_key: str,
):
    """Mark every message in a row thread as resolved."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    if not _validate_row_kind(row_kind):
        return Response(status_code=400)

    resolved = _load_draft_version_or_404(draft_version_id, auth)
    if isinstance(resolved, Response):
        return resolved
    version_uuid, _org_id = resolved

    # #781 follow-up: base64url → raw row key. Malformed input → 400.
    try:
        row_key = decode_row_key(row_key)
    except (ValueError, UnicodeDecodeError):
        return Response(status_code=400)

    try:
        with _connect() as conn:
            updated = resolve_row_thread(
                conn,
                draft_version_id=version_uuid,
                row_kind=row_kind,
                row_key=row_key,
                resolved_by_user_id=auth["id"],
            )
            conn.commit()
    except Exception:
        logger.exception("Failed to resolve row thread")
        return Response(status_code=500)

    if updated > 0:
        log_row_annotation_resolve(auth["id"], version_uuid, row_kind, row_key)

    messages = _load_panel_messages(version_uuid, row_kind, row_key)
    return _row_panel_fragment(version_uuid, row_kind, row_key, messages, auth)


# ---------------------------------------------------------------------------
# Handler: POST /annotations/version/{...}/reopen
# ---------------------------------------------------------------------------


def post_row_reopen_handler(
    req: Request,
    draft_version_id: str,
    row_kind: str,
    row_key: str,
):
    """Flip a previously resolved row thread back to ``resolved=FALSE``."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    if not _validate_row_kind(row_kind):
        return Response(status_code=400)

    resolved = _load_draft_version_or_404(draft_version_id, auth)
    if isinstance(resolved, Response):
        return resolved
    version_uuid, _org_id = resolved

    # #781 follow-up: base64url → raw row key. Malformed input → 400.
    try:
        row_key = decode_row_key(row_key)
    except (ValueError, UnicodeDecodeError):
        return Response(status_code=400)

    try:
        with _connect() as conn:
            updated = reopen_row_thread(
                conn,
                draft_version_id=version_uuid,
                row_kind=row_kind,
                row_key=row_key,
            )
            conn.commit()
    except Exception:
        logger.exception("Failed to reopen row thread")
        return Response(status_code=500)

    if updated > 0:
        log_row_annotation_reopen(auth["id"], version_uuid, row_kind, row_key)

    messages = _load_panel_messages(version_uuid, row_kind, row_key)
    return _row_panel_fragment(version_uuid, row_kind, row_key, messages, auth)


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_annotation_routes(rt) -> None:  # type: ignore[no-untyped-def]
    """Mount annotation API routes on the FastHTML route decorator *rt*."""
    rt("/api/annotations", methods=["GET"])(list_annotations_handler)
    rt("/api/annotations", methods=["POST"])(create_annotation_handler)
    rt("/api/annotations/{id}/reply", methods=["POST"])(reply_annotation_handler)
    rt("/api/annotations/{id}/resolve", methods=["POST"])(resolve_annotation_handler)
    rt("/api/annotations/{id}", methods=["DELETE"])(delete_annotation_handler)

    # §9.4 row-annotation routes (PR-B).  The unprefixed ``/annotations``
    # path matches the sprint plan §6 contract; PR-C wires it up from the
    # impact-report side panel.
    #
    # #773: ``row_key`` uses the ``:path`` converter because affected-entity
    # and EU-compliance rows store the raw ontology URI as the key, and URIs
    # contain ``/``, ``:``, and ``#`` which Starlette decodes BEFORE routing
    # — a plain ``{row_key}`` segment would 404 once a URI key arrives.
    # ``:path`` matches greedily; the actual segment is base64url so it
    # never contains ``/`` in practice (base64url alphabet is
    # ``[A-Za-z0-9_-]``), but the ``:path`` converter is kept because
    # older URLs encoded with the previous percent-encoding scheme may
    # still be in flight during a rolling deploy. Outbound encoding goes
    # through :func:`app.annotations.row_keys.safe_row_key`; inbound
    # decoding goes through :func:`app.annotations.row_keys.decode_row_key`.
    #
    # The POST sub-routes (``/messages``, ``/resolve``, ``/reopen``) keep
    # their own methods so no greedy ``:path`` ambiguity arises — Starlette
    # routes by ``(path, method)`` and the GET base + POST tail-suffix
    # patterns each match only on their own verb.
    base = "/annotations/version/{draft_version_id}/{row_kind}/{row_key:path}"
    rt(base, methods=["GET"])(get_row_panel_handler)
    rt(f"{base}/messages", methods=["POST"])(post_row_message_handler)
    rt(f"{base}/resolve", methods=["POST"])(post_row_resolve_handler)
    rt(f"{base}/reopen", methods=["POST"])(post_row_reopen_handler)
