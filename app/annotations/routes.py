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
)
from app.annotations.models import (
    Annotation,
    create_annotation,
    create_reply,
    delete_annotation,
    get_annotation,
    list_annotations_for_target,
    list_replies,
    resolve_annotation,
)
from app.auth.helpers import require_auth as _require_auth
from app.auth.provider import UserDict
from app.auth.users import get_user
from app.db import get_connection as _connect
from app.notifications.wire import notify_annotation_reply
from app.ui.surfaces.annotation_popover import AnnotationPopover

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
    """Render a datetime as an Estonian-style timestamp."""
    if value is None:
        return "—"
    try:
        return value.strftime("%d.%m.%Y %H:%M")
    except AttributeError:
        return str(value)


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
# Route registration
# ---------------------------------------------------------------------------


def register_annotation_routes(rt) -> None:  # type: ignore[no-untyped-def]
    """Mount annotation API routes on the FastHTML route decorator *rt*."""
    rt("/api/annotations", methods=["GET"])(list_annotations_handler)
    rt("/api/annotations", methods=["POST"])(create_annotation_handler)
    rt("/api/annotations/{id}/reply", methods=["POST"])(reply_annotation_handler)
    rt("/api/annotations/{id}/resolve", methods=["POST"])(resolve_annotation_handler)
    rt("/api/annotations/{id}", methods=["DELETE"])(delete_annotation_handler)
