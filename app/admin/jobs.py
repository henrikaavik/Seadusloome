"""Admin job queue snapshot and card rendering."""

from __future__ import annotations

import logging

from fasthtml.common import *  # noqa: F403

from app.admin._shared import _tooltip
from app.jobs.queue import Job, JobQueue
from app.ui.data.data_table import Column, DataTable
from app.ui.primitives.badge import Badge
from app.ui.primitives.button import Button  # noqa: F401, F811  -- shadow guard
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.time import format_tallinn

logger = logging.getLogger(__name__)


def _get_job_queue_snapshot() -> dict:  # type: ignore[type-arg]
    """Fetch recent jobs grouped by status for the admin card.

    Swallows DB errors so the rest of the dashboard still renders if
    Postgres is temporarily unreachable — the health card above this
    one will already have flagged the outage.
    """
    snapshot: dict = {  # type: ignore[type-arg]
        "pending": [],
        "running": [],
        "failed": [],
        "retrying": [],
    }
    try:
        queue = JobQueue()
        snapshot["pending"] = queue.list_by_status("pending", limit=5)
        snapshot["running"] = queue.list_by_status("running", limit=5)
        snapshot["failed"] = queue.list_by_status("failed", limit=5)
        # #455: surface ``retrying`` for operational visibility. After
        # the #441 fix the queue no longer parks jobs there, but the
        # CHECK constraint still allows it and we want any drift to
        # show up loudly on the dashboard rather than silently.
        snapshot["retrying"] = queue.list_by_status("retrying", limit=5)
    except Exception:
        logger.exception("Failed to fetch job queue snapshot")
    return snapshot


def _job_queue_card():
    """Render the background job queue status card.

    Shows counts of pending/running/failed/retrying jobs at a glance,
    plus a table of the most recent failures so an admin can spot
    broken pipelines without needing to SSH into Postgres.
    """
    snapshot = _get_job_queue_snapshot()
    pending: list[Job] = snapshot["pending"]
    running: list[Job] = snapshot["running"]
    failed: list[Job] = snapshot["failed"]
    retrying: list[Job] = snapshot["retrying"]

    has_any = bool(pending or running or failed or retrying)

    if not has_any:
        body: object = P("Taustajobisid pole.", cls="muted-text")  # noqa: F405
        return Card(
            CardHeader(
                H3(  # noqa: F405
                    "Taustajobide j\u00e4rjekord",
                    _tooltip("Parse, anal\u00fc\u00fcs ja ekspordi t\u00f6\u00f6d"),
                    cls="card-title",
                )
            ),
            CardBody(body),
            id="job-queue-card",
        )

    # #477: the retrying badge should only render when there's
    # actually something to retry. After the #441 fix this should
    # normally be 0; keeping a ``0 kordab`` badge permanently in the
    # UI made it look like a live metric rather than a drift guard.
    # The underlying query still runs so drift still surfaces loudly
    # the moment a row ends up in ``retrying``.
    summary_children: list = [
        Badge(f"{len(pending)} ootel", variant="default"),
        " ",
        Badge(f"{len(running)} töötab", variant="primary"),
        " ",
    ]
    if len(retrying) > 0:
        summary_children.append(Badge(f"{len(retrying)} kordab", variant="warning"))
        summary_children.append(" ")
    summary_children.append(Badge(f"{len(failed)} ebaõnnestus", variant="danger"))
    summary = Div(*summary_children, cls="job-queue-summary")  # noqa: F405

    body_children: list = [summary]

    if failed:
        columns = [
            Column(key="job_type", label="Tüüp", sortable=False),
            Column(key="error_message", label="Viga", sortable=False),
            Column(key="attempts", label="Katseid", sortable=False),
            Column(key="finished_at", label="Lõpetatud", sortable=False),
        ]
        rows = []
        for job in failed:
            error_raw = job.error_message or "—"
            # Truncate long error messages for readability in the card.
            if len(error_raw) > 120:
                error_raw = error_raw[:117] + "..."
            finished = job.finished_at
            rows.append(
                {
                    "job_type": job.job_type,
                    "error_message": error_raw,
                    "attempts": f"{job.attempts}/{job.max_attempts}",
                    "finished_at": format_tallinn(finished),
                }
            )
        body_children.append(H4("Viimased ebaõnnestunud jobid", cls="section-subtitle"))  # noqa: F405
        body_children.append(DataTable(columns=columns, rows=rows))

    return Card(
        CardHeader(
            H3(  # noqa: F405
                "Taustajobide j\u00e4rjekord",
                _tooltip("Parse, anal\u00fc\u00fcs ja ekspordi t\u00f6\u00f6d"),
                cls="card-title",
            )
        ),
        CardBody(*body_children),
        id="job-queue-card",
    )
