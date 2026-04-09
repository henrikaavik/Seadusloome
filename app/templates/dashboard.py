"""Personal dashboard for authenticated users."""

from __future__ import annotations

import logging

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.auth.audit import log_action
from app.db import get_connection as _connect

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _get_recent_activity(user_id: str) -> list[dict]:  # type: ignore[type-arg]
    """Return the last 20 audit_log entries for the given user."""
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT id, action, detail, created_at "
                "FROM audit_log WHERE user_id = %s "
                "ORDER BY created_at DESC LIMIT 20",
                (user_id,),
            ).fetchall()
        return [
            {
                "id": r[0],
                "action": r[1],
                "detail": r[2],
                "created_at": r[3],
            }
            for r in rows
        ]
    except Exception:
        logger.exception("Failed to fetch recent activity for user %s", user_id)
        return []


def _get_bookmarks(user_id: str) -> list[dict]:  # type: ignore[type-arg]
    """Return all bookmarks for the given user."""
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT id, entity_uri, label, created_at "
                "FROM bookmarks WHERE user_id = %s "
                "ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        return [
            {
                "id": str(r[0]),
                "entity_uri": r[1],
                "label": r[2],
                "created_at": r[3],
            }
            for r in rows
        ]
    except Exception:
        logger.exception("Failed to fetch bookmarks for user %s", user_id)
        return []


def _get_user_org_info(user_id: str) -> dict | None:  # type: ignore[type-arg]
    """Return organization info for the given user (name, member count, role)."""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT o.name, u.role, "
                "(SELECT COUNT(*) FROM users u2 WHERE u2.org_id = o.id) AS member_count "
                "FROM users u "
                "JOIN organizations o ON o.id = u.org_id "
                "WHERE u.id = %s",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "org_name": row[0],
            "role": row[1],
            "member_count": row[2],
        }
    except Exception:
        logger.exception("Failed to fetch org info for user %s", user_id)
        return None


def _add_bookmark(user_id: str, entity_uri: str, label: str | None) -> dict | None:  # type: ignore[type-arg]
    """Add a bookmark for the given user. Returns the created bookmark or None."""
    try:
        with _connect() as conn:
            row = conn.execute(
                "INSERT INTO bookmarks (user_id, entity_uri, label) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (user_id, entity_uri) DO NOTHING "
                "RETURNING id, entity_uri, label, created_at",
                (user_id, entity_uri, label),
            ).fetchone()
            conn.commit()
        if row is None:
            return None
        return {
            "id": str(row[0]),
            "entity_uri": row[1],
            "label": row[2],
            "created_at": row[3],
        }
    except Exception:
        logger.exception("Failed to add bookmark for user %s", user_id)
        return None


def _remove_bookmark(bookmark_id: str, user_id: str) -> bool:
    """Remove a bookmark by ID, scoped to the given user. Returns True on success."""
    try:
        with _connect() as conn:
            result = conn.execute(
                "DELETE FROM bookmarks WHERE id = %s AND user_id = %s",
                (bookmark_id, user_id),
            )
            conn.commit()
        return (result.rowcount or 0) > 0
    except Exception:
        logger.exception("Failed to remove bookmark %s for user %s", bookmark_id, user_id)
        return False


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

_ROLE_LABELS = {
    "admin": "Administraator",
    "org_admin": "Organisatsiooni admin",
    "reviewer": "Ülevaataja",
    "drafter": "Koostaja",
}


def _render_activity_section(activity: list[dict]) -> list:  # type: ignore[type-arg]
    """Render the recent activity section."""
    if not activity:
        return [P("Tegevusi ei leitud.", style="color:gray")]

    rows = []
    for entry in activity:
        ts = entry["created_at"]
        ts_str = ts.strftime("%d.%m.%Y %H:%M") if ts else "—"
        rows.append(
            Tr(
                Td(ts_str),
                Td(entry["action"]),
                Td(str(entry["detail"]) if entry["detail"] else "—"),
            )
        )
    return [
        Table(
            Thead(Tr(Th("Aeg"), Th("Tegevus"), Th("Detailid"))),
            Tbody(*rows),
        )
    ]


def _render_bookmarks_section(bookmarks: list[dict]) -> list:  # type: ignore[type-arg]
    """Render the bookmarks section."""
    if not bookmarks:
        return [P("Järjehoidjaid ei leitud.", style="color:gray")]

    rows = []
    for bm in bookmarks:
        rows.append(
            Tr(
                Td(bm["label"] or bm["entity_uri"]),
                Td(A(bm["entity_uri"], href=bm["entity_uri"])),
                Td(
                    Form(
                        Button("Eemalda", type="submit", cls="button secondary"),
                        method="post",
                        action=f"/api/bookmarks/{bm['id']}/delete",
                        style="display:inline",
                    )
                ),
            )
        )
    return [
        Table(
            Thead(Tr(Th("Nimi"), Th("URI"), Th("Tegevused"))),
            Tbody(*rows),
        )
    ]


def _render_org_section(org_info: dict | None) -> list:  # type: ignore[type-arg]
    """Render the organization info section."""
    if org_info is None:
        return [P("Te ei kuulu ühtegi organisatsiooni.", style="color:gray")]

    return [
        Table(
            Tbody(
                Tr(Th("Organisatsioon"), Td(org_info["org_name"])),
                Tr(Th("Teie roll"), Td(_ROLE_LABELS.get(org_info["role"], org_info["role"]))),
                Tr(Th("Liikmeid"), Td(str(org_info["member_count"]))),
            )
        )
    ]


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def dashboard_page(req: Request):
    """GET /dashboard — personal dashboard for authenticated users."""
    auth = req.scope.get("auth", {})
    user_id = auth.get("id")
    full_name = auth.get("full_name", "Kasutaja")

    activity = _get_recent_activity(user_id) if user_id else []
    bookmarks = _get_bookmarks(user_id) if user_id else []
    org_info = _get_user_org_info(user_id) if user_id else None

    content = [
        P(f"Tere tulemast, {full_name}!"),
        H3("Organisatsioon"),
        *_render_org_section(org_info),
        H3("Järjehoidjad"),
        *_render_bookmarks_section(bookmarks),
        Form(
            Fieldset(
                Label("URI", Input(name="entity_uri", type="text", required=True)),
                Label("Nimi", Input(name="label", type="text")),
            ),
            Button("Lisa järjehoidja", type="submit"),
            method="post",
            action="/api/bookmarks",
        ),
        H3("Viimased tegevused"),
        *_render_activity_section(activity),
        Form(
            Button("Logi välja", type="submit"),
            method="post",
            action="/auth/logout",
        ),
    ]

    return Titled("Töölaud", *content)


def add_bookmark(req: Request, entity_uri: str, label: str = ""):
    """POST /api/bookmarks — add a bookmark for the current user."""
    auth = req.scope.get("auth", {})
    user_id = auth.get("id")
    if not user_id:
        return RedirectResponse(url="/auth/login", status_code=303)

    actual_label = label.strip() if label else None
    bookmark = _add_bookmark(user_id, entity_uri.strip(), actual_label)
    if bookmark:
        log_action(user_id, "bookmark.add", {"entity_uri": entity_uri, "label": actual_label})
    return RedirectResponse(url="/dashboard", status_code=303)


def remove_bookmark(req: Request, bookmark_id: str):
    """POST /api/bookmarks/{bookmark_id}/delete — remove a bookmark."""
    auth = req.scope.get("auth", {})
    user_id = auth.get("id")
    if not user_id:
        return RedirectResponse(url="/auth/login", status_code=303)

    success = _remove_bookmark(bookmark_id, user_id)
    if success:
        log_action(user_id, "bookmark.remove", {"bookmark_id": bookmark_id})
    return RedirectResponse(url="/dashboard", status_code=303)


def index_redirect(req: Request):
    """GET / — redirect authenticated users to the dashboard."""
    auth = req.scope.get("auth")
    if auth:
        return RedirectResponse(url="/dashboard", status_code=303)
    return RedirectResponse(url="/auth/login", status_code=303)


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_dashboard_routes(rt) -> None:  # type: ignore[no-untyped-def]
    """Register personal dashboard routes on the FastHTML route decorator *rt*."""
    rt("/dashboard", methods=["GET"])(dashboard_page)
    rt("/api/bookmarks", methods=["POST"])(add_bookmark)
    rt("/api/bookmarks/{bookmark_id}/delete", methods=["POST"])(remove_bookmark)
