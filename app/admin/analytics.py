"""Admin usage analytics page and helpers.

Shows daily usage trends (uploads, chat messages, drafter sessions) from
the ``usage_daily`` materialized view.  The view can be refreshed via
the ``refresh_usage_daily`` background job or the manual button on the page.
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
from app.ui.surfaces.info_box import InfoBox
from app.ui.theme import get_theme_from_request

logger = logging.getLogger(__name__)


def _refresh_usage_daily() -> bool:
    """Refresh the ``usage_daily`` materialized view concurrently.

    Returns True on success, False on error. Errors are logged but never
    propagated so callers can degrade gracefully.
    """
    try:
        with _connect() as conn:
            conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY usage_daily")
            conn.commit()
        return True
    except Exception:
        logger.exception("Failed to refresh usage_daily materialized view")
        return False


def _get_usage_data(days: int = 30) -> list[dict]:  # type: ignore[type-arg]
    """Query ``usage_daily`` for the last *days* days.

    Returns a list of dicts with keys: ``day``, ``uploads``,
    ``chat_messages``, ``drafter_sessions``.  Empty list on error.
    """
    rows_out: list[dict] = []  # type: ignore[type-arg]
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT day, uploads, chat_messages, drafter_sessions "
                "FROM usage_daily "
                "WHERE day >= CURRENT_DATE - %s "
                "ORDER BY day DESC",
                (days,),
            ).fetchall()
            rows_out = [
                {
                    "day": r[0],
                    "uploads": r[1],
                    "chat_messages": r[2],
                    "drafter_sessions": r[3],
                }
                for r in rows
            ]
    except Exception:
        logger.exception("Failed to fetch usage_daily data")
    return rows_out


def _usage_summary(data: list[dict]) -> dict:  # type: ignore[type-arg]
    """Compute totals across the given usage data rows."""
    return {
        "total_uploads": sum(r["uploads"] for r in data),
        "total_chat_messages": sum(r["chat_messages"] for r in data),
        "total_drafter_sessions": sum(r["drafter_sessions"] for r in data),
        "days": len(data),
    }


def _svg_bar_chart(data: list[dict], key: str, label: str, color: str) -> object:  # type: ignore[type-arg]
    """Render a simple inline SVG bar chart for the given metric.

    Shows the most recent 30 days left-to-right with the newest day on
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


def admin_analytics_page(req: Request):
    """GET /admin/analytics -- usage analytics page."""
    auth = req.scope.get("auth")
    theme = get_theme_from_request(req)

    days_str = req.query_params.get("days", "30")
    try:
        days = max(1, min(365, int(days_str)))
    except ValueError:
        days = 30

    data = _get_usage_data(days)
    summary = _usage_summary(data)

    summary_dl = Dl(  # noqa: F405
        Dt("Perioodi pikkus"),  # noqa: F405
        Dd(Badge(f"{summary['days']} paeva", variant="default")),  # noqa: F405
        Dt("Uleslaadimisi kokku"),  # noqa: F405
        Dd(Badge(str(summary["total_uploads"]), variant="primary")),  # noqa: F405
        Dt("Vestluse sonumeid kokku"),  # noqa: F405
        Dd(Badge(str(summary["total_chat_messages"]), variant="primary")),  # noqa: F405
        Dt("Koostamise seansse kokku"),  # noqa: F405
        Dd(Badge(str(summary["total_drafter_sessions"]), variant="primary")),  # noqa: F405
        cls="info-list",
    )

    # Charts
    uploads_chart = _svg_bar_chart(data, "uploads", "Uleslaadimised paevas", "#0066cc")
    chat_chart = _svg_bar_chart(data, "chat_messages", "Vestluse sonumid paevas", "#2e8b57")
    drafter_chart = _svg_bar_chart(
        data, "drafter_sessions", "Koostamise seansid paevas", "#9b59b6"
    )

    # Detail table
    if data:
        columns = [
            Column(key="day", label="Kuupaev", sortable=False),
            Column(key="uploads", label="Uleslaadimised", sortable=False),
            Column(key="chat_messages", label="Vestluse sonumid", sortable=False),
            Column(key="drafter_sessions", label="Koostamise seansid", sortable=False),
        ]
        rows = [
            {
                "day": (
                    r["day"].strftime("%d.%m.%Y")
                    if hasattr(r["day"], "strftime")
                    else str(r["day"])
                ),
                "uploads": str(r["uploads"]),
                "chat_messages": str(r["chat_messages"]),
                "drafter_sessions": str(r["drafter_sessions"]),
            }
            for r in data
        ]
        table = DataTable(columns=columns, rows=rows)
    else:
        table = P("Kasutusandmed puuduvad.", cls="muted-text")  # noqa: F405

    content = (
        H1("Kasutusanaluutika", cls="page-title"),  # noqa: F405
        P(A("\u2190 Tagasi adminipaneelile", href="/admin"), cls="back-link"),  # noqa: F405
        InfoBox(
            P(  # noqa: F405
                "Kasutusanaluutika naitab uleslaadimiste, vestluse sonumite "
                "ja koostamise seansside statistikat paevade kaupa."
            ),
            variant="info",
            dismissible=True,
        ),
        Card(
            CardHeader(
                H3(  # noqa: F405
                    "Kokkuvote",
                    _tooltip(f"Viimased {days} paeva"),
                    cls="card-title",
                )
            ),
            CardBody(summary_dl),
        ),
        Card(
            CardHeader(H3("Trendid", cls="card-title")),  # noqa: F405
            CardBody(uploads_chart, chat_chart, drafter_chart),
        ),
        Card(
            CardHeader(H3("Detailne tabel", cls="card-title")),  # noqa: F405
            CardBody(table),
        ),
    )

    return PageShell(
        *content,
        title="Kasutusanaluutika",
        user=auth,
        theme=theme,
        active_nav="/admin",
    )
