"""FastHTML routes for the notification inbox and HTMX endpoints.

Route map:

    GET  /notifications                   -- notification inbox page
    POST /notifications/{id}/read         -- mark single as read (HTMX)
    POST /notifications/read-all          -- mark all as read (HTMX)
    GET  /api/notifications/unread-count  -- OOB HTML badge for bell polling
    GET  /api/notifications               -- HTMX partial: recent notifications list

All routes require authentication (they are NOT in ``SKIP_PATHS``).
"""

from __future__ import annotations

import logging
from typing import Any

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request
from starlette.responses import Response

from app.auth.helpers import require_auth as _require_auth
from app.auth.provider import UserDict
from app.db import get_connection as _connect
from app.notifications.models import (
    count_unread,
    list_notifications_for_user,
    mark_all_read,
    mark_read,
)
from app.ui.layout import PageShell
from app.ui.theme import get_theme_from_request

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# FT Components
# ---------------------------------------------------------------------------


def _time_ago(dt: Any) -> str:
    """Format a datetime as a relative time string in Estonian."""
    from datetime import UTC, datetime

    if dt is None:
        return ""
    now = datetime.now(UTC)
    if dt.tzinfo is None:
        # Naive datetime — assume UTC
        dt = dt.replace(tzinfo=UTC)
    diff = now - dt
    seconds = int(diff.total_seconds())

    if seconds < 60:
        return "just nüüd"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min tagasi"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} t tagasi"
    days = hours // 24
    if days < 30:
        return f"{days} p tagasi"
    return dt.strftime("%d.%m.%Y")


def NotificationItem(notif: Any, *, compact: bool = False):  # noqa: ANN201, N802
    """Single notification row."""
    read_cls = "notification-item--read" if notif.read else "notification-item--unread"
    dot = (
        Span(cls="notification-dot")  # noqa: F405
        if not notif.read
        else None
    )

    title_el = Span(notif.title, cls="notification-title")  # noqa: F405
    time_el = Span(_time_ago(notif.created_at), cls="notification-time")  # noqa: F405
    body_el = (
        P(notif.body, cls="notification-body")  # noqa: F405
        if notif.body and not compact
        else None
    )

    content = Div(  # noqa: F405
        Div(dot, title_el, time_el, cls="notification-header"),  # noqa: F405
        body_el,
        cls=f"notification-item {read_cls}",
        id=f"notification-{notif.id}",
    )

    if notif.link and not notif.read:
        # Wrap in a link and mark as read on click
        return A(  # noqa: F405
            content,
            href=notif.link,
            hx_post=f"/notifications/{notif.id}/read",
            hx_swap="none",
            cls="notification-link",
        )
    if notif.link:
        return A(content, href=notif.link, cls="notification-link")  # noqa: F405
    if not notif.read:
        return Div(  # noqa: F405
            content,
            Button(  # noqa: F405
                "Loe",
                hx_post=f"/notifications/{notif.id}/read",
                hx_target=f"#notification-{notif.id}",
                hx_swap="outerHTML",
                cls="notification-mark-btn btn-sm",
            ),
            cls="notification-item-wrapper",
        )
    return content


def NotificationList(notifications: list[Any], *, compact: bool = False):  # noqa: ANN201, N802
    """List of notification items."""
    if not notifications:
        return Div(  # noqa: F405
            P("Teavitusi pole.", cls="notification-empty"),  # noqa: F405
            cls="notification-list",
        )
    items = [NotificationItem(n, compact=compact) for n in notifications]
    return Div(*items, cls="notification-list")  # noqa: F405


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def notifications_page(req: Request):
    """GET /notifications -- full notification inbox page."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth: UserDict = auth_or_redirect
    theme = get_theme_from_request(req)

    try:
        with _connect() as conn:
            notifications = list_notifications_for_user(conn, auth["id"], limit=50)
            unread = count_unread(conn, auth["id"])
    except Exception:
        logger.exception("Failed to load notifications page")
        notifications = []
        unread = 0

    mark_all_btn = (
        Button(  # noqa: F405
            "Märgi kõik loetuks",
            hx_post="/notifications/read-all",
            hx_target="#notification-container",
            hx_swap="innerHTML",
            cls="btn btn-secondary btn-sm",
        )
        if unread > 0
        else None
    )

    return PageShell(
        Div(  # noqa: F405
            Div(  # noqa: F405
                H1(f"Teavitused ({unread})", cls="page-title"),  # noqa: F405
                mark_all_btn,
                cls="notification-page-header",
            ),
            Div(  # noqa: F405
                NotificationList(notifications),
                id="notification-container",
            ),
            cls="notification-page",
        ),
        title="Teavitused",
        user=auth,
        theme=theme,
        unread_count=unread,
        active_nav="notifications",
    )


def mark_single_read(req: Request, id: str):
    """POST /notifications/{id}/read -- mark one notification as read (HTMX)."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth: UserDict = auth_or_redirect

    try:
        import uuid as _uuid

        notif_id = _uuid.UUID(id)
    except (ValueError, TypeError):
        return Response("Vigane ID", status_code=400)

    try:
        with _connect() as conn:
            mark_read(conn, notif_id, user_id=auth["id"])
            conn.commit()
    except Exception:
        logger.exception("Failed to mark notification %s as read", id)

    # Return an empty response -- the HTMX swap will handle UI update.
    # If hx_target was the item itself, return a minimal read-state item.
    return Span(cls="notification-dot--read")  # noqa: F405


def mark_all_read_handler(req: Request):
    """POST /notifications/read-all -- mark all as read (HTMX)."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth: UserDict = auth_or_redirect

    try:
        with _connect() as conn:
            mark_all_read(conn, auth["id"])
            conn.commit()
            notifications = list_notifications_for_user(conn, auth["id"], limit=50)
    except Exception:
        logger.exception("Failed to mark all notifications as read")
        notifications = []

    return NotificationList(notifications)


def api_unread_count(req: Request):
    """GET /api/notifications/unread-count -- OOB HTML badge for bell polling."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return Span(id="bell-badge", cls="bell-badge bell-badge--hidden", hx_swap_oob="true")  # noqa: F405
    auth: UserDict = auth_or_redirect

    try:
        with _connect() as conn:
            count = count_unread(conn, auth["id"])
    except Exception:
        logger.exception("Failed to count unread notifications")
        count = 0

    if count > 0:
        return Span(  # noqa: F405
            str(count if count < 100 else "99+"),
            cls="bell-badge",
            id="bell-badge",
            hx_swap_oob="true",
        )
    return Span(id="bell-badge", cls="bell-badge bell-badge--hidden", hx_swap_oob="true")  # noqa: F405


def api_notifications_partial(req: Request):
    """GET /api/notifications -- HTMX partial: recent notifications dropdown."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return Div(P("Palun logige sisse."))  # noqa: F405
    auth: UserDict = auth_or_redirect

    limit_param = req.query_params.get("limit", "5")
    try:
        limit = int(limit_param)
    except (ValueError, TypeError):
        limit = 5
    limit = min(limit, 20)

    try:
        with _connect() as conn:
            notifications = list_notifications_for_user(conn, auth["id"], limit=limit)
    except Exception:
        logger.exception("Failed to load notifications partial")
        notifications = []

    return Div(  # noqa: F405
        NotificationList(notifications, compact=True),
        A("Vaata koiki", href="/notifications", cls="notification-view-all"),  # noqa: F405
        cls="notification-dropdown-content",
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_notification_routes(rt: Any) -> None:
    """Register notification routes on the FastHTML route table."""
    rt("/notifications", methods=["GET"])(notifications_page)
    rt("/notifications/{id}/read", methods=["POST"])(mark_single_read)
    rt("/notifications/read-all", methods=["POST"])(mark_all_read_handler)
    rt("/api/notifications/unread-count", methods=["GET"])(api_unread_count)
    rt("/api/notifications", methods=["GET"])(api_notifications_partial)
