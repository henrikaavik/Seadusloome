"""Admin job monitor page — full-page view of background job health.

Provides summary badges, per-type breakdown, recent failures with
expandable error messages, retry and purge actions. All data comes
from the ``background_jobs`` table.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.db import get_connection as _connect
from app.ui.data.data_table import Column, DataTable
from app.ui.layout import PageShell
from app.ui.primitives.badge import Badge, BadgeVariant
from app.ui.primitives.button import Button  # noqa: F401, F811  -- shadow guard
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.theme import get_theme_from_request

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _get_status_counts() -> dict[str, int]:
    """Return job counts grouped by status."""
    counts: dict[str, int] = {
        "pending": 0,
        "running": 0,
        "failed": 0,
        "success": 0,
    }
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM background_jobs GROUP BY status"
            ).fetchall()
            for r in rows:
                counts[r[0]] = r[1]
    except Exception:
        logger.exception("Failed to fetch job status counts")
    return counts


def _get_type_breakdown() -> list[dict]:  # type: ignore[type-arg]
    """Return per-job-type counts grouped by status."""
    breakdown: list[dict] = []  # type: ignore[type-arg]
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT job_type, status, COUNT(*) "
                "FROM background_jobs "
                "GROUP BY job_type, status "
                "ORDER BY job_type, status"
            ).fetchall()
            # Pivot: group by job_type
            types: dict[str, dict[str, int]] = {}  # type: ignore[type-arg]
            for r in rows:
                jt = r[0]
                if jt not in types:
                    types[jt] = {"pending": 0, "running": 0, "failed": 0, "success": 0}
                types[jt][r[1]] = r[2]
            for jt, counts in sorted(types.items()):
                breakdown.append(
                    {
                        "job_type": jt,
                        "pending": counts.get("pending", 0),
                        "running": counts.get("running", 0),
                        "failed": counts.get("failed", 0),
                        "success": counts.get("success", 0),
                    }
                )
    except Exception:
        logger.exception("Failed to fetch job type breakdown")
    return breakdown


def _get_recent_failed(limit: int = 20) -> list[dict]:  # type: ignore[type-arg]
    """Return recent failed jobs with error messages."""
    jobs: list[dict] = []  # type: ignore[type-arg]
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT id, job_type, error_message, attempts, max_attempts, "
                "finished_at, created_at "
                "FROM background_jobs "
                "WHERE status = 'failed' "
                "ORDER BY finished_at DESC NULLS LAST "
                "LIMIT %s",
                (limit,),
            ).fetchall()
            jobs = [
                {
                    "id": r[0],
                    "job_type": r[1],
                    "error_message": r[2] or "",
                    "attempts": r[3],
                    "max_attempts": r[4],
                    "finished_at": r[5],
                    "created_at": r[6],
                }
                for r in rows
            ]
    except Exception:
        logger.exception("Failed to fetch recent failed jobs")
    return jobs


def _retry_job(job_id: int) -> bool:
    """Reset a job to 'pending' status for retry. Returns True on success."""
    try:
        with _connect() as conn:
            result = conn.execute(
                "UPDATE background_jobs "
                "SET status = 'pending', "
                "    error_message = NULL, "
                "    finished_at = NULL, "
                "    claimed_by = NULL, "
                "    claimed_at = NULL, "
                "    started_at = NULL, "
                "    scheduled_for = %s "
                "WHERE id = %s AND status = 'failed'",
                (datetime.now(UTC), job_id),
            )
            conn.commit()
            return result.rowcount > 0  # type: ignore[union-attr]
    except Exception:
        logger.exception("Failed to retry job id=%d", job_id)
        return False


def _purge_completed(days: int = 7) -> int:
    """Delete completed (success) jobs older than *days*. Returns count deleted."""
    try:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        with _connect() as conn:
            result = conn.execute(
                "DELETE FROM background_jobs WHERE status = 'success' AND finished_at < %s",
                (cutoff,),
            )
            conn.commit()
            return result.rowcount  # type: ignore[return-value]
    except Exception:
        logger.exception("Failed to purge completed jobs")
        return 0


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

_STATUS_VARIANT: dict[str, BadgeVariant] = {
    "pending": "default",
    "running": "primary",
    "failed": "danger",
    "success": "success",
}

_STATUS_LABEL = {
    "pending": "Ootel",
    "running": "T\u00f6\u00f6tab",
    "failed": "Eba\u00f5nnestunud",
    "success": "\u00d5nnestunud",
}


def _summary_badges(counts: dict[str, int]):
    """Render summary badges for each status."""
    badges = []
    for status in ("pending", "running", "failed", "success"):
        count = counts.get(status, 0)
        label = _STATUS_LABEL[status]
        variant = _STATUS_VARIANT[status]
        badges.append(Badge(f"{count} {label.lower()}", variant=variant))
        badges.append(" ")
    return Div(*badges, cls="job-summary-badges")  # noqa: F405


def _type_breakdown_table(breakdown: list[dict]):  # type: ignore[type-arg]
    """Render the per-job-type breakdown table."""
    if not breakdown:
        return P("T\u00f6\u00f6de ei leitud.", cls="muted-text")  # noqa: F405

    columns = [
        Column(key="job_type", label="T\u00f6\u00f6 t\u00fc\u00fcp", sortable=False),
        Column(key="pending", label="Ootel", sortable=False),
        Column(key="running", label="T\u00f6\u00f6tab", sortable=False),
        Column(key="failed", label="Eba\u00f5nnestunud", sortable=False),
        Column(key="success", label="\u00d5nnestunud", sortable=False),
    ]
    rows = [
        {
            "job_type": b["job_type"],
            "pending": str(b["pending"]),
            "running": str(b["running"]),
            "failed": str(b["failed"]),
            "success": str(b["success"]),
        }
        for b in breakdown
    ]
    return DataTable(columns=columns, rows=rows)


def _failed_jobs_table(failed_jobs: list[dict]):  # type: ignore[type-arg]
    """Render the recent failed jobs with expandable errors and retry buttons."""
    if not failed_jobs:
        return P("Eba\u00f5nnestunud t\u00f6\u00f6sid ei leitud.", cls="muted-text")  # noqa: F405

    rows_ft = []
    for job in failed_jobs:
        error = job["error_message"]
        short_error = error[:120] + "..." if len(error) > 120 else error
        finished = job["finished_at"]
        finished_str = finished.strftime("%d.%m.%Y %H:%M") if finished else "\u2014"

        # Expandable error: Details element for long errors
        if len(error) > 120:
            error_cell = Details(  # noqa: F405
                Summary(short_error),  # noqa: F405
                Pre(error, cls="error-detail-pre"),  # noqa: F405
                cls="error-expandable",
            )
        else:
            error_cell = Span(short_error)  # noqa: F405

        retry_btn = Button(
            "Proovi uuesti",
            hx_post=f"/admin/jobs/{job['id']}/retry",
            hx_target="#job-monitor-content",
            hx_swap="innerHTML",
            variant="secondary",
            size="sm",
        )

        rows_ft.append(
            Tr(  # noqa: F405
                Td(str(job["id"]), data_label="ID"),  # noqa: F405
                Td(job["job_type"], data_label="T\u00fc\u00fcp"),  # noqa: F405
                Td(error_cell, data_label="Veateade"),  # noqa: F405
                Td(  # noqa: F405
                    f"{job['attempts']}/{job['max_attempts']}", data_label="Katseid"
                ),
                Td(finished_str, data_label="L\u00f5petatud"),  # noqa: F405
                Td(retry_btn, data_label="Toiming"),  # noqa: F405
            )
        )

    return Div(  # noqa: F405
        Table(  # noqa: F405
            Thead(  # noqa: F405
                Tr(  # noqa: F405
                    Th("ID", scope="col"),  # noqa: F405
                    Th("T\u00fc\u00fcp", scope="col"),  # noqa: F405
                    Th("Veateade", scope="col"),  # noqa: F405
                    Th("Katseid", scope="col"),  # noqa: F405
                    Th("L\u00f5petatud", scope="col"),  # noqa: F405
                    Th("Toiming", scope="col"),  # noqa: F405
                )
            ),
            Tbody(*rows_ft),  # noqa: F405
            cls="data-table",
            role="table",
        ),
        cls="data-table-wrapper",
    )


def _job_monitor_content() -> object:
    """Render the full job monitor content (swappable via HTMX)."""
    counts = _get_status_counts()
    breakdown = _get_type_breakdown()
    failed_jobs = _get_recent_failed()

    return Div(  # noqa: F405
        Card(
            CardHeader(H3("Kokkuv\u00f5te", cls="card-title")),  # noqa: F405
            CardBody(_summary_badges(counts)),
        ),
        Card(
            CardHeader(H3("T\u00f6\u00f6de t\u00fc\u00fcbi kaupa", cls="card-title")),  # noqa: F405
            CardBody(_type_breakdown_table(breakdown)),
        ),
        Card(
            CardHeader(
                Div(  # noqa: F405
                    H3("Viimased eba\u00f5nnestunud t\u00f6\u00f6d", cls="card-title"),  # noqa: F405
                    Button(
                        "Puhasta vanad t\u00f6\u00f6d",
                        hx_post="/admin/jobs/purge",
                        hx_target="#job-monitor-content",
                        hx_swap="innerHTML",
                        hx_confirm=(
                            "Kas olete kindel? Kustutab \u00f5nnestunud "
                            "t\u00f6\u00f6d, mis on vanemad kui 7 p\u00e4eva."
                        ),
                        variant="danger",
                        size="sm",
                    ),
                    cls="card-header-row",
                ),
            ),
            CardBody(_failed_jobs_table(failed_jobs)),
        ),
        id="job-monitor-content",
    )


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def admin_jobs_page(req: Request):
    """GET /admin/jobs -- full job monitor page."""
    auth = req.scope.get("auth")
    theme = get_theme_from_request(req)

    content = (
        H1("T\u00f6\u00f6de monitor", cls="page-title"),  # noqa: F405
        P(A("\u2190 Tagasi adminipaneelile", href="/admin"), cls="back-link"),  # noqa: F405
        _job_monitor_content(),
    )

    return PageShell(
        *content,
        title="T\u00f6\u00f6de monitor",
        user=auth,
        theme=theme,
        active_nav="/admin",
    )


def admin_job_retry(req: Request, id: int):
    """POST /admin/jobs/{id}/retry -- retry a single failed job."""
    success = _retry_job(id)
    if not success:
        return JSONResponse(
            {"error": "T\u00f6\u00f6d ei leitud v\u00f5i ei ole eba\u00f5nnestunud staatuses."},
            status_code=404,
        )
    # Return refreshed content
    return _job_monitor_content()


def admin_jobs_purge(req: Request):
    """POST /admin/jobs/purge -- delete completed jobs older than 7 days."""
    deleted = _purge_completed(days=7)
    logger.info("Purged %d completed jobs older than 7 days", deleted)
    # Return refreshed content
    return _job_monitor_content()
