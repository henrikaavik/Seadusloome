"""Admin audit log page and helpers."""

from __future__ import annotations

import logging

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request

from app.db import get_connection as _connect
from app.ui.data.data_table import Column, DataTable
from app.ui.data.pagination import Pagination
from app.ui.layout import PageShell
from app.ui.primitives.button import Button  # noqa: F401, F811  -- shadow guard
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.theme import get_theme_from_request

logger = logging.getLogger(__name__)


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
                    "time": ts.strftime("%d.%m.%Y %H:%M") if ts else "\u2014",
                    "user_name": entry["user_name"],
                    "action": entry["action"],
                    "detail": str(entry["detail"]) if entry["detail"] else "\u2014",
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
        H1("Auditilogi", cls="page-title"),  # noqa: F405
        P(A("\u2190 Tagasi adminipaneelile", href="/admin"), cls="back-link"),  # noqa: F405
        Card(
            CardHeader(H3("Kirjed", cls="card-title")),  # noqa: F405
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
