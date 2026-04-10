"""Admin sync status card, sync trigger handler, and sync log helpers."""

from __future__ import annotations

import logging
import threading

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request

from app.admin._shared import _tooltip
from app.db import get_connection as _connect
from app.ui.data.data_table import Column, DataTable
from app.ui.forms.app_form import AppForm
from app.ui.primitives.badge import StatusBadge
from app.ui.primitives.button import Button  # noqa: F401, F811  -- shadow guard
from app.ui.surfaces.card import Card, CardBody, CardHeader

# Module-level lock so two admins clicking "Sync now" at the same time
# don't trigger two parallel clones.
_sync_lock = threading.Lock()
_sync_in_progress = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

_SYNC_STATUS_MAP = {
    "running": ("running", "Käimas"),
    "success": ("ok", "Õnnestus"),
    "failed": ("failed", "Ebaõnnestus"),
}


def _get_sync_logs(limit: int = 5) -> list[dict]:  # type: ignore[type-arg]
    """Return the most recent sync_log entries."""
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT id, started_at, finished_at, status, entity_count, error_message "
                "FROM sync_log ORDER BY started_at DESC LIMIT %s",
                (limit,),
            ).fetchall()
        return [
            {
                "id": r[0],
                "started_at": r[1],
                "finished_at": r[2],
                "status": r[3],
                "entity_count": r[4],
                "error_message": r[5],
            }
            for r in rows
        ]
    except Exception:
        logger.exception("Failed to fetch sync logs")
        return []


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _sync_status_badge(status: str):
    """Return a StatusBadge for a sync_log status value."""
    key, _ = _SYNC_STATUS_MAP.get(status, ("pending", status))
    return StatusBadge(key)  # type: ignore[arg-type]


def _sync_trigger_form():
    """Render the 'Sync now' button as an HTMX form.

    Posts to /admin/sync. The endpoint swaps this same card in-place
    with a confirmation message so the admin gets immediate feedback
    without a full-page reload.
    """
    return AppForm(
        Button(
            "Sünkroniseeri kohe",
            type="submit",
            variant="primary",
            size="sm",
            cls="sync-trigger-btn",
        ),
        method="post",
        action="/admin/sync",
        hx_post="/admin/sync",
        hx_target="#sync-card",
        hx_swap="outerHTML",
        cls="sync-trigger-form",
    )


def _sync_card(sync_logs: list[dict], *, status_banner: tuple[str, str] | None = None):  # type: ignore[type-arg]
    """Render the sync status card.

    Args:
        sync_logs: recent sync_log rows from the DB
        status_banner: optional (variant, message) tuple shown above the
            log table — used by POST /admin/sync to surface 'queued' /
            'already running' feedback.
    """
    if not sync_logs:
        body = P("Sünkroniseerimisi ei leitud.", cls="muted-text")  # noqa: F405
    else:
        columns = [
            Column(key="started", label="Algusaeg", sortable=False),
            Column(
                key="status",
                label="Staatus",
                sortable=False,
                render=lambda r: _sync_status_badge(r["status_raw"]),
            ),
            Column(key="entity_count", label="Olemeid", sortable=False),
            Column(key="error_message", label="Veateade", sortable=False),
        ]
        rows = []
        for entry in sync_logs:
            started = entry["started_at"]
            rows.append(
                {
                    "started": started.strftime("%d.%m.%Y %H:%M") if started else "—",
                    "status_raw": entry["status"],
                    "status": entry["status"],
                    "entity_count": (
                        str(entry["entity_count"]) if entry["entity_count"] is not None else "—"
                    ),
                    "error_message": entry["error_message"] or "—",
                }
            )
        body = DataTable(columns=columns, rows=rows)

    body_nodes: list = []
    if status_banner is not None:
        variant, message = status_banner
        body_nodes.append(Div(message, cls=f"sync-banner sync-banner-{variant}", role="status"))  # noqa: F405
    body_nodes.append(body)
    body_nodes.append(_sync_trigger_form())

    return Card(
        CardHeader(
            H3(  # noqa: F405
                "S\u00fcnkroniseerimise staatus",
                _tooltip("GitHub \u2192 RDF \u2192 Jena s\u00fcnkroniseerimise ajalugu"),
                cls="card-title",
            )
        ),
        CardBody(*body_nodes),
        id="sync-card",
    )


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def _run_sync_and_clear_flag():
    """Background wrapper: runs the sync pipeline and clears the in-progress flag."""
    global _sync_in_progress
    try:
        # Imported here to avoid circular dependency on app.templates during
        # module load, and to ensure the sync uses the runtime env vars.
        from app.sync.orchestrator import run_sync

        run_sync()
    except Exception:
        logger.exception("Admin-triggered sync raised an unhandled exception")
    finally:
        with _sync_lock:
            _sync_in_progress = False


def trigger_sync(req: Request):
    """POST /admin/sync — admin-only sync trigger.

    Runs the ontology sync pipeline in a background thread so the request
    returns immediately. Re-renders the sync card with a status banner so
    an HTMX-capable client gets inline feedback; a plain form submit sees
    the same card on the next full page load via `/admin`.
    """
    global _sync_in_progress

    already_running = False
    with _sync_lock:
        if _sync_in_progress:
            already_running = True
        else:
            _sync_in_progress = True

    if already_running:
        banner = ("warning", "Sünkroniseerimine on juba käimas.")
    else:
        thread = threading.Thread(target=_run_sync_and_clear_flag, daemon=True)
        thread.start()
        banner = ("info", "Sünkroniseerimine käivitati — vaata tulemust allpool olevast logist.")

    sync_logs = _get_sync_logs()
    return _sync_card(sync_logs, status_banner=banner)
