"""Admin performance page — request latency, slow routes, job/LLM timings.

All data comes from the ``metrics`` table via Postgres aggregation queries.
The page is mounted at ``/admin/performance`` and requires the ``admin`` role.
"""

from __future__ import annotations

import logging

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
# Data helpers
# ---------------------------------------------------------------------------


def _get_latency_percentiles() -> dict[str, float]:
    """Return p50, p95, p99 request latency (ms) for the last hour."""
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
                "  AND recorded_at >= now() - interval '1 hour'"
            ).fetchone()
            if row:
                return {"p50": float(row[0]), "p95": float(row[1]), "p99": float(row[2])}
    except Exception:
        logger.exception("Failed to fetch latency percentiles")
    return defaults


def _get_slowest_routes(limit: int = 10) -> list[dict[str, object]]:
    """Return the slowest routes by average latency in the last hour."""
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
                "  AND recorded_at >= now() - interval '1 hour' "
                "GROUP BY labels->>'path', labels->>'method' "
                "ORDER BY AVG(value) DESC "
                "LIMIT %s",
                (limit,),
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


def _get_job_durations(limit: int = 10) -> list[dict[str, object]]:
    """Return average job execution times from the last hour."""
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT "
                "  COALESCE(labels->>'job_type', name) AS job_name, "
                "  COUNT(*) AS executions, "
                "  ROUND(AVG(value)::numeric, 2) AS avg_ms, "
                "  ROUND(MAX(value)::numeric, 2) AS max_ms "
                "FROM metrics "
                "WHERE name = 'job_duration_ms' "
                "  AND recorded_at >= now() - interval '1 hour' "
                "GROUP BY COALESCE(labels->>'job_type', name) "
                "ORDER BY AVG(value) DESC "
                "LIMIT %s",
                (limit,),
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


def _get_llm_latencies(limit: int = 10) -> list[dict[str, object]]:
    """Return average LLM call latencies from the last hour."""
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT "
                "  COALESCE(labels->>'feature', 'unknown') AS feature, "
                "  COUNT(*) AS calls, "
                "  ROUND(AVG(value)::numeric, 2) AS avg_ms, "
                "  ROUND(MAX(value)::numeric, 2) AS max_ms "
                "FROM metrics "
                "WHERE name = 'llm_call_duration_ms' "
                "  AND recorded_at >= now() - interval '1 hour' "
                "GROUP BY COALESCE(labels->>'feature', 'unknown') "
                "ORDER BY AVG(value) DESC "
                "LIMIT %s",
                (limit,),
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


def _latency_card(percentiles: dict[str, float]):
    """Card showing p50/p95/p99 latency for the last hour."""
    body = Dl(  # noqa: F405
        Dt("p50"),  # noqa: F405
        Dd(Badge(f"{percentiles['p50']:.1f} ms", variant="default")),  # noqa: F405
        Dt("p95"),  # noqa: F405
        Dd(Badge(f"{percentiles['p95']:.1f} ms", variant="primary")),  # noqa: F405
        Dt("p99"),  # noqa: F405
        Dd(Badge(f"{percentiles['p99']:.1f} ms", variant="danger")),  # noqa: F405
        cls="info-list",
    )
    return Card(
        CardHeader(
            H3(  # noqa: F405
                "P\u00e4ringu latentsus (viimane tund)",
                _tooltip("p50, p95 ja p99 protsentiilid"),
                cls="card-title",
            )
        ),
        CardBody(body),
        id="latency-card",
    )


def _slowest_routes_card(routes: list[dict[str, object]]):
    """Card showing the slowest routes by average latency."""
    if not routes:
        body: object = P("Andmed puuduvad.", cls="muted-text")  # noqa: F405
    else:
        columns = [
            Column(key="method", label="Meetod", sortable=False),
            Column(key="path", label="Tee", sortable=False),
            Column(key="request_count", label="P\u00e4ringuid", sortable=False),
            Column(key="avg_ms", label="Kesk. (ms)", sortable=False),
            Column(key="max_ms", label="Maks. (ms)", sortable=False),
        ]
        rows = [
            {
                "method": r["method"],
                "path": str(r["path"]),
                "request_count": str(r["request_count"]),
                "avg_ms": f"{r['avg_ms']:.1f}",
                "max_ms": f"{r['max_ms']:.1f}",
            }
            for r in routes
        ]
        body = DataTable(columns=columns, rows=rows)

    return Card(
        CardHeader(
            H3(  # noqa: F405
                "Aeglaseimad marsruudid",
                _tooltip("Keskmise latentsuse j\u00e4rgi viimase tunni jooksul"),
                cls="card-title",
            )
        ),
        CardBody(body),
        id="slowest-routes-card",
    )


def _job_durations_card(jobs: list[dict[str, object]]):
    """Card showing job execution times."""
    if not jobs:
        body: object = P("Andmed puuduvad.", cls="muted-text")  # noqa: F405
    else:
        columns = [
            Column(key="job_name", label="Jobi t\u00fc\u00fcp", sortable=False),
            Column(key="executions", label="T\u00e4itmisi", sortable=False),
            Column(key="avg_ms", label="Kesk. (ms)", sortable=False),
            Column(key="max_ms", label="Maks. (ms)", sortable=False),
        ]
        rows = [
            {
                "job_name": j["job_name"],
                "executions": str(j["executions"]),
                "avg_ms": f"{j['avg_ms']:.1f}",
                "max_ms": f"{j['max_ms']:.1f}",
            }
            for j in jobs
        ]
        body = DataTable(columns=columns, rows=rows)

    return Card(
        CardHeader(
            H3(  # noqa: F405
                "Jobide t\u00e4itmisajad",
                _tooltip("Taustajobide kestvus viimase tunni jooksul"),
                cls="card-title",
            )
        ),
        CardBody(body),
        id="job-durations-card",
    )


def _llm_latencies_card(latencies: list[dict[str, object]]):
    """Card showing LLM call latencies by feature."""
    if not latencies:
        body: object = P("Andmed puuduvad.", cls="muted-text")  # noqa: F405
    else:
        columns = [
            Column(key="feature", label="Funktsioon", sortable=False),
            Column(key="calls", label="Kutseid", sortable=False),
            Column(key="avg_ms", label="Kesk. (ms)", sortable=False),
            Column(key="max_ms", label="Maks. (ms)", sortable=False),
        ]
        rows = [
            {
                "feature": lat["feature"],
                "calls": str(lat["calls"]),
                "avg_ms": f"{lat['avg_ms']:.1f}",
                "max_ms": f"{lat['max_ms']:.1f}",
            }
            for lat in latencies
        ]
        body = DataTable(columns=columns, rows=rows)

    return Card(
        CardHeader(
            H3(  # noqa: F405
                "LLM-i latentsus",
                _tooltip("LLM-kutsete kestvus funktsiooni kaupa"),
                cls="card-title",
            )
        ),
        CardBody(body),
        id="llm-latencies-card",
    )


# ---------------------------------------------------------------------------
# Page handler
# ---------------------------------------------------------------------------


def admin_performance_page(req: Request):
    """GET /admin/performance — performance metrics dashboard.

    Helpers are imported as locals inside the function body so the page
    works correctly when rebound by the ``app.templates.admin_dashboard``
    shim — that shim swaps ``__globals__`` to its own module dict, which
    means private card builders (``_latency_card``,
    ``_slowest_routes_card``, etc.) cannot be resolved via the function's
    global namespace. The whole body is wrapped in a try/except so any
    backend failure (missing ``metrics`` table, transient DB error)
    renders a styled error banner instead of bubbling up as a raw 500.
    """
    auth = req.scope.get("auth")
    theme = get_theme_from_request(req)
    try:
        from app.admin.performance import (
            _get_job_durations,
            _get_latency_percentiles,
            _get_llm_latencies,
            _get_slowest_routes,
            _job_durations_card,
            _latency_card,
            _llm_latencies_card,
            _slowest_routes_card,
        )

        percentiles = _get_latency_percentiles()
        routes = _get_slowest_routes()
        jobs = _get_job_durations()
        llm = _get_llm_latencies()

        content = (
            H1("Jõudlus", cls="page-title"),  # noqa: F405
            P(A("← Tagasi adminipaneelile", href="/admin"), cls="back-link"),  # noqa: F405
            _latency_card(percentiles),
            _slowest_routes_card(routes),
            _job_durations_card(jobs),
            _llm_latencies_card(llm),
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
