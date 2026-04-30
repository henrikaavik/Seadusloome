"""Personal dashboard for authenticated users."""

from __future__ import annotations

import json
import logging
from typing import Any

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.auth.audit import log_action
from app.db import get_connection as _connect
from app.ui.data.data_table import Column, DataTable
from app.ui.forms.app_form import AppForm
from app.ui.forms.form_field import FormField
from app.ui.layout import PageShell
from app.ui.primitives.badge import Badge
from app.ui.primitives.button import Button
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.surfaces.info_box import InfoBox
from app.ui.theme import get_theme_from_request
from app.ui.time import format_tallinn

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


def _get_dashboard_counts(user_id: str, org_id: str | None) -> dict[str, int]:
    """Return counts for drafts, drafter sessions, and conversations."""
    counts: dict[str, int] = {"drafts": 0, "drafter_sessions": 0, "conversations": 0}
    if not user_id:
        return counts
    try:
        with _connect() as conn:
            if org_id:
                row = conn.execute(
                    "SELECT COUNT(*) FROM drafts WHERE org_id = %s",
                    (org_id,),
                ).fetchone()
                counts["drafts"] = row[0] if row else 0

                row = conn.execute(
                    "SELECT COUNT(*) FROM drafting_sessions WHERE user_id = %s AND org_id = %s",
                    (user_id, org_id),
                ).fetchone()
                counts["drafter_sessions"] = row[0] if row else 0

            row = conn.execute(
                "SELECT COUNT(*) FROM conversations WHERE user_id = %s",
                (user_id,),
            ).fetchone()
            counts["conversations"] = row[0] if row else 0
    except Exception:
        logger.exception("Failed to fetch dashboard counts for user %s", user_id)
    return counts


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

_ROLE_LABELS = {
    "admin": "Administraator",
    "org_admin": "Organisatsiooni admin",
    "reviewer": "Ülevaataja",
    "drafter": "Koostaja",
}

# Map common audit-detail dict keys to Estonian labels for the
# recent-activity table.  Keys not in this mapping fall through to the
# first key/value pair so we don't silently drop interesting data.
_AUDIT_DETAIL_LABELS: dict[str, str] = {
    "draft_id": "Eelnõu",
    "user_id": "Kasutaja",
    "report_id": "Aruanne",
    "filename": "Fail",
    "title": "Pealkiri",
    "role": "Roll",
    "entity_uri": "URI",
    "label": "Nimi",
    "bookmark_id": "Järjehoidja",
    "session_id": "Sessioon",
    "conversation_id": "Vestlus",
    "org_id": "Organisatsioon",
}


def _audit_detail_summary(action: str, detail: Any) -> str:
    """Return an Estonian-friendly one-line summary of an audit detail payload.

    The audit_log.detail column is a JSONB value (typically a dict) that
    psycopg returns as a Python dict.  Older rows or test fixtures may pass
    in a JSON string, so we accept both and degrade gracefully when the
    payload is empty / unparsable.

    Args:
        action: The audit action label (kept for forward-compatible context;
            currently unused but reserved so callers don't need to change
            shape if we add per-action formatters).
        detail: The raw payload — dict, JSON string, or ``None``.

    Returns:
        A short human-readable string. ``"—"`` when ``detail`` is empty.
    """
    del action  # reserved for future per-action overrides

    if detail is None or detail == "":
        return "—"

    if isinstance(detail, str):
        # Some legacy code paths may stash a raw JSON string; try to parse
        # it but fall back to the literal text rather than crashing.
        try:
            detail = json.loads(detail)
        except (TypeError, ValueError):
            return detail

    if not isinstance(detail, dict):
        return str(detail)

    parts: list[str] = []
    for key, label in _AUDIT_DETAIL_LABELS.items():
        if key in detail and detail[key] not in (None, ""):
            value = str(detail[key])
            # Truncate UUIDs to the first 8 chars for at-a-glance display;
            # full value is still available in the disclosure below.
            if len(value) == 36 and value.count("-") == 4:
                value = value[:8] + "…"
            parts.append(f"{label}: {value}")

    if not parts:
        # Surface SOMETHING for unmapped payloads instead of an empty cell.
        first = next(iter(detail.items()), None)
        if first is not None:
            parts.append(f"{first[0]}: {first[1]}")

    return " · ".join(parts) or "—"


def _audit_detail_cell(row: dict[str, Any]):
    """Render the recent-activity ``Detailid`` cell.

    Shows a readable Estonian summary plus a ``<details>`` disclosure
    containing the raw JSON for power users who need the exact payload.
    Empty / scalar payloads collapse to the summary alone.
    """
    action = row.get("action", "")
    raw = row.get("detail_raw")
    summary = _audit_detail_summary(action, raw)

    if raw in (None, ""):
        return summary

    if isinstance(raw, dict):
        raw_str = json.dumps(raw, indent=2, ensure_ascii=False, default=str)
    else:
        raw_str = str(raw)

    return Details(
        Summary(summary),
        Pre(raw_str, cls="audit-detail-json"),
        cls="audit-detail",
    )


def _activity_card(activity: list[dict]):  # type: ignore[type-arg]
    """Render the recent activity card."""
    if not activity:
        body = P("Tegevusi ei leitud.", cls="muted-text")
    else:
        columns = [
            Column(key="time", label="Aeg", sortable=False),
            Column(key="action", label="Tegevus", sortable=False),
            Column(
                key="detail",
                label="Detailid",
                sortable=False,
                render=_audit_detail_cell,
            ),
        ]
        rows = []
        for entry in activity:
            ts = entry["created_at"]
            rows.append(
                {
                    "time": format_tallinn(ts),
                    "action": entry["action"],
                    # Carry the raw payload so the render callback can produce
                    # both the summary and the disclosure.  The ``detail`` key
                    # itself is unused by the renderer but kept for
                    # backwards-compatibility with any consumer that still
                    # reads the row dict directly.
                    "detail": "",
                    "detail_raw": entry["detail"],
                }
            )
        body = DataTable(
            columns=columns,
            rows=rows,
            empty_message="Tegevusi ei leitud.",
        )
    return Card(
        CardHeader(H3("Viimased tegevused", cls="card-title")),
        CardBody(body),
    )


def _bookmarks_card(bookmarks: list[dict]):  # type: ignore[type-arg]
    """Render the bookmarks card (list + add form)."""
    if not bookmarks:
        table: object = P("Järjehoidjaid ei leitud.", cls="muted-text")
    else:
        columns = [
            Column(key="label", label="Nimi", sortable=False),
            Column(
                key="entity_uri",
                label="URI",
                sortable=False,
                render=lambda r: A(r["entity_uri"], href=r["entity_uri"]),
            ),
            Column(
                key="actions",
                label="Tegevused",
                sortable=False,
                render=lambda r: AppForm(
                    Button(
                        "Eemalda",
                        type="submit",
                        variant="secondary",
                        size="sm",
                    ),
                    method="post",
                    action=f"/api/bookmarks/{r['id']}/delete",
                    cls="inline-form",
                ),
            ),
        ]
        rows = [
            {
                "id": bm["id"],
                "label": bm["label"] or bm["entity_uri"],
                "entity_uri": bm["entity_uri"],
            }
            for bm in bookmarks
        ]
        table = DataTable(columns=columns, rows=rows)

    add_form = AppForm(
        FormField(name="entity_uri", label="URI", type="text", required=True),
        FormField(name="label", label="Nimi", type="text"),
        Button("Lisa järjehoidja", type="submit", variant="primary"),
        method="post",
        action="/api/bookmarks",
        cls="bookmark-add-form",
    )

    return Card(
        CardHeader(H3("Järjehoidjad", cls="card-title")),
        CardBody(table, add_form),
    )


def _org_card(org_info: dict | None):  # type: ignore[type-arg]
    """Render the organisation info card."""
    if org_info is None:
        body = P("Te ei kuulu ühtegi organisatsiooni.", cls="muted-text")
    else:
        body = Dl(
            Dt("Organisatsioon"),
            Dd(org_info["org_name"]),
            Dt("Teie roll"),
            Dd(
                Badge(
                    _ROLE_LABELS.get(org_info["role"], org_info["role"]),
                    variant="primary",
                )
            ),
            Dt("Liikmeid"),
            Dd(str(org_info["member_count"])),
            cls="info-list",
        )
    return Card(
        CardHeader(H3("Organisatsioon", cls="card-title")),
        CardBody(body),
    )


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def dashboard_page(req: Request):
    """GET /dashboard — personal dashboard for authenticated users."""
    auth = req.scope.get("auth", {})
    theme = get_theme_from_request(req)
    user_id = auth.get("id")
    full_name = auth.get("full_name", "Kasutaja")

    activity = _get_recent_activity(user_id) if user_id else []
    bookmarks = _get_bookmarks(user_id) if user_id else []
    org_info = _get_user_org_info(user_id) if user_id else None
    counts = _get_dashboard_counts(user_id, auth.get("org_id")) if user_id else {}

    # Build the counts summary line
    draft_count = counts.get("drafts", 0)
    session_count = counts.get("drafter_sessions", 0)
    conv_count = counts.get("conversations", 0)

    content = (
        H1(f"Tere tulemast, {full_name}!", cls="page-title"),
        InfoBox(
            P(
                "Tere tulemast Seadusloome s\u00fcsteemi! "
                "Siit leiate kiirlingid teie eeln\u00f5udele, koostajale ja vestlustele. "
                "Kasutage vasakul olevat men\u00fc\u00fcd navigeerimiseks."
            ),
            P(
                f"Teil on {draft_count} eeln\u00f5u"
                f"{'d' if draft_count != 1 else ''}"
                f", {session_count} koostaja sessiooni"
                f" ja {conv_count} vestlus"
                f"{'t' if conv_count != 1 else ''}"
                "."
            ),
            variant="info",
            dismissible=True,
        ),
        _org_card(org_info),
        _bookmarks_card(bookmarks),
        _activity_card(activity),
    )

    return PageShell(
        *content,
        title="Töölaud",
        user=auth or None,
        theme=theme,
        active_nav="/dashboard",
    )


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
