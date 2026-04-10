"""Admin rate limit / usage-vs-budget card."""

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


def _get_rate_limit_stats() -> dict:  # type: ignore[type-arg]
    """Return per-org cost vs budget and top-user message rates for the admin card."""
    import os

    max_cost = float(os.environ.get("ORG_MAX_MONTHLY_COST_USD", "50.0"))
    max_msgs = int(os.environ.get("CHAT_MAX_MESSAGES_PER_HOUR", "100"))

    stats: dict = {  # type: ignore[type-arg]
        "max_monthly_cost_usd": max_cost,
        "max_messages_per_hour": max_msgs,
        "org_costs": [],
        "top_users_hourly": [],
    }
    try:
        with _connect() as conn:
            # Per-org monthly cost
            rows = conn.execute(
                "SELECT o.name, COALESCE(SUM(u.cost_usd), 0) AS total_cost "
                "FROM organizations o "
                "LEFT JOIN llm_usage u ON u.org_id = o.id "
                "AND u.created_at >= date_trunc('month', now()) "
                "GROUP BY o.id, o.name ORDER BY total_cost DESC LIMIT 10"
            ).fetchall()
            stats["org_costs"] = [{"org_name": r[0], "cost_usd": float(r[1])} for r in rows]

            # Top users by message count in the last hour
            rows = conn.execute(
                "SELECT usr.full_name, COUNT(m.id) AS msg_count "
                "FROM messages m "
                "JOIN conversations c ON c.id = m.conversation_id "
                "JOIN users usr ON usr.id = c.user_id "
                "WHERE m.role = 'user' "
                "AND m.created_at > now() - interval '1 hour' "
                "GROUP BY usr.id, usr.full_name ORDER BY msg_count DESC LIMIT 5"
            ).fetchall()
            stats["top_users_hourly"] = [{"user_name": r[0], "message_count": r[1]} for r in rows]
    except Exception:
        logger.exception("Failed to fetch rate limit stats")
    return stats


def _rate_limit_card():
    """Render the usage vs limits card for the admin dashboard."""
    stats = _get_rate_limit_stats()

    summary = Dl(  # noqa: F405
        Dt("Sonumite limiit (tunnis)"),  # noqa: F405
        Dd(Badge(f"{stats['max_messages_per_hour']}", variant="default")),  # noqa: F405
        Dt("Kulu limiit (kuus, USD)"),  # noqa: F405
        Dd(Badge(f"${stats['max_monthly_cost_usd']:.2f}", variant="default")),  # noqa: F405
        cls="info-list",
    )

    body_children: list = [summary]

    if stats["org_costs"]:
        columns = [
            Column(key="org_name", label="Organisatsioon", sortable=False),
            Column(key="cost", label="Kulu (USD)", sortable=False),
            Column(key="budget_pct", label="Eelarvest", sortable=False),
        ]
        rows = []
        for oc in stats["org_costs"]:
            max_cost = stats["max_monthly_cost_usd"]
            pct = (oc["cost_usd"] / max_cost * 100) if max_cost > 0 else 0
            variant = "danger" if pct >= 90 else ("warning" if pct >= 70 else "default")
            rows.append(
                {
                    "org_name": oc["org_name"],
                    "cost": f"${oc['cost_usd']:.4f}",
                    "budget_pct": Badge(f"{pct:.0f}%", variant=variant),
                }
            )
        body_children.append(H4("Organisatsioonide kulu vs eelarve", cls="section-subtitle"))  # noqa: F405
        body_children.append(DataTable(columns=columns, rows=rows))

    if stats["top_users_hourly"]:
        columns = [
            Column(key="user_name", label="Kasutaja", sortable=False),
            Column(key="msg_count", label="Sonumeid (tund)", sortable=False),
            Column(key="limit_pct", label="Limiidist", sortable=False),
        ]
        rows = []
        for tu in stats["top_users_hourly"]:
            max_msgs = stats["max_messages_per_hour"]
            pct = (tu["message_count"] / max_msgs * 100) if max_msgs > 0 else 0
            variant = "danger" if pct >= 90 else ("warning" if pct >= 70 else "default")
            rows.append(
                {
                    "user_name": tu["user_name"],
                    "msg_count": str(tu["message_count"]),
                    "limit_pct": Badge(f"{pct:.0f}%", variant=variant),
                }
            )
        body_children.append(H4("Aktiivsemad kasutajad (viimane tund)", cls="section-subtitle"))  # noqa: F405
        body_children.append(DataTable(columns=columns, rows=rows))

    return Card(
        CardHeader(
            H3(  # noqa: F405
                "Kasutuslimiidid",
                _tooltip("Org kulu vs eelarve, kasutajate s\u00f5numite limiidid"),
                cls="card-title",
            )
        ),
        CardBody(*body_children),
        id="rate-limit-card",
    )
