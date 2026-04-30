"""Admin sync status card, sync trigger handler, and sync log helpers."""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request

from app.admin._shared import _tooltip
from app.db import get_connection as _connect
from app.sync.orchestrator import (
    PHASE_CLONING,
    PHASE_CONVERTING,
    PHASE_REINGESTING,
    PHASE_UPLOADING,
    PHASE_VALIDATING,
    _insert_running_row,
    has_recent_running_row,
)
from app.ui.data.data_table import Column, DataTable
from app.ui.forms.app_form import AppForm
from app.ui.primitives.badge import StatusBadge
from app.ui.primitives.button import Button  # noqa: F401, F811  -- shadow guard
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.time import format_tallinn

# Module-level lock — keeps rapid double-clicks on the "Sync now" button
# from both spawning a thread before the DB's running row becomes visible.
# The authoritative lock is now at the DB layer (sync_log.status='running'),
# but this in-memory guard closes the race window for admins on the same
# worker process.
_sync_lock = threading.Lock()
_sync_in_progress = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Phase metadata
# ---------------------------------------------------------------------------

# Ordered list of pipeline phases with their Estonian display labels. Order
# matters: the UI uses the index to decide which pills are done / active /
# pending. Keep in sync with app/sync/orchestrator.py phase constants.
_PROGRESS_PHASES: list[tuple[str, str]] = [
    (PHASE_CLONING, "Kloonimine"),
    (PHASE_CONVERTING, "Konverteerimine"),
    (PHASE_VALIDATING, "Valideerimine"),
    (PHASE_UPLOADING, "\u00dcleslaadimine"),
    (PHASE_REINGESTING, "Taasindekseerimine"),
]

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

_SYNC_STATUS_MAP = {
    "running": ("running", "K\u00e4imas"),
    "success": ("ok", "\u00d5nnestus"),
    "failed": ("failed", "Eba\u00f5nnestus"),
}


def _get_sync_logs(limit: int = 5) -> list[dict]:  # type: ignore[type-arg]
    """Return the most recent sync_log entries."""
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT id, started_at, finished_at, status, entity_count, "
                "error_message, current_step "
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
                "current_step": r[6] if len(r) > 6 else None,
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


# Threshold above which we collapse the full error_message into a
# ``<details>`` disclosure.  SHACL validation reports can run to a few KB,
# which dominates the admin sync table when shown inline.
_SYNC_ERROR_INLINE_LIMIT = 80


def _sync_error_cell(row: dict):  # type: ignore[type-arg]
    """Render the ``Veateade`` cell in the sync log table.

    Short messages render as plain text; long messages (e.g. SHACL warning
    blocks) are truncated to the first 80 chars with a ``<details>``
    disclosure that exposes the full text in a scrollable preformatted
    block.  The em-dash placeholder for an empty message renders as-is.
    """
    msg = row.get("error_message") or "—"
    if msg == "—":
        return msg
    if len(msg) <= _SYNC_ERROR_INLINE_LIMIT:
        return msg
    return Details(  # noqa: F405
        Summary(msg[:_SYNC_ERROR_INLINE_LIMIT] + "…"),  # noqa: F405
        Pre(msg, cls="sync-error-full"),  # noqa: F405
        cls="sync-error",
    )


def _sync_trigger_form():
    """Render the 'Sync now' button as an HTMX form.

    Posts to /admin/sync. The endpoint swaps this same card in-place
    with a confirmation message so the admin gets immediate feedback
    without a full-page reload.
    """
    return AppForm(
        Button(
            "S\u00fcnkroniseeri kohe",
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


def _elapsed_seconds(started_at: datetime | None) -> int:
    """Return whole seconds since ``started_at``. 0 if unknown."""
    if started_at is None:
        return 0
    # Postgres may hand back a naive datetime depending on driver config;
    # treat naive as UTC since that's what _insert_running_row writes.
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - started_at
    return max(0, int(delta.total_seconds()))


def _format_elapsed(seconds: int) -> str:
    """Format as ``M:SS`` for display next to the running badge."""
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}:{secs:02d}"


def _progress_pills(current_step: str | None):
    """Render the 5-phase progress indicator.

    Pills before the current step are marked ``done``; the current step is
    ``active``; later steps are ``pending``. If ``current_step`` is not in
    the known list (legacy rows), every pill renders as pending so the
    admin still sees the skeleton.
    """
    current_index = -1
    for i, (key, _) in enumerate(_PROGRESS_PHASES):
        if key == current_step:
            current_index = i
            break

    items = []
    for i, (_key, label) in enumerate(_PROGRESS_PHASES):
        if current_index == -1:
            state = "pending"
        elif i < current_index:
            state = "done"
        elif i == current_index:
            state = "active"
        else:
            state = "pending"

        # done phases get a check glyph; active uses a pulsing dot;
        # pending shows the plain index. This reads well at a glance
        # even if text labels wrap on small screens.
        if state == "done":
            marker = Span("\u2713", cls="sync-step-marker", aria_hidden="true")  # noqa: F405
        elif state == "active":
            marker = Span(
                Span(cls="sync-step-spinner", aria_hidden="true"),  # noqa: F405
                cls="sync-step-marker",
                aria_hidden="true",
            )
        else:
            marker = Span(
                str(i + 1),
                cls="sync-step-marker",
                aria_hidden="true",
            )

        items.append(
            Li(  # noqa: F405
                marker,
                Span(label, cls="sync-step-label"),  # noqa: F405
                cls=f"sync-step sync-step-{state}",
                data_phase=_PROGRESS_PHASES[i][0],
                aria_current="step" if state == "active" else None,
            )
        )
    return Ol(  # noqa: F405
        *items,
        cls="sync-steps",
        role="list",
        aria_label="S\u00fcnkroniseerimise edenemine",
    )


def _running_panel(entry: dict):  # type: ignore[type-arg]
    """Render the live progress panel shown while a sync is running."""
    started_at = entry.get("started_at")
    elapsed = _elapsed_seconds(started_at)
    current_step = entry.get("current_step") or PHASE_CLONING
    # Map current phase to a friendly caption shown under the header.
    phase_caption = dict(_PROGRESS_PHASES).get(current_step, "Alustamine")
    return Div(  # noqa: F405
        Div(  # noqa: F405
            Div(  # noqa: F405
                Span(cls="sync-live-indicator", aria_hidden="true"),  # noqa: F405
                Span("S\u00fcnkroniseerimine k\u00e4ib", cls="sync-live-title"),  # noqa: F405
                cls="sync-live-title-row",
            ),
            Span(
                _format_elapsed(elapsed),
                cls="sync-elapsed",
                data_testid="sync-elapsed",
                aria_label=f"Kestnud {_format_elapsed(elapsed)}",
            ),
            cls="sync-running-header",
        ),
        Div(  # noqa: F405
            f"Praegu: {phase_caption}",
            cls="sync-phase-caption",
        ),
        _progress_pills(current_step),
        cls="sync-running-panel",
    )


def _sync_card(
    sync_logs: list[dict],  # type: ignore[type-arg]
    *,
    status_banner: tuple[str, str] | None = None,
):
    """Render the sync status card.

    Args:
        sync_logs: recent sync_log rows from the DB (newest first).
        status_banner: optional (variant, message) tuple shown above the
            log table — used by POST /admin/sync to surface 'queued' /
            'already running' feedback.

    Auto-polling: when the newest entry has status='running', the
    returned card carries ``hx-get="/admin/sync/status"`` with an every-3s
    trigger so it re-renders itself until the sync reaches a terminal
    state. When status is terminal the polling attributes are absent, so
    HTMX stops polling automatically.
    """
    is_running = bool(sync_logs) and sync_logs[0].get("status") == "running"

    # Defensive: if the caller just triggered a sync (banner variant='info')
    # but our query hasn't seen the running row yet (possible on slow DB
    # round-trips even with synchronous insert), treat the card as running
    # so the response always carries the polling trigger. The first poll
    # (3s later) will pick up the real row and render the progress panel
    # correctly. Without this fallback the card would render with no
    # polling and never update until the admin manually refreshed.
    just_triggered = status_banner is not None and status_banner[0] == "info"
    poll_for_updates = is_running or just_triggered

    body_nodes: list = []

    if status_banner is not None:
        variant, message = status_banner
        body_nodes.append(
            Div(message, cls=f"sync-banner sync-banner-{variant}", role="status")  # noqa: F405
        )

    if is_running:
        body_nodes.append(_running_panel(sync_logs[0]))
    elif just_triggered:
        # Synthesize a minimal running panel from the trigger timestamp
        # so the admin sees the progress skeleton immediately. First
        # poll will swap in the real one.
        from datetime import UTC as _UTC
        from datetime import datetime as _dt

        body_nodes.append(
            _running_panel(
                {
                    "started_at": _dt.now(_UTC),
                    "current_step": PHASE_CLONING,
                }
            )
        )

    # Historical log table — always shown (gives context even while
    # the live panel is up).
    log_rows = sync_logs[1:] if is_running else sync_logs
    if not log_rows:
        body_nodes.append(
            P("S\u00fcnkroniseerimisi ei leitud.", cls="muted-text")  # noqa: F405
        )
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
            Column(
                key="error_message",
                label="Veateade",
                sortable=False,
                render=_sync_error_cell,
            ),
        ]
        rows = []
        for entry in log_rows:
            started = entry["started_at"]
            rows.append(
                {
                    "started": format_tallinn(started),
                    "status_raw": entry["status"],
                    "status": entry["status"],
                    "entity_count": (
                        str(entry["entity_count"])
                        if entry["entity_count"] is not None
                        else "\u2014"
                    ),
                    "error_message": entry["error_message"] or "\u2014",
                }
            )
        body_nodes.append(DataTable(columns=columns, rows=rows))

    body_nodes.append(_sync_trigger_form())

    card_kwargs: dict = {"id": "sync-card"}
    if poll_for_updates:
        # Poll ourselves every 3s while the sync is running. HTMX swaps
        # this same element outerHTML with the next render — when the
        # sync finishes the new response omits these attrs and polling
        # stops.
        card_kwargs.update(
            {
                "hx_get": "/admin/sync/status",
                "hx_trigger": "every 3s",
                "hx_swap": "outerHTML",
            }
        )

    return Card(
        CardHeader(
            H3(  # noqa: F405
                "S\u00fcnkroniseerimise staatus",
                _tooltip("GitHub \u2192 RDF \u2192 Jena s\u00fcnkroniseerimise ajalugu"),
                cls="card-title",
            )
        ),
        CardBody(*body_nodes),
        **card_kwargs,
    )


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def _run_sync_and_clear_flag(log_id: int | None = None, started_at: datetime | None = None):
    """Background wrapper: runs the sync pipeline and clears the in-progress flag.

    The admin POST handler inserts the ``running`` sync_log row
    synchronously (to close the UI race where the response would
    otherwise be rendered before the thread's INSERT landed) and
    forwards the row id here. The orchestrator reuses that row instead
    of creating a duplicate.
    """
    global _sync_in_progress
    try:
        # Imported here to avoid circular dependency on app.templates during
        # module load, and to ensure the sync uses the runtime env vars.
        from app.sync.orchestrator import run_sync

        run_sync(log_id=log_id, started_at=started_at)
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

    The ``running`` sync_log row is inserted synchronously BEFORE the
    background thread starts. Without this the main request thread
    would query sync_log and render the card before the worker had a
    chance to INSERT, leaving the response with no running panel and
    no HTMX polling trigger — which is what produced the "banner only,
    no progress" report in production.
    """
    global _sync_in_progress

    already_running_memory = False
    with _sync_lock:
        if _sync_in_progress:
            already_running_memory = True
        else:
            _sync_in_progress = True

    # DB-level check catches syncs kicked off by the webhook or another
    # worker process where the in-memory flag wouldn't see them.
    already_running_db = False
    if not already_running_memory:
        already_running_db = has_recent_running_row()
        if already_running_db:
            # Release the flag we just set — another sync owns the slot.
            with _sync_lock:
                _sync_in_progress = False

    if already_running_memory or already_running_db:
        banner = ("warning", "S\u00fcnkroniseerimine on juba k\u00e4imas.")
    else:
        # Synchronous running-row INSERT: the card we return must already
        # reflect the in-flight sync or HTMX won't start polling.
        started_at = datetime.now(UTC)
        log_id = _insert_running_row(started_at, PHASE_CLONING)

        thread = threading.Thread(
            target=_run_sync_and_clear_flag,
            kwargs={"log_id": log_id, "started_at": started_at},
            daemon=True,
        )
        thread.start()
        banner = (
            "info",
            "S\u00fcnkroniseerimine k\u00e4ivitati \u2014 edenemist n\u00e4eb allpool reaalajas.",
        )

    sync_logs = _get_sync_logs()
    return _sync_card(sync_logs, status_banner=banner)


def sync_status_card(req: Request):
    """GET /admin/sync/status — re-render the sync card for HTMX polling.

    Used by the running-state card's ``hx-get`` trigger. Returns the
    same fragment as the POST handler but without a status banner (no
    fresh admin action to announce).
    """
    sync_logs = _get_sync_logs()
    return _sync_card(sync_logs)
