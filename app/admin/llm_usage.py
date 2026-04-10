"""Admin LLM usage statistics card."""

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


def _get_llm_usage_stats() -> dict:  # type: ignore[type-arg]
    """Return LLM usage stats for the current month."""
    stats: dict = {  # type: ignore[type-arg]
        "total_input": 0,
        "total_output": 0,
        "total_cost": 0.0,
        "top_features": [],
    }
    try:
        with _connect() as conn:
            # Totals for the current month
            row = conn.execute(
                "SELECT COALESCE(SUM(tokens_input), 0), "
                "COALESCE(SUM(tokens_output), 0), "
                "COALESCE(SUM(cost_usd), 0) "
                "FROM llm_usage WHERE created_at >= date_trunc('month', now())"
            ).fetchone()
            if row:
                stats["total_input"] = row[0]
                stats["total_output"] = row[1]
                stats["total_cost"] = float(row[2])

            # Top 3 features by cost
            rows = conn.execute(
                "SELECT feature, SUM(tokens_input), SUM(tokens_output), SUM(cost_usd) "
                "FROM llm_usage WHERE created_at >= date_trunc('month', now()) "
                "GROUP BY feature ORDER BY SUM(cost_usd) DESC LIMIT 3"
            ).fetchall()
            stats["top_features"] = [
                {
                    "feature": r[0],
                    "tokens_input": r[1],
                    "tokens_output": r[2],
                    "cost_usd": float(r[3]),
                }
                for r in rows
            ]
    except Exception:
        logger.exception("Failed to fetch LLM usage stats")
    return stats


def _llm_usage_card():
    """Render the LLM usage card for the admin dashboard."""
    stats = _get_llm_usage_stats()

    total_tokens = stats["total_input"] + stats["total_output"]
    summary = Dl(  # noqa: F405
        Dt("Tokeneid kokku (kuu)"),  # noqa: F405
        Dd(Badge(f"{total_tokens:,}", variant="primary")),  # noqa: F405
        Dt("Sisend-tokenid"),  # noqa: F405
        Dd(Badge(f"{stats['total_input']:,}", variant="default")),  # noqa: F405
        Dt("Väljund-tokenid"),  # noqa: F405
        Dd(Badge(f"{stats['total_output']:,}", variant="default")),  # noqa: F405
        Dt("Kulu kokku (USD)"),  # noqa: F405
        Dd(Badge(f"${stats['total_cost']:.4f}", variant="primary")),  # noqa: F405
        cls="info-list",
    )

    body_children: list = [summary]

    if stats["top_features"]:
        columns = [
            Column(key="feature", label="Funktsioon", sortable=False),
            Column(key="tokens", label="Tokeneid", sortable=False),
            Column(key="cost", label="Kulu (USD)", sortable=False),
        ]
        rows = [
            {
                "feature": f["feature"],
                "tokens": f"{f['tokens_input'] + f['tokens_output']:,}",
                "cost": f"${f['cost_usd']:.4f}",
            }
            for f in stats["top_features"]
        ]
        body_children.append(H4("Top 3 funktsiooni kulu järgi", cls="section-subtitle"))  # noqa: F405
        body_children.append(DataTable(columns=columns, rows=rows))

    return Card(
        CardHeader(
            H3(  # noqa: F405
                "LLM kasutus",
                _tooltip("Tokenite ja kulu statistika jooksvast kuust"),
                cls="card-title",
            )
        ),
        CardBody(*body_children),
        id="llm-usage-card",
    )
