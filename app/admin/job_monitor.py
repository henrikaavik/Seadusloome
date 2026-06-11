"""Admin job monitor page — full-page view of background job health.

Provides summary badges, per-type breakdown, per-handler 24h aggregate
stats (count / success rate / p95 duration) sourced from the
``metrics`` table, a paginated + filterable failed-jobs table with
expandable error messages, retry and purge actions, and an HTMX-loaded
detail fragment per job (payload JSON + traceback + attempts history).

Job rows come from ``background_jobs``; per-handler stats come from
``metrics`` rows whose ``name = 'job_execution_ms'`` (collector ships
with PR #835). When no metrics rows exist the per-handler card renders
a graceful empty-state in Estonian.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlencode

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.db import get_connection as _connect
from app.ui.data.data_table import Column, DataTable
from app.ui.data.pagination import Pagination
from app.ui.forms.app_form import AppForm
from app.ui.layout import PageShell
from app.ui.primitives.badge import Badge, BadgeVariant
from app.ui.primitives.button import Button  # noqa: F401, F811  -- shadow guard
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.theme import get_theme_from_request
from app.ui.time import format_tallinn

logger = logging.getLogger(__name__)

# Status values accepted from filter input. The DB uses 'success' rather
# than 'completed' (see migrations/005), so the filter normalises the
# UI-friendly 'completed' alias into 'success' before querying.
_VALID_STATUSES: tuple[str, ...] = ("pending", "running", "failed", "success")
_STATUS_ALIASES: dict[str, str] = {"completed": "success"}

_DEFAULT_PAGE_SIZE = 20


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


def _parse_date(value: str | None) -> date | None:
    """Parse a date string (YYYY-MM-DD) or return None on any failure."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _normalise_status(value: str | None) -> str | None:
    """Map UI-friendly status values to DB values; reject anything unknown."""
    if not value:
        return None
    v = value.strip().lower()
    v = _STATUS_ALIASES.get(v, v)
    return v if v in _VALID_STATUSES else None


def _build_jobs_where(
    *,
    handlers: list[str] | None = None,
    status: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> tuple[str, list]:
    """Build a WHERE clause for ``background_jobs`` filtering.

    ``handlers`` filters ``job_type`` via ``= ANY(%s)`` so we keep
    parameterisation tight (no IN-list string interpolation). The
    date range is applied against ``started_at`` per the issue spec
    so a job that has not started yet is excluded from time-windowed
    views.
    """
    clauses: list[str] = []
    params: list = []

    if handlers:
        clauses.append("job_type = ANY(%s)")
        params.append(list(handlers))
    if status:
        clauses.append("status = %s")
        params.append(status)
    if date_from:
        clauses.append("started_at >= %s")
        params.append(datetime.combine(date_from, datetime.min.time()))
    if date_to:
        clauses.append("started_at < %s")
        params.append(datetime.combine(date_to + timedelta(days=1), datetime.min.time()))

    where = " AND ".join(clauses) if clauses else "TRUE"
    return where, params


def _get_filtered_jobs_page(
    *,
    page: int = 1,
    per_page: int = _DEFAULT_PAGE_SIZE,
    handlers: list[str] | None = None,
    status: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> tuple[list[dict], int]:  # type: ignore[type-arg]
    """Return a page of background_jobs matching the filters + total count."""
    jobs: list[dict] = []  # type: ignore[type-arg]
    total = 0
    try:
        where, params = _build_jobs_where(
            handlers=handlers, status=status, date_from=date_from, date_to=date_to
        )
        with _connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM background_jobs WHERE {where}",  # type: ignore[arg-type]
                params,
            ).fetchone()
            total = row[0] if row else 0

            offset = (page - 1) * per_page
            rows = conn.execute(
                f"SELECT id, job_type, status, error_message, attempts, max_attempts, "  # type: ignore[arg-type]
                f"finished_at, started_at, created_at "
                f"FROM background_jobs "
                f"WHERE {where} "
                f"ORDER BY COALESCE(finished_at, started_at, created_at) DESC NULLS LAST "
                f"LIMIT %s OFFSET %s",
                [*params, per_page, offset],
            ).fetchall()
            jobs = [
                {
                    "id": r[0],
                    "job_type": r[1],
                    "status": r[2],
                    "error_message": r[3] or "",
                    "attempts": r[4],
                    "max_attempts": r[5],
                    "finished_at": r[6],
                    "started_at": r[7],
                    "created_at": r[8],
                }
                for r in rows
            ]
    except Exception:
        logger.exception("Failed to fetch filtered jobs page %d", page)
    return jobs, total


def _get_recent_failed(limit: int = 20) -> list[dict]:  # type: ignore[type-arg]
    """Return recent failed jobs with error messages.

    Kept for backwards compatibility — the main page now uses
    ``_get_filtered_jobs_page``, but unit tests + the route module
    still import this helper.
    """
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


def _get_handler_stats_24h() -> list[dict]:  # type: ignore[type-arg]
    """Return per-handler aggregate stats from the ``metrics`` table.

    Reads rows with ``name = 'job_execution_ms'`` recorded in the last
    24 hours and aggregates them per ``labels->>'handler'``:

    * ``count`` — total samples
    * ``success_rate`` — fraction (0–1) where ``labels->>'status' IN ('ok',
      'success')``. The metric collector emits ``'success'`` post-#835
      (2026-05-25); we still accept the historical ``'ok'`` label so older
      rows in the ``metrics`` table count toward the same success bucket
      instead of being silently treated as failures.
    * ``p95_ms`` — 95th-percentile duration via ``percentile_cont``

    Returns ``[]`` on any error or when the collector has not yet
    written any rows — caller renders the empty-state.
    """
    stats: list[dict] = []  # type: ignore[type-arg]
    try:
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        with _connect() as conn:
            rows = conn.execute(
                "SELECT labels->>'handler' AS handler, "
                "       COUNT(*)::int AS samples, "
                "       AVG(CASE WHEN labels->>'status' IN ('ok', 'success') "
                "                THEN 1.0 ELSE 0.0 END) "
                "         AS success_rate, "
                "       percentile_cont(0.95) WITHIN GROUP (ORDER BY value) AS p95_ms "
                "FROM metrics "
                "WHERE name = 'job_execution_ms' "
                "  AND recorded_at >= %s "
                "  AND labels->>'handler' IS NOT NULL "
                "GROUP BY labels->>'handler' "
                "ORDER BY samples DESC, handler ASC",
                (cutoff,),
            ).fetchall()
            stats = [
                {
                    "handler": r[0],
                    "count": int(r[1]) if r[1] is not None else 0,
                    "success_rate": float(r[2]) if r[2] is not None else 0.0,
                    "p95_ms": float(r[3]) if r[3] is not None else 0.0,
                }
                for r in rows
            ]
    except Exception:
        logger.exception("Failed to fetch per-handler metrics aggregates")
    return stats


def _get_job_detail(job_id: int) -> dict | None:  # type: ignore[type-arg]
    """Return a single background_jobs row keyed by id, or None."""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT id, job_type, status, payload, error_message, attempts, "
                "max_attempts, started_at, finished_at, created_at, scheduled_for, "
                "claimed_by, claimed_at, result "
                "FROM background_jobs WHERE id = %s",
                (job_id,),
            ).fetchone()
            if not row:
                return None
            return {
                "id": row[0],
                "job_type": row[1],
                "status": row[2],
                "payload": row[3],
                "error_message": row[4] or "",
                "attempts": row[5],
                "max_attempts": row[6],
                "started_at": row[7],
                "finished_at": row[8],
                "created_at": row[9],
                "scheduled_for": row[10],
                "claimed_by": row[11],
                "claimed_at": row[12],
                "result": row[13],
            }
    except Exception:
        logger.exception("Failed to fetch job detail id=%d", job_id)
        return None


def _retry_job(job_id: int) -> bool:
    """Reset a failed job to 'pending' for ONE more attempt. True on success.

    #852: ``attempts`` is deliberately PRESERVED (it used to be reset to
    0, which made a poison job retryable forever and defeated
    ``max_attempts``). A failed job already has ``attempts >=
    max_attempts``, so each admin click grants exactly one extra
    attempt: the worker runs it, and on failure ``mark_failed`` sees
    ``attempts + 1 > max_attempts`` and flips it straight back to
    ``failed`` (handlers likewise see ``attempt >= max_attempts`` and
    apply their final-attempt domain consequences). The climbing
    counter (e.g. ``4/3``) doubles as the audit trail of admin retries.
    """
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
# Filter handling
# ---------------------------------------------------------------------------


def _extract_filters(req: Request) -> dict:  # type: ignore[type-arg]
    """Extract filter values from query params.

    Multi-select ``handler`` uses ``getlist`` so repeated
    ``?handler=parse_draft&handler=extract_entities`` survives.
    """
    qp = req.query_params
    return {
        "handlers": [h for h in qp.getlist("handler") if h],
        "status": qp.get("status", ""),
        "from": qp.get("from", ""),
        "to": qp.get("to", ""),
    }


def _filter_query_string(filters: dict) -> str:  # type: ignore[type-arg]
    """Re-encode current filters as a query string (without ``page``)."""
    pairs: list[tuple[str, str]] = []
    for h in filters.get("handlers", []) or []:
        pairs.append(("handler", h))
    for key, target in (("status", "status"), ("from", "from"), ("to", "to")):
        v = filters.get(key, "")
        if v:
            pairs.append((target, v))
    return urlencode(pairs)


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
    "running": "Töötab",
    "failed": "Ebaõnnestunud",
    "success": "Õnnestunud",
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
        return P("Tööde ei leitud.", cls="muted-text")  # noqa: F405

    columns = [
        Column(key="job_type", label="Töö tüüp", sortable=False),
        Column(key="pending", label="Ootel", sortable=False),
        Column(key="running", label="Töötab", sortable=False),
        Column(key="failed", label="Ebaõnnestunud", sortable=False),
        Column(key="success", label="Õnnestunud", sortable=False),
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


def _handler_stats_card(stats: list[dict]):  # type: ignore[type-arg]
    """Render the 24h per-handler metrics card.

    When the collector hasn't written any rows yet (``stats == []``) we
    show a neutral empty-state explaining what the card will eventually
    surface — the issue explicitly calls this out as the expected
    pre-PR-#835 behaviour.
    """
    if not stats:
        empty = P(  # noqa: F405
            "Aktiivsuse statistikat pole veel kogutud. "
            "Tööde kestust hakatakse koguma kohe, kui taustatööde "
            "telemeetria on aktiveeritud.",
            cls="muted-text",
        )
        return Card(
            CardHeader(
                H3(
                    "Töötlejate statistika (viimased 24h)",  # noqa: F405
                    cls="card-title",
                ),
            ),
            CardBody(empty),
        )

    columns = [
        Column(key="handler", label="Töötleja", sortable=False),
        Column(key="count", label="Käivitusi", sortable=False),
        Column(key="success_rate", label="Edukuse määr", sortable=False),
        Column(key="p95_ms", label="p95 kestus", sortable=False),
    ]
    rows = []
    for s in stats:
        rate_pct = f"{s['success_rate'] * 100:.1f}%"
        p95 = f"{s['p95_ms']:.0f} ms"
        rows.append(
            {
                "handler": s["handler"],
                "count": str(s["count"]),
                "success_rate": rate_pct,
                "p95_ms": p95,
            }
        )
    return Card(
        CardHeader(
            H3(
                "Töötlejate statistika (viimased 24h)",  # noqa: F405
                cls="card-title",
            ),
        ),
        CardBody(DataTable(columns=columns, rows=rows)),
    )


def _filter_form(
    filters: dict,  # type: ignore[type-arg]
    handler_options: list[str],
) -> object:
    """Render the filter form (handler multi-select + status + dates)."""
    # Status dropdown
    status_choices = [
        ("", "Kõik staatused"),
        ("pending", "Ootel"),
        ("running", "Töötab"),
        ("failed", "Ebaõnnestunud"),
        ("success", "Õnnestunud"),
    ]
    status_options = []
    for value, label in status_choices:
        selected = "selected" if filters.get("status", "") == value else None
        status_options.append(Option(label, value=value, selected=selected))  # noqa: F405

    # Handler multi-select (Estonian "Töötleja tüüp")
    selected_handlers = set(filters.get("handlers", []) or [])
    handler_opts = []
    for h in handler_options:
        selected = "selected" if h in selected_handlers else None
        handler_opts.append(Option(h, value=h, selected=selected))  # noqa: F405

    return AppForm(
        Div(  # noqa: F405
            Div(  # noqa: F405
                Label("Töötleja tüüp", fr="filter-handler"),  # noqa: F405
                Select(  # noqa: F405
                    *handler_opts,
                    name="handler",
                    id="filter-handler",
                    multiple=True,
                    size=min(6, max(3, len(handler_options))) if handler_options else 3,
                ),
                cls="filter-field",
            ),
            Div(  # noqa: F405
                Label("Staatus", fr="filter-status"),  # noqa: F405
                Select(*status_options, name="status", id="filter-status"),  # noqa: F405
                cls="filter-field",
            ),
            Div(  # noqa: F405
                Label("Alguskuupäev", fr="filter-from"),  # noqa: F405
                Input(  # noqa: F405
                    type="date",
                    name="from",
                    id="filter-from",
                    value=filters.get("from", ""),
                ),
                cls="filter-field",
            ),
            Div(  # noqa: F405
                Label("Lõppkuupäev", fr="filter-to"),  # noqa: F405
                Input(  # noqa: F405
                    type="date",
                    name="to",
                    id="filter-to",
                    value=filters.get("to", ""),
                ),
                cls="filter-field",
            ),
            cls="filter-row",
        ),
        Div(  # noqa: F405
            Button("Filtreeri", type="submit", variant="primary", size="sm"),
            A(  # noqa: F405
                "Tühjenda",
                href="/admin/jobs",
                cls="btn btn-secondary btn-sm",
            ),
            cls="filter-actions",
        ),
        method="get",
        action="/admin/jobs",
        cls="jobs-filter-form",
    )


def _jobs_table(
    jobs: list[dict],  # type: ignore[type-arg]
    status_filter: str | None,
) -> object:
    """Render the filtered jobs table with expandable errors + actions.

    Each row exposes a Details disclosure that lazily HTMX-loads the
    detail fragment from ``/admin/jobs/{id}/detail`` on first open
    (``hx_trigger="toggle once"``). The retry button stays inline so
    admins can recover from a failure without expanding the row.
    """
    if not jobs:
        return P(  # noqa: F405
            "Töid antud filtritega ei leitud.",
            cls="muted-text",
        )

    rows_ft = []
    for job in jobs:
        error = job.get("error_message", "")
        short_error = error[:120] + "..." if len(error) > 120 else error
        finished = job.get("finished_at")
        finished_str = format_tallinn(finished) if finished else "—"
        status_value = job.get("status", "")
        status_label = _STATUS_LABEL.get(status_value, status_value)
        status_variant = _STATUS_VARIANT.get(status_value, "default")
        status_cell = Badge(status_label, variant=status_variant)

        # Error cell
        if error and len(error) > 120:
            error_cell: object = Details(  # noqa: F405
                Summary(short_error),  # noqa: F405
                Pre(error, cls="error-detail-pre"),  # noqa: F405
                cls="error-expandable",
            )
        elif error:
            error_cell = Span(short_error)  # noqa: F405
        else:
            error_cell = Span("—")  # noqa: F405

        retry_btn: object
        if status_value == "failed":
            retry_btn = Button(
                "Proovi uuesti",
                hx_post=f"/admin/jobs/{job['id']}/retry",
                hx_target="#job-monitor-content",
                hx_swap="innerHTML",
                # #852: state-mutating action — confirm like the adjacent
                # purge button. One click grants exactly one extra
                # attempt (the attempt counter is preserved).
                hx_confirm=(
                    "Kas olete kindel? Töö käivitatakse uuesti "
                    "ühe lisakatsega; katsete ajalugu säilib."
                ),
                variant="secondary",
                size="sm",
            )
        else:
            retry_btn = Span("—", cls="muted-text")  # noqa: F405

        # Job row + expand row pair. The expand row holds a Div target
        # that the disclosure fragment HTMX-swaps into on first open.
        detail_target_id = f"job-detail-{job['id']}"
        rows_ft.append(
            Tr(  # noqa: F405
                Td(str(job["id"]), data_label="ID"),  # noqa: F405
                Td(job["job_type"], data_label="Tüüp"),  # noqa: F405
                Td(status_cell, data_label="Staatus"),  # noqa: F405
                Td(error_cell, data_label="Veateade"),  # noqa: F405
                Td(  # noqa: F405
                    f"{job.get('attempts', 0)}/{job.get('max_attempts', 0)}",
                    data_label="Katseid",
                ),
                Td(finished_str, data_label="Lõpetatud"),  # noqa: F405
                Td(  # noqa: F405
                    Details(  # noqa: F405
                        Summary(  # noqa: F405
                            "Näita detaile",
                            cls="job-detail-summary",
                        ),
                        Div(  # noqa: F405
                            P(  # noqa: F405
                                "Laen...", cls="muted-text"
                            ),
                            id=detail_target_id,
                            hx_get=f"/admin/jobs/{job['id']}/detail",
                            hx_trigger="toggle once from:closest details",
                            hx_swap="innerHTML",
                        ),
                        cls="job-detail-disclosure",
                    ),
                    data_label="Detailid",
                ),
                Td(retry_btn, data_label="Toiming"),  # noqa: F405
            )
        )

    return Div(  # noqa: F405
        Table(  # noqa: F405
            Thead(  # noqa: F405
                Tr(  # noqa: F405
                    Th("ID", scope="col"),  # noqa: F405
                    Th("Tüüp", scope="col"),  # noqa: F405
                    Th("Staatus", scope="col"),  # noqa: F405
                    Th("Veateade", scope="col"),  # noqa: F405
                    Th("Katseid", scope="col"),  # noqa: F405
                    Th("Lõpetatud", scope="col"),  # noqa: F405
                    Th("Detailid", scope="col"),  # noqa: F405
                    Th("Toiming", scope="col"),  # noqa: F405
                )
            ),
            Tbody(*rows_ft),  # noqa: F405
            cls="data-table",
            role="table",
            data_status_filter=status_filter or "",
        ),
        cls="data-table-wrapper",
    )


def _render_job_detail_fragment(job: dict) -> object:  # type: ignore[type-arg]
    """Render the disclosure body for a single job row.

    Sections: payload JSON · attempts history · traceback (when failed).
    All copy is Estonian; the payload is pretty-printed JSON.
    """
    payload = job.get("payload")
    try:
        payload_pretty = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        payload_pretty = str(payload) if payload is not None else "—"

    history_items = [
        Li(f"Loodud: {format_tallinn(job['created_at'])}")  # noqa: F405
        if job.get("created_at")
        else Li("Loomise aega pole salvestatud"),  # noqa: F405
    ]
    if job.get("scheduled_for"):
        history_items.append(
            Li(f"Planeeritud: {format_tallinn(job['scheduled_for'])}")  # noqa: F405
        )
    if job.get("claimed_at"):
        claimed_by = job.get("claimed_by") or "?"
        history_items.append(
            Li(  # noqa: F405
                f"Võetud töösse: {format_tallinn(job['claimed_at'])} (töötaja: {claimed_by})"
            )
        )
    if job.get("started_at"):
        history_items.append(
            Li(f"Alustatud: {format_tallinn(job['started_at'])}")  # noqa: F405
        )
    if job.get("finished_at"):
        history_items.append(
            Li(f"Lõpetatud: {format_tallinn(job['finished_at'])}")  # noqa: F405
        )
    history_items.append(
        Li(f"Katseid: {job.get('attempts', 0)}/{job.get('max_attempts', 0)}")  # noqa: F405
    )

    sections: list = [
        H4("Päringu sisu (payload)", cls="job-detail-heading"),  # noqa: F405
        Pre(payload_pretty, cls="job-detail-payload"),  # noqa: F405
        H4("Katsete ajalugu", cls="job-detail-heading"),  # noqa: F405
        Ul(*history_items, cls="job-detail-history"),  # noqa: F405
    ]

    error = job.get("error_message", "")
    if error:
        sections.extend(
            [
                H4("Veateade / stacktrace", cls="job-detail-heading"),  # noqa: F405
                Pre(error, cls="job-detail-traceback"),  # noqa: F405
            ]
        )

    return Div(*sections, cls="job-detail-fragment")  # noqa: F405


def _job_monitor_content(req: Request | None = None) -> object:
    """Render the full job monitor content (swappable via HTMX).

    ``req`` is optional so the legacy zero-arg callers (the retry and
    purge handlers, plus old unit tests) keep working — when missing
    we render an unfiltered first page.
    """
    counts = _get_status_counts()
    breakdown = _get_type_breakdown()
    handler_stats = _get_handler_stats_24h()

    # Filter + pagination state
    filters: dict = {"handlers": [], "status": "", "from": "", "to": ""}
    page = 1
    if req is not None:
        filters = _extract_filters(req)
        page_str = req.query_params.get("page", "1")
        try:
            page = max(1, int(page_str))
        except ValueError:
            page = 1

    status_value = _normalise_status(filters.get("status", "") or None)
    date_from = _parse_date(filters.get("from", "") or None)
    date_to = _parse_date(filters.get("to", "") or None)
    handlers = filters.get("handlers", []) or None

    jobs, total = _get_filtered_jobs_page(
        page=page,
        per_page=_DEFAULT_PAGE_SIZE,
        handlers=handlers,
        status=status_value,
        date_from=date_from,
        date_to=date_to,
    )
    total_pages = max(1, (total + _DEFAULT_PAGE_SIZE - 1) // _DEFAULT_PAGE_SIZE)

    qs = _filter_query_string(filters)
    base_url = f"/admin/jobs?{qs}" if qs else "/admin/jobs"
    pagination = Pagination(
        current_page=page,
        total_pages=total_pages,
        base_url=base_url,
        page_size=_DEFAULT_PAGE_SIZE,
        total=total,
    )

    handler_options = [b["job_type"] for b in breakdown]
    filter_form = _filter_form(filters, handler_options)

    return Div(  # noqa: F405
        Card(
            CardHeader(H3("Kokkuvõte", cls="card-title")),  # noqa: F405
            CardBody(_summary_badges(counts)),
        ),
        _handler_stats_card(handler_stats),
        Card(
            CardHeader(H3("Tööde tüübi kaupa", cls="card-title")),  # noqa: F405
            CardBody(_type_breakdown_table(breakdown)),
        ),
        Card(
            CardHeader(H3("Filtrid", cls="card-title")),  # noqa: F405
            CardBody(filter_form),
        ),
        Card(
            CardHeader(
                Div(  # noqa: F405
                    H3("Tööde loend", cls="card-title"),  # noqa: F405
                    Button(
                        "Puhasta vanad tööd",
                        hx_post="/admin/jobs/purge",
                        hx_target="#job-monitor-content",
                        hx_swap="innerHTML",
                        hx_confirm=(
                            "Kas olete kindel? Kustutab õnnestunud "
                            "tööd, mis on vanemad kui 7 päeva."
                        ),
                        variant="danger",
                        size="sm",
                    ),
                    cls="card-header-row",
                ),
            ),
            CardBody(_jobs_table(jobs, status_value), pagination),
        ),
        id="job-monitor-content",
    )


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def admin_jobs_page(req: Request):
    """GET /admin/jobs -- full job monitor page.

    Helpers are imported as locals so this handler works correctly when
    rebound by the admin_dashboard shim (which swaps ``__globals__`` to
    its own module dict). On error renders a styled error banner.
    """
    auth = req.scope.get("auth")
    theme = get_theme_from_request(req)
    try:
        from app.admin.job_monitor import _job_monitor_content

        content = (
            H1("Tööde monitor", cls="page-title"),  # noqa: F405
            P(A("← Tagasi adminipaneelile", href="/admin"), cls="back-link"),  # noqa: F405
            _job_monitor_content(req),
        )

        return PageShell(
            *content,
            title="Tööde monitor",
            user=auth,
            theme=theme,
            active_nav="/admin",
        )
    except Exception:
        logger.exception("Failed to render admin jobs page")
        from app.admin._shared import _render_admin_error_page

        return _render_admin_error_page(title="Tööde monitor", user=auth, theme=theme)


def admin_job_retry(req: Request, id: int):
    """POST /admin/jobs/{id}/retry -- retry a single failed job."""
    try:
        from app.admin.job_monitor import _job_monitor_content, _retry_job

        success = _retry_job(id)
        if not success:
            msg = "Tööd ei leitud või ei ole ebaõnnestunud staatuses."
            return JSONResponse({"error": msg}, status_code=404)
        # Return refreshed content
        return _job_monitor_content(req)
    except Exception:
        logger.exception("Failed to retry admin job id=%s", id)
        return JSONResponse(
            {"error": "Töö taaskäivitamine ebaõnnestus."},
            status_code=500,
        )


def admin_jobs_purge(req: Request):
    """POST /admin/jobs/purge -- delete completed jobs older than 7 days."""
    try:
        from app.admin.job_monitor import _job_monitor_content, _purge_completed

        deleted = _purge_completed(days=7)
        logger.info("Purged %d completed jobs older than 7 days", deleted)
        # Return refreshed content
        return _job_monitor_content(req)
    except Exception:
        logger.exception("Failed to purge completed jobs")
        return JSONResponse(
            {"error": "Tööde puhastamine ebaõnnestus."},
            status_code=500,
        )


def admin_job_detail(req: Request, id: int):
    """GET /admin/jobs/{id}/detail -- HTMX-loaded detail fragment.

    Returns a Div with the payload JSON (pretty-printed), attempts
    history, and traceback (if failed). When the row is missing or
    the DB fails, returns a small in-place error fragment so the
    disclosure doesn't appear empty.
    """
    try:
        from app.admin.job_monitor import _get_job_detail, _render_job_detail_fragment

        job = _get_job_detail(id)
        if job is None:
            return Div(  # noqa: F405
                P(  # noqa: F405
                    "Tööd ei leitud.",
                    cls="muted-text",
                ),
                cls="job-detail-fragment",
            )
        return _render_job_detail_fragment(job)
    except Exception:
        logger.exception("Failed to render admin job detail id=%s", id)
        return Div(  # noqa: F405
            P(  # noqa: F405
                "Detailide laadimine ebaõnnestus.",
                cls="muted-text",
            ),
            cls="job-detail-fragment",
        )
