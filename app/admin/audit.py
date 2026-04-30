"""Admin audit log page, filtering helpers, and CSV export."""

from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request
from starlette.responses import Response

from app.db import get_connection as _connect
from app.ui.data.data_table import Column, DataTable
from app.ui.data.pagination import Pagination
from app.ui.forms.app_form import AppForm
from app.ui.layout import PageShell
from app.ui.primitives.button import Button  # noqa: F401, F811  -- shadow guard
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.theme import get_theme_from_request
from app.ui.time import format_tallinn

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _parse_date(value: str | None) -> date | None:
    """Parse a date string (YYYY-MM-DD) or return None."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _build_audit_where(
    *,
    action: str | None = None,
    user_id: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    query: str | None = None,
) -> tuple[str, list]:
    """Build WHERE clause and params for filtered audit queries."""
    clauses: list[str] = []
    params: list = []

    if action:
        clauses.append("a.action = %s")
        params.append(action)
    if user_id:
        clauses.append("a.user_id = %s")
        params.append(user_id)
    if date_from:
        clauses.append("a.created_at >= %s")
        params.append(datetime.combine(date_from, datetime.min.time()))
    if date_to:
        clauses.append("a.created_at < %s")
        # Use day after to include all entries on the to-date
        from datetime import timedelta

        params.append(datetime.combine(date_to + timedelta(days=1), datetime.min.time()))
    if query:
        clauses.append("a.detail::text ILIKE %s")
        params.append(f"%{query}%")

    where = " AND ".join(clauses) if clauses else "TRUE"
    return where, params


def _get_audit_log_page(
    page: int = 1,
    per_page: int = 25,
    *,
    action: str | None = None,
    user_id: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    query: str | None = None,
) -> tuple[list[dict], int]:  # type: ignore[type-arg]
    """Return a page of audit_log entries and total count, with optional filters."""
    entries: list[dict] = []  # type: ignore[type-arg]
    total = 0
    try:
        where, params = _build_audit_where(
            action=action, user_id=user_id, date_from=date_from, date_to=date_to, query=query
        )
        with _connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM audit_log a WHERE {where}",  # type: ignore[arg-type]
                params,
            ).fetchone()
            total = row[0] if row else 0

            offset = (page - 1) * per_page
            rows = conn.execute(
                f"SELECT a.id, a.user_id, u.full_name, a.action, a.detail, a.created_at "  # type: ignore[arg-type]
                f"FROM audit_log a "
                f"LEFT JOIN users u ON u.id = a.user_id "
                f"WHERE {where} "
                f"ORDER BY a.created_at DESC LIMIT %s OFFSET %s",
                [*params, per_page, offset],
            ).fetchall()
            entries = [
                {
                    "id": r[0],
                    "user_id": str(r[1]) if r[1] else None,
                    "user_name": r[2] or "S\u00fcsteem",
                    "action": r[3],
                    "detail": r[4],
                    "created_at": r[5],
                }
                for r in rows
            ]
    except Exception:
        logger.exception("Failed to fetch audit log page %d", page)
    return entries, total


def _get_distinct_actions() -> list[str]:
    """Return sorted distinct action values from audit_log."""
    try:
        with _connect() as conn:
            rows = conn.execute("SELECT DISTINCT action FROM audit_log ORDER BY action").fetchall()
            return [r[0] for r in rows]
    except Exception:
        logger.exception("Failed to fetch distinct audit actions")
        return []


def _get_audit_users() -> list[dict]:  # type: ignore[type-arg]
    """Return users that have audit log entries (id + name)."""
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT a.user_id, COALESCE(u.full_name, 'S\u00fcsteem') AS name "
                "FROM audit_log a "
                "LEFT JOIN users u ON u.id = a.user_id "
                "WHERE a.user_id IS NOT NULL "
                "ORDER BY name"
            ).fetchall()
            return [{"id": str(r[0]), "name": r[1]} for r in rows]
    except Exception:
        logger.exception("Failed to fetch audit users")
        return []


def _get_all_filtered_entries(
    *,
    action: str | None = None,
    user_id: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    query: str | None = None,
) -> list[dict]:  # type: ignore[type-arg]
    """Return ALL matching audit entries (no pagination) for CSV export."""
    entries: list[dict] = []  # type: ignore[type-arg]
    try:
        where, params = _build_audit_where(
            action=action, user_id=user_id, date_from=date_from, date_to=date_to, query=query
        )
        with _connect() as conn:
            rows = conn.execute(
                f"SELECT a.id, a.user_id, u.full_name, a.action, a.detail, a.created_at "  # type: ignore[arg-type]
                f"FROM audit_log a "
                f"LEFT JOIN users u ON u.id = a.user_id "
                f"WHERE {where} "
                f"ORDER BY a.created_at DESC",
                params,
            ).fetchall()
            entries = [
                {
                    "id": r[0],
                    "user_id": str(r[1]) if r[1] else "",
                    "user_name": r[2] or "S\u00fcsteem",
                    "action": r[3],
                    "detail": str(r[4]) if r[4] else "",
                    "created_at": r[5],
                }
                for r in rows
            ]
    except Exception:
        logger.exception("Failed to fetch all audit entries for export")
    return entries


# ---------------------------------------------------------------------------
# Filter form component
# ---------------------------------------------------------------------------


def _extract_filters(req: Request) -> dict:  # type: ignore[type-arg]
    """Extract filter values from query params."""
    return {
        "action": req.query_params.get("action", ""),
        "user": req.query_params.get("user", ""),
        "from": req.query_params.get("from", ""),
        "to": req.query_params.get("to", ""),
        "query": req.query_params.get("query", ""),
    }


def _audit_filter_form(
    filters: dict,  # type: ignore[type-arg]
    actions: list[str],
    users: list[dict],  # type: ignore[type-arg]
) -> object:
    """Render the audit log filter controls."""
    # Action type dropdown
    action_options = [Option("K\u00f5ik tegevused", value="")]
    for act in actions:
        selected = "selected" if filters["action"] == act else None
        action_options.append(Option(act, value=act, selected=selected))

    # User selector
    user_options = [Option("K\u00f5ik kasutajad", value="")]
    for u in users:
        selected = "selected" if filters["user"] == u["id"] else None
        user_options.append(Option(u["name"], value=u["id"], selected=selected))

    return AppForm(
        Div(  # noqa: F405
            Div(  # noqa: F405
                Label("Tegevus", fr="filter-action"),  # noqa: F405
                Select(*action_options, name="action", id="filter-action"),  # noqa: F405
                cls="filter-field",
            ),
            Div(  # noqa: F405
                Label("Kasutaja", fr="filter-user"),  # noqa: F405
                Select(*user_options, name="user", id="filter-user"),  # noqa: F405
                cls="filter-field",
            ),
            Div(  # noqa: F405
                Label("Alguskuup\u00e4ev", fr="filter-from"),  # noqa: F405
                Input(  # noqa: F405
                    type="date", name="from", id="filter-from", value=filters["from"]
                ),
                cls="filter-field",
            ),
            Div(  # noqa: F405
                Label("L\u00f5ppkuup\u00e4ev", fr="filter-to"),  # noqa: F405
                Input(type="date", name="to", id="filter-to", value=filters["to"]),  # noqa: F405
                cls="filter-field",
            ),
            Div(  # noqa: F405
                Label("Otsing detailides", fr="filter-query"),  # noqa: F405
                Input(  # noqa: F405
                    type="text",
                    name="query",
                    id="filter-query",
                    value=filters["query"],
                    placeholder="Otsi detailidest...",
                ),
                cls="filter-field",
            ),
            cls="filter-row",
        ),
        Div(  # noqa: F405
            Button("Filtreeri", type="submit", variant="primary", size="sm"),
            A(  # noqa: F405
                "T\u00fchjenda",
                href="/admin/audit",
                cls="btn btn-secondary btn-sm",
            ),
            cls="filter-actions",
        ),
        method="get",
        action="/admin/audit",
        hx_get="/admin/audit",
        hx_target="#audit-results",
        hx_swap="innerHTML",
        hx_push_url="true",
        cls="audit-filter-form",
    )


def _csv_export_link(filters: dict) -> object:  # type: ignore[type-arg]
    """Render an export-to-CSV link/button with current filters."""
    params = {k: v for k, v in filters.items() if v}
    from urllib.parse import urlencode

    qs = urlencode(params) if params else ""
    href = f"/admin/audit/export?{qs}" if qs else "/admin/audit/export"
    return A(  # noqa: F405
        "Ekspordi CSV",
        href=href,
        cls="btn btn-secondary btn-sm",
        download="auditilogi.csv",
    )


# ---------------------------------------------------------------------------
# Page handler
# ---------------------------------------------------------------------------


def _audit_results_content(
    entries: list[dict],  # type: ignore[type-arg]
    page: int,
    total_pages: int,
    per_page: int,
    total: int,
    filters: dict,  # type: ignore[type-arg]
) -> tuple:
    """Render the audit table + pagination (the swappable content)."""
    if not entries:
        body: object = P("Auditilogis kirjeid ei leitud.", cls="muted-text")  # noqa: F405
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
                    "time": format_tallinn(ts),
                    "user_name": entry["user_name"],
                    "action": entry["action"],
                    "detail": str(entry["detail"]) if entry["detail"] else "\u2014",
                }
            )
        body = DataTable(columns=columns, rows=rows)

    # Build base_url with current filters for pagination links
    from urllib.parse import urlencode

    filter_params = {k: v for k, v in filters.items() if v}
    qs = urlencode(filter_params) if filter_params else ""
    base_url = f"/admin/audit?{qs}" if qs else "/admin/audit"

    pagination = Pagination(
        current_page=page,
        total_pages=total_pages,
        base_url=base_url,
        page_size=per_page,
        total=total,
    )

    return body, pagination


def admin_audit_page(req: Request):
    """GET /admin/audit -- paginated, filterable audit log viewer.

    The body is wrapped in a top-level try/except so any backend failure
    (missing materialized view, transient DB error, malformed row) renders
    a styled error banner instead of bubbling up as a raw 500.

    Helpers are imported as locals inside the function body so the page
    works correctly when the function is rebound by the
    ``app.templates.admin_dashboard`` shim \u2014 that shim swaps
    ``__globals__`` to its own module dict, which means private helpers
    that live in this module cannot be resolved via the function's
    global namespace.
    """
    auth = req.scope.get("auth")
    theme = get_theme_from_request(req)
    try:
        # Bind module-private helpers as locals so __globals__ rebinding
        # by the admin_dashboard shim does not break name resolution.
        from app.admin.audit import (
            _audit_filter_form,
            _audit_results_content,
            _csv_export_link,
            _extract_filters,
            _get_audit_log_page,
            _get_audit_users,
            _get_distinct_actions,
            _parse_date,
        )

        filters = _extract_filters(req)

        page_str = req.query_params.get("page", "1")
        try:
            page = max(1, int(page_str))
        except ValueError:
            page = 1

        per_page = 25
        action_filter = filters["action"] or None
        user_filter = filters["user"] or None
        date_from = _parse_date(filters["from"])
        date_to = _parse_date(filters["to"])
        query_filter = filters["query"] or None

        entries, total = _get_audit_log_page(
            page,
            per_page,
            action=action_filter,
            user_id=user_filter,
            date_from=date_from,
            date_to=date_to,
            query=query_filter,
        )
        total_pages = max(1, (total + per_page - 1) // per_page)

        # Fetch filter options
        actions = _get_distinct_actions()
        users = _get_audit_users()

        body, pagination = _audit_results_content(
            entries, page, total_pages, per_page, total, filters
        )

        filter_form = _audit_filter_form(filters, actions, users)
        export_link = _csv_export_link(filters)

        content = (
            H1("Auditilogi", cls="page-title"),  # noqa: F405
            P(A("\u2190 Tagasi adminipaneelile", href="/admin"), cls="back-link"),  # noqa: F405
            Card(
                CardHeader(
                    Div(  # noqa: F405
                        H3("Filtrid", cls="card-title"),  # noqa: F405
                        export_link,
                        cls="card-header-row",
                    ),
                ),
                CardBody(filter_form),
            ),
            Card(
                CardHeader(H3("Kirjed", cls="card-title")),  # noqa: F405
                CardBody(Div(body, pagination, id="audit-results")),  # noqa: F405
            ),
        )

        return PageShell(
            *content,
            title="Auditilogi",
            user=auth,
            theme=theme,
            active_nav="/admin",
        )
    except Exception:
        logger.exception("Failed to render admin audit page")
        from app.admin._shared import _render_admin_error_page

        return _render_admin_error_page(title="Auditilogi", user=auth, theme=theme)


# ---------------------------------------------------------------------------
# CSV export handler
# ---------------------------------------------------------------------------


def admin_audit_export(req: Request):
    """GET /admin/audit/export -- download filtered audit log as CSV.

    Helpers are imported as locals so this handler works correctly when
    rebound by the admin_dashboard shim. On error returns a styled
    error response rather than a raw 500.
    """
    try:
        # Bind module-private helpers as locals so __globals__ rebinding
        # by the admin_dashboard shim does not break name resolution.
        from app.admin.audit import (
            _extract_filters,
            _get_all_filtered_entries,
            _parse_date,
        )
        from app.ui.time import format_tallinn

        filters = _extract_filters(req)

        action_filter = filters["action"] or None
        user_filter = filters["user"] or None
        date_from = _parse_date(filters["from"])
        date_to = _parse_date(filters["to"])
        query_filter = filters["query"] or None

        entries = _get_all_filtered_entries(
            action=action_filter,
            user_id=user_filter,
            date_from=date_from,
            date_to=date_to,
            query=query_filter,
        )

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["ID", "Kasutaja ID", "Kasutaja", "Tegevus", "Detailid", "Kuup\u00e4ev"])

        for entry in entries:
            ts = entry["created_at"]
            writer.writerow(
                [
                    entry["id"],
                    entry["user_id"],
                    entry["user_name"],
                    entry["action"],
                    entry["detail"],
                    format_tallinn(ts) if ts else "",
                ]
            )

        csv_content = output.getvalue()
        return Response(
            content=csv_content,
            media_type="text/csv",
            headers={
                "Content-Disposition": "attachment; filename=auditilogi.csv",
            },
        )
    except Exception:
        logger.exception("Failed to export admin audit log")
        return Response(
            content="Andmete eksportimine eba\u00f5nnestus.",
            status_code=500,
            media_type="text/plain; charset=utf-8",
        )
