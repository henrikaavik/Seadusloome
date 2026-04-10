"""Admin user statistics card and quick links card."""

from __future__ import annotations

import logging

from fasthtml.common import *  # noqa: F403

from app.admin._shared import _tooltip
from app.db import get_connection as _connect
from app.ui.data.data_table import Column, DataTable
from app.ui.primitives.badge import Badge
from app.ui.primitives.button import Button  # noqa: F401, F811  -- shadow guard
from app.ui.surfaces.card import Card, CardBody, CardHeader

logger = logging.getLogger(__name__)


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


def _user_stats_card(stats: dict):  # type: ignore[type-arg]
    """Render the user statistics card."""
    summary = Dl(  # noqa: F405
        Dt("Kasutajaid kokku"),  # noqa: F405
        Dd(Badge(str(stats["total_users"]), variant="primary")),  # noqa: F405
        Dt("Aktiivseid seansse"),  # noqa: F405
        Dd(Badge(str(stats["active_sessions"]), variant="default")),  # noqa: F405
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
        body_children.append(H4("Kasutajaid organisatsioonide kaupa", cls="section-subtitle"))  # noqa: F405
        body_children.append(DataTable(columns=columns, rows=rows))

    return Card(
        CardHeader(
            H3(  # noqa: F405
                "Kasutajate statistika",
                _tooltip("Kasutajate arv, seansid, jaotus organisatsioonide kaupa"),
                cls="card-title",
            )
        ),
        CardBody(*body_children),
    )


def _quick_links_card():
    """Render the quick links card."""
    return Card(
        CardHeader(H3("Kiirlingid", cls="card-title")),  # noqa: F405
        CardBody(
            Ul(  # noqa: F405
                Li(A("Organisatsioonid", href="/admin/organizations")),  # noqa: F405
                Li(A("Kasutajad", href="/admin/users")),  # noqa: F405
                Li(A("Auditilogi", href="/admin/audit")),  # noqa: F405
                cls="quick-links",
            )
        ),
    )
