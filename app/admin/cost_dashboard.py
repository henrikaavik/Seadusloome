"""Admin LLM cost dashboard — full page with per-org, per-feature, per-model,
and monthly trend views over ``llm_usage`` table aggregations.
"""

from __future__ import annotations

import logging
import os

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request

from app.db import get_connection as _connect
from app.ui.data.data_table import Column, DataTable
from app.ui.layout import PageShell
from app.ui.primitives.badge import Badge
from app.ui.primitives.button import Button  # noqa: F401, F811  -- shadow guard
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.surfaces.info_box import InfoBox
from app.ui.theme import get_theme_from_request

logger = logging.getLogger(__name__)

# Feature labels in Estonian for the cost breakdown.
_FEATURE_LABELS: dict[str, str] = {
    "drafter_clarify": "Koostaja tapsustamine",
    "drafter_draft": "Koostaja mustand",
    "chat": "Vestlus",
    "extraction": "Eraldamine",
}


def _get_cost_by_org() -> list[dict]:  # type: ignore[type-arg]
    """Return total cost per org for the current month, with budget info."""
    max_cost = float(os.environ.get("ORG_MAX_MONTHLY_COST_USD", "50.0"))
    rows_out: list[dict] = []  # type: ignore[type-arg]
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT o.name, COALESCE(SUM(u.cost_usd), 0) AS total_cost "
                "FROM organizations o "
                "LEFT JOIN llm_usage u ON u.org_id = o.id "
                "AND u.created_at >= date_trunc('month', now()) "
                "GROUP BY o.id, o.name ORDER BY total_cost DESC"
            ).fetchall()
            rows_out = [
                {
                    "org_name": r[0],
                    "cost_usd": float(r[1]),
                    "budget_usd": max_cost,
                }
                for r in rows
            ]
    except Exception:
        logger.exception("Failed to fetch cost by org")
    return rows_out


def _get_cost_by_feature() -> list[dict]:  # type: ignore[type-arg]
    """Return cost breakdown by feature for the current month."""
    rows_out: list[dict] = []  # type: ignore[type-arg]
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT feature, "
                "COALESCE(SUM(tokens_input), 0), "
                "COALESCE(SUM(tokens_output), 0), "
                "COALESCE(SUM(cost_usd), 0) "
                "FROM llm_usage "
                "WHERE created_at >= date_trunc('month', now()) "
                "GROUP BY feature ORDER BY SUM(cost_usd) DESC"
            ).fetchall()
            rows_out = [
                {
                    "feature": r[0],
                    "tokens_input": r[1],
                    "tokens_output": r[2],
                    "cost_usd": float(r[3]),
                }
                for r in rows
            ]
    except Exception:
        logger.exception("Failed to fetch cost by feature")
    return rows_out


def _get_cost_by_model() -> list[dict]:  # type: ignore[type-arg]
    """Return cost breakdown by model for the current month."""
    rows_out: list[dict] = []  # type: ignore[type-arg]
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT model, "
                "COALESCE(SUM(tokens_input), 0), "
                "COALESCE(SUM(tokens_output), 0), "
                "COALESCE(SUM(cost_usd), 0) "
                "FROM llm_usage "
                "WHERE created_at >= date_trunc('month', now()) "
                "GROUP BY model ORDER BY SUM(cost_usd) DESC"
            ).fetchall()
            rows_out = [
                {
                    "model": r[0],
                    "tokens_input": r[1],
                    "tokens_output": r[2],
                    "cost_usd": float(r[3]),
                }
                for r in rows
            ]
    except Exception:
        logger.exception("Failed to fetch cost by model")
    return rows_out


def _get_monthly_trend(months: int = 6) -> list[dict]:  # type: ignore[type-arg]
    """Return monthly cost totals for the last *months* months."""
    rows_out: list[dict] = []  # type: ignore[type-arg]
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT date_trunc('month', created_at) AS month, "
                "COALESCE(SUM(tokens_input), 0), "
                "COALESCE(SUM(tokens_output), 0), "
                "COALESCE(SUM(cost_usd), 0) "
                "FROM llm_usage "
                "WHERE created_at >= date_trunc('month', now()) - interval '%s months' "
                "GROUP BY date_trunc('month', created_at) "
                "ORDER BY month DESC",
                (months,),
            ).fetchall()
            rows_out = [
                {
                    "month": r[0],
                    "tokens_input": r[1],
                    "tokens_output": r[2],
                    "cost_usd": float(r[3]),
                }
                for r in rows
            ]
    except Exception:
        logger.exception("Failed to fetch monthly trend")
    return rows_out


def _progress_bar(value: float, maximum: float) -> object:
    """Render an inline progress bar as a visual budget indicator."""
    pct = min(100, (value / maximum * 100)) if maximum > 0 else 0
    color = "#e74c3c" if pct >= 90 else ("#f39c12" if pct >= 70 else "#2e8b57")
    bar = (
        f'<div class="progress-bar-track" style="width:100%;height:8px;'
        f'background:#eee;border-radius:4px;overflow:hidden">'
        f'<div style="width:{pct:.0f}%;height:100%;background:{color};'
        f'border-radius:4px"></div></div>'
    )
    return Safe(bar)  # noqa: F405


def admin_cost_page(req: Request):
    """GET /admin/costs -- LLM cost dashboard page.

    Helpers are imported as locals inside the function body so the page
    works correctly when rebound by the ``app.templates.admin_dashboard``
    shim — that shim swaps ``__globals__`` to its own module dict, which
    means private helpers (``_progress_bar``, ``_FEATURE_LABELS``,
    ``_tooltip``) cannot be resolved via the function's global namespace.
    The whole body is wrapped in a try/except so any backend failure
    renders a styled error banner instead of bubbling up as a raw 500.
    """
    auth = req.scope.get("auth")
    theme = get_theme_from_request(req)
    try:
        from app.admin._shared import _tooltip
        from app.admin.cost_dashboard import (
            _FEATURE_LABELS,
            _get_cost_by_feature,
            _get_cost_by_model,
            _get_cost_by_org,
            _get_monthly_trend,
            _progress_bar,
        )

        org_costs = _get_cost_by_org()
        feature_costs = _get_cost_by_feature()
        model_costs = _get_cost_by_model()
        monthly = _get_monthly_trend()

        # ---- Card 1: Cost by org with progress bars ----
        if org_costs:
            org_columns = [
                Column(key="org_name", label="Organisatsioon", sortable=False),
                Column(key="cost", label="Kulu (USD)", sortable=False),
                Column(key="budget", label="Eelarve (USD)", sortable=False),
                Column(key="progress", label="Kasutus", sortable=False),
                Column(key="pct", label="Protsent", sortable=False),
            ]
            org_rows = []
            for oc in org_costs:
                pct = (oc["cost_usd"] / oc["budget_usd"] * 100) if oc["budget_usd"] > 0 else 0
                variant = "danger" if pct >= 90 else ("warning" if pct >= 70 else "default")
                org_rows.append(
                    {
                        "org_name": oc["org_name"],
                        "cost": f"${oc['cost_usd']:.4f}",
                        "budget": f"${oc['budget_usd']:.2f}",
                        "progress": _progress_bar(oc["cost_usd"], oc["budget_usd"]),
                        "pct": Badge(f"{pct:.0f}%", variant=variant),
                    }
                )
            org_table: object = DataTable(columns=org_columns, rows=org_rows)
        else:
            org_table = P(  # noqa: F405
                "Organisatsioonide kuluandmed puuduvad.", cls="muted-text"
            )

        org_card = Card(
            CardHeader(
                H3(  # noqa: F405
                    "Kulu organisatsioonide kaupa",
                    _tooltip("Jooksva kuu kulu vs eelarve organisatsioonide loikes"),
                    cls="card-title",
                )
            ),
            CardBody(org_table),
        )

        # ---- Card 2: Cost by feature ----
        if feature_costs:
            max_feature_cost = max(f["cost_usd"] for f in feature_costs) or 1
            feat_columns = [
                Column(key="feature", label="Funktsioon", sortable=False),
                Column(key="tokens", label="Tokeneid", sortable=False),
                Column(key="cost", label="Kulu (USD)", sortable=False),
                Column(key="bar", label="Osakaal", sortable=False),
            ]
            feat_rows = []
            for fc in feature_costs:
                label = _FEATURE_LABELS.get(fc["feature"], fc["feature"])
                total_tokens = fc["tokens_input"] + fc["tokens_output"]
                feat_rows.append(
                    {
                        "feature": label,
                        "tokens": f"{total_tokens:,}",
                        "cost": f"${fc['cost_usd']:.4f}",
                        "bar": _progress_bar(fc["cost_usd"], max_feature_cost),
                    }
                )
            feat_table: object = DataTable(columns=feat_columns, rows=feat_rows)
        else:
            feat_table = P(  # noqa: F405
                "Funktsiooni kuluandmed puuduvad.", cls="muted-text"
            )

        feat_card = Card(
            CardHeader(
                H3(  # noqa: F405
                    "Kulu funktsioonide kaupa",
                    _tooltip("Kulu jaotus funktsioonide loikes jooksval kuul"),
                    cls="card-title",
                )
            ),
            CardBody(feat_table),
        )

        # ---- Card 3: Cost by model ----
        if model_costs:
            model_columns = [
                Column(key="model", label="Mudel", sortable=False),
                Column(key="tokens_input", label="Sisend-tokenid", sortable=False),
                Column(key="tokens_output", label="Valjund-tokenid", sortable=False),
                Column(key="cost", label="Kulu (USD)", sortable=False),
            ]
            model_rows = [
                {
                    "model": mc["model"],
                    "tokens_input": f"{mc['tokens_input']:,}",
                    "tokens_output": f"{mc['tokens_output']:,}",
                    "cost": f"${mc['cost_usd']:.4f}",
                }
                for mc in model_costs
            ]
            model_table: object = DataTable(columns=model_columns, rows=model_rows)
        else:
            model_table = P("Mudeli kuluandmed puuduvad.", cls="muted-text")  # noqa: F405

        model_card = Card(
            CardHeader(
                H3(  # noqa: F405
                    "Kulu mudelite kaupa",
                    _tooltip("Sonnet vs Opus vs Haiku tokenid ja kulu"),
                    cls="card-title",
                )
            ),
            CardBody(model_table),
        )

        # ---- Card 4: Monthly trend ----
        if monthly:
            trend_columns = [
                Column(key="month", label="Kuu", sortable=False),
                Column(key="tokens_input", label="Sisend-tokenid", sortable=False),
                Column(key="tokens_output", label="Valjund-tokenid", sortable=False),
                Column(key="cost", label="Kulu (USD)", sortable=False),
            ]
            trend_rows = [
                {
                    "month": (
                        m["month"].strftime("%m/%Y")
                        if hasattr(m["month"], "strftime")
                        else str(m["month"])
                    ),
                    "tokens_input": f"{m['tokens_input']:,}",
                    "tokens_output": f"{m['tokens_output']:,}",
                    "cost": f"${m['cost_usd']:.4f}",
                }
                for m in monthly
            ]
            trend_table: object = DataTable(columns=trend_columns, rows=trend_rows)
        else:
            trend_table = P("Igakuised andmed puuduvad.", cls="muted-text")  # noqa: F405

        trend_card = Card(
            CardHeader(
                H3(  # noqa: F405
                    "Igakuine trend",
                    _tooltip("Viimased 6 kuud kulu ja tokenite kasutus"),
                    cls="card-title",
                )
            ),
            CardBody(trend_table),
        )

        content = (
            H1("LLM kulud", cls="page-title"),  # noqa: F405
            P(  # noqa: F405
                A("\u2190 Tagasi adminipaneelile", href="/admin"),  # noqa: F405
                cls="back-link",
            ),
            InfoBox(
                P(  # noqa: F405
                    "LLM kulude ulevaade naitab jooksva kuu kulusid organisatsioonide, "
                    "funktsioonide ja mudelite loikes ning igakuist trendi."
                ),
                variant="info",
                dismissible=True,
            ),
            org_card,
            feat_card,
            model_card,
            trend_card,
        )

        return PageShell(
            *content,
            title="LLM kulud",
            user=auth,
            theme=theme,
            active_nav="/admin",
        )
    except Exception:
        logger.exception("Failed to render admin cost page")
        from app.admin._shared import _render_admin_error_page

        return _render_admin_error_page(title="LLM kulud", user=auth, theme=theme)
