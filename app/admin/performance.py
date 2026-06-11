"""Admin performance page — latency percentiles + label breakdowns for all
five timing series the platform records.

Series covered (issue #198):
    - ``http_request_duration_ms`` — HTTP request latency (MetricsMiddleware)
    - ``job_execution_ms``         — Background job handler runtime
    - ``llm_call_ms``              — Claude / LLM provider call latency
    - ``sparql_query_ms``          — Jena / SPARQL client call latency
    - ``rag_retrieval_ms``         — Voyage embedding + pgvector retrieval

All data comes from the ``metrics`` table via Postgres aggregation queries.
The page is mounted at ``/admin/performance`` and requires the ``admin`` role.

Time window is selectable via ``?window=1h|24h|7d`` (default ``1h``); an
invalid or missing value falls back to ``1h``.
"""

from __future__ import annotations

import logging
from typing import cast

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request

from app.admin._shared import _tooltip
from app.db import get_connection as _connect
from app.ui.data.data_table import Column, DataTable
from app.ui.layout import PageShell
from app.ui.primitives.badge import Badge
from app.ui.primitives.button import Button  # noqa: F401, F811  -- shadow guard
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.theme import get_theme_from_request

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Window selector
# ---------------------------------------------------------------------------

# Maps a URL ``?window=`` value → (SQL interval string, Estonian label).
_WINDOW_CHOICES: dict[str, tuple[str, str]] = {
    "1h": ("1 hour", "Viimane tund"),
    "24h": ("24 hours", "Viimased 24 tundi"),
    "7d": ("7 days", "Viimased 7 päeva"),
}
_DEFAULT_WINDOW = "1h"


def _parse_window(raw: str | None) -> str:
    """Return a valid window key (``1h``/``24h``/``7d``); default ``1h``."""
    if raw is None:
        return _DEFAULT_WINDOW
    key = raw.strip().lower()
    return key if key in _WINDOW_CHOICES else _DEFAULT_WINDOW


def _interval_for(window: str) -> str:
    """Return the SQL ``interval`` string for *window*."""
    return _WINDOW_CHOICES.get(window, _WINDOW_CHOICES[_DEFAULT_WINDOW])[0]


# Series-name → (page-title in Estonian, breakdown-label-key | None,
# breakdown-column-header). When the breakdown key is ``None`` only the
# percentile summary renders (no DataTable).
_SERIES_LABELS: dict[str, tuple[str, str | None, str]] = {
    "http_request_duration_ms": ("HTTP päringud", "path", "Tee"),
    "job_execution_ms": ("Taustajobid", "handler", "Käitleja"),
    "llm_call_ms": ("LLM kutsed", "feature", "Funktsioon"),
    "sparql_query_ms": ("SPARQL päringud", "operation", "Operatsioon"),
    "rag_retrieval_ms": ("RAG retrieval", "feature", "Funktsioon"),
}


# ---------------------------------------------------------------------------
# Generic data helpers (work for any series + window)
# ---------------------------------------------------------------------------


def _empty_summary() -> dict[str, float | int]:
    """The zero-row default returned when a series has no data."""
    return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "count": 0}


def _get_series_summary(name: str, window: str = _DEFAULT_WINDOW) -> dict[str, float | int]:
    """Return p50/p95/p99/count for metric *name* over the window.

    Falls back to zeros on DB error or when no rows match.
    """
    interval = _interval_for(window)
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT "
                "  COALESCE(percentile_cont(0.50) WITHIN GROUP (ORDER BY value), 0), "
                "  COALESCE(percentile_cont(0.95) WITHIN GROUP (ORDER BY value), 0), "
                "  COALESCE(percentile_cont(0.99) WITHIN GROUP (ORDER BY value), 0), "
                "  COUNT(*) "
                "FROM metrics "
                "WHERE name = %s "
                "  AND recorded_at >= now() - %s::interval",
                (name, interval),
            ).fetchone()
            if row:
                return {
                    "p50": float(row[0]),
                    "p95": float(row[1]),
                    "p99": float(row[2]),
                    "count": int(row[3]),
                }
    except Exception:
        logger.exception("Failed to fetch summary for %s", name)
    return _empty_summary()


def _get_series_breakdown(
    name: str,
    label_key: str,
    window: str = _DEFAULT_WINDOW,
    limit: int = 10,
) -> list[dict[str, object]]:
    """Return top-N buckets for *name* grouped by ``labels->>label_key``.

    Each entry has ``bucket`` (label value, never ``None``), ``p95``, and
    ``count``. Empty list on DB error or when the series has no rows.
    """
    interval = _interval_for(window)
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT "
                "  COALESCE(labels->>%s, '—') AS bucket, "
                "  COALESCE(percentile_cont(0.95) WITHIN GROUP (ORDER BY value), 0) AS p95, "
                "  COUNT(*) AS row_count "
                "FROM metrics "
                "WHERE name = %s "
                "  AND recorded_at >= now() - %s::interval "
                "GROUP BY bucket "
                "ORDER BY p95 DESC "
                "LIMIT %s",
                (label_key, name, interval, limit),
            ).fetchall()
            return [
                {
                    "bucket": r[0],
                    "p95": float(r[1]),
                    "count": int(r[2]),
                }
                for r in rows
            ]
    except Exception:
        logger.exception("Failed to fetch breakdown for %s by %s", name, label_key)
    return []


# ---------------------------------------------------------------------------
# Backward-compat helpers (kept so tests/test_performance_page.py + the
# pre-existing slow-routes card keep working with their old call shape).
# ---------------------------------------------------------------------------


def _get_latency_percentiles(window: str = _DEFAULT_WINDOW) -> dict[str, float]:
    """Return p50, p95, p99 HTTP request latency (ms) for *window*.

    Legacy 3-key shape + 3-column SELECT preserved for backward-compat with
    the original ``_latency_card`` callsite and the ``#545`` regression
    tests in ``tests/test_performance_page.py``.
    """
    interval = _interval_for(window)
    defaults = {"p50": 0.0, "p95": 0.0, "p99": 0.0}
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT "
                "  COALESCE(percentile_cont(0.50) WITHIN GROUP (ORDER BY value), 0), "
                "  COALESCE(percentile_cont(0.95) WITHIN GROUP (ORDER BY value), 0), "
                "  COALESCE(percentile_cont(0.99) WITHIN GROUP (ORDER BY value), 0) "
                "FROM metrics "
                "WHERE name = 'http_request_duration_ms' "
                "  AND recorded_at >= now() - %s::interval",
                (interval,),
            ).fetchone()
            if row:
                return {"p50": float(row[0]), "p95": float(row[1]), "p99": float(row[2])}
    except Exception:
        logger.exception("Failed to fetch latency percentiles")
    return defaults


def _get_slowest_routes(
    limit: int = 10,
    window: str = _DEFAULT_WINDOW,
) -> list[dict[str, object]]:
    """Return the slowest routes by average latency in the window."""
    interval = _interval_for(window)
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT "
                "  labels->>'path' AS path, "
                "  labels->>'method' AS method, "
                "  COUNT(*) AS request_count, "
                "  ROUND(AVG(value)::numeric, 2) AS avg_ms, "
                "  ROUND(MAX(value)::numeric, 2) AS max_ms "
                "FROM metrics "
                "WHERE name = 'http_request_duration_ms' "
                "  AND recorded_at >= now() - %s::interval "
                "GROUP BY labels->>'path', labels->>'method' "
                "ORDER BY AVG(value) DESC "
                "LIMIT %s",
                (interval, limit),
            ).fetchall()
            return [
                {
                    "path": r[0],
                    "method": r[1],
                    "request_count": r[2],
                    "avg_ms": float(r[3]),
                    "max_ms": float(r[4]),
                }
                for r in rows
            ]
    except Exception:
        logger.exception("Failed to fetch slowest routes")
    return []


def _get_job_durations(
    limit: int = 10,
    window: str = _DEFAULT_WINDOW,
) -> list[dict[str, object]]:
    """Return average job execution times from the window (legacy shape)."""
    interval = _interval_for(window)
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT "
                "  COALESCE(labels->>'handler', labels->>'job_type', name) AS job_name, "
                "  COUNT(*) AS executions, "
                "  ROUND(AVG(value)::numeric, 2) AS avg_ms, "
                "  ROUND(MAX(value)::numeric, 2) AS max_ms "
                "FROM metrics "
                "WHERE name IN ('job_execution_ms', 'job_duration_ms') "
                "  AND recorded_at >= now() - %s::interval "
                "GROUP BY COALESCE(labels->>'handler', labels->>'job_type', name) "
                "ORDER BY AVG(value) DESC "
                "LIMIT %s",
                (interval, limit),
            ).fetchall()
            return [
                {
                    "job_name": r[0],
                    "executions": r[1],
                    "avg_ms": float(r[2]),
                    "max_ms": float(r[3]),
                }
                for r in rows
            ]
    except Exception:
        logger.exception("Failed to fetch job durations")
    return []


def _get_llm_latencies(
    limit: int = 10,
    window: str = _DEFAULT_WINDOW,
) -> list[dict[str, object]]:
    """Return average LLM call latencies from the window (legacy shape)."""
    interval = _interval_for(window)
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT "
                "  COALESCE(labels->>'feature', 'unknown') AS feature, "
                "  COUNT(*) AS calls, "
                "  ROUND(AVG(value)::numeric, 2) AS avg_ms, "
                "  ROUND(MAX(value)::numeric, 2) AS max_ms "
                "FROM metrics "
                "WHERE name IN ('llm_call_ms', 'llm_call_duration_ms') "
                "  AND recorded_at >= now() - %s::interval "
                "GROUP BY COALESCE(labels->>'feature', 'unknown') "
                "ORDER BY AVG(value) DESC "
                "LIMIT %s",
                (interval, limit),
            ).fetchall()
            return [
                {
                    "feature": r[0],
                    "calls": r[1],
                    "avg_ms": float(r[2]),
                    "max_ms": float(r[3]),
                }
                for r in rows
            ]
    except Exception:
        logger.exception("Failed to fetch LLM latencies")
    return []


# ---------------------------------------------------------------------------
# Card builders
# ---------------------------------------------------------------------------


def _window_selector(active: str):
    """Render the 1h / 24h / 7d window pill selector as plain GET links."""
    pills = []
    for key, (_interval, label) in _WINDOW_CHOICES.items():
        cls = "window-pill window-pill--active" if key == active else "window-pill"
        pills.append(
            A(  # noqa: F405
                label,
                href=f"/admin/performance?window={key}",
                cls=cls,
                aria_pressed="true" if key == active else "false",
            )
        )
    return Div(  # noqa: F405
        Span("Ajavahemik:", cls="window-selector-label"),  # noqa: F405
        *pills,
        cls="window-selector",
        role="group",
        aria_label="Ajavahemiku valik",
    )


def _percentile_dl(summary: dict[str, float | int]):
    """Return the p50/p95/p99 + count description list used in every card."""
    return Dl(  # noqa: F405
        Dt("p50"),  # noqa: F405
        Dd(Badge(f"{float(summary['p50']):.1f} ms", variant="default")),  # noqa: F405
        Dt("p95"),  # noqa: F405
        Dd(Badge(f"{float(summary['p95']):.1f} ms", variant="primary")),  # noqa: F405
        Dt("p99"),  # noqa: F405
        Dd(Badge(f"{float(summary['p99']):.1f} ms", variant="danger")),  # noqa: F405
        Dt("Mõõtmisi"),  # noqa: F405
        Dd(f"{int(summary['count']):,}".replace(",", " ")),  # noqa: F405
        cls="info-list",
    )


def _series_card(
    series_name: str,
    title: str,
    window: str,
    summary: dict[str, float | int],
    breakdown: list[dict[str, object]] | None,
    breakdown_label: str | None,
    breakdown_column_header: str | None,
):
    """One card per metric series with summary + optional breakdown table."""
    if int(summary["count"]) == 0:
        body: object = P("Andmeid pole.", cls="muted-text")  # noqa: F405
    else:
        body_parts: list[object] = [_percentile_dl(summary)]
        if breakdown and breakdown_label and breakdown_column_header:
            columns = [
                Column(key="bucket", label=breakdown_column_header, sortable=False),
                Column(key="p95", label="p95 (ms)", sortable=False),
                Column(key="count", label="Mõõtmisi", sortable=False),
            ]
            rows = [
                {
                    "bucket": str(b["bucket"]),
                    "p95": f"{cast(float, b['p95']):.1f}",
                    "count": f"{cast(int, b['count']):,}".replace(",", " "),
                }
                for b in breakdown
            ]
            body_parts.append(DataTable(columns=columns, rows=rows))
        body = Div(*body_parts)  # noqa: F405

    _, win_label = _WINDOW_CHOICES.get(window, _WINDOW_CHOICES[_DEFAULT_WINDOW])
    return Card(
        CardHeader(
            H3(  # noqa: F405
                f"{title} ({win_label.lower()})",
                _tooltip(f"p50/p95/p99 protsentiilid + top-10 jaotus ({series_name})"),
                cls="card-title",
            )
        ),
        CardBody(body),
        id=f"series-card-{series_name.replace('_', '-')}",
    )


def _slowest_routes_card(routes: list[dict[str, object]], window: str = _DEFAULT_WINDOW):
    """Card showing the slowest routes by average latency (kept from #545)."""
    if not routes:
        body: object = P("Andmeid pole.", cls="muted-text")  # noqa: F405
    else:
        columns = [
            Column(key="method", label="Meetod", sortable=False),
            Column(key="path", label="Tee", sortable=False),
            Column(key="request_count", label="Päringuid", sortable=False),
            Column(key="avg_ms", label="Kesk. (ms)", sortable=False),
            Column(key="max_ms", label="Maks. (ms)", sortable=False),
        ]
        rows = [
            {
                "method": r["method"],
                "path": str(r["path"]),
                "request_count": str(r["request_count"]),
                "avg_ms": f"{cast(float, r['avg_ms']):.1f}",
                "max_ms": f"{cast(float, r['max_ms']):.1f}",
            }
            for r in routes
        ]
        body = DataTable(columns=columns, rows=rows)

    _, win_label = _WINDOW_CHOICES.get(window, _WINDOW_CHOICES[_DEFAULT_WINDOW])
    return Card(
        CardHeader(
            H3(  # noqa: F405
                f"Aeglaseimad marsruudid ({win_label.lower()})",
                _tooltip("Keskmise latentsuse järgi"),
                cls="card-title",
            )
        ),
        CardBody(body),
        id="slowest-routes-card",
    )


# ---------------------------------------------------------------------------
# Page handler
# ---------------------------------------------------------------------------


def admin_performance_page(req: Request):
    """GET /admin/performance — jõudluse jälgimine: kõik viis ajaridu.

    Module-private card builders are imported as locals inside the
    function body so tests can patch them on this module's real path.
    The whole body is wrapped in a try/except so any backend failure
    (missing ``metrics`` table, transient DB error) renders a styled
    error banner instead of bubbling up as a raw 500.
    """
    auth = req.scope.get("auth")
    theme = get_theme_from_request(req)
    try:
        from app.admin.performance import (
            _SERIES_LABELS,
            _get_series_breakdown,
            _get_series_summary,
            _get_slowest_routes,
            _parse_window,
            _series_card,
            _slowest_routes_card,
            _window_selector,
        )

        window = _parse_window(req.query_params.get("window"))

        series_cards: list[object] = []
        for series_name, (title, breakdown_label, column_header) in _SERIES_LABELS.items():
            summary = _get_series_summary(series_name, window)
            breakdown: list[dict[str, object]] | None = None
            if breakdown_label is not None and int(summary["count"]) > 0:
                breakdown = _get_series_breakdown(series_name, breakdown_label, window)
            series_cards.append(
                _series_card(
                    series_name,
                    title,
                    window,
                    summary,
                    breakdown,
                    breakdown_label,
                    column_header,
                )
            )

        routes = _get_slowest_routes(window=window)

        content = (
            H1("Jõudlus", cls="page-title"),  # noqa: F405
            P(A("← Tagasi adminipaneelile", href="/admin"), cls="back-link"),  # noqa: F405
            _window_selector(window),
            *series_cards,
            _slowest_routes_card(routes, window=window),
        )

        return PageShell(
            *content,
            title="Jõudlus",
            user=auth,
            theme=theme,
            active_nav="/admin",
        )
    except Exception:
        logger.exception("Failed to render admin performance page")
        from app.admin._shared import _render_admin_error_page

        return _render_admin_error_page(title="Jõudlus", user=auth, theme=theme)
