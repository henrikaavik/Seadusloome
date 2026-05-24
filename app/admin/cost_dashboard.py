"""Admin LLM cost dashboard — full page with per-org, per-feature, per-model,
top-spenders, daily trend sparkline, and monthly aggregates over
``llm_usage`` table aggregations.

Supports a window selector (``?window=7d|30d|90d|ytd``, default ``30d``),
an org filter (``?org=<uuid>``), and a CSV export endpoint at
``/admin/costs/export``.
"""

from __future__ import annotations

import csv
import io
import logging
import os
from datetime import UTC, datetime

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request
from starlette.responses import Response

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
    "drafter_clarify": "Koostaja täpsustamine",
    "drafter_draft": "Koostaja mustand",
    "chat": "Nõustaja",
    "extraction": "Eraldamine",
}

# Allowed time-window selector values (URL ``?window=...``).
_WINDOW_CHOICES: tuple[str, ...] = ("7d", "30d", "90d", "ytd")
_DEFAULT_WINDOW = "30d"

# Estonian labels for the window selector tabs.
_WINDOW_LABELS: dict[str, str] = {
    "7d": "Viimased 7 päeva",
    "30d": "Viimased 30 päeva",
    "90d": "Viimased 90 päeva",
    "ytd": "Aasta algusest",
}

# Track features for which we have already warned about a missing Estonian label
# so the log stays quiet on subsequent renders within one process lifetime.
_warned_missing_labels: set[str] = set()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalise_window(window: str | None) -> str:
    """Return a safe window value (one of ``_WINDOW_CHOICES``)."""
    if not window:
        return _DEFAULT_WINDOW
    w = window.strip().lower()
    if w in _WINDOW_CHOICES:
        return w
    return _DEFAULT_WINDOW


def _window_start(window: str) -> datetime:
    """Return the UTC datetime marking the start of the requested window."""
    w = _normalise_window(window)
    now = datetime.now(tz=UTC)
    if w == "7d":
        return now.replace(hour=0, minute=0, second=0, microsecond=0) - _delta_days(7)
    if w == "30d":
        return now.replace(hour=0, minute=0, second=0, microsecond=0) - _delta_days(30)
    if w == "90d":
        return now.replace(hour=0, minute=0, second=0, microsecond=0) - _delta_days(90)
    # ytd
    return datetime(now.year, 1, 1, tzinfo=UTC)


def _delta_days(days: int):
    """Return a ``timedelta`` for *days* (helper to keep imports tidy)."""
    from datetime import timedelta

    return timedelta(days=days)


def _feature_label(key: str) -> str:
    """Return the Estonian label for *key*, falling back to the raw key.

    First time we see a feature key missing from ``_FEATURE_LABELS`` we emit a
    single warning via ``logger.warning`` so the operator notices a stale dict;
    the page render is never blocked.
    """
    label = _FEATURE_LABELS.get(key)
    if label is not None:
        return label
    if key not in _warned_missing_labels:
        _warned_missing_labels.add(key)
        logger.warning(
            "cost_dashboard: feature %r missing from _FEATURE_LABELS; "
            "showing raw key. Add an Estonian label.",
            key,
        )
    return key


def _format_window_label(window: str) -> str:
    """Return Estonian human label for the active window."""
    return _WINDOW_LABELS.get(_normalise_window(window), _WINDOW_LABELS[_DEFAULT_WINDOW])


# ---------------------------------------------------------------------------
# Multi-org access helper
# ---------------------------------------------------------------------------


def _user_can_see_all_orgs(auth: dict | None) -> bool:  # type: ignore[type-arg]
    """System ``admin`` role may filter across every org; others (e.g.
    ``org_admin``) are scoped to their own ``org_id`` only."""
    if not auth:
        return False
    return auth.get("role") == "admin"


# ---------------------------------------------------------------------------
# DB query helpers (all window/org-aware)
# ---------------------------------------------------------------------------


def _get_orgs_for_filter() -> list[dict]:  # type: ignore[type-arg]
    """Return id+name for every organization, for the org-filter dropdown."""
    rows_out: list[dict] = []  # type: ignore[type-arg]
    try:
        with _connect() as conn:
            rows = conn.execute("SELECT id, name FROM organizations ORDER BY name").fetchall()
            rows_out = [{"id": str(r[0]), "name": r[1]} for r in rows]
    except Exception:
        logger.exception("Failed to fetch orgs for cost-dashboard filter")
    return rows_out


def _get_cost_by_org(window: str = _DEFAULT_WINDOW, org_id: str | None = None) -> list[dict]:  # type: ignore[type-arg]
    """Return total cost per org for the selected window, with budget info.

    When *org_id* is given the result is restricted to that single org.
    """
    max_cost = float(os.environ.get("ORG_MAX_MONTHLY_COST_USD", "50.0"))
    start = _window_start(window)
    params: list = [start]
    where_clause = ""
    if org_id:
        where_clause = "WHERE o.id = %s"
        params.append(org_id)
    rows_out: list[dict] = []  # type: ignore[type-arg]
    try:
        with _connect() as conn:
            sql = (
                "SELECT o.name, COALESCE(SUM(u.cost_usd), 0) AS total_cost "
                "FROM organizations o "
                "LEFT JOIN llm_usage u ON u.org_id = o.id "
                "AND u.created_at >= %s "
                f"{where_clause} "
                "GROUP BY o.id, o.name ORDER BY total_cost DESC"
            )
            rows = conn.execute(sql, params).fetchall()
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


def _get_cost_by_feature(window: str = _DEFAULT_WINDOW, org_id: str | None = None) -> list[dict]:  # type: ignore[type-arg]
    """Return cost breakdown by feature for the selected window."""
    start = _window_start(window)
    where = "WHERE created_at >= %s"
    params: list = [start]
    if org_id:
        where += " AND org_id = %s"
        params.append(org_id)
    rows_out: list[dict] = []  # type: ignore[type-arg]
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT feature, "
                "COALESCE(SUM(tokens_input), 0), "
                "COALESCE(SUM(tokens_output), 0), "
                "COALESCE(SUM(cost_usd), 0) "
                f"FROM llm_usage {where} "
                "GROUP BY feature ORDER BY SUM(cost_usd) DESC",
                params,
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


def _get_cost_by_model(window: str = _DEFAULT_WINDOW, org_id: str | None = None) -> list[dict]:  # type: ignore[type-arg]
    """Return cost breakdown by model for the selected window.

    The ``llm_usage`` table stores aggregate ``cost_usd`` per call but no
    separate ``cost_input_usd`` / ``cost_output_usd`` columns; we therefore
    surface raw input/output token counts plus the total cost. (Per-token
    prices live in ``app.llm.pricing``; if the schema gains split-cost
    columns in the future, extend the SELECT here.)
    """
    start = _window_start(window)
    where = "WHERE created_at >= %s"
    params: list = [start]
    if org_id:
        where += " AND org_id = %s"
        params.append(org_id)
    rows_out: list[dict] = []  # type: ignore[type-arg]
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT model, "
                "COALESCE(SUM(tokens_input), 0), "
                "COALESCE(SUM(tokens_output), 0), "
                "COALESCE(SUM(cost_usd), 0) "
                f"FROM llm_usage {where} "
                "GROUP BY model ORDER BY SUM(cost_usd) DESC",
                params,
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
    """Return monthly cost totals for the last *months* months.

    Kept here for the dashboard's long-range view and for backward
    compatibility with existing tests/imports.
    """
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


def _get_daily_trend(window: str = _DEFAULT_WINDOW, org_id: str | None = None) -> list[dict]:  # type: ignore[type-arg]
    """Return per-day cost totals across the selected window."""
    start = _window_start(window)
    where = "WHERE created_at >= %s"
    params: list = [start]
    if org_id:
        where += " AND org_id = %s"
        params.append(org_id)
    rows_out: list[dict] = []  # type: ignore[type-arg]
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT date_trunc('day', created_at)::date AS day, "
                "COALESCE(SUM(cost_usd), 0) "
                f"FROM llm_usage {where} "
                "GROUP BY day ORDER BY day ASC",
                params,
            ).fetchall()
            rows_out = [{"day": r[0], "cost_usd": float(r[1])} for r in rows]
    except Exception:
        logger.exception("Failed to fetch daily trend")
    return rows_out


def _get_top_users(
    window: str = _DEFAULT_WINDOW, org_id: str | None = None, limit: int = 10
) -> list[dict]:  # type: ignore[type-arg]
    """Return the top-N spenders by total ``cost_usd`` in the window."""
    start = _window_start(window)
    where = "WHERE u.created_at >= %s"
    params: list = [start]
    if org_id:
        where += " AND u.org_id = %s"
        params.append(org_id)
    rows_out: list[dict] = []  # type: ignore[type-arg]
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT u.user_id, "
                "COALESCE(usr.full_name, 'Tundmatu'), "
                "COALESCE(usr.email, ''), "
                "COALESCE(SUM(u.tokens_input + u.tokens_output), 0), "
                "COALESCE(SUM(u.cost_usd), 0) AS total "
                "FROM llm_usage u "
                "LEFT JOIN users usr ON usr.id = u.user_id "
                f"{where} "
                "GROUP BY u.user_id, usr.full_name, usr.email "
                "ORDER BY total DESC LIMIT %s",
                [*params, limit],
            ).fetchall()
            rows_out = [
                {
                    "user_id": str(r[0]) if r[0] else "",
                    "full_name": r[1],
                    "email": r[2],
                    "tokens": int(r[3]),
                    "cost_usd": float(r[4]),
                }
                for r in rows
            ]
    except Exception:
        logger.exception("Failed to fetch top users by spend")
    return rows_out


# ---------------------------------------------------------------------------
# UI rendering helpers
# ---------------------------------------------------------------------------


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


def _window_tabs(active: str, org_id: str | None) -> object:
    """Render the 7d/30d/90d/YTD tab strip; each link keeps the ``org`` filter."""
    active = _normalise_window(active)
    from urllib.parse import urlencode

    items = []
    for choice in _WINDOW_CHOICES:
        params: dict[str, str] = {"window": choice}
        if org_id:
            params["org"] = org_id
        href = "/admin/costs?" + urlencode(params)
        cls = "btn btn-sm " + ("btn-primary" if choice == active else "btn-secondary")
        items.append(
            A(  # noqa: F405
                _WINDOW_LABELS[choice],
                href=href,
                cls=cls,
                role="tab",
                aria_selected="true" if choice == active else "false",
            )
        )
    return Div(*items, cls="cost-window-tabs", role="tablist")  # noqa: F405


def _org_filter_form(
    orgs: list[dict],  # type: ignore[type-arg]
    selected: str | None,
    window: str,
) -> object:
    """Render the per-org dropdown filter (visible to system admins)."""
    options = [Option("Kõik organisatsioonid", value="")]  # noqa: F405
    for o in orgs:
        selected_attr = "selected" if selected and o["id"] == selected else None
        options.append(Option(o["name"], value=o["id"], selected=selected_attr))  # noqa: F405

    return Form(  # noqa: F405
        Input(type="hidden", name="window", value=window),  # noqa: F405
        Div(  # noqa: F405
            Label("Organisatsioon", fr="filter-cost-org"),  # noqa: F405
            Select(  # noqa: F405
                *options,
                name="org",
                id="filter-cost-org",
                onchange="this.form.submit()",
            ),
            cls="filter-field",
        ),
        method="get",
        action="/admin/costs",
        cls="cost-org-filter",
    )


def _csv_export_link(window: str, org_id: str | None) -> object:
    """Render the CSV export link with the current window/org filter applied."""
    from urllib.parse import urlencode

    params: dict[str, str] = {"format": "csv", "window": window}
    if org_id:
        params["org"] = org_id
    href = "/admin/costs/export?" + urlencode(params)
    return A(  # noqa: F405
        "Ekspordi CSV",
        href=href,
        cls="btn btn-secondary btn-sm",
        download="llm-kulud.csv",
    )


def _sparkline(points: list[dict]) -> object:  # type: ignore[type-arg]
    """Render a dependency-free SVG sparkline over [(day, cost_usd), …].

    Returns an Estonian-language empty-state message when there is no data.
    """
    if not points:
        return P(  # noqa: F405
            "Päevatrendi andmeid pole.", cls="muted-text"
        )

    width = 320
    height = 60
    pad_x = 4
    pad_y = 4
    inner_w = width - 2 * pad_x
    inner_h = height - 2 * pad_y
    max_val = max(p["cost_usd"] for p in points) or 1.0
    n = len(points)
    if n == 1:
        # Single point — draw a dot at centre.
        cx = pad_x + inner_w / 2
        cy = pad_y + inner_h - (points[0]["cost_usd"] / max_val) * inner_h
        body = (
            f'<svg class="cost-sparkline" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" '
            f'role="img" aria-label="Päevakulude sparkline">'
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="2.5" fill="#2e8b57" />'
            f"</svg>"
        )
        return Safe(body)  # noqa: F405

    step = inner_w / (n - 1)
    coords: list[str] = []
    for i, p in enumerate(points):
        x = pad_x + i * step
        y = pad_y + inner_h - (p["cost_usd"] / max_val) * inner_h
        coords.append(f"{x:.1f},{y:.1f}")
    polyline = " ".join(coords)

    # Highlight the last point for the "you are here" effect.
    last_x = pad_x + (n - 1) * step
    last_y = pad_y + inner_h - (points[-1]["cost_usd"] / max_val) * inner_h
    total = sum(p["cost_usd"] for p in points)
    label = (
        f"Päevakulude sparkline {len(points)} päeva üle, "
        f"kogusumma ${total:.4f}, maksimum ${max_val:.4f}"
    )

    body = (
        f'<svg class="cost-sparkline" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="{label}">'
        f'<polyline fill="none" stroke="#2e8b57" stroke-width="1.5" '
        f'points="{polyline}" />'
        f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="2.5" fill="#2e8b57" />'
        f"</svg>"
    )
    return Safe(body)  # noqa: F405


def _empty_state() -> object:
    """Standard empty-state notice used in every card when the window is dry."""
    return P(  # noqa: F405
        "Kuludest pole andmeid valitud ajavahemikus.",
        cls="muted-text",
    )


# ---------------------------------------------------------------------------
# Page handler
# ---------------------------------------------------------------------------


def admin_cost_page(req: Request):
    """GET /admin/costs -- LLM cost dashboard page.

    Helpers are imported as locals inside the function body so the page
    works correctly when rebound by the ``app.templates.admin_dashboard``
    shim — that shim swaps ``__globals__`` to its own module dict, which
    means private helpers (``_progress_bar``, ``_FEATURE_LABELS``,
    ``_tooltip``, etc.) cannot be resolved via the function's global
    namespace. The whole body is wrapped in a try/except so any backend
    failure renders a styled error banner instead of bubbling up as a
    raw 500.
    """
    auth = req.scope.get("auth")
    theme = get_theme_from_request(req)
    try:
        from app.admin._shared import _tooltip
        from app.admin.cost_dashboard import (
            _csv_export_link,
            _empty_state,
            _feature_label,
            _format_window_label,
            _get_cost_by_feature,
            _get_cost_by_model,
            _get_cost_by_org,
            _get_daily_trend,
            _get_monthly_trend,
            _get_orgs_for_filter,
            _get_top_users,
            _normalise_window,
            _org_filter_form,
            _progress_bar,
            _sparkline,
            _user_can_see_all_orgs,
            _window_tabs,
        )

        # ---- Parse query params ----
        window = _normalise_window(req.query_params.get("window"))
        requested_org = req.query_params.get("org") or None

        # Org scoping: system admins can pick any org or "all"; other admin-ish
        # roles get pinned to their own org regardless of the query string.
        is_multi_org_admin = _user_can_see_all_orgs(auth)
        if is_multi_org_admin:
            effective_org_id = requested_org
        else:
            effective_org_id = auth.get("org_id") if auth else None

        # ---- Data queries ----
        org_costs = _get_cost_by_org(window, effective_org_id)
        feature_costs = _get_cost_by_feature(window, effective_org_id)
        model_costs = _get_cost_by_model(window, effective_org_id)
        top_users = _get_top_users(window, effective_org_id, limit=10)
        daily = _get_daily_trend(window, effective_org_id)
        monthly = _get_monthly_trend()

        # ---- Toolbar (window tabs + org filter + CSV export) ----
        toolbar_children: list = [_window_tabs(window, effective_org_id)]
        if is_multi_org_admin:
            orgs = _get_orgs_for_filter()
            toolbar_children.append(_org_filter_form(orgs, effective_org_id, window))
        toolbar_children.append(_csv_export_link(window, effective_org_id))
        toolbar = Div(*toolbar_children, cls="cost-toolbar")  # noqa: F405

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
            org_table = _empty_state()

        org_card = Card(
            CardHeader(
                H3(  # noqa: F405
                    "Kulu organisatsioonide kaupa",
                    _tooltip("Valitud ajavahemiku kulu vs eelarve organisatsioonide lõikes"),
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
                label = _feature_label(fc["feature"])
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
            feat_table = _empty_state()

        feat_card = Card(
            CardHeader(
                H3(  # noqa: F405
                    "Kulu funktsioonide kaupa",
                    _tooltip("Kulu jaotus funktsioonide lõikes valitud ajavahemikus"),
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
                Column(key="tokens_output", label="Väljund-tokenid", sortable=False),
                Column(key="cost", label="Kulu kokku (USD)", sortable=False),
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
            model_table = _empty_state()

        model_card = Card(
            CardHeader(
                H3(  # noqa: F405
                    "Kulu mudelite kaupa",
                    _tooltip("Sonnet vs Opus vs Haiku — sisend-/väljund-tokenid ja kulu"),
                    cls="card-title",
                )
            ),
            CardBody(model_table),
        )

        # ---- Card 4: Top 10 users by spend ----
        if top_users:
            user_columns = [
                Column(key="rank", label="Koht", sortable=False),
                Column(key="full_name", label="Kasutaja", sortable=False),
                Column(key="email", label="E-post", sortable=False),
                Column(key="tokens", label="Tokeneid", sortable=False),
                Column(key="cost", label="Kulu (USD)", sortable=False),
            ]
            user_rows = [
                {
                    "rank": str(i + 1),
                    "full_name": u["full_name"],
                    "email": u["email"] or "—",
                    "tokens": f"{u['tokens']:,}",
                    "cost": f"${u['cost_usd']:.4f}",
                }
                for i, u in enumerate(top_users)
            ]
            users_table: object = DataTable(columns=user_columns, rows=user_rows)
        else:
            users_table = _empty_state()

        users_card = Card(
            CardHeader(
                H3(  # noqa: F405
                    "Top 10 kasutajat kulu järgi",
                    _tooltip("Valitud ajavahemiku suurimad LLM-i kulutajad"),
                    cls="card-title",
                )
            ),
            CardBody(users_table),
        )

        # ---- Card 5: Daily trend (sparkline) ----
        sparkline = _sparkline(daily)
        if daily:
            total_window = sum(d["cost_usd"] for d in daily)
            summary_line = P(  # noqa: F405
                f"Kogusumma valitud ajavahemikus: ${total_window:.4f} "
                f"({len(daily)} päeva andmeid)",
                cls="muted-text",
            )
        else:
            summary_line = ""

        daily_card = Card(
            CardHeader(
                H3(  # noqa: F405
                    "Päevatrend",
                    _tooltip("Päevased kulud valitud ajavahemikus"),
                    cls="card-title",
                )
            ),
            CardBody(sparkline, summary_line),
        )

        # ---- Card 6: Monthly trend ----
        if monthly:
            trend_columns = [
                Column(key="month", label="Kuu", sortable=False),
                Column(key="tokens_input", label="Sisend-tokenid", sortable=False),
                Column(key="tokens_output", label="Väljund-tokenid", sortable=False),
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
            trend_table = _empty_state()

        trend_card = Card(
            CardHeader(
                H3(  # noqa: F405
                    "Igakuine trend",
                    _tooltip("Viimase 6 kuu kulu ja tokenite kasutus"),
                    cls="card-title",
                )
            ),
            CardBody(trend_table),
        )

        # ---- Page assembly ----
        window_label = _format_window_label(window)
        content = (
            H1("LLM kulud", cls="page-title"),  # noqa: F405
            P(  # noqa: F405
                A("← Tagasi adminipaneelile", href="/admin"),  # noqa: F405
                cls="back-link",
            ),
            InfoBox(
                P(  # noqa: F405
                    f"LLM kulude ülevaade — aktiivne ajavahemik: {window_label}. "
                    "Kasuta allolevaid valikuid vahemiku ja organisatsiooni "
                    "filtreerimiseks ning andmete eksportimiseks CSV-failina."
                ),
                variant="info",
                dismissible=True,
            ),
            toolbar,
            org_card,
            users_card,
            daily_card,
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


# ---------------------------------------------------------------------------
# CSV export handler
# ---------------------------------------------------------------------------


def admin_cost_export(req: Request):
    """GET /admin/costs/export -- download per-feature cost rows as CSV.

    Mirrors :func:`app.admin.audit.admin_audit_export`. Honours the same
    ``window`` and ``org`` filters as the page. Non-system-admins are
    pinned to their own org regardless of the query string.

    Helpers are imported as locals so this handler works correctly when
    rebound by the admin_dashboard shim. On error returns a plain
    500 response rather than letting the exception bubble up.
    """
    try:
        from app.admin.cost_dashboard import (
            _feature_label,
            _format_window_label,
            _get_cost_by_feature,
            _normalise_window,
            _user_can_see_all_orgs,
        )

        auth = req.scope.get("auth")

        fmt = (req.query_params.get("format") or "csv").lower()
        if fmt != "csv":
            return Response(
                content="Toetatud on ainult CSV-formaat.",
                status_code=400,
                media_type="text/plain; charset=utf-8",
            )

        window = _normalise_window(req.query_params.get("window"))
        requested_org = req.query_params.get("org") or None
        if _user_can_see_all_orgs(auth):
            effective_org_id = requested_org
        else:
            effective_org_id = auth.get("org_id") if auth else None

        rows = _get_cost_by_feature(window, effective_org_id)
        window_label = _format_window_label(window)

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "Ajavahemik",
                "Funktsioon",
                "Funktsiooni võti",
                "Sisend-tokenid",
                "Väljund-tokenid",
                "Tokeneid kokku",
                "Kulu (USD)",
            ]
        )
        for r in rows:
            writer.writerow(
                [
                    window_label,
                    _feature_label(r["feature"]),
                    r["feature"],
                    r["tokens_input"],
                    r["tokens_output"],
                    r["tokens_input"] + r["tokens_output"],
                    f"{r['cost_usd']:.6f}",
                ]
            )

        csv_content = output.getvalue()
        return Response(
            content=csv_content,
            media_type="text/csv",
            headers={
                "Content-Disposition": (f"attachment; filename=llm-kulud-{window}.csv"),
            },
        )
    except Exception:
        logger.exception("Failed to export admin cost CSV")
        return Response(
            content="Andmete eksportimine ebaõnnestus.",
            status_code=500,
            media_type="text/plain; charset=utf-8",
        )


# Names re-exported for callers (the admin_dashboard shim, tests, etc.).
__all__ = [
    "_FEATURE_LABELS",
    "_WINDOW_CHOICES",
    "admin_cost_export",
    "admin_cost_page",
]
