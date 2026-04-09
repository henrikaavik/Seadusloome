"""Admin dashboard with system health, sync status, user stats, and audit log."""

from __future__ import annotations

import logging
import threading

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.auth.roles import require_role
from app.db import get_connection as _connect
from app.jobs.queue import Job, JobQueue
from app.sync.jena_loader import check_health as jena_check_health
from app.ui.data.data_table import Column, DataTable
from app.ui.data.pagination import Pagination
from app.ui.forms.app_form import AppForm
from app.ui.layout import PageShell
from app.ui.primitives.badge import Badge, StatusBadge
from app.ui.primitives.button import Button
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.theme import get_theme_from_request

# Module-level lock so two admins clicking "Sync now" at the same time
# don't trigger two parallel clones.
_sync_lock = threading.Lock()
_sync_in_progress = False

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

_SYNC_STATUS_MAP = {
    "running": ("running", "Käimas"),
    "success": ("ok", "Õnnestus"),
    "failed": ("failed", "Ebaõnnestus"),
}


def _sync_status_badge(status: str):
    """Return a StatusBadge for a sync_log status value."""
    key, _ = _SYNC_STATUS_MAP.get(status, ("pending", status))
    return StatusBadge(key)  # type: ignore[arg-type]


def _health_card(jena_ok: bool, pg_ok: bool):
    """Render the system health card."""
    body = Dl(
        Dt("Apache Jena Fuseki"),
        Dd(StatusBadge("ok") if jena_ok else StatusBadge("failed")),
        Dt("PostgreSQL"),
        Dd(StatusBadge("ok") if pg_ok else StatusBadge("failed")),
        cls="info-list",
    )
    return Card(
        CardHeader(H3("Süsteemi tervis", cls="card-title")),
        CardBody(body),
    )


def _sync_trigger_form():
    """Render the 'Sync now' button as an HTMX form.

    Posts to /admin/sync. The endpoint swaps this same card in-place
    with a confirmation message so the admin gets immediate feedback
    without a full-page reload.
    """
    return AppForm(
        Button(
            "Sünkroniseeri kohe",
            type="submit",
            variant="primary",
            size="sm",
            cls="sync-trigger-btn",
        ),
        method="post",
        action="/admin/sync",
        hx_post="/admin/sync",
        hx_target="#sync-card",
        hx_swap="outerHTML",
        cls="sync-trigger-form",
    )


def _sync_card(sync_logs: list[dict], *, status_banner: tuple[str, str] | None = None):  # type: ignore[type-arg]
    """Render the sync status card.

    Args:
        sync_logs: recent sync_log rows from the DB
        status_banner: optional (variant, message) tuple shown above the
            log table — used by POST /admin/sync to surface 'queued' /
            'already running' feedback.
    """
    if not sync_logs:
        body = P("Sünkroniseerimisi ei leitud.", cls="muted-text")
    else:
        columns = [
            Column(key="started", label="Algusaeg", sortable=False),
            Column(
                key="status",
                label="Staatus",
                sortable=False,
                render=lambda r: _sync_status_badge(r["status_raw"]),
            ),
            Column(key="entity_count", label="Olemeid", sortable=False),
            Column(key="error_message", label="Veateade", sortable=False),
        ]
        rows = []
        for entry in sync_logs:
            started = entry["started_at"]
            rows.append(
                {
                    "started": started.strftime("%d.%m.%Y %H:%M") if started else "—",
                    "status_raw": entry["status"],
                    "status": entry["status"],
                    "entity_count": (
                        str(entry["entity_count"]) if entry["entity_count"] is not None else "—"
                    ),
                    "error_message": entry["error_message"] or "—",
                }
            )
        body = DataTable(columns=columns, rows=rows)

    body_nodes: list = []
    if status_banner is not None:
        variant, message = status_banner
        body_nodes.append(Div(message, cls=f"sync-banner sync-banner-{variant}", role="status"))
    body_nodes.append(body)
    body_nodes.append(_sync_trigger_form())

    return Card(
        CardHeader(H3("Sünkroniseerimise staatus", cls="card-title")),
        CardBody(*body_nodes),
        id="sync-card",
    )


def _get_job_queue_snapshot() -> dict:  # type: ignore[type-arg]
    """Fetch recent jobs grouped by status for the admin card.

    Swallows DB errors so the rest of the dashboard still renders if
    Postgres is temporarily unreachable — the health card above this
    one will already have flagged the outage.
    """
    snapshot: dict = {  # type: ignore[type-arg]
        "pending": [],
        "running": [],
        "failed": [],
        "retrying": [],
    }
    try:
        queue = JobQueue()
        snapshot["pending"] = queue.list_by_status("pending", limit=5)
        snapshot["running"] = queue.list_by_status("running", limit=5)
        snapshot["failed"] = queue.list_by_status("failed", limit=5)
        # #455: surface ``retrying`` for operational visibility. After
        # the #441 fix the queue no longer parks jobs there, but the
        # CHECK constraint still allows it and we want any drift to
        # show up loudly on the dashboard rather than silently.
        snapshot["retrying"] = queue.list_by_status("retrying", limit=5)
    except Exception:
        logger.exception("Failed to fetch job queue snapshot")
    return snapshot


def _job_queue_card():
    """Render the background job queue status card.

    Shows counts of pending/running/failed/retrying jobs at a glance,
    plus a table of the most recent failures so an admin can spot
    broken pipelines without needing to SSH into Postgres.
    """
    snapshot = _get_job_queue_snapshot()
    pending: list[Job] = snapshot["pending"]
    running: list[Job] = snapshot["running"]
    failed: list[Job] = snapshot["failed"]
    retrying: list[Job] = snapshot["retrying"]

    has_any = bool(pending or running or failed or retrying)

    if not has_any:
        body: object = P("Taustajobisid pole.", cls="muted-text")
        return Card(
            CardHeader(H3("Taustajobide järjekord", cls="card-title")),
            CardBody(body),
            id="job-queue-card",
        )

    # #477: the retrying badge should only render when there's
    # actually something to retry. After the #441 fix this should
    # normally be 0; keeping a ``0 kordab`` badge permanently in the
    # UI made it look like a live metric rather than a drift guard.
    # The underlying query still runs so drift still surfaces loudly
    # the moment a row ends up in ``retrying``.
    summary_children: list = [
        Badge(f"{len(pending)} ootel", variant="default"),
        " ",
        Badge(f"{len(running)} töötab", variant="primary"),
        " ",
    ]
    if len(retrying) > 0:
        summary_children.append(Badge(f"{len(retrying)} kordab", variant="warning"))
        summary_children.append(" ")
    summary_children.append(Badge(f"{len(failed)} ebaõnnestus", variant="danger"))
    summary = Div(*summary_children, cls="job-queue-summary")

    body_children: list = [summary]

    if failed:
        columns = [
            Column(key="job_type", label="Tüüp", sortable=False),
            Column(key="error_message", label="Viga", sortable=False),
            Column(key="attempts", label="Katseid", sortable=False),
            Column(key="finished_at", label="Lõpetatud", sortable=False),
        ]
        rows = []
        for job in failed:
            error_raw = job.error_message or "—"
            # Truncate long error messages for readability in the card.
            if len(error_raw) > 120:
                error_raw = error_raw[:117] + "..."
            finished = job.finished_at
            rows.append(
                {
                    "job_type": job.job_type,
                    "error_message": error_raw,
                    "attempts": f"{job.attempts}/{job.max_attempts}",
                    "finished_at": finished.strftime("%d.%m.%Y %H:%M") if finished else "—",
                }
            )
        body_children.append(H4("Viimased ebaõnnestunud jobid", cls="section-subtitle"))
        body_children.append(DataTable(columns=columns, rows=rows))

    return Card(
        CardHeader(H3("Taustajobide järjekord", cls="card-title")),
        CardBody(*body_children),
        id="job-queue-card",
    )


def _user_stats_card(stats: dict):  # type: ignore[type-arg]
    """Render the user statistics card."""
    summary = Dl(
        Dt("Kasutajaid kokku"),
        Dd(Badge(str(stats["total_users"]), variant="primary")),
        Dt("Aktiivseid seansse"),
        Dd(Badge(str(stats["active_sessions"]), variant="default")),
        cls="info-list",
    )

    body_children: list = [summary]

    if stats["users_per_org"]:
        columns = [
            Column(key="org_name", label="Organisatsioon", sortable=False),
            Column(key="user_count", label="Kasutajaid", sortable=False),
        ]
        rows = [
            {"org_name": org["org_name"], "user_count": str(org["user_count"])}
            for org in stats["users_per_org"]
        ]
        body_children.append(H4("Kasutajaid organisatsioonide kaupa", cls="section-subtitle"))
        body_children.append(DataTable(columns=columns, rows=rows))

    return Card(
        CardHeader(H3("Kasutajate statistika", cls="card-title")),
        CardBody(*body_children),
    )


def _quick_links_card():
    """Render the quick links card."""
    return Card(
        CardHeader(H3("Kiirlingid", cls="card-title")),
        CardBody(
            Ul(
                Li(A("Organisatsioonid", href="/admin/organizations")),
                Li(A("Kasutajad", href="/admin/users")),
                Li(A("Auditilogi", href="/admin/audit")),
                cls="quick-links",
            )
        ),
    )


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def admin_dashboard_page(req: Request):
    """GET /admin — admin dashboard with system overview."""
    auth = req.scope.get("auth")
    theme = get_theme_from_request(req)

    jena_ok = jena_check_health()
    pg_ok = _check_postgres()
    sync_logs = _get_sync_logs()
    user_stats = _get_user_stats()

    content = (
        H1("Administreerimise töölaud", cls="page-title"),
        _health_card(jena_ok, pg_ok),
        _sync_card(sync_logs),
        _job_queue_card(),
        _user_stats_card(user_stats),
        _quick_links_card(),
    )

    return PageShell(
        *content,
        title="Administreerimise töölaud",
        user=auth,
        theme=theme,
        active_nav="/admin",
    )


def admin_audit_page(req: Request):
    """GET /admin/audit — paginated audit log viewer."""
    auth = req.scope.get("auth")
    theme = get_theme_from_request(req)

    page_str = req.query_params.get("page", "1")
    try:
        page = max(1, int(page_str))
    except ValueError:
        page = 1

    per_page = 25
    entries, total = _get_audit_log_page(page, per_page)
    total_pages = max(1, (total + per_page - 1) // per_page)

    if not entries:
        body: object = P("Auditilogis kirjeid ei leitud.", cls="muted-text")
    else:
        columns = [
            Column(key="time", label="Aeg", sortable=False),
            Column(key="user_name", label="Kasutaja", sortable=False),
            Column(key="action", label="Tegevus", sortable=False),
            Column(key="detail", label="Detailid", sortable=False),
        ]
        rows = []
        for entry in entries:
            ts = entry["created_at"]
            rows.append(
                {
                    "time": ts.strftime("%d.%m.%Y %H:%M") if ts else "—",
                    "user_name": entry["user_name"],
                    "action": entry["action"],
                    "detail": str(entry["detail"]) if entry["detail"] else "—",
                }
            )
        body = DataTable(columns=columns, rows=rows)

    pagination = Pagination(
        current_page=page,
        total_pages=total_pages,
        base_url="/admin/audit",
        page_size=per_page,
        total=total,
    )

    content = (
        H1("Auditilogi", cls="page-title"),
        P(A("← Tagasi adminipaneelile", href="/admin"), cls="back-link"),
        Card(
            CardHeader(H3("Kirjed", cls="card-title")),
            CardBody(body, pagination),
        ),
    )

    return PageShell(
        *content,
        title="Auditilogi",
        user=auth,
        theme=theme,
        active_nav="/admin",
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


def _run_sync_and_clear_flag():
    """Background wrapper: runs the sync pipeline and clears the in-progress flag."""
    global _sync_in_progress
    try:
        # Imported here to avoid circular dependency on app.templates during
        # module load, and to ensure the sync uses the runtime env vars.
        from app.sync.orchestrator import run_sync

        run_sync()
    except Exception:
        logger.exception("Admin-triggered sync raised an unhandled exception")
    finally:
        with _sync_lock:
            _sync_in_progress = False


def trigger_sync(req: Request):
    """POST /admin/sync — admin-only sync trigger.

    Runs the ontology sync pipeline in a background thread so the request
    returns immediately. Re-renders the sync card with a status banner so
    an HTMX-capable client gets inline feedback; a plain form submit sees
    the same card on the next full page load via `/admin`.
    """
    global _sync_in_progress

    already_running = False
    with _sync_lock:
        if _sync_in_progress:
            already_running = True
        else:
            _sync_in_progress = True

    if already_running:
        banner = ("warning", "Sünkroniseerimine on juba käimas.")
    else:
        thread = threading.Thread(target=_run_sync_and_clear_flag, daemon=True)
        thread.start()
        banner = ("info", "Sünkroniseerimine käivitati — vaata tulemust allpool olevast logist.")

    sync_logs = _get_sync_logs()
    return _sync_card(sync_logs, status_banner=banner)


# ---------------------------------------------------------------------------
# Apply admin role decorator
# ---------------------------------------------------------------------------

_admin_dashboard = require_role("admin")(admin_dashboard_page)
_admin_audit = require_role("admin")(admin_audit_page)
_admin_sync = require_role("admin")(trigger_sync)


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_admin_routes(rt) -> None:  # type: ignore[no-untyped-def]
    """Register admin dashboard routes on the FastHTML route decorator *rt*."""
    rt("/admin", methods=["GET"])(_admin_dashboard)
    rt("/admin/audit", methods=["GET"])(_admin_audit)
    rt("/admin/sync", methods=["POST"])(_admin_sync)
    rt("/api/health", methods=["GET"])(health_check)
