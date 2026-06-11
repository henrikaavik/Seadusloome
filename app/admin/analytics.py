"""Admin usage analytics page and helpers.

Shows daily usage trends (uploads, chat messages, drafter sessions) from
the ``usage_daily`` materialized view.  The view can be refreshed via
the ``refresh_usage_daily`` background job or the manual button on the
page (``POST /admin/analytics/refresh``).  Per-day rows can be exported
as CSV via ``GET /admin/analytics/export``.
"""

from __future__ import annotations

import csv
import io
import logging
import threading
from datetime import UTC, date, datetime
from urllib.parse import urlencode

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from app.db import get_connection as _connect
from app.ui.data.data_table import Column, DataTable
from app.ui.layout import PageShell
from app.ui.primitives.badge import Badge
from app.ui.primitives.button import Button  # noqa: F401, F811  -- shadow guard
from app.ui.surfaces.alert import Alert
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.surfaces.info_box import InfoBox
from app.ui.theme import get_theme_from_request

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Time-range presets
# ---------------------------------------------------------------------------
#
# ``window=ytd`` cannot be expressed as a fixed number of days, so we resolve
# the value into ``(label, days)`` pairs at request time.  The dict ordering
# is the ordering used by the selector UI.
_WINDOWS: dict[str, str] = {
    "7d": "7 päeva",
    "30d": "30 päeva",
    "90d": "90 päeva",
    "ytd": "Aasta algusest",
}
_DEFAULT_WINDOW = "30d"


def _resolve_window(window: str | None) -> tuple[str, int]:
    """Normalise *window* to a ``(slug, days)`` pair.

    Unknown values fall back to ``_DEFAULT_WINDOW``.  ``ytd`` is converted
    into the number of days since the first of January in Europe/Tallinn
    time (good enough for a UTC-stored ``day`` column).
    """
    slug = (window or _DEFAULT_WINDOW).lower()
    if slug not in _WINDOWS:
        slug = _DEFAULT_WINDOW
    if slug == "7d":
        return slug, 7
    if slug == "30d":
        return slug, 30
    if slug == "90d":
        return slug, 90
    today = datetime.now(UTC).date()
    days = (today - date(today.year, 1, 1)).days + 1
    return slug, max(1, days)


# ---------------------------------------------------------------------------
# Estonian labels for the per-metric breakdown (mirrors ``_FEATURE_LABELS``
# pattern in ``app.admin.cost_dashboard``).
# ---------------------------------------------------------------------------
_METRIC_LABELS: dict[str, str] = {
    "uploads": "Üleslaadimised",
    "chat_messages": "Vestluse sõnumid",
    "drafter_sessions": "Koostamise seansid",
}

_METRIC_COLORS: dict[str, str] = {
    "uploads": "#0066cc",
    "chat_messages": "#2e8b57",
    "drafter_sessions": "#9b59b6",
}


# ---------------------------------------------------------------------------
# Refresh wiring (#861-C)
# ---------------------------------------------------------------------------
#
# REFRESH MATERIALIZED VIEW CONCURRENTLY can take seconds on a busy database,
# so it must run off the request path. The project's ``FOR UPDATE SKIP
# LOCKED`` queue would be the natural home, but its handler registry
# (``app/jobs/registry.py``) is deliberately framework-free — it must NOT
# import FastHTML/Starlette so the standalone worker can stay slim — and this
# module imports ``fasthtml.common`` at the top. Wiring a queue handler would
# therefore mean a new framework-free handler module, which is out of scope
# for this fix. Per the issue's sanctioned fallback we instead run the
# refresh on a short-lived daemon thread and record the real completion
# timestamp; the request returns immediately and the page polls the timestamp
# on reload.
#
# The completion timestamp is a marker row in the ``metrics`` table
# (``recorded_at`` defaults to ``now()`` at commit), read back by
# ``_get_last_refresh`` — the *real* completion time, not the unreliable
# ``pg_stat_all_tables.last_analyze`` proxy (which reflects ANALYZE, not
# REFRESH, and also fires on autoanalyze).
_REFRESH_METRIC_NAME = "usage_daily_refresh_ms"


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------


def _refresh_usage_daily() -> bool:
    """Refresh the ``usage_daily`` materialized view concurrently.

    On success also writes a ``usage_daily_refresh_ms`` marker row into the
    ``metrics`` table (with ``recorded_at`` defaulting to ``now()`` at commit
    time) so :func:`_get_last_refresh` can read back the real completion
    timestamp (#861-C). The marker INSERT shares the refresh connection and
    is committed atomically with it, so a recorded timestamp always implies a
    completed refresh.

    Returns True on success, False on error. Errors are logged but never
    propagated so callers can degrade gracefully.
    """
    try:
        with _connect() as conn:
            conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY usage_daily")
            # Marker row: value carries no meaning beyond "a refresh finished";
            # ``recorded_at`` (default now()) is the signal we read back.
            conn.execute(
                "INSERT INTO metrics (name, value, labels) VALUES (%s, %s, %s::jsonb)",
                (_REFRESH_METRIC_NAME, 0, '{"source": "refresh_usage_daily"}'),
            )
            conn.commit()
        return True
    except Exception:
        logger.exception("Failed to refresh usage_daily materialized view")
        return False


def trigger_usage_daily_refresh() -> bool:
    """Start a background ``usage_daily`` refresh; return whether it launched.

    Runs :func:`_refresh_usage_daily` on a short-lived daemon thread so the
    HTTP request returns immediately (#861-C). Returns ``True`` when the
    thread was started, ``False`` if the thread could not be launched
    (logged, never raised) so the caller can degrade to a failure flash.

    Clean, framework-free signature so it can later be wrapped as a REST
    endpoint or MCP tool (Phase 5). The actual completion timestamp is
    recorded by the thread and surfaced via :func:`_get_last_refresh`.
    """
    try:
        thread = threading.Thread(
            target=_refresh_usage_daily,
            name="usage-daily-refresh",
            daemon=True,
        )
        thread.start()
        return True
    except Exception:
        logger.exception("Failed to start usage_daily refresh thread")
        return False


def _get_usage_data(days: int = 30) -> list[dict]:  # type: ignore[type-arg]
    """Query ``usage_daily`` for the last *days* days.

    Returns a list of dicts with keys: ``day``, ``uploads``,
    ``chat_messages``, ``drafter_sessions``.  Empty list on error.

    Note: the materialized view exposes the column as ``draft_uploads``,
    so we alias it to ``uploads`` to keep the dict shape stable for
    downstream UI code and tests.  Sums over ``org_id`` because the
    detail-by-org view is rendered from ``_get_usage_by_org`` instead.
    """
    rows_out: list[dict] = []  # type: ignore[type-arg]
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT day, "
                "       COALESCE(SUM(draft_uploads), 0)    AS uploads, "
                "       COALESCE(SUM(chat_messages), 0)    AS chat_messages, "
                "       COALESCE(SUM(drafter_sessions), 0) AS drafter_sessions "
                "FROM usage_daily "
                "WHERE day >= CURRENT_DATE - %s "
                "GROUP BY day "
                "ORDER BY day DESC",
                (days,),
            ).fetchall()
            rows_out = [
                {
                    "day": r[0],
                    "uploads": int(r[1]),
                    "chat_messages": int(r[2]),
                    "drafter_sessions": int(r[3]),
                }
                for r in rows
            ]
    except Exception:
        logger.exception("Failed to fetch usage_daily data")
    return rows_out


def _get_usage_by_org(days: int = 30) -> list[dict]:  # type: ignore[type-arg]
    """Return aggregated per-org usage counts over the last *days* days.

    Joins ``usage_daily`` with ``organizations`` so rows that have no
    org row (legacy data) are still reported under the literal label
    "Tundmatu organisatsioon".  Sorted by total descending so the
    busiest orgs surface first.
    """
    rows_out: list[dict] = []  # type: ignore[type-arg]
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT COALESCE(o.name, 'Tundmatu organisatsioon') AS org_name, "
                "       COALESCE(SUM(u.draft_uploads), 0)           AS uploads, "
                "       COALESCE(SUM(u.chat_messages), 0)           AS chat_messages, "
                "       COALESCE(SUM(u.drafter_sessions), 0)        AS drafter_sessions "
                "FROM usage_daily u "
                "LEFT JOIN organizations o ON o.id = u.org_id "
                "WHERE u.day >= CURRENT_DATE - %s "
                "GROUP BY o.name "
                "ORDER BY (SUM(u.draft_uploads) + SUM(u.chat_messages) "
                "        + SUM(u.drafter_sessions)) DESC NULLS LAST, "
                "         o.name",
                (days,),
            ).fetchall()
            rows_out = [
                {
                    "org_name": r[0],
                    "uploads": int(r[1]),
                    "chat_messages": int(r[2]),
                    "drafter_sessions": int(r[3]),
                    "total": int(r[1]) + int(r[2]) + int(r[3]),
                }
                for r in rows
            ]
    except Exception:
        logger.exception("Failed to fetch usage by org")
    return rows_out


def _get_last_refresh() -> datetime | None:
    """Return the last successful refresh-completion timestamp of ``usage_daily``.

    Reads the ``recorded_at`` of the most recent ``usage_daily_refresh_ms``
    marker row that :func:`_refresh_usage_daily` writes on every successful
    refresh (#861-C) — this is the *real* completion time, not a statistics
    proxy.

    Falls back to ``pg_stat_all_tables.last_analyze`` only when no marker row
    exists yet (e.g. the first page load after deploy, before any refresh has
    run through the new code path). Returns ``None`` when neither signal is
    available or the query fails.
    """
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT recorded_at FROM metrics "
                "WHERE name = %s "
                "ORDER BY recorded_at DESC LIMIT 1",
                (_REFRESH_METRIC_NAME,),
            ).fetchone()
            if row and row[0]:
                return row[0]

            # Legacy fallback: best-effort proxy from the planner stats.
            row = conn.execute(
                "SELECT GREATEST("
                "  COALESCE(last_analyze, '-infinity'::timestamptz), "
                "  COALESCE(last_autoanalyze, '-infinity'::timestamptz)"
                ") "
                "FROM pg_stat_all_tables "
                "WHERE relname = 'usage_daily' "
                "LIMIT 1"
            ).fetchone()
        if row and row[0] and row[0].year > 1970:
            return row[0]
    except Exception:
        logger.exception("Failed to fetch usage_daily last refresh timestamp")
    return None


def _usage_summary(data: list[dict]) -> dict:  # type: ignore[type-arg]
    """Compute totals across the given usage data rows."""
    return {
        "total_uploads": sum(r["uploads"] for r in data),
        "total_chat_messages": sum(r["chat_messages"] for r in data),
        "total_drafter_sessions": sum(r["drafter_sessions"] for r in data),
        "days": len(data),
    }


# ---------------------------------------------------------------------------
# SVG helpers — kept dependency-free.
# ---------------------------------------------------------------------------


def _svg_bar_chart(data: list[dict], key: str, label: str, color: str) -> object:  # type: ignore[type-arg]
    """Render a simple inline SVG bar chart for the given metric.

    Shows the most recent days left-to-right with the newest day on
    the right.  Each bar is labelled with its value on hover via a
    ``<title>`` element.
    """
    if not data:
        return P(f"{label}: andmed puuduvad", cls="muted-text")  # noqa: F405

    # Data comes in DESC order (newest first); reverse for left-to-right.
    sorted_data = list(reversed(data))

    max_val = max((r[key] for r in sorted_data), default=1) or 1
    bar_width = 12
    gap = 2
    chart_width = len(sorted_data) * (bar_width + gap)
    chart_height = 80
    label_height = 20

    bars = []
    for i, row in enumerate(sorted_data):
        val = row[key]
        bar_h = max(1, int((val / max_val) * (chart_height - label_height)))
        x = i * (bar_width + gap)
        y = chart_height - label_height - bar_h
        day_val = row["day"]
        day_str = day_val.strftime("%d.%m") if hasattr(day_val, "strftime") else str(day_val)
        bars.append(
            f'<rect x="{x}" y="{y}" width="{bar_width}" height="{bar_h}" '
            f'fill="{color}" rx="2">'
            f"<title>{day_str}: {val}</title></rect>"
        )

    svg_content = "".join(bars)
    svg = (
        f'<svg viewBox="0 0 {chart_width} {chart_height}" '
        f'class="usage-chart" role="img" aria-label="{label}">'
        f"{svg_content}</svg>"
    )
    return Div(  # noqa: F405
        H4(label, cls="section-subtitle"),  # noqa: F405
        Safe(svg),  # noqa: F405
        cls="usage-chart-container",
    )


def _svg_sparkline(data: list[dict], key: str, color: str) -> object:  # type: ignore[type-arg]
    """Render a compact inline SVG line sparkline (no axes, no labels).

    Designed for inline display inside summary cards or table cells.
    Falls back to an em-dash placeholder when there is no data.
    """
    if not data:
        return Safe("<span class='muted-text'>—</span>")  # noqa: F405

    sorted_data = list(reversed(data))
    values = [r[key] for r in sorted_data]
    if not any(values):
        return Safe("<span class='muted-text'>—</span>")  # noqa: F405

    max_val = max(values) or 1
    width = 120
    height = 28
    step = width / max(1, (len(values) - 1)) if len(values) > 1 else width
    points = []
    for i, val in enumerate(values):
        x = i * step
        # invert so larger values draw higher on the chart
        y = height - (val / max_val) * (height - 2) - 1
        points.append(f"{x:.1f},{y:.1f}")
    polyline = " ".join(points)
    svg = (
        f'<svg viewBox="0 0 {width} {height}" class="usage-sparkline" '
        f'role="img" aria-label="Trend" width="{width}" height="{height}">'
        f'<polyline fill="none" stroke="{color}" stroke-width="1.5" '
        f'stroke-linecap="round" stroke-linejoin="round" points="{polyline}" />'
        f"</svg>"
    )
    return Safe(svg)  # noqa: F405


# ---------------------------------------------------------------------------
# Window selector
# ---------------------------------------------------------------------------


def _window_selector(active: str) -> object:
    """Render a row of pill links for the time-range presets."""
    pills = []
    for slug, label in _WINDOWS.items():
        is_active = slug == active
        pills.append(
            A(  # noqa: F405
                label,
                href=f"/admin/analytics?window={slug}",
                cls="analytics-window-pill" + (" is-active" if is_active else ""),
                aria_current="page" if is_active else None,
            )
        )
    return Div(  # noqa: F405
        Div(*pills, cls="analytics-window-pills"),
        cls="analytics-window-selector",
    )


# ---------------------------------------------------------------------------
# Page handler
# ---------------------------------------------------------------------------


def admin_analytics_page(req: Request):
    """GET /admin/analytics -- usage analytics page.

    Helpers are imported as locals inside the function body so the page
    works correctly when rebound by the ``app.templates.admin_dashboard``
    shim — that shim swaps ``__globals__`` to its own module dict, which
    means private helpers cannot be resolved via the function's global
    namespace.  The whole body is wrapped in a try/except so any backend
    failure (missing ``usage_daily`` materialized view, transient DB
    error) renders a styled error banner instead of bubbling up as a
    raw 500.
    """
    auth = req.scope.get("auth")
    theme = get_theme_from_request(req)
    try:
        from app.admin._shared import _tooltip
        from app.admin.analytics import (
            _METRIC_COLORS,
            _METRIC_LABELS,
            _get_last_refresh,
            _get_usage_by_org,
            _get_usage_data,
            _resolve_window,
            _svg_bar_chart,
            _svg_sparkline,
            _usage_summary,
            _window_selector,
        )
        from app.ui.time import format_tallinn

        # Resolve the time-range selector.  Accept ``?window=`` first;
        # the legacy ``?days=`` parameter still maps onto the closest
        # preset so old bookmarks keep working.
        window_param = req.query_params.get("window")
        if not window_param:
            legacy = req.query_params.get("days")
            if legacy:
                try:
                    val = int(legacy)
                    window_param = "7d" if val <= 7 else ("30d" if val <= 30 else "90d")
                except ValueError:
                    window_param = None
        window_slug, days = _resolve_window(window_param)

        data = _get_usage_data(days)
        by_org = _get_usage_by_org(days)
        summary = _usage_summary(data)
        last_refresh = _get_last_refresh()

        # ---- Optional flash banner from a refresh redirect ----
        # #861-C: the refresh now runs as a background job, so the common
        # outcome is "queued". The legacy ``ok`` state is still honoured for
        # any in-flight redirect/bookmark.
        flash_banner: object | None = None
        refreshed_flag = req.query_params.get("refreshed")
        if refreshed_flag == "queued":
            flash_banner = Alert(
                P(  # noqa: F405
                    "Andmete värskendamine pandi taustatööde järjekorda. "
                    "Värskendatud andmed ilmuvad mõne hetke pärast — "
                    "laadi leht uuesti."
                ),
                variant="info",
                title="Värskendamine järjekorras",
            )
        elif refreshed_flag == "ok":
            ts_raw = req.query_params.get("refreshed_at", "")
            flash_banner = Alert(
                P(  # noqa: F405
                    "Andmed värskendati edukalt." + (f" (Aeg: {ts_raw})" if ts_raw else "")
                ),
                variant="success",
                title="Värskendatud",
            )
        elif refreshed_flag == "fail":
            flash_banner = Alert(
                P("Andmete värskendamine ebaõnnestus. Proovi uuesti."),  # noqa: F405
                variant="danger",
                title="Viga",
            )

        # ---- Header: window selector + refresh form + CSV export ----
        last_refresh_str = format_tallinn(last_refresh) if last_refresh else "—"
        refresh_form = Form(  # noqa: F405
            Button(
                "Värskenda andmeid",
                type="submit",
                variant="primary",
                size="sm",
            ),
            Span(  # noqa: F405
                f"Viimati värskendatud: {last_refresh_str}",
                cls="analytics-refresh-meta muted-text",
            ),
            method="post",
            action=f"/admin/analytics/refresh?window={window_slug}",
            cls="analytics-refresh-form",
        )
        export_link = A(  # noqa: F405
            "Ekspordi CSV",
            href=f"/admin/analytics/export?window={window_slug}",
            cls="btn btn-secondary btn-sm",
            download="kasutusandmed.csv",
        )

        controls_card = Card(
            CardHeader(
                Div(  # noqa: F405
                    H3("Ajavahemik", cls="card-title"),  # noqa: F405
                    Div(refresh_form, export_link, cls="analytics-controls-actions"),  # noqa: F405
                    cls="card-header-row",
                ),
            ),
            CardBody(_window_selector(window_slug)),
        )

        # ---- Summary card with per-metric sparklines ----
        if data:
            summary_rows = [
                {
                    "metric": _METRIC_LABELS["uploads"],
                    "total": Badge(str(summary["total_uploads"]), variant="primary"),
                    "trend": _svg_sparkline(data, "uploads", _METRIC_COLORS["uploads"]),
                },
                {
                    "metric": _METRIC_LABELS["chat_messages"],
                    "total": Badge(str(summary["total_chat_messages"]), variant="primary"),
                    "trend": _svg_sparkline(
                        data, "chat_messages", _METRIC_COLORS["chat_messages"]
                    ),
                },
                {
                    "metric": _METRIC_LABELS["drafter_sessions"],
                    "total": Badge(str(summary["total_drafter_sessions"]), variant="primary"),
                    "trend": _svg_sparkline(
                        data, "drafter_sessions", _METRIC_COLORS["drafter_sessions"]
                    ),
                },
            ]
            summary_table: object = DataTable(
                columns=[
                    Column(key="metric", label="Mõõdik", sortable=False),
                    Column(key="total", label="Kokku", sortable=False),
                    Column(key="trend", label="Trend", sortable=False),
                ],
                rows=summary_rows,
            )
        else:
            summary_table = P(  # noqa: F405
                "Selles ajavahemikus pole andmeid.", cls="muted-text"
            )

        summary_card = Card(
            CardHeader(
                H3(  # noqa: F405
                    "Kokkuvõte",
                    _tooltip(f"Viimased {days} päeva ({_WINDOWS[window_slug]})"),
                    cls="card-title",
                )
            ),
            CardBody(
                Dl(  # noqa: F405
                    Dt("Perioodi pikkus"),  # noqa: F405
                    Dd(Badge(f"{summary['days']} päeva", variant="default")),  # noqa: F405
                    cls="info-list",
                ),
                summary_table,
            ),
        )

        # ---- Charts card ----
        if data:
            charts_body: object = Div(  # noqa: F405
                _svg_bar_chart(
                    data,
                    "uploads",
                    _METRIC_LABELS["uploads"] + " päevas",
                    _METRIC_COLORS["uploads"],
                ),
                _svg_bar_chart(
                    data,
                    "chat_messages",
                    _METRIC_LABELS["chat_messages"] + " päevas",
                    _METRIC_COLORS["chat_messages"],
                ),
                _svg_bar_chart(
                    data,
                    "drafter_sessions",
                    _METRIC_LABELS["drafter_sessions"] + " päevas",
                    _METRIC_COLORS["drafter_sessions"],
                ),
            )
        else:
            charts_body = P(  # noqa: F405
                "Selles ajavahemikus pole andmeid.", cls="muted-text"
            )
        charts_card = Card(
            CardHeader(H3("Trendid", cls="card-title")),  # noqa: F405
            CardBody(charts_body),
        )

        # ---- Per-org breakdown card ----
        if by_org:
            org_columns = [
                Column(key="org_name", label="Organisatsioon", sortable=False),
                Column(key="uploads", label=_METRIC_LABELS["uploads"], sortable=False),
                Column(
                    key="chat_messages",
                    label=_METRIC_LABELS["chat_messages"],
                    sortable=False,
                ),
                Column(
                    key="drafter_sessions",
                    label=_METRIC_LABELS["drafter_sessions"],
                    sortable=False,
                ),
                Column(key="total", label="Kokku", sortable=False),
            ]
            org_rows = [
                {
                    "org_name": row["org_name"],
                    "uploads": str(row["uploads"]),
                    "chat_messages": str(row["chat_messages"]),
                    "drafter_sessions": str(row["drafter_sessions"]),
                    "total": Badge(str(row["total"]), variant="primary"),
                }
                for row in by_org
            ]
            org_table: object = DataTable(columns=org_columns, rows=org_rows)
        else:
            org_table = P(  # noqa: F405
                "Selles ajavahemikus pole andmeid.", cls="muted-text"
            )
        org_card = Card(
            CardHeader(
                H3(  # noqa: F405
                    "Organisatsioonide kaupa",
                    _tooltip(
                        "Üleslaadimised, vestluse sõnumid ja koostamise seansid "
                        "organisatsioonide lõikes."
                    ),
                    cls="card-title",
                )
            ),
            CardBody(org_table),
        )

        # ---- Detail table ----
        if data:
            columns = [
                Column(key="day", label="Kuupäev", sortable=False),
                Column(key="uploads", label=_METRIC_LABELS["uploads"], sortable=False),
                Column(
                    key="chat_messages",
                    label=_METRIC_LABELS["chat_messages"],
                    sortable=False,
                ),
                Column(
                    key="drafter_sessions",
                    label=_METRIC_LABELS["drafter_sessions"],
                    sortable=False,
                ),
            ]
            rows = [
                {
                    "day": (
                        format_tallinn(r["day"], fmt="%d.%m.%Y")
                        if hasattr(r["day"], "strftime")
                        else str(r["day"])
                    ),
                    "uploads": str(r["uploads"]),
                    "chat_messages": str(r["chat_messages"]),
                    "drafter_sessions": str(r["drafter_sessions"]),
                }
                for r in data
            ]
            table: object = DataTable(columns=columns, rows=rows)
        else:
            table = P(  # noqa: F405
                "Selles ajavahemikus pole andmeid.", cls="muted-text"
            )

        detail_card = Card(
            CardHeader(H3("Detailne tabel", cls="card-title")),  # noqa: F405
            CardBody(table),
        )

        content: list[object] = [
            H1("Kasutusanalüütika", cls="page-title"),  # noqa: F405
            P(  # noqa: F405
                A("← Tagasi adminipaneelile", href="/admin"),  # noqa: F405
                cls="back-link",
            ),
        ]
        if flash_banner is not None:
            content.append(flash_banner)
        content.extend(
            [
                InfoBox(
                    P(  # noqa: F405
                        "Kasutusanalüütika näitab üleslaadimiste, vestluse "
                        "sõnumite ja koostamise seansside statistikat "
                        "päevade ja organisatsioonide kaupa. Vali ajavahemik "
                        "või laadi andmed CSV-failina alla."
                    ),
                    variant="info",
                    dismissible=True,
                ),
                controls_card,
                summary_card,
                charts_card,
                org_card,
                detail_card,
            ]
        )

        return PageShell(
            *content,
            title="Kasutusanalüütika",
            user=auth,
            theme=theme,
            active_nav="/admin",
        )
    except Exception:
        logger.exception("Failed to render admin analytics page")
        from app.admin._shared import _render_admin_error_page

        return _render_admin_error_page(title="Kasutusanalüütika", user=auth, theme=theme)


# ---------------------------------------------------------------------------
# Manual refresh handler
# ---------------------------------------------------------------------------


def admin_analytics_refresh(req: Request):
    """POST /admin/analytics/refresh — start a ``usage_daily`` refresh.

    The REFRESH runs off the request path on a background thread (#861-C) so
    a slow concurrent refresh never blocks the request thread. Redirects back
    to ``/admin/analytics`` with a flash banner; the ``?window=`` selection
    is preserved so the admin lands on the same view they triggered the
    refresh from. The real completion timestamp is recorded by the refresh
    and surfaced via ``_get_last_refresh`` on the next page load.
    """
    try:
        from app.admin.analytics import _resolve_window, trigger_usage_daily_refresh

        # Window can ride on the query string (``?window=…``) so plain
        # form posts that put the selection in the URL keep working.
        slug, _days = _resolve_window(req.query_params.get("window"))

        started = trigger_usage_daily_refresh()
        flash = "queued" if started else "fail"
        params = {"window": slug, "refreshed": flash}
        qs = urlencode(params)
        return RedirectResponse(f"/admin/analytics?{qs}", status_code=303)
    except Exception:
        logger.exception("Failed to handle admin analytics refresh")
        return RedirectResponse("/admin/analytics?refreshed=fail", status_code=303)


# ---------------------------------------------------------------------------
# CSV export handler — mirrors ``app.admin.audit.admin_audit_export``.
# ---------------------------------------------------------------------------


def admin_analytics_export(req: Request):
    """GET /admin/analytics/export — download per-day rows as CSV.

    Helpers are imported as locals so this handler works correctly when
    rebound by the admin_dashboard shim.  On error returns a styled
    plain-text response rather than a raw 500.
    """
    try:
        from app.admin.analytics import _get_usage_data, _resolve_window

        slug, days = _resolve_window(req.query_params.get("window"))
        data = _get_usage_data(days)

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "Kuupäev",
                "Üleslaadimised",
                "Vestluse sõnumid",
                "Koostamise seansid",
            ]
        )
        for row in data:
            day_val = row["day"]
            day_str = (
                day_val.strftime("%Y-%m-%d") if hasattr(day_val, "strftime") else str(day_val)
            )
            writer.writerow(
                [
                    day_str,
                    row["uploads"],
                    row["chat_messages"],
                    row["drafter_sessions"],
                ]
            )

        csv_content = output.getvalue()
        filename = f"kasutusandmed_{slug}.csv"
        return Response(
            content=csv_content,
            media_type="text/csv",
            headers={
                "Content-Disposition": f"attachment; filename={filename}",
            },
        )
    except Exception:
        logger.exception("Failed to export admin analytics data")
        return Response(
            content="Andmete eksportimine ebaõnnestus.",
            status_code=500,
            media_type="text/plain; charset=utf-8",
        )
