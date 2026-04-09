"""Admin dashboard with system health, sync status, user stats, and audit log."""

from __future__ import annotations

import logging

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.auth.roles import require_role
from app.db import get_connection as _connect
from app.sync.jena_loader import check_health as jena_check_health

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _check_postgres() -> bool:
    """Check if PostgreSQL is reachable."""
    try:
        with _connect() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        logger.exception("PostgreSQL health check failed")
        return False


def _get_sync_logs(limit: int = 5) -> list[dict]:  # type: ignore[type-arg]
    """Return the most recent sync_log entries."""
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT id, started_at, finished_at, status, entity_count, error_message "
                "FROM sync_log ORDER BY started_at DESC LIMIT %s",
                (limit,),
            ).fetchall()
        return [
            {
                "id": r[0],
                "started_at": r[1],
                "finished_at": r[2],
                "status": r[3],
                "entity_count": r[4],
                "error_message": r[5],
            }
            for r in rows
        ]
    except Exception:
        logger.exception("Failed to fetch sync logs")
        return []


def _get_user_stats() -> dict:  # type: ignore[type-arg]
    """Return user statistics: total count, users per org, active sessions."""
    stats: dict = {  # type: ignore[type-arg]
        "total_users": 0,
        "users_per_org": [],
        "active_sessions": 0,
    }
    try:
        with _connect() as conn:
            # Total users
            row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
            stats["total_users"] = row[0] if row else 0

            # Users per org
            rows = conn.execute(
                "SELECT o.name, COUNT(u.id) AS user_count "
                "FROM organizations o "
                "LEFT JOIN users u ON u.org_id = o.id "
                "GROUP BY o.id, o.name ORDER BY o.name"
            ).fetchall()
            stats["users_per_org"] = [{"org_name": r[0], "user_count": r[1]} for r in rows]

            # Active sessions (not expired)
            row = conn.execute("SELECT COUNT(*) FROM sessions WHERE expires_at > now()").fetchone()
            stats["active_sessions"] = row[0] if row else 0
    except Exception:
        logger.exception("Failed to fetch user stats")
    return stats


def _get_audit_log_page(page: int = 1, per_page: int = 25) -> tuple[list[dict], int]:  # type: ignore[type-arg]
    """Return a page of audit_log entries and total count."""
    entries: list[dict] = []  # type: ignore[type-arg]
    total = 0
    try:
        with _connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()
            total = row[0] if row else 0

            offset = (page - 1) * per_page
            rows = conn.execute(
                "SELECT a.id, a.user_id, u.full_name, a.action, a.detail, a.created_at "
                "FROM audit_log a "
                "LEFT JOIN users u ON u.id = a.user_id "
                "ORDER BY a.created_at DESC LIMIT %s OFFSET %s",
                (per_page, offset),
            ).fetchall()
            entries = [
                {
                    "id": r[0],
                    "user_id": str(r[1]) if r[1] else None,
                    "user_name": r[2] or "Süsteem",
                    "action": r[3],
                    "detail": r[4],
                    "created_at": r[5],
                }
                for r in rows
            ]
    except Exception:
        logger.exception("Failed to fetch audit log page %d", page)
    return entries, total


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

_STATUS_LABELS = {
    "running": "Käimas",
    "success": "Õnnestus",
    "failed": "Ebaõnnestus",
}

_STATUS_COLORS = {
    "running": "orange",
    "success": "green",
    "failed": "red",
}


def _render_health_section(jena_ok: bool, pg_ok: bool) -> list:
    """Render the system health section."""

    def _status_badge(ok: bool) -> Span:  # type: ignore[type-arg]
        color = "green" if ok else "red"
        text = "OK" if ok else "Viga"
        return Span(text, style=f"color:{color};font-weight:bold")

    return [
        Table(
            Tbody(
                Tr(Th("Apache Jena Fuseki"), Td(_status_badge(jena_ok))),
                Tr(Th("PostgreSQL"), Td(_status_badge(pg_ok))),
            )
        )
    ]


def _render_sync_section(sync_logs: list[dict]) -> list:  # type: ignore[type-arg]
    """Render the sync status section."""
    if not sync_logs:
        return [P("Sünkroniseerimisi ei leitud.", style="color:gray")]

    rows = []
    for entry in sync_logs:
        started = entry["started_at"]
        started_str = started.strftime("%d.%m.%Y %H:%M") if started else "—"
        status = entry["status"]
        color = _STATUS_COLORS.get(status, "black")
        label = _STATUS_LABELS.get(status, status)
        rows.append(
            Tr(
                Td(started_str),
                Td(Span(label, style=f"color:{color};font-weight:bold")),
                Td(str(entry["entity_count"]) if entry["entity_count"] is not None else "—"),
                Td(entry["error_message"] or "—"),
            )
        )
    return [
        Table(
            Thead(Tr(Th("Algusaeg"), Th("Staatus"), Th("Olemeid"), Th("Veateade"))),
            Tbody(*rows),
        )
    ]


def _render_user_stats_section(stats: dict) -> list:  # type: ignore[type-arg]
    """Render the user stats section."""
    content: list = [
        Table(
            Tbody(
                Tr(Th("Kasutajaid kokku"), Td(str(stats["total_users"]))),
                Tr(Th("Aktiivseid seansse"), Td(str(stats["active_sessions"]))),
            )
        )
    ]

    if stats["users_per_org"]:
        org_rows = [
            Tr(Td(org["org_name"]), Td(str(org["user_count"]))) for org in stats["users_per_org"]
        ]
        content.append(H4("Kasutajaid organisatsioonide kaupa"))
        content.append(
            Table(
                Thead(Tr(Th("Organisatsioon"), Th("Kasutajaid"))),
                Tbody(*org_rows),
            )
        )

    return content


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def admin_dashboard_page(req: Request):
    """GET /admin — admin dashboard with system overview."""
    jena_ok = jena_check_health()
    pg_ok = _check_postgres()
    sync_logs = _get_sync_logs()
    user_stats = _get_user_stats()

    content = [
        H3("Süsteemi tervis"),
        *_render_health_section(jena_ok, pg_ok),
        H3("Sünkroniseerimise staatus"),
        *_render_sync_section(sync_logs),
        H3("Kasutajate statistika"),
        *_render_user_stats_section(user_stats),
        H3("Kiirlingid"),
        Ul(
            Li(A("Organisatsioonid", href="/admin/organizations")),
            Li(A("Kasutajad", href="/admin/users")),
            Li(A("Auditilogi", href="/admin/audit")),
        ),
    ]

    return Titled("Administreerimise töölaud", *content)


def admin_audit_page(req: Request):
    """GET /admin/audit — paginated audit log viewer."""
    page_str = req.query_params.get("page", "1")
    try:
        page = max(1, int(page_str))
    except ValueError:
        page = 1

    per_page = 25
    entries, total = _get_audit_log_page(page, per_page)
    total_pages = max(1, (total + per_page - 1) // per_page)

    if not entries:
        table = P("Auditilogis kirjeid ei leitud.", style="color:gray")
    else:
        rows = []
        for entry in entries:
            ts = entry["created_at"]
            ts_str = ts.strftime("%d.%m.%Y %H:%M") if ts else "—"
            rows.append(
                Tr(
                    Td(ts_str),
                    Td(entry["user_name"]),
                    Td(entry["action"]),
                    Td(str(entry["detail"]) if entry["detail"] else "—"),
                )
            )
        table = Table(
            Thead(Tr(Th("Aeg"), Th("Kasutaja"), Th("Tegevus"), Th("Detailid"))),
            Tbody(*rows),
        )

    # Pagination nav
    nav_items = []
    if page > 1:
        nav_items.append(A("Eelmine", href=f"/admin/audit?page={page - 1}"))
    nav_items.append(Span(f" Lehekülg {page}/{total_pages} ({total} kirjet) "))
    if page < total_pages:
        nav_items.append(A("Järgmine", href=f"/admin/audit?page={page + 1}"))

    return Titled(
        "Auditilogi",
        A("Tagasi adminipaneelile", href="/admin"),
        table,
        P(*nav_items),
    )


def health_check(req: Request):
    """GET /api/health — JSON health check endpoint (unauthenticated).

    Returns a JSON response suitable for Coolify or uptime monitoring:
    {"status": "ok", "jena": true/false, "postgres": true/false}
    """
    jena_ok = jena_check_health()
    pg_ok = _check_postgres()
    overall = "ok" if (jena_ok and pg_ok) else "degraded"

    return JSONResponse({"status": overall, "jena": jena_ok, "postgres": pg_ok})


# ---------------------------------------------------------------------------
# Apply admin role decorator
# ---------------------------------------------------------------------------

_admin_dashboard = require_role("admin")(admin_dashboard_page)
_admin_audit = require_role("admin")(admin_audit_page)


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_admin_routes(rt) -> None:  # type: ignore[no-untyped-def]
    """Register admin dashboard routes on the FastHTML route decorator *rt*."""
    rt("/admin", methods=["GET"])(_admin_dashboard)
    rt("/admin/audit", methods=["GET"])(_admin_audit)
    rt("/api/health", methods=["GET"])(health_check)
