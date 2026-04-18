"""FastHTML routes for the Phase 2 Document Upload module.

Route map:

    GET  /drafts                     — list the caller's org's drafts
    GET  /drafts/new                 — upload form
    POST /drafts                     — multipart upload handler
    GET  /drafts/{draft_id}          — draft detail page with status tracker
    GET  /drafts/{draft_id}/status   — HTMX polling fragment (status only)
    POST /drafts/{draft_id}/delete   — delete draft + encrypted file

All routes require authentication (they are **not** in ``SKIP_PATHS``).
The listing and detail pages additionally enforce ``draft.org_id ==
user.org_id`` for every returned record. Single-draft lookups that fail
that check return a 404 rather than a 403 so we never leak the fact
that a draft from another org exists.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

from fasthtml.common import *  # noqa: F403
from fasthtml.common import to_xml
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from app.auth.audit import log_action
from app.auth.helpers import require_auth as _require_auth
from app.auth.policy import can_delete_draft, can_edit_draft, can_view_draft
from app.auth.users import list_users
from app.db import get_connection as _connect
from app.docs._helpers import _not_found_page, _parse_uuid
from app.docs.audit import (
    log_draft_delete,
    log_draft_upload,
    log_draft_view,
)
from app.docs.draft_model import (
    DEFAULT_SORT,
    Draft,
    delete_draft,
    fetch_draft,
    list_drafts_for_org_filtered,
    list_eelnous_for_vtk,
    list_vtks_for_org,
    touch_draft_access,
    touch_draft_access_conn,
    update_draft_parent_vtk,
)
from app.docs.graph_builder import write_doc_lineage
from app.docs.upload import DraftUploadError, handle_upload
from app.jobs.queue import JobQueue
from app.rag.retriever import delete_chunks_for_draft
from app.ui.data.data_table import Column, DataTable
from app.ui.data.pagination import Pagination
from app.ui.feedback.empty_state import EmptyState
from app.ui.feedback.flash import push_flash
from app.ui.layout import PageShell
from app.ui.primitives.annotation_button import AnnotationButton
from app.ui.primitives.badge import Badge, BadgeVariant
from app.ui.primitives.button import Button
from app.ui.surfaces.alert import Alert
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.surfaces.info_box import InfoBox
from app.ui.surfaces.modal import ConfirmModal, Modal, ModalBody, ModalFooter, ModalScript
from app.ui.theme import get_theme_from_request
from app.ui.time import format_tallinn

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Status display helpers
# ---------------------------------------------------------------------------

# Public pipeline stages in order. "failed" is a terminal branch rendered
# separately so the tracker reads left-to-right during normal operation.
_STATUS_STAGES: tuple[tuple[str, str], ...] = (
    ("uploaded", "Üles laaditud"),
    ("parsing", "Töötlemine"),
    ("extracting", "Olemite eraldamine"),
    ("analyzing", "Mõjude analüüs"),
    ("ready", "Valmis"),
)

_STATUS_LABELS: dict[str, str] = dict(_STATUS_STAGES)
_STATUS_LABELS["failed"] = "Ebaõnnestus"

_TERMINAL_STATUSES: frozenset[str] = frozenset({"ready", "failed"})

_PAGE_SIZE = 25

_DELETE_CONFIRM = (
    "Kas olete kindel, et soovite selle eelnõu kustutada? Seda tegevust ei saa tagasi võtta."
)

# #572: drafts whose ``last_accessed_at`` is older than this are stale
# and the UI surfaces a "Hoia alles" (Keep) button alongside the delete
# button. The same threshold is used by the archive-warning scan job.
_STALE_THRESHOLD_DAYS = 90


def _is_draft_stale(draft: Draft) -> bool:
    """Return True when the draft has not been accessed for 90+ days (#572)."""
    last = getattr(draft, "last_accessed_at", None)
    if last is None:
        return False
    try:
        elapsed = (datetime.now(UTC) - last).total_seconds()
    except (TypeError, ValueError):
        return False
    return elapsed > _STALE_THRESHOLD_DAYS * 24 * 60 * 60


_STATUS_KEY_MAP: dict[str, str] = {
    "uploaded": "pending",
    "parsing": "running",
    "extracting": "running",
    "analyzing": "running",
    "ready": "ok",
    "failed": "failed",
}

_STATUS_VARIANT_MAP: dict[str, BadgeVariant] = {
    "uploaded": "default",
    "parsing": "primary",
    "extracting": "primary",
    "analyzing": "primary",
    "ready": "success",
    "failed": "danger",
}


def _status_badge(status: str):
    """Return a Badge for a draft status.

    We use plain ``Badge`` instead of ``StatusBadge`` because the latter
    ships its own English-ish label set and our domain statuses
    (uploaded/parsing/extracting/analyzing) need Estonian copy.
    """
    key = _STATUS_KEY_MAP.get(status, "pending")
    variant: BadgeVariant = _STATUS_VARIANT_MAP.get(status, "default")
    label = _STATUS_LABELS.get(status, status)
    return Badge(label, variant=variant, cls=f"draft-status draft-status-{key}")


def _format_timestamp(value: Any) -> str:
    """Render a ``datetime`` in Europe/Tallinn (see app.ui.time)."""
    return format_tallinn(value)


# #457: stop polling after this many seconds since the draft was
# created. Without an upper bound the page hammers /status forever
# whenever a worker hangs (or the queue is paused), and the user has
# no actionable signal.
_POLLING_TIMEOUT_SECONDS = 300


def _is_status_polling_stale(draft: Draft) -> bool:
    """Return True if we should stop polling and surface a warning.

    #470: we use ``updated_at`` (bumped by every handler on each
    pipeline transition) rather than ``created_at``. A long-running
    draft whose pipeline is still making progress will keep bumping
    ``updated_at``, so the polling budget resets on each transition.
    A pipeline that's genuinely hung leaves ``updated_at`` frozen, and
    the polling window elapses against that frozen timestamp. If
    ``updated_at`` is missing for any reason (older rows, DB race),
    fall back to ``created_at`` so we still honour the timeout.
    """
    reference = draft.updated_at or draft.created_at
    if reference is None:
        return False
    try:
        elapsed = (datetime.now(UTC) - reference).total_seconds()
    except (TypeError, ValueError):
        return False
    return elapsed > _POLLING_TIMEOUT_SECONDS


# #606/#607: typical wall-clock range per pipeline stage, in seconds.
# Hard-coded for now — a real histogram from background_jobs.finished_at
# minus claimed_at can replace this later. Keep keys in sync with
# _STATUS_STAGES (and note that ``uploaded`` + ``ready`` are excluded
# because they're not running stages).
_TYPICAL_STAGE_SECONDS: dict[str, tuple[int, int]] = {
    "parsing": (10, 60),
    "extracting": (60, 240),
    "analyzing": (30, 180),
}


def _poll_interval_seconds(draft: Draft) -> int:
    """Return the HTMX poll interval in seconds with exponential-ish backoff.

    0-30s since creation → 3s, 30-120s → 6s, 120s+ → 10s. See #607.
    """
    reference = draft.created_at
    if reference is None:
        return 3
    try:
        elapsed = (datetime.now(UTC) - reference).total_seconds()
    except (TypeError, ValueError):
        return 3
    if elapsed < 30:
        return 3
    if elapsed < 120:
        return 6
    return 10


def _elapsed_seconds(draft: Draft) -> int | None:
    """Seconds between ``draft.updated_at`` and now (for #606 timer)."""
    reference = draft.updated_at or draft.created_at
    if reference is None:
        return None
    try:
        return max(0, int((datetime.now(UTC) - reference).total_seconds()))
    except (TypeError, ValueError):
        return None


def _processing_duration_seconds(draft: Draft) -> int | None:
    """Total wall-clock processing duration for a terminal draft (#657, #670).

    Prefers ``processing_completed_at - created_at`` — ``processing_completed_at``
    is frozen at the moment the pipeline flips into ``ready`` / ``failed``
    (migration 023), so later edits (rename, VTK link, re-tag) no longer
    inflate the label on an already-finished draft.

    Falls back to ``updated_at - created_at`` for legacy rows where the
    backfill left ``processing_completed_at`` NULL, so the label keeps
    rendering for drafts that finished before migration 023 ran.

    Returns ``None`` when neither timestamp pair is available.
    """
    if draft.created_at is None:
        return None
    completion = draft.processing_completed_at or draft.updated_at
    if completion is None:
        return None
    try:
        return max(0, int((completion - draft.created_at).total_seconds()))
    except (TypeError, ValueError):
        return None


def _format_elapsed(seconds: int) -> str:
    """Render seconds as ``M:SS möödas`` / ``H:MM:SS möödas``.

    #657: the original ``M:SS`` output wrapped past 60 minutes into
    three-digit minute counts like "8835:14" for drafts whose pipeline
    had been running (or in the UI bug's case, appeared to be running)
    for hours. The ticker now switches to ``H:MM:SS`` once the raw
    seconds value clears the one-hour mark so the label stays legible
    for genuinely long pipelines.
    """
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d} möödas"
    return f"{minutes}:{secs:02d} möödas"


def _format_elapsed_final(seconds: int) -> str:
    """Render a FROZEN elapsed label for terminal drafts (#657).

    Used when the draft is in ``ready`` or ``failed`` — the label is
    computed once server-side and NOT wrapped in the
    ``.draft-stage-elapsed`` class so the client-side ticker skips
    over it. Format mirrors ``_format_elapsed`` for consistency but
    swaps the "möödas" suffix for "Analüüsitud" prefix so the user
    reads the label as a completion marker rather than a running
    timer.
    """
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"Analüüsitud {hours} h {minutes} min {secs} s"
    if minutes > 0:
        return f"Analüüsitud {minutes} min {secs} s"
    return f"Analüüsitud {secs} s"


def _status_tracker(draft: Draft):
    """Render the 6-stage horizontal status tracker.

    Wrapped in a polling Div so HTMX can refresh it every 3 seconds
    until the draft reaches a terminal state OR the polling timeout
    elapses (#457). After the timeout we drop the polling attributes
    and surface a yellow alert nudging the user to check the admin
    dashboard so they don't sit on the page forever.
    """
    items: list = []
    current_index = -1
    for idx, (key, _) in enumerate(_STATUS_STAGES):
        if key == draft.status:
            current_index = idx
            break

    elapsed = _elapsed_seconds(draft)

    for idx, (key, label) in enumerate(_STATUS_STAGES):
        classes = ["draft-stage"]
        is_active = False
        if draft.status == "failed":
            # On failure every stage past the last successful one is dim.
            classes.append("draft-stage-idle")
        elif current_index >= 0 and idx < current_index:
            classes.append("draft-stage-done")
        elif current_index >= 0 and idx == current_index:
            classes.append("draft-stage-active")
            is_active = True
        else:
            classes.append("draft-stage-idle")

        # #606: render elapsed time + typical range under the active
        # stage. The elapsed value is bumped client-side by a small
        # inline interval so the user sees a live ticker without any
        # extra HTMX polls.
        # #657: the live ticker is ONLY attached when the draft is in
        # a non-terminal state. For ``ready`` we render a frozen
        # "Analüüsitud N min" label without the ``.draft-stage-elapsed``
        # class so the client-side tick loop skips it. ``failed`` falls
        # through to the idle branch above — no ticker is ever attached
        # to a failed draft because no stage is marked active.
        extras: list = []
        if is_active and elapsed is not None and draft.status not in _TERMINAL_STATUSES:
            extras.append(
                Span(  # noqa: F405
                    _format_elapsed(elapsed),
                    cls="draft-stage-elapsed",
                    data_elapsed_seconds=str(elapsed),
                )
            )
            typical = _TYPICAL_STAGE_SECONDS.get(key)
            if typical is not None:
                low, high = typical
                low_min, high_min = low // 60 or 1, high // 60 or 1
                # Prefer a "N-M min" display; fall back to seconds when
                # the lower bound is sub-minute (e.g. parsing 10s-60s).
                if low < 60:
                    range_text = f"tüüpiline aeg {low}s-{high // 60 or 1} min"
                else:
                    range_text = f"tüüpiline aeg {low_min}-{high_min} min"
                extras.append(
                    Span(  # noqa: F405
                        range_text,
                        cls="draft-stage-typical muted-text",
                    )
                )
        elif is_active and draft.status == "ready":
            # #657: render a frozen "Analüüsitud" label on the "Valmis"
            # stage. Deliberately NOT ``.draft-stage-elapsed`` so the
            # client-side ticker leaves it alone. The displayed
            # duration is the total pipeline time (upload -> ready),
            # not "time since completion".
            duration = _processing_duration_seconds(draft)
            if duration is not None:
                extras.append(
                    Span(  # noqa: F405
                        _format_elapsed_final(duration),
                        cls="draft-stage-done-label muted-text",
                    )
                )

        items.append(
            Li(  # noqa: F405
                Span(str(idx + 1), cls="draft-stage-number", aria_hidden="true"),  # noqa: F405
                Span(label, cls="draft-stage-label"),  # noqa: F405
                *extras,
                cls=" ".join(classes),
            )
        )

    tracker = Ol(*items, cls="draft-status-tracker", aria_label="Töötluse staatus")  # noqa: F405

    # Build the poll attributes only while the draft is still
    # progressing AND we haven't blown the polling timeout (#457).
    polling_stale = _is_status_polling_stale(draft)
    poll_attrs: dict[str, Any] = {}
    if draft.status not in _TERMINAL_STATUSES and not polling_stale:
        poll_attrs = {
            "hx_get": f"/drafts/{draft.id}/status",
            # #607: exponential-ish polling backoff. Freshly created
            # drafts poll every 3s so the tracker feels responsive, but
            # as the wall-clock elapses we slow down to avoid hammering
            # the server for genuinely-slow pipelines. The upper bound
            # is still the _POLLING_TIMEOUT_SECONDS budget above.
            "hx_trigger": f"every {_poll_interval_seconds(draft)}s",
            "hx_target": "this",
            "hx_swap": "outerHTML",
        }

    header = Div(  # noqa: F405
        Span("Staatus:", cls="draft-status-label-text"),  # noqa: F405
        _status_badge(draft.status),
        cls="draft-status-header",
    )

    children: list = [header, tracker]
    if draft.status == "failed" and draft.error_message:
        # #656: surface a "Proovi uuesti" retry button alongside the
        # red error banner so the user never has to re-upload the
        # original file just to re-run the pipeline. The button posts
        # to /drafts/{id}/retry which resets the draft back to
        # ``uploaded`` and re-enqueues parse_draft. HTMX drives the
        # submit so the action stays on the page; an HX-Redirect
        # bounces the browser back to the detail page once the reset
        # commits.
        children.append(
            Alert(
                Div(
                    P(draft.error_message, cls="draft-failed-message"),  # noqa: F405
                    Form(  # noqa: F405
                        Button(
                            "Proovi uuesti",
                            type="submit",
                            variant="primary",
                            size="md",
                        ),
                        method="post",
                        action=f"/drafts/{draft.id}/retry",
                        enctype="application/x-www-form-urlencoded",
                        hx_post=f"/drafts/{draft.id}/retry",
                        hx_target="body",
                        hx_swap="outerHTML",
                        cls="inline-form draft-retry-form",
                    ),
                    cls="draft-failed-body",
                ),
                variant="danger",
                title="Töötlemine ebaõnnestus",
            )
        )
    elif polling_stale and draft.status not in _TERMINAL_STATUSES:
        # The pipeline has been running longer than the polling
        # timeout. Stop the auto-poll and replace the old admin-
        # dashboard dead-end (#606) with an actionable pair: a manual
        # "Kontrolli uuesti kohe" button that fires one immediate
        # /status fetch, plus a short guidance line telling the user
        # when to escalate.
        children.append(
            Alert(
                Div(
                    P(  # noqa: F405
                        "Töötlemine võtab oodatust kauem aega. "
                        "Kui analüüs on kauem kui 10 minutit peatunud, "
                        "võtke ühendust meeskonnaga.",
                        cls="stale-guidance",
                    ),
                    Button(
                        "Kontrolli uuesti kohe",
                        type="button",
                        variant="secondary",
                        size="sm",
                        hx_get=f"/drafts/{draft.id}/status",
                        hx_target=f"#draft-status-{draft.id}",
                        hx_swap="outerHTML",
                    ),
                    cls="stale-repoll",
                ),
                variant="warning",
                title="Töötlemine venib",
            )
        )

    # #606: inline ticker that increments the ".draft-stage-elapsed"
    # span once per second so users see a live "1:40 möödas" counter
    # without extra HTMX polls. The script runs on every HTMX swap
    # because HTMX executes inline scripts inside swapped fragments.
    # The window-level interval id is reused across swaps to avoid
    # stacking multiple timers.
    #
    # #657: two bug-fixes applied here.
    #   1. Format: past 60 minutes the old ``M:SS`` template emitted
    #      unreadable three-digit minute counts ("8835:14"). Switch to
    #      ``H:MM:SS möödas`` once the raw counter clears one hour.
    #   2. Stop condition: when no ``.draft-stage-elapsed`` nodes
    #      remain (terminal swap — the ``ready``/``failed`` status
    #      fragment no longer renders one), clearInterval so the
    #      timer is not left dangling on the window.
    if any("draft-stage-elapsed" in str(child) for child in children):
        children.append(
            Script(  # noqa: F405
                "(function () {\n"
                "  if (window.__draftElapsedTimer) "
                "clearInterval(window.__draftElapsedTimer);\n"
                "  function pad(n) { return n < 10 ? '0' + n : '' + n; }\n"
                "  function format(raw) {\n"
                "    if (raw >= 3600) {\n"
                "      var h = Math.floor(raw / 3600);\n"
                "      var m = Math.floor((raw % 3600) / 60);\n"
                "      var s = raw % 60;\n"
                "      return h + ':' + pad(m) + ':' + pad(s) + ' möödas';\n"
                "    }\n"
                "    var mm = Math.floor(raw / 60);\n"
                "    var ss = raw % 60;\n"
                "    return mm + ':' + pad(ss) + ' möödas';\n"
                "  }\n"
                "  function tick() {\n"
                "    var nodes = document.querySelectorAll('.draft-stage-elapsed');\n"
                "    if (nodes.length === 0) {\n"
                "      clearInterval(window.__draftElapsedTimer);\n"
                "      window.__draftElapsedTimer = null;\n"
                "      return;\n"
                "    }\n"
                "    nodes.forEach(function (el) {\n"
                "      var raw = parseInt(el.getAttribute('data-elapsed-seconds'), 10);\n"
                "      if (isNaN(raw)) return;\n"
                "      raw += 1;\n"
                "      el.setAttribute('data-elapsed-seconds', raw);\n"
                "      el.textContent = format(raw);\n"
                "    });\n"
                "  }\n"
                "  window.__draftElapsedTimer = setInterval(tick, 1000);\n"
                "})();\n"
            )
        )

    # #604: announce stage transitions to screen readers. ``polite``
    # avoids interrupting the user mid-task; ``aria_atomic=false`` means
    # assistive tech reads just the changed node instead of the whole
    # tracker. The existing failed-state Alert still carries
    # ``role="alert"`` for the more urgent announcement.
    return Div(  # noqa: F405
        *children,
        id=f"draft-status-{draft.id}",
        cls="draft-status-wrapper",
        aria_live="polite",
        aria_atomic="false",
        **poll_attrs,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
# ``_parse_uuid`` and ``_not_found_page`` now live in
# :mod:`app.docs._helpers` so :mod:`app.docs.retry_handler` can import
# them without triggering an import cycle with this module (#671).


# ---------------------------------------------------------------------------
# GET /drafts — listing
# ---------------------------------------------------------------------------


def _draft_rows(drafts: list[Draft]) -> list[dict[str, Any]]:
    """Shape ``Draft`` objects into the dict rows expected by DataTable."""
    rows: list[dict[str, Any]] = []
    for draft in drafts:
        rows.append(
            {
                "id": str(draft.id),
                "doc_type_raw": draft.doc_type,
                "title": draft.title,
                "filename": draft.filename,
                "status_raw": draft.status,
                "created_at": _format_timestamp(draft.created_at),
            }
        )
    return rows


# #643: badge variant per doc_type for the Tüüp column on the drafts
# list. Eelnõu = subtle "default" pill (it's the dominant case, no
# need to draw attention); VTK = "primary" so it stands out at a
# glance — VTKs are rarer and operationally distinct.
_DOC_TYPE_BADGE: dict[str, tuple[str, BadgeVariant]] = {
    "eelnou": ("Eelnõu", "default"),
    "vtk": ("VTK", "primary"),
}


def _draft_list_columns() -> list[Column]:
    """Return the column definitions for the drafts DataTable."""

    def _title_cell(row: dict[str, Any]):
        return A(  # noqa: F405
            row["title"],
            href=f"/drafts/{row['id']}",
            cls="data-table-link",
        )

    def _status_cell(row: dict[str, Any]):
        return _status_badge(row["status_raw"])

    def _actions_cell(row: dict[str, Any]):
        return A(  # noqa: F405
            "Vaata",
            href=f"/drafts/{row['id']}",
            cls="btn btn-secondary btn-sm",
        )

    def _doc_type_cell(row: dict[str, Any]):
        label, variant = _DOC_TYPE_BADGE.get(row["doc_type_raw"], ("Eelnõu", "default"))
        return Badge(label, variant=variant, cls=f"doc-type doc-type-{row['doc_type_raw']}")

    return [
        Column(key="doc_type", label="Tüüp", sortable=False, render=_doc_type_cell),
        Column(key="title", label="Pealkiri", sortable=False, render=_title_cell),
        Column(key="filename", label="Failinimi", sortable=False),
        Column(
            key="status",
            label="Staatus",
            sortable=False,
            render=_status_cell,
        ),
        Column(key="created_at", label="Üles laaditud", sortable=False),
        Column(
            key="actions",
            label="Tegevused",
            sortable=False,
            render=_actions_cell,
        ),
    ]


# ---------------------------------------------------------------------------
# Filter bar (#642)
# ---------------------------------------------------------------------------

# Document-type checkbox group on the filter bar.  Order matches the
# spec — "Eelnõu" comes before "VTK" because it is the dominant doc
# type and the default selection.
_DOC_TYPE_CHOICES: tuple[tuple[str, str], ...] = (
    ("eelnou", "Eelnõu"),
    ("vtk", "VTK"),
)
_DOC_TYPE_VALUES: frozenset[str] = frozenset(v for v, _ in _DOC_TYPE_CHOICES)

# Status checkbox group — same six values used by the pipeline.
# Reusing _STATUS_LABELS keeps the Estonian copy in one place.
_STATUS_VALUES: tuple[str, ...] = (
    "uploaded",
    "parsing",
    "extracting",
    "analyzing",
    "ready",
    "failed",
)

# Sort dropdown options (label, value).
_SORT_CHOICES: tuple[tuple[str, str], ...] = (
    ("created_desc", "Üleslaadimise kuupäev (uuemad enne)"),
    ("created_asc", "Üleslaadimise kuupäev (vanemad enne)"),
    ("title_asc", "Pealkiri (A–Ü)"),
    ("title_desc", "Pealkiri (Ü–A)"),
    ("status", "Staatus"),
)
_SORT_VALUES: frozenset[str] = frozenset(v for v, _ in _SORT_CHOICES)


def _parse_date_param(raw: str | None) -> date | None:
    """Parse a YYYY-MM-DD ``<input type="date">`` value, tolerantly.

    Returns ``None`` for both missing and malformed inputs so a corrupted
    URL doesn't crash the page.
    """
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _parse_filters_from_request(req: Request) -> dict:
    """Extract the filter bar's state from ``req.query_params``.

    All values are validated/clamped here so the rendering and the
    SQL-query helpers can both consume the same dict without re-parsing.
    Unknown checkbox values silently drop -- a user-tampered URL
    degrades to "all selected" rather than an error.
    """
    qp = req.query_params

    q_raw = qp.get("q", "").strip()

    # multi-value checkboxes: starlette's QueryParams.getlist preserves
    # repeated keys so ``?type=eelnou&type=vtk`` round-trips correctly.
    selected_types = {v for v in qp.getlist("type") if v in _DOC_TYPE_VALUES}
    if not selected_types:
        # No checkbox ticked -> "show everything" (matches default UI).
        selected_types = set(_DOC_TYPE_VALUES)
    selected_statuses = {v for v in qp.getlist("status") if v in _STATUS_VALUES}
    if not selected_statuses:
        selected_statuses = set(_STATUS_VALUES)

    uploader_raw = qp.get("uploader", "").strip()
    uploader_id: uuid.UUID | None = None
    if uploader_raw:
        try:
            uploader_id = uuid.UUID(uploader_raw)
        except ValueError:
            uploader_id = None

    sort = qp.get("sort", DEFAULT_SORT)
    if sort not in _SORT_VALUES:
        sort = DEFAULT_SORT

    return {
        "q": q_raw,
        "doc_types": selected_types,
        "statuses": selected_statuses,
        "uploader_id": uploader_id,
        "date_from": _parse_date_param(qp.get("from")),
        "date_to": _parse_date_param(qp.get("to")),
        "sort": sort,
    }


def _filter_querystring(filters: dict, *, page: int | None = None) -> str:
    """Render the active filters back into a querystring for pagination links.

    Only non-default fields are emitted -- a "no filters" view links to
    a clean ``/drafts`` URL.  Page is appended last when supplied.
    """
    parts: list[tuple[str, str]] = []
    if filters.get("q"):
        parts.append(("q", filters["q"]))

    selected_types: set[str] = filters.get("doc_types") or set()
    if selected_types and selected_types != set(_DOC_TYPE_VALUES):
        for v, _label in _DOC_TYPE_CHOICES:
            if v in selected_types:
                parts.append(("type", v))

    selected_statuses: set[str] = filters.get("statuses") or set()
    if selected_statuses and selected_statuses != set(_STATUS_VALUES):
        for v in _STATUS_VALUES:
            if v in selected_statuses:
                parts.append(("status", v))

    uploader_id = filters.get("uploader_id")
    if uploader_id:
        parts.append(("uploader", str(uploader_id)))

    if filters.get("date_from"):
        parts.append(("from", filters["date_from"].isoformat()))
    if filters.get("date_to"):
        parts.append(("to", filters["date_to"].isoformat()))

    if filters.get("sort") and filters["sort"] != DEFAULT_SORT:
        parts.append(("sort", filters["sort"]))

    if page is not None and page > 1:
        parts.append(("page", str(page)))

    if not parts:
        return ""
    from urllib.parse import urlencode

    return "?" + urlencode(parts)


def _has_active_filters(filters: dict) -> bool:
    """True when at least one filter narrows the default view.

    Used to pick between the "no drafts at all" empty state and the
    "no drafts match these filters" empty state.
    """
    if filters.get("q"):
        return True
    if filters.get("uploader_id"):
        return True
    if filters.get("date_from") or filters.get("date_to"):
        return True
    if filters.get("doc_types") and set(filters["doc_types"]) != set(_DOC_TYPE_VALUES):
        return True
    if filters.get("statuses") and set(filters["statuses"]) != set(_STATUS_VALUES):
        return True
    return False


def _filter_bar(*, filters: dict, uploaders: list[dict]):
    """Render the HTMX-driven filter bar above the drafts table.

    Targets ``#drafts-table-wrapper`` so changing a filter swaps just
    the table + pagination, not the whole page (page-load case still
    serves the full ``PageShell`` because ``HX-Request`` is missing).
    """

    # ---- Search ----------------------------------------------------
    search_field = Div(  # noqa: F405
        Label(  # noqa: F405
            "Otsi", For="filter-q", cls="form-field-label"
        ),
        Input(  # noqa: F405
            type="search",
            id="filter-q",
            name="q",
            value=filters.get("q") or "",
            placeholder="Pealkiri, failinimi või olem (nt § 121)",
            cls="form-field-input",
            hx_get="/drafts",
            hx_target="#drafts-table-wrapper",
            hx_swap="innerHTML",
            hx_push_url="true",
            hx_include="closest form",
            hx_trigger="input changed delay:300ms, keyup[key=='Enter']",
        ),
        cls="form-field filter-search",
    )

    # ---- Doc type checkboxes --------------------------------------
    selected_types: set[str] = filters.get("doc_types") or set(_DOC_TYPE_VALUES)
    type_inputs = []
    for value, label in _DOC_TYPE_CHOICES:
        attrs: dict = {
            "type": "checkbox",
            "name": "type",
            "value": value,
            "id": f"filter-type-{value}",
        }
        if value in selected_types:
            attrs["checked"] = True
        type_inputs.append(
            Label(  # noqa: F405
                Input(**attrs),  # noqa: F405
                Span(label),  # noqa: F405
                cls="checkbox-label",
                For=f"filter-type-{value}",
            )
        )
    type_group = Fieldset(  # noqa: F405
        Legend("Tüüp", cls="form-field-label"),  # noqa: F405
        Div(*type_inputs, cls="checkbox-group"),  # noqa: F405
        cls="form-field",
    )

    # ---- Status checkboxes ----------------------------------------
    selected_statuses: set[str] = filters.get("statuses") or set(_STATUS_VALUES)
    status_inputs = []
    for value in _STATUS_VALUES:
        attrs = {
            "type": "checkbox",
            "name": "status",
            "value": value,
            "id": f"filter-status-{value}",
        }
        if value in selected_statuses:
            attrs["checked"] = True
        status_inputs.append(
            Label(  # noqa: F405
                Input(**attrs),  # noqa: F405
                Span(_STATUS_LABELS.get(value, value)),  # noqa: F405
                cls="checkbox-label",
                For=f"filter-status-{value}",
            )
        )
    status_group = Fieldset(  # noqa: F405
        Legend("Staatus", cls="form-field-label"),  # noqa: F405
        Div(*status_inputs, cls="checkbox-group"),  # noqa: F405
        cls="form-field",
    )

    # ---- Uploader select ------------------------------------------
    uploader_options = [Option("Kõik üleslaadijad", value="")]  # noqa: F405
    selected_uploader = filters.get("uploader_id")
    selected_uploader_str = str(selected_uploader) if selected_uploader else ""
    for u in uploaders:
        opt_attrs: dict = {"value": u["id"]}
        if u["id"] == selected_uploader_str:
            opt_attrs["selected"] = True
        label_text = u.get("full_name") or u.get("email") or u["id"]
        uploader_options.append(Option(label_text, **opt_attrs))  # noqa: F405

    uploader_field = Div(  # noqa: F405
        Label("Üleslaadija", For="filter-uploader", cls="form-field-label"),  # noqa: F405
        Select(  # noqa: F405
            *uploader_options,
            id="filter-uploader",
            name="uploader",
            cls="form-field-input",
        ),
        cls="form-field",
    )

    # ---- Date range ------------------------------------------------
    date_from_value = filters["date_from"].isoformat() if filters.get("date_from") else ""
    date_to_value = filters["date_to"].isoformat() if filters.get("date_to") else ""
    date_from_field = Div(  # noqa: F405
        Label("Alates", For="filter-from", cls="form-field-label"),  # noqa: F405
        Input(  # noqa: F405
            type="date",
            id="filter-from",
            name="from",
            value=date_from_value,
            cls="form-field-input",
        ),
        cls="form-field",
    )
    date_to_field = Div(  # noqa: F405
        Label("Kuni", For="filter-to", cls="form-field-label"),  # noqa: F405
        Input(  # noqa: F405
            type="date",
            id="filter-to",
            name="to",
            value=date_to_value,
            cls="form-field-input",
        ),
        cls="form-field",
    )

    # ---- Sort ------------------------------------------------------
    sort_options = []
    current_sort = filters.get("sort") or DEFAULT_SORT
    for value, label in _SORT_CHOICES:
        opt_attrs = {"value": value}
        if value == current_sort:
            opt_attrs["selected"] = True
        sort_options.append(Option(label, **opt_attrs))  # noqa: F405
    sort_field = Div(  # noqa: F405
        Label("Sorteeri", For="filter-sort", cls="form-field-label"),  # noqa: F405
        Select(*sort_options, id="filter-sort", name="sort", cls="form-field-input"),  # noqa: F405
        cls="form-field",
    )

    # ---- Reset link -----------------------------------------------
    reset_link = A(  # noqa: F405
        "Lähtesta filtrid",
        href="/drafts",
        cls="filter-reset-link",
    )

    return Form(  # noqa: F405
        search_field,
        Div(  # noqa: F405
            type_group,
            status_group,
            uploader_field,
            date_from_field,
            date_to_field,
            sort_field,
            cls="filter-row",
        ),
        Div(reset_link, cls="filter-actions"),  # noqa: F405
        method="get",
        action="/drafts",
        cls="drafts-filter-bar",
        role="search",
        aria_label="Eelnõude filtrid",
        hx_get="/drafts",
        hx_target="#drafts-table-wrapper",
        hx_swap="innerHTML",
        hx_push_url="true",
        hx_trigger="change",
    )


def _drafts_table_section(
    *,
    drafts: list[Draft],
    total: int,
    page: int,
    filters: dict,
    has_active_filters: bool,
):
    """Render just the table + pagination wrapper, swappable by HTMX.

    Wrapped in a div with the id HTMX targets so filter changes only
    re-render the table; the surrounding form keeps focus.
    """
    if total == 0:
        if has_active_filters:
            body: Any = EmptyState(
                "Filtritele vastavaid eelnõusid pole",
                message=(
                    "Proovige muuta otsingusõna või lähtestada filtrid, et näha "
                    "kõiki organisatsiooni eelnõusid."
                ),
                icon="🔍",
                action=A(  # noqa: F405
                    "Lähtesta filtrid",
                    href="/drafts",
                    cls="btn btn-secondary btn-md",
                ),
            )
        else:
            body = EmptyState(
                "Teie organisatsioon ei ole veel ühtegi eelnõu üles laadinud.",
                message=(
                    "Laadige üles .docx või .pdf eelnõu, et näha selle mõju "
                    "olemasolevatele seadustele. Süsteem analüüsib automaatselt "
                    "viiteid, konflikte ja EL-i vastavust."
                ),
                icon="📄",
                action=A(  # noqa: F405
                    "Laadi üles uus eelnõu",
                    href="/drafts/new",
                    cls="btn btn-primary btn-md",
                ),
            )
        return Div(body, id="drafts-table-wrapper")  # noqa: F405

    table = DataTable(
        columns=_draft_list_columns(),
        rows=_draft_rows(drafts),
        empty_message="Eelnõusid ei leitud.",
    )

    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    # Pagination links must round-trip every active filter so the user
    # stays inside the same filtered slice when paging.  We bake the
    # filter querystring into base_url; pagination's own helper appends
    # ``page=N`` on top.
    base_url = "/drafts" + _filter_querystring(filters)
    if "?" not in base_url:
        # Pagination._build_url tolerates URLs without an existing
        # querystring, so this is defensive only.
        pass
    pagination = Pagination(
        current_page=page,
        total_pages=total_pages,
        base_url=base_url,
        page_size=_PAGE_SIZE,
        total=total,
    )

    return Div(table, pagination, id="drafts-table-wrapper")  # noqa: F405


def drafts_list_page(req: Request):
    """GET /drafts — filtered + paginated workspace listing (#642).

    Two render paths:

    * Plain GET (no ``HX-Request`` header): full ``PageShell`` with
      filter bar + table.
    * HTMX request (``HX-Request: true``): just the table-wrapper
      partial so the filter bar keeps its focus + selection state.
    """
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect
    theme = get_theme_from_request(req)
    org_id = auth.get("org_id")

    page_str = req.query_params.get("page", "1")
    try:
        page = max(1, int(page_str))
    except ValueError:
        page = 1
    offset = (page - 1) * _PAGE_SIZE

    if not org_id:
        # Unaffiliated user — no filter bar makes sense, render the
        # warning alert and bail.
        return PageShell(
            H1("Eelnõud", cls="page-title"),  # noqa: F405
            Alert(
                "Te ei kuulu ühtegi organisatsiooni, seega ei saa Te eelnõusid "
                "näha ega üles laadida.",
                variant="warning",
            ),
            title="Eelnõud",
            user=auth,
            theme=theme,
            active_nav="/drafts",
            request=req,
        )

    filters = _parse_filters_from_request(req)
    has_active_filters = _has_active_filters(filters)

    drafts, total = list_drafts_for_org_filtered(
        org_id,
        q=filters["q"] or None,
        doc_types=filters["doc_types"],
        statuses=filters["statuses"],
        uploader_id=filters["uploader_id"],
        date_from=filters["date_from"],
        date_to=filters["date_to"],
        sort=filters["sort"],
        limit=_PAGE_SIZE,
        offset=offset,
    )

    table_section = _drafts_table_section(
        drafts=drafts,
        total=total,
        page=page,
        filters=filters,
        has_active_filters=has_active_filters,
    )

    # HTMX swap path — return just the wrapper so filter focus is
    # preserved.  The form-level ``hx-target`` points here.
    if req.headers.get("HX-Request") == "true":
        return table_section

    uploaders = list_users(org_id=str(org_id))
    filter_bar = _filter_bar(filters=filters, uploaders=uploaders)

    header_children: list = [H1("Eelnõud", cls="page-title")]  # noqa: F405
    header_children.append(
        InfoBox(
            P(
                "See on teie organisatsiooni eelnõude töölaud. Siin saate "
                "üles laadida uusi eelnõu kavandeid (.docx või .pdf) ja "
                "väljatöötamiskavatsusi (VTK), jälgida nende töötlust "
                "(parsimine → entiteetide ekstraktimine → mõjuanalüüs) "
                "ning vaadata ja eksportida valmis mõjuaruandeid."
            ),
            P(
                "Iga üleslaaditud eelnõu kohta süsteem tuvastab "
                "automaatselt viited (õigusaktidele, sätetele, EL "
                "direktiividele, Riigikohtu lahenditele), võrdleb seda "
                "kehtiva õiguskorraga, leiab võimalikud konfliktid ja "
                "katmata regulatsioonialad ning koostab .docx "
                "mõjuaruande. Saate nimekirja filtreerida tüübi, staatuse, "
                "üleslaadija ja kuupäeva järgi ning otsida pealkirjast, "
                "failinimest või eelnõus mainitud viidete tekstist."
            ),
            P(
                "Vajutage „Laadi üles uus eelnõu“, et alustada. "
                "Maksimaalne failisuurus on 50 MB. Eelnõud säilivad kuni "
                "nende kustutamiseni; tundlikud failid on krüpteeritud "
                "puhkeolekus ja nähtavad ainult teie organisatsiooni "
                "liikmetele."
            ),
            variant="info",
            dismissible=True,
        )
    )
    header_children.append(
        Div(
            A(
                "Laadi üles uus eelnõu",
                href="/drafts/new",
                cls="btn btn-primary btn-md",
            ),
            cls="page-actions",
        )
    )

    return PageShell(
        *header_children,
        Card(
            CardHeader(H3("Minu organisatsiooni eelnõud", cls="card-title")),  # noqa: F405
            CardBody(filter_bar, table_section),
        ),
        title="Eelnõud",
        user=auth,
        theme=theme,
        active_nav="/drafts",
        request=req,
    )


# ---------------------------------------------------------------------------
# GET /drafts/new — upload form
# ---------------------------------------------------------------------------


# #602: client-side 50 MB cap matches the server-side limit in
# ``app/docs/upload.py``. Surfaced in the browser so users don't wait
# for a large upload to transfer before being told it's too big. The
# inline script also renders "filename — 12.3 MB" below the picker so
# there is immediate visual confirmation of the selection.
_UPLOAD_MAX_BYTES = 50 * 1024 * 1024

_FILE_PICKER_SCRIPT = (
    "(function () {\n"
    "  var input = document.getElementById('field-file');\n"
    "  if (!input) return;\n"
    "  var info = document.getElementById('field-file-info');\n"
    "  var err = document.getElementById('field-file-error');\n"
    "  var submit = document.getElementById('upload-submit');\n"
    f"  var MAX = {_UPLOAD_MAX_BYTES};\n"
    "  function fmt(bytes) {\n"
    "    if (bytes < 1024) return bytes + ' B';\n"
    "    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';\n"
    "    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';\n"
    "  }\n"
    "  input.addEventListener('change', function () {\n"
    "    var file = input.files && input.files[0];\n"
    "    if (!file) {\n"
    "      if (info) info.textContent = '';\n"
    "      if (err) { err.textContent = ''; err.hidden = true; }\n"
    "      if (submit) submit.disabled = false;\n"
    "      return;\n"
    "    }\n"
    "    if (info) info.textContent = file.name + ' \\u2014 ' + fmt(file.size);\n"
    "    if (file.size > MAX) {\n"
    "      if (err) {\n"
    "        err.textContent = 'Fail on liiga suur (' + fmt(file.size) "
    "+ '). Maksimaalne suurus on 50 MB.';\n"
    "        err.hidden = false;\n"
    "      }\n"
    "      if (submit) submit.disabled = true;\n"
    "      input.value = '';\n"
    "      if (info) info.textContent = '';\n"
    "    } else {\n"
    "      if (err) { err.textContent = ''; err.hidden = true; }\n"
    "      if (submit) submit.disabled = false;\n"
    "    }\n"
    "  });\n"
    "})();\n"
)


# #640: inline toggle that disables the "Seotud VTK" picker when the
# "VTK" radio is selected.  A VTK cannot have a parent VTK (enforced by
# the DB CHECK constraint in migration 019) so the control is purely
# UX polish; server-side validation in ``create_draft_handler`` is the
# authoritative guard.
_DOC_TYPE_TOGGLE_SCRIPT = (
    "(function () {\n"
    "  var picker = document.getElementById('field-parent-vtk');\n"
    "  if (!picker) return;\n"
    "  var radios = document.querySelectorAll('input[name=\"doc_type\"]');\n"
    "  function sync() {\n"
    "    var chosen = document.querySelector('input[name=\"doc_type\"]:checked');\n"
    "    var isVtk = chosen && chosen.value === 'vtk';\n"
    "    picker.disabled = !!isVtk;\n"
    "    if (isVtk) picker.value = '';\n"
    "  }\n"
    "  radios.forEach(function (r) { r.addEventListener('change', sync); });\n"
    "  sync();\n"
    "})();\n"
)


def _doc_type_radio(*, selected: str = "eelnou"):
    """Render the "Dokumendi tüüp" radio group (#640).

    Two options — "Eelnõu" (default) and "VTK" — rendered as native
    radio inputs so the form gracefully degrades without JS. The
    server-side validation in ``create_draft_handler`` is the
    authoritative check; the client-side toggle below just hides the
    VTK picker when "VTK" is selected.
    """
    normalised = selected if selected in {"eelnou", "vtk"} else "eelnou"
    return Div(  # noqa: F405
        Label(  # noqa: F405
            "Dokumendi tüüp",
            Span(" *", cls="form-field-required", aria_hidden="true"),  # noqa: F405
            cls="form-field-label",
        ),
        Div(  # noqa: F405
            Label(  # noqa: F405
                Input(  # noqa: F405
                    type="radio",
                    name="doc_type",
                    value="eelnou",
                    id="doc-type-eelnou",
                    checked=(normalised == "eelnou"),
                ),
                Span("Eelnõu"),  # noqa: F405
                fr="doc-type-eelnou",
                cls="form-radio-option",
            ),
            Label(  # noqa: F405
                Input(  # noqa: F405
                    type="radio",
                    name="doc_type",
                    value="vtk",
                    id="doc-type-vtk",
                    checked=(normalised == "vtk"),
                ),
                Span("VTK"),  # noqa: F405
                fr="doc-type-vtk",
                cls="form-radio-option",
            ),
            cls="form-radio-group",
            role="radiogroup",
            aria_label="Dokumendi tüüp",
        ),
        cls="form-field",
    )


def _vtk_picker(
    vtks: list[Draft],
    *,
    selected: uuid.UUID | str | None = None,
    disabled: bool = False,
    field_id: str = "field-parent-vtk",
    name: str = "parent_vtk_id",
    label: str = "Seotud VTK",
):
    """Render the VTK ``<select>`` picker used on upload + link-vtk (#640).

    Populated server-side with the caller's org's VTKs (no cross-org
    leak possible). First option is an empty "— vali —" sentinel so
    "no link" round-trips through the form.  Renders as a ``<select>``
    element so the control works without JS.
    """
    selected_str = str(selected) if selected else ""
    options: list = [Option("— vali —", value="", selected=(selected_str == ""))]  # noqa: F405
    for vtk in vtks:
        vtk_id = str(vtk.id)
        options.append(
            Option(  # noqa: F405
                vtk.title,
                value=vtk_id,
                selected=(vtk_id == selected_str),
            )
        )
    select_kwargs: dict[str, Any] = {
        "name": name,
        "id": field_id,
        "cls": "input input-select",
    }
    if disabled:
        select_kwargs["disabled"] = True
    return Div(  # noqa: F405
        Label(label, fr=field_id, cls="form-field-label"),  # noqa: F405
        Select(*options, **select_kwargs),  # noqa: F405
        Small(  # noqa: F405
            "Valikuline — seoge eelnõu selle VTKga, millest see tuleneb.",
            cls="form-field-help",
        ),
        cls="form-field",
    )


def _upload_form(
    *,
    title_value: str = "",
    error: str | None = None,
    vtks: list[Draft] | None = None,
    doc_type_value: str = "eelnou",
    parent_vtk_id_value: str | None = None,
):
    """Render the multipart upload form.

    IMPORTANT: this form uses the raw ``Form`` primitive from
    ``fasthtml.common`` rather than :class:`AppForm` because file uploads
    **must** use ``enctype="multipart/form-data"``. AppForm defaults to
    ``application/x-www-form-urlencoded`` and would silently drop the file.

    #640: adds a "Dokumendi tüüp" radio group and a "Seotud VTK"
    ``<select>`` populated with the caller's org's VTKs. Validation of
    both fields happens server-side in ``create_draft_handler``.
    """
    error_alert = Alert(error, variant="danger") if error else None
    picker_disabled = doc_type_value == "vtk"
    vtk_list = vtks or []

    return Form(  # noqa: F405
        Div(
            Label(  # noqa: F405
                "Pealkiri",
                Span(" *", cls="form-field-required", aria_hidden="true"),  # noqa: F405
                fr="field-title",
                cls="form-field-label",
            ),
            Input(  # noqa: F405
                name="title",
                type="text",
                id="field-title",
                value=title_value,
                required=True,
                maxlength="200",
                cls="input",
            ),
            Small(  # noqa: F405
                "Kuni 200 tähemärki.",
                cls="form-field-help",
            ),
            cls="form-field",
        ),
        _doc_type_radio(selected=doc_type_value),
        _vtk_picker(
            vtk_list,
            selected=parent_vtk_id_value,
            disabled=picker_disabled,
        ),
        Div(
            Label(  # noqa: F405
                "Fail",
                Span(" *", cls="form-field-required", aria_hidden="true"),  # noqa: F405
                fr="field-file",
                cls="form-field-label",
            ),
            Input(  # noqa: F405
                name="file",
                type="file",
                id="field-file",
                accept=".docx,.pdf",
                required=True,
                cls="input input-file",
            ),
            Small(  # noqa: F405
                "Toetatud failitüübid: .docx, .pdf. Maksimaalne suurus 50 MB.",
                cls="form-field-help",
            ),
            # #602: client-side picker feedback — filename + formatted
            # size, plus an inline error when the picked file exceeds
            # 50 MB so the user is not forced to wait for a large
            # upload to transfer before being told it's too big.
            P("", id="field-file-info", cls="form-field-help muted-text"),  # noqa: F405
            Div(  # noqa: F405
                "",
                id="field-file-error",
                cls="form-field-error",
                role="alert",
                hidden=True,
            ),
            cls="form-field",
        ),
        Div(
            Button(
                "Laadi üles",
                type="submit",
                variant="primary",
                id="upload-submit",
            ),
            A("Tühista", href="/drafts", cls="btn btn-ghost btn-md"),  # noqa: F405
            # #599: spinner shown while the upload request is in
            # flight. HTMX toggles ``.htmx-request`` on the indicator
            # element referenced by ``hx-indicator`` so the form never
            # appears frozen.
            Span("", cls="btn-spinner upload-spinner", aria_hidden="true"),  # noqa: F405
            cls="form-actions",
        ),
        Script(_FILE_PICKER_SCRIPT),  # noqa: F405
        Script(_DOC_TYPE_TOGGLE_SCRIPT),  # noqa: F405
        method="post",
        action="/drafts",
        enctype="multipart/form-data",
        cls="upload-form",
        hx_indicator=".upload-spinner",
        **({"data-error": "1"} if error_alert else {}),
    ), error_alert


def new_draft_page(req: Request):
    """GET /drafts/new — render the upload form."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect
    theme = get_theme_from_request(req)

    if not auth.get("org_id"):
        return PageShell(
            H1("Uus eelnõu", cls="page-title"),  # noqa: F405
            Alert(
                "Te ei kuulu ühtegi organisatsiooni, seega ei saa Te eelnõusid "
                "üles laadida. Võtke ühendust administraatoriga.",
                variant="warning",
            ),
            P(A("← Tagasi eelnõude nimekirja", href="/drafts"), cls="back-link"),  # noqa: F405
            title="Uus eelnõu",
            user=auth,
            theme=theme,
            active_nav="/drafts",
            request=req,
        )

    # #640: populate the "Seotud VTK" picker at render time with the
    # caller's org's VTKs. Cross-org leaks are impossible because the
    # helper scopes the query to ``auth['org_id']``. The ``org_id``
    # check at the top of the handler guarantees a non-None value here.
    org_id_str = str(auth["org_id"])
    vtks = list_vtks_for_org(org_id_str)
    form, error_alert = _upload_form(vtks=vtks)
    card_children: list = []
    if error_alert is not None:
        card_children.append(error_alert)
    card_children.append(form)

    return PageShell(
        H1("Uus eeln\u00f5u", cls="page-title"),  # noqa: F405
        InfoBox(
            P(
                "Valige fail (.docx v\u00f5i .pdf, kuni 50 MB) ja andke sellele "
                "pealkiri. P\u00e4rast \u00fcleslaadimist anal\u00fc\u00fcsib "
                "s\u00fcsteem eeln\u00f5u automaatselt."
            ),
            variant="info",
            dismissible=True,
        ),
        Card(CardBody(*card_children)),
        P(A("\u2190 Tagasi eeln\u00f5ude nimekirja", href="/drafts"), cls="back-link"),  # noqa: F405
        title="Uus eeln\u00f5u",
        user=auth,
        theme=theme,
        active_nav="/drafts",
        request=req,
    )


# ---------------------------------------------------------------------------
# POST /drafts — create handler
# ---------------------------------------------------------------------------


_VALID_DOC_TYPES: frozenset[str] = frozenset({"eelnou", "vtk"})


def _validate_parent_vtk_fk(
    conn: Any,
    parent_vtk_id: uuid.UUID,
    org_id: str,
) -> str | None:
    """Return an Estonian error message if *parent_vtk_id* is not a usable
    VTK for the current org, or ``None`` when the FK is valid.

    Scopes the lookup to ``org_id`` so a cross-org FK (URL-tampered by a
    malicious client) looks exactly like a missing row — we never
    confirm the existence of another org's draft in an error message.
    """
    row = conn.execute(
        "select doc_type from drafts where id = %s and org_id = %s",
        (str(parent_vtk_id), str(org_id)),
    ).fetchone()
    if row is None:
        return "Valitud VTK ei ole kättesaadav."
    if row[0] != "vtk":
        return "Valitud VTK ei ole kättesaadav."
    return None


async def create_draft_handler(req: Request):
    """POST /drafts — accept a multipart upload and create a draft row.

    #640: validates ``doc_type`` and ``parent_vtk_id`` from the upload
    form. Both fields are optional server-side (``doc_type`` defaults
    to ``eelnou``, ``parent_vtk_id`` defaults to unset) but any value
    present must pass the full validation gauntlet:

    * ``doc_type`` must be one of ``{'eelnou', 'vtk'}``.
    * A VTK upload cannot carry a ``parent_vtk_id`` (DB CHECK mirror).
    * A ``parent_vtk_id`` must exist, belong to the caller's org, and
      have ``doc_type = 'vtk'``.
    """
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect
    theme = get_theme_from_request(req)

    form = await req.form()
    title_raw = form.get("title", "")
    upload = form.get("file")
    title_value = str(title_raw) if title_raw is not None else ""

    # #640: new upload fields. Empty / missing values default to the
    # "plain eelnõu with no VTK link" shape so legacy clients and
    # forms that predate the picker keep working.
    doc_type_raw = form.get("doc_type", "eelnou")
    doc_type_value = str(doc_type_raw) if doc_type_raw else "eelnou"
    parent_vtk_raw = form.get("parent_vtk_id", "")
    parent_vtk_str = str(parent_vtk_raw).strip() if parent_vtk_raw else ""
    parent_vtk_uuid = _parse_uuid(parent_vtk_str) if parent_vtk_str else None

    error_message: str | None = None
    status_code = 200

    if doc_type_value not in _VALID_DOC_TYPES:
        error_message = "Vigane dokumendi tüüp."
        status_code = 400
    elif parent_vtk_str and parent_vtk_uuid is None:
        # The user submitted something in the picker but it wasn't a UUID.
        error_message = "Valitud VTK ei ole kättesaadav."
        status_code = 400
    elif parent_vtk_uuid is not None and doc_type_value == "vtk":
        # A VTK cannot have a parent VTK — same rule as the DB CHECK.
        error_message = "VTK ei saa olla seotud teise VTKga."
        status_code = 400
    elif parent_vtk_uuid is not None:
        # FK target must exist, be in the same org, and be a VTK.
        org_id = auth.get("org_id")
        if not org_id:
            error_message = "Valitud VTK ei ole kättesaadav."
            status_code = 400
        else:
            try:
                with _connect() as conn:
                    fk_error = _validate_parent_vtk_fk(conn, parent_vtk_uuid, str(org_id))
            except Exception:
                logger.exception(
                    "Failed to validate parent_vtk_id=%s for org=%s",
                    parent_vtk_uuid,
                    org_id,
                )
                fk_error = "Valitud VTK ei ole kättesaadav."
            if fk_error is not None:
                error_message = fk_error
                status_code = 400

    if error_message is None:
        if upload is None or not hasattr(upload, "read"):
            error_message = "Palun valige üleslaaditav fail."
        else:
            try:
                draft = await handle_upload(
                    auth,
                    title_value,
                    upload,  # type: ignore[arg-type]
                    doc_type=doc_type_value,
                    parent_vtk_id=parent_vtk_uuid,
                )
            except DraftUploadError as exc:
                error_message = str(exc)
            else:
                log_draft_upload(
                    auth.get("id"),
                    draft.id,
                    filename=draft.filename,
                    content_type=draft.content_type,
                    file_size=draft.file_size,
                )
                # #598: queue a success toast for the detail page.
                push_flash(
                    req,
                    "Eelnõu üles laaditud, analüüs algas.",
                    kind="success",
                )
                return RedirectResponse(url=f"/drafts/{draft.id}", status_code=303)

    # At this point we definitely have an error_message.
    assert error_message is not None  # narrows for type-checkers

    # #598: also surface the validation error as a danger toast so the
    # banner + toast pattern is consistent with the happy-path redirect.
    push_flash(req, error_message, kind="danger")

    vtks = list_vtks_for_org(str(auth["org_id"])) if auth.get("org_id") else []
    form_el, _ = _upload_form(
        title_value=title_value,
        error=error_message,
        vtks=vtks,
        doc_type_value=doc_type_value if doc_type_value in _VALID_DOC_TYPES else "eelnou",
        parent_vtk_id_value=parent_vtk_str or None,
    )
    page = PageShell(
        H1("Uus eelnõu", cls="page-title"),  # noqa: F405
        Alert(error_message, variant="danger"),
        Card(CardBody(form_el)),
        P(A("← Tagasi eelnõude nimekirja", href="/drafts"), cls="back-link"),  # noqa: F405
        title="Uus eelnõu",
        user=auth,
        theme=theme,
        active_nav="/drafts",
        request=req,
    )
    if status_code == 400:
        # Render the page as the 400 body so the browser sees the right
        # status code but the user still gets the form + error banner.
        return HTMLResponse(to_xml(page), status_code=400)
    return page


# ---------------------------------------------------------------------------
# GET /drafts/{draft_id} — detail page
# ---------------------------------------------------------------------------


_DELETE_MODAL_ID = "delete-draft-modal"
_DELETE_TRIGGER_ID = "delete-draft-trigger"
_DELETE_FORM_ID = "delete-draft-form"

# #640: identifiers for the "Seo VTKga" modal + its embedded form.
_LINK_VTK_MODAL_ID = "link-vtk-modal"
_LINK_VTK_TRIGGER_ID = "link-vtk-trigger"
_LINK_VTK_FORM_ID = "link-vtk-form"
_DRAFT_METADATA_ID = "draft-metadata"

# #640: wire the "Seo VTKga" trigger button to the Modal primitive.
# The modal contains a form that HTMX-POSTs to ``/drafts/{id}/link-vtk``
# and targets ``#draft-metadata`` with ``outerHTML`` so the new
# "Seotud VTK" row replaces the old one in place.
_LINK_VTK_MODAL_SCRIPT = (
    "(function () {\n"
    f"  var trigger = document.getElementById('{_LINK_VTK_TRIGGER_ID}');\n"
    "  if (!trigger || !window.Modal) return;\n"
    "  trigger.addEventListener('click', function (evt) {\n"
    "    evt.preventDefault();\n"
    f"    window.Modal.open('{_LINK_VTK_MODAL_ID}');\n"
    "  });\n"
    f"  var form = document.getElementById('{_LINK_VTK_FORM_ID}');\n"
    "  if (form && window.htmx) {\n"
    "    form.addEventListener('htmx:afterRequest', function (evt) {\n"
    "      if (evt.detail && evt.detail.successful) {\n"
    f"        window.Modal.close('{_LINK_VTK_MODAL_ID}');\n"
    "      }\n"
    "    });\n"
    "  }\n"
    "})();\n"
)

# #601: bridge modal confirm click to the HTMX delete form. The modal
# primitive exposes ``window.Modal.open(id)`` / ``.close(id)`` from
# ``app/static/js/modal.js``; this inline script wires the trigger
# button to open the modal and the modal's confirm button to fire the
# hidden form's submit event via ``htmx.trigger()``. Focus is restored
# to the trigger automatically by ``modal.js::close``.
_DELETE_MODAL_SCRIPT = (
    "(function () {\n"
    f"  var trigger = document.getElementById('{_DELETE_TRIGGER_ID}');\n"
    f"  var confirmBtn = document.getElementById('{_DELETE_MODAL_ID}-confirm');\n"
    f"  var form = document.getElementById('{_DELETE_FORM_ID}');\n"
    "  if (!trigger || !confirmBtn || !form || !window.Modal) return;\n"
    "  trigger.addEventListener('click', function (evt) {\n"
    "    evt.preventDefault();\n"
    f"    window.Modal.open('{_DELETE_MODAL_ID}');\n"
    "  });\n"
    "  confirmBtn.addEventListener('click', function () {\n"
    f"    window.Modal.close('{_DELETE_MODAL_ID}');\n"
    "    if (window.htmx && typeof window.htmx.trigger === 'function') {\n"
    "      window.htmx.trigger(form, 'submit');\n"
    "    } else {\n"
    "      form.submit();\n"
    "    }\n"
    "  });\n"
    "})();\n"
)


def _seotud_vtk_row(
    draft: Draft,
    *,
    parent_vtk: Draft | None,
    can_edit: bool,
) -> list[Any]:
    """Build the ``<dt>``/``<dd>`` pair for the "Seotud VTK" metadata row (#640).

    Only rendered for eelnõud — a VTK cannot itself be linked to a
    parent VTK. The body varies by state:

    * Linked — hyperlink to the VTK + an "Eemalda" unlink control
      (owner-only).
    * Unlinked + editor — "—" placeholder + a "Seo VTKga" button that
      opens the link modal.
    * Unlinked + viewer — just "—".
    """
    if draft.doc_type != "eelnou":
        return []
    children: list[Any] = [Dt("Seotud VTK")]  # noqa: F405
    if parent_vtk is not None:
        link = A(  # noqa: F405
            parent_vtk.title,
            href=f"/drafts/{parent_vtk.id}",
            cls="data-table-link",
        )
        if can_edit:
            unlink_form = Form(  # noqa: F405
                Input(type="hidden", name="parent_vtk_id", value=""),  # noqa: F405
                Button(
                    "Eemalda",
                    type="submit",
                    variant="ghost",
                    size="sm",
                ),
                method="post",
                action=f"/drafts/{draft.id}/link-vtk",
                enctype="application/x-www-form-urlencoded",
                hx_post=f"/drafts/{draft.id}/link-vtk",
                hx_target=f"#{_DRAFT_METADATA_ID}",
                hx_swap="outerHTML",
                cls="inline-form unlink-vtk-form",
            )
            children.append(Dd(link, " ", unlink_form))  # noqa: F405
        else:
            children.append(Dd(link))  # noqa: F405
    else:
        if can_edit:
            trigger = Button(
                "Seo VTKga",
                type="button",
                variant="secondary",
                size="sm",
                id=_LINK_VTK_TRIGGER_ID,
                aria_haspopup="dialog",
                aria_controls=_LINK_VTK_MODAL_ID,
            )
            children.append(Dd("\u2014 ", trigger))  # noqa: F405
        else:
            children.append(Dd("\u2014"))  # noqa: F405
    return children


def _draft_metadata_block(
    draft: Draft,
    *,
    parent_vtk: Draft | None,
    can_edit: bool,
) -> Any:
    """Render the metadata ``<dl>`` with a stable id for HTMX swap (#640).

    Wrapped in a ``<div id="draft-metadata">`` so the link-vtk handler
    can target it with ``hx-target="#draft-metadata"`` +
    ``hx-swap="outerHTML"`` and replace the entire block in place.
    """
    seotud_rows = _seotud_vtk_row(draft, parent_vtk=parent_vtk, can_edit=can_edit)
    dl = Dl(  # noqa: F405
        Dt("Pealkiri"),  # noqa: F405
        Dd(draft.title),  # noqa: F405
        Dt("Failinimi"),  # noqa: F405
        Dd(draft.filename),  # noqa: F405
        Dt("Failisuurus"),  # noqa: F405
        Dd(f"{draft.file_size:,} baiti".replace(",", " ")),  # noqa: F405
        Dt("Failitüüp"),  # noqa: F405
        Dd(draft.content_type),  # noqa: F405
        Dt("Üles laaditud"),  # noqa: F405
        Dd(_format_timestamp(draft.created_at)),  # noqa: F405
        *seotud_rows,
        cls="info-list",
    )
    return Div(dl, id=_DRAFT_METADATA_ID)  # noqa: F405


def _link_vtk_modal(
    draft: Draft,
    *,
    vtks: list[Draft],
    selected_vtk_id: uuid.UUID | None,
) -> list[Any]:
    """Build the "Seo VTKga" modal + its companion script (#640).

    Rendered as a sibling of the metadata block. The modal's form
    HTMX-POSTs to ``/drafts/{id}/link-vtk`` and swaps ``#draft-metadata``
    with the response fragment. ``_LINK_VTK_MODAL_SCRIPT`` handles the
    open-on-trigger-click wiring and auto-close on successful submit.
    """
    picker = _vtk_picker(
        vtks,
        selected=selected_vtk_id,
        field_id="link-vtk-select",
        name="parent_vtk_id",
        label="Seotud VTK",
    )
    modal_form = Form(  # noqa: F405
        picker,
        ModalFooter(
            Button("Tühista", type="button", variant="secondary", data_modal_close=""),
            Button("Salvesta", type="submit", variant="primary"),
        ),
        id=_LINK_VTK_FORM_ID,
        method="post",
        action=f"/drafts/{draft.id}/link-vtk",
        enctype="application/x-www-form-urlencoded",
        hx_post=f"/drafts/{draft.id}/link-vtk",
        hx_target=f"#{_DRAFT_METADATA_ID}",
        hx_swap="outerHTML",
        cls="link-vtk-form",
    )
    return [
        Modal(
            ModalBody(modal_form),
            title="Seo VTKga",
            id=_LINK_VTK_MODAL_ID,
            size="md",
        ),
        ModalScript(),
        Script(_LINK_VTK_MODAL_SCRIPT),  # noqa: F405
    ]


def _vtk_children_card(
    vtk: Draft,
    *,
    children: list[Draft],
    uploader_index: dict[str, dict[str, Any]] | None = None,
) -> Any:
    """#643: render the "Sellest VTKst tulenevad eelnõud" card on VTK detail.

    Lists eelnõud whose ``parent_vtk_id`` equals this VTK, newest-first.
    Each row links to the child eelnõu's detail page and shows status
    badge, uploader name (resolved from the bulk ``uploader_index``
    dict so we don't N+1 a per-child user lookup), and upload date.
    Empty state surfaces the EmptyState primitive so the card matches
    the rest of the design system.
    """
    if not children:
        body: Any = EmptyState(
            "VTKga pole veel eelnõusid seotud.",
            message=(
                "Kui sellele VTK-le järgneb eelnõu, valige üleslaadimisel "
                "'Seotud VTK' väljas see VTK — siia tekib siis vastav rida."
            ),
            icon="📄",
        )
    else:
        index = uploader_index or {}
        rows: list[Any] = []
        for child in children:
            uploader = index.get(str(child.user_id)) if child.user_id else None
            uploader_label = (
                str(uploader.get("full_name") or uploader.get("email") or "—") if uploader else "—"
            )
            rows.append(
                Tr(  # noqa: F405
                    Td(  # noqa: F405
                        A(  # noqa: F405
                            child.title,
                            href=f"/drafts/{child.id}",
                            cls="data-table-link",
                        ),
                        data_label="Pealkiri",
                    ),
                    Td(_status_badge(child.status), data_label="Staatus"),  # noqa: F405
                    Td(uploader_label, data_label="Üleslaadija"),  # noqa: F405
                    Td(_format_timestamp(child.created_at), data_label="Üles laaditud"),  # noqa: F405
                )
            )
        body = Table(  # noqa: F405
            Thead(  # noqa: F405
                Tr(  # noqa: F405
                    Th("Pealkiri"),  # noqa: F405
                    Th("Staatus"),  # noqa: F405
                    Th("Üleslaadija"),  # noqa: F405
                    Th("Üles laaditud"),  # noqa: F405
                )
            ),
            Tbody(*rows),  # noqa: F405
            cls="data-table vtk-children-table",
        )
    return Card(
        CardHeader(H3("Sellest VTKst tulenevad eelnõud", cls="card-title")),  # noqa: F405
        CardBody(body),
    )


def _draft_detail_body(
    draft: Draft,
    auth: Mapping[str, Any] | None = None,
    *,
    parent_vtk: Draft | None = None,
    org_vtks: list[Draft] | None = None,
) -> list[Any]:
    """Build the metadata + actions body of the draft detail page.

    The delete form is only rendered when ``auth`` is allowed to delete
    per ``app.auth.policy.can_delete_draft`` (issue #568). Before this
    check the button was shown to every same-org viewer, which made the
    route handler's stricter owner-only check surprising for reviewers
    and org admins who could click and get a 404.

    #640: ``parent_vtk`` + ``org_vtks`` are optional extras used to
    render the "Seotud VTK" metadata row and the link-vtk modal. They
    default to ``None``/``[]`` so callers that don't care (e.g. the
    actions-only HTMX fragment endpoint) can keep their current call
    shape.
    """
    can_edit = can_edit_draft(auth, draft)
    metadata = _draft_metadata_block(draft, parent_vtk=parent_vtk, can_edit=can_edit)

    actions: list = []
    # #600: the CTA block is rendered here but the wrapping container
    # is always present so it can listen for the ``draft-ready`` event
    # and re-fetch itself once the pipeline transitions. Only add the
    # "Vaata mõjuaruannet" link when the draft has reached ``ready``.
    if draft.status == "ready":
        actions.append(
            A(  # noqa: F405
                "Vaata mõjuaruannet",
                href=f"/drafts/{draft.id}/report",
                cls="btn btn-primary btn-md",
            )
        )

    # #572: stale drafts (not accessed for 90+ days) get a "Hoia alles"
    # button so the owner can reset the archive clock. The owner-only
    # rule matches the delete policy — resetting the clock is a
    # governance action, not a passive read.
    if _is_draft_stale(draft) and can_delete_draft(auth, draft):
        actions.append(
            Form(  # noqa: F405
                Button(
                    "Hoia alles",
                    type="submit",
                    variant="primary",
                    size="md",
                ),
                # #599: spinner beside the submit so the form isn't
                # visually frozen during the HTMX round-trip.
                Span("", cls="btn-spinner inline-spinner", aria_hidden="true"),  # noqa: F405
                method="post",
                action=f"/drafts/{draft.id}/keep",
                enctype="application/x-www-form-urlencoded",
                hx_post=f"/drafts/{draft.id}/keep",
                hx_target="body",
                hx_swap="outerHTML",
                hx_indicator=".inline-spinner",
                cls="inline-form",
            )
        )

    # #601: the delete action now uses the shared Modal primitive
    # instead of the native ``confirm()`` + HTMX ``hx_confirm`` combo.
    # The visible trigger button opens the modal; the modal's confirm
    # button programmatically submits a hidden HTMX form. This gives
    # us a single accessible prompt with focus trap, Escape-to-cancel,
    # and focus restoration to the trigger on close.
    if can_delete_draft(auth, draft):
        actions.append(
            Button(
                "Kustuta eelnõu",
                type="button",
                variant="danger",
                size="md",
                id=_DELETE_TRIGGER_ID,
                aria_haspopup="dialog",
                aria_controls=_DELETE_MODAL_ID,
            )
        )
        actions.append(
            Form(  # noqa: F405
                # Hidden HTMX form driven by the modal's confirm button
                # (see ``_DELETE_MODAL_SCRIPT``). The native ``action``
                # attribute remains as a no-JS fallback — users without
                # JS can't open the modal, but if something else POSTs
                # the form they still hit the right endpoint.
                # #599: spinner shown while HTMX is mid-request. Even
                # though the form itself is ``hidden``, HTMX toggles
                # ``.htmx-request`` on the indicator class on the root
                # element so the sibling visible spinner (placed next
                # to the trigger) can display.
                Span("", cls="btn-spinner delete-spinner", aria_hidden="true"),  # noqa: F405
                id=_DELETE_FORM_ID,
                method="post",
                action=f"/drafts/{draft.id}/delete",
                enctype="application/x-www-form-urlencoded",
                hx_post=f"/drafts/{draft.id}/delete",
                hx_target="body",
                hx_swap="outerHTML",
                hx_indicator=".delete-spinner",
                cls="inline-form",
                hidden=True,
            )
        )
        actions.append(
            ConfirmModal(
                "Kustuta eelnõu",
                _DELETE_CONFIRM,
                id=_DELETE_MODAL_ID,
                confirm_label="Kustuta",
                cancel_label="Tühista",
                confirm_variant="danger",
            )
        )
        actions.append(ModalScript())
        actions.append(Script(_DELETE_MODAL_SCRIPT))  # noqa: F405

    # #600: wrap the actions in a self-refetching container keyed on
    # the ``draft-ready`` event that the status-fragment handler emits
    # via HX-Trigger when the pipeline transitions into the terminal
    # ``ready`` state. The container re-fetches its own HTML so the
    # "Vaata mõjuaruannet" CTA appears without a full-page refresh.
    actions_container = Div(  # noqa: F405
        *actions,
        id=f"draft-actions-{draft.id}",
        cls="draft-actions",
        hx_get=f"/drafts/{draft.id}/actions",
        hx_trigger="draft-ready from:body",
        hx_swap="outerHTML",
    )

    body: list[Any] = [metadata, actions_container]
    # #640: only eelnõud get the link-vtk modal, and only when the
    # caller may edit the draft. VTKs can't have parents (DB CHECK) and
    # viewers shouldn't see the form.
    if can_edit and draft.doc_type == "eelnou":
        body.extend(
            _link_vtk_modal(
                draft,
                vtks=org_vtks or [],
                selected_vtk_id=draft.parent_vtk_id,
            )
        )
    return body


def draft_detail_page(req: Request, draft_id: str):
    """GET /drafts/{draft_id} — full draft detail with status tracker."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect
    theme = get_theme_from_request(req)

    parsed = _parse_uuid(draft_id)
    if parsed is None:
        return _not_found_page(req)

    draft = fetch_draft(parsed)
    if draft is None:
        return _not_found_page(req)
    if not can_view_draft(auth, draft):
        # Defensive: return 404 (not 403) so we never leak the existence
        # of drafts belonging to other organisations.
        return _not_found_page(req)

    log_draft_view(auth.get("id"), draft.id)
    # #572: surface-to-user counts as access; reset the archive clock.
    touch_draft_access_conn(draft.id)

    # #640: resolve the parent VTK (if any) and fetch the org's VTK
    # catalogue for the link-vtk picker. Both queries are scoped to
    # the caller's org so cross-org leaks are impossible.
    parent_vtk: Draft | None = None
    if draft.parent_vtk_id is not None:
        candidate = fetch_draft(draft.parent_vtk_id)
        # Defensive: only surface the parent if it's still in the same
        # org and really is a VTK. A schema drift or a delete race
        # must not leak another org's draft title into this page.
        if (
            candidate is not None
            and str(candidate.org_id) == str(draft.org_id)
            and candidate.doc_type == "vtk"
        ):
            parent_vtk = candidate
    org_vtks: list[Draft] = []
    if can_edit_draft(auth, draft) and draft.doc_type == "eelnou":
        org_vtks = list_vtks_for_org(draft.org_id)

    # #643: VTK detail surfaces a "Sellest VTKst tulenevad eelnõud"
    # card. Org-scoping is enforced inside `list_eelnous_for_vtk` at
    # the SQL layer — no post-filter needed.
    vtk_children: list[Draft] = []
    uploader_index: dict[str, dict[str, Any]] = {}
    if draft.doc_type == "vtk":
        vtk_children = list_eelnous_for_vtk(draft.id, org_id=draft.org_id)
        # Bulk-resolve uploader names for the children card so we
        # don't fan out N+1 `get_user` calls. One org-scoped lookup
        # gives us every uploader we could possibly need to render.
        if vtk_children:
            uploader_index = {str(u["id"]): u for u in list_users(org_id=str(draft.org_id))}

    detail_body = _draft_detail_body(
        draft,
        auth=auth,
        parent_vtk=parent_vtk,
        org_vtks=org_vtks,
    )
    tracker = _status_tracker(draft)

    return PageShell(
        H1(draft.title, cls="page-title"),  # noqa: F405
        P(A("\u2190 Tagasi eeln\u00f5ude nimekirja", href="/drafts"), cls="back-link"),  # noqa: F405
        InfoBox(
            P(
                "Eeln\u00f5u l\u00e4bib automaatselt mitu etappi: "
                "teksti eraldamine \u2192 viidete tuvastamine \u2192 "
                "m\u00f5juanal\u00fc\u00fcs. "
                "Tulemused ilmuvad allpool."
            ),
            variant="info",
            dismissible=True,
        ),
        Card(
            CardHeader(H3("Staatus", cls="card-title")),  # noqa: F405
            CardBody(
                tracker,
                AnnotationButton("draft", str(draft.id)),
            ),
        ),
        Card(
            CardHeader(H3("\u00dcksikasjad", cls="card-title")),  # noqa: F405
            # #603: the old CardFooter rendered ``draft.graph_uri`` — an
            # internal Jena named-graph URI — to the user. That leaked
            # implementation detail with no operational value; audit
            # logs and admin tools still have the URI, only the
            # user-facing detail page omits it.
            CardBody(*detail_body),
        ),
        # #643: VTK-only card listing follow-on eelnõud. Skipped on
        # eelnõu detail since VTKs are the only doc_type that can have
        # children in our model.
        _vtk_children_card(draft, children=vtk_children, uploader_index=uploader_index)
        if draft.doc_type == "vtk"
        else "",
        title=draft.title,
        user=auth,
        theme=theme,
        active_nav="/drafts",
        request=req,
    )


# ---------------------------------------------------------------------------
# GET /drafts/{draft_id}/status — HTMX polling fragment
# ---------------------------------------------------------------------------


def draft_status_fragment(req: Request, draft_id: str):
    """GET /drafts/{draft_id}/status — just the status-tracker Div.

    Returned raw (no PageShell) so HTMX can swap it with ``outerHTML``
    without injecting a second copy of the layout into the page body.
    Covers issue #347.

    #600: when the draft reaches ``ready`` we also emit an
    ``HX-Trigger: draft-ready`` response header so the detail page's
    actions container re-fetches itself and surfaces the "Vaata
    mõjuaruannet" CTA without requiring a full page refresh.
    """
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    parsed = _parse_uuid(draft_id)
    if parsed is None:
        return Div(  # noqa: F405
            Alert("Eelnõu ei leitud.", variant="warning"),
            id=f"draft-status-{draft_id}",
        )

    draft = fetch_draft(parsed)
    if draft is None or not can_view_draft(auth, draft):
        return Div(  # noqa: F405
            Alert("Eelnõu ei leitud.", variant="warning"),
            id=f"draft-status-{draft_id}",
        )

    tracker = _status_tracker(draft)
    if draft.status == "ready":
        # Emit HX-Trigger: draft-ready so the actions container on the
        # detail page (hx-trigger="draft-ready from:body") re-fetches
        # itself and surfaces the "Vaata mõjuaruannet" CTA. We have to
        # render to HTML explicitly because HTMX reads the trigger from
        # the response headers, and the raw FT return path doesn't let
        # us attach custom headers.
        return HTMLResponse(to_xml(tracker), headers={"HX-Trigger": "draft-ready"})
    return tracker


# ---------------------------------------------------------------------------
# GET /drafts/{draft_id}/actions — HTMX fragment for the action row (#600)
# ---------------------------------------------------------------------------


def draft_actions_fragment(req: Request, draft_id: str):
    """Return just the ``.draft-actions`` container for HTMX re-render.

    The container is wired with ``hx-trigger="draft-ready from:body"``
    so that when :func:`draft_status_fragment` emits
    ``HX-Trigger: draft-ready`` on the ``ready`` transition, the action
    row refreshes itself with the "Vaata mõjuaruannet" CTA and any
    other status-gated controls.
    """
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    parsed = _parse_uuid(draft_id)
    if parsed is None:
        return Div(id=f"draft-actions-{draft_id}", cls="draft-actions")  # noqa: F405

    draft = fetch_draft(parsed)
    if draft is None or not can_view_draft(auth, draft):
        return Div(id=f"draft-actions-{draft_id}", cls="draft-actions")  # noqa: F405

    body = _draft_detail_body(draft, auth=auth)
    # ``_draft_detail_body`` returns ``[metadata_block, actions_container,
    # ...maybe_link_vtk_modal_bits]``; index 1 is the actions container
    # we want to swap. (Before #640 the list was exactly two elements
    # and ``body[-1]`` worked; adding the link-vtk modal trailing items
    # made the negative index unsafe.)
    return body[1]


# ---------------------------------------------------------------------------
# POST /drafts/{draft_id}/delete — delete handler
# ---------------------------------------------------------------------------


def delete_draft_handler(req: Request, draft_id: str):
    """POST /drafts/{draft_id}/delete — remove the draft + encrypted file.

    Owner-only per NFR §5 matrix (fixed by #568). Any same-org colleague
    used to be able to delete another user's draft because the handler
    authorized on ``org_id`` alone. The helper in ``app.auth.policy``
    enforces the full rule: owner OR system admin.
    """
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    parsed = _parse_uuid(draft_id)
    if parsed is None:
        return _not_found_page(req)

    draft = fetch_draft(parsed)
    if draft is None:
        return _not_found_page(req)
    if not can_delete_draft(auth, draft):
        # Return 404 rather than 403 so we don't leak existence of the
        # draft to cross-org or non-owner callers.
        return _not_found_page(req)

    # #628: single transaction for every DB-side deletion. Previously
    # the handler opened two separate ``_connect()`` contexts (row +
    # rag_chunks, then background_jobs cancellation) and sandwiched
    # two external calls (encrypted file + Jena graph) between them
    # with no boundary. A failure partway through left the system in
    # an inconsistent state: row gone but jobs still pending, or jobs
    # cancelled but encrypted file still on disk. The refactor folds
    # every DB mutation into ONE transactional block and hands the
    # slow/flaky external cleanup off to a background ``draft_cleanup``
    # job that can retry independently.
    storage_path: str | None = None
    try:
        with _connect() as conn:
            storage_path = delete_draft(conn, parsed)
            # #576: polymorphic soft reference — clear any rag_chunks rows
            # tied to this draft inside the same transaction as the row
            # delete so either both land or neither does. Today no private
            # draft ingestion exists so this is a no-op, but wiring it now
            # means future ingestion code can't forget.
            try:
                delete_chunks_for_draft(conn, parsed)
            except Exception:
                logger.exception("Failed to delete rag_chunks for draft id=%s", parsed)
            # #454/#478: cancel any pending/claimed/running/retrying
            # background jobs that still reference this draft. Doing
            # this in the SAME transaction as the row delete means a
            # rollback doesn't strand orphaned jobs on the queue.
            # #478 added ``running`` because a worker that picked up
            # the job just before deletion would otherwise leave the
            # row behind and produce a spurious failure.
            conn.execute(
                """
                DELETE FROM background_jobs
                WHERE payload->>'draft_id' = %s
                  AND status IN ('pending', 'claimed', 'running', 'retrying')
                """,
                (str(parsed),),
            )
            conn.commit()
    except Exception:
        logger.exception("Failed to delete draft id=%s", parsed)
        return _not_found_page(req)

    # #628: enqueue an async cleanup job for the external effects that
    # can fail independently of the user-visible delete. The job
    # retries on its own schedule; a flaky Jena instance no longer
    # blocks the user flow or leaves the DB inconsistent. Failure to
    # enqueue is logged but non-fatal — the DB is already clean and
    # the operator can always delete the file/graph manually.
    cleanup_payload = {
        "draft_id": str(parsed),
        "storage_path": storage_path,
        "graph_uri": draft.graph_uri,
    }
    try:
        cleanup_job_id = JobQueue().enqueue("draft_cleanup", cleanup_payload, priority=0)
        logger.info(
            "Orphan cleanup job enqueued draft=%s job_id=%s storage_path=%s",
            parsed,
            cleanup_job_id,
            storage_path,
        )
    except Exception:
        logger.exception(
            "Failed to enqueue draft_cleanup job for draft id=%s — "
            "external resources may be orphaned",
            parsed,
        )

    log_draft_delete(
        auth.get("id"),
        parsed,
        filename=draft.filename,
    )

    # #598: queue a success toast for the drafts listing page.
    push_flash(req, "Eelnõu kustutatud.", kind="success")

    # #467: when the browser drives the delete via HTMX (the form has
    # ``hx_post`` + ``hx_target='body'`` + ``hx_swap='outerHTML'`` — see
    # ``_draft_detail_body``), returning a plain 303 here makes HTMX
    # follow the redirect as an AJAX GET, fetch the drafts-list partial
    # (whose first element is a ``<title>`` tag from ``PageShell``), and
    # swap that entire partial into ``<body>``. The rendered page ends
    # up with a ``<title>`` inside the body, which browsers treat as
    # invalid HTML and render as visible text — corrupting the layout.
    #
    # The fix is to detect HTMX requests and return an empty 204 with an
    # ``HX-Redirect`` header so HTMX performs a **real** browser
    # navigation to ``/drafts`` instead of swapping. Non-HTMX clients
    # (JS-disabled users hitting the native form action) still get the
    # 303 redirect.
    if req.headers.get("HX-Request") == "true":
        return Response(
            status_code=204,
            headers={"HX-Redirect": "/drafts"},
        )
    return RedirectResponse(url="/drafts", status_code=303)


# ---------------------------------------------------------------------------
# POST /drafts/{draft_id}/keep — reset last_accessed_at (#572)
# ---------------------------------------------------------------------------


def keep_draft_handler(req: Request, draft_id: str):
    """POST /drafts/{draft_id}/keep — reset the 90-day archive clock.

    Owner-only per the same policy as delete — resetting the archive
    clock is a governance action that re-commits the org to retaining
    the draft for another 90 days. Same-org reviewers and admins MUST
    NOT be able to bypass the owner's intent to let a stale draft
    auto-warn.
    """
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    parsed = _parse_uuid(draft_id)
    if parsed is None:
        return _not_found_page(req)

    draft = fetch_draft(parsed)
    if draft is None:
        return _not_found_page(req)
    if not can_delete_draft(auth, draft):
        # 404 (not 403) — see delete_draft_handler for the reasoning.
        return _not_found_page(req)

    try:
        with _connect() as conn:
            touch_draft_access(conn, parsed)
            conn.commit()
    except Exception:
        logger.exception("Failed to reset last_accessed_at for draft=%s", parsed)
        return _not_found_page(req)

    log_action(
        auth.get("id"),
        "draft.keep",
        {"draft_id": str(parsed)},
    )

    # #598: queue a success toast for the detail page redirect target.
    push_flash(req, "90-päevane loendur lähtestatud.", kind="success")

    # HTMX-driven submits get an HX-Redirect so the browser performs a
    # real navigation rather than swapping a partial into <body>.
    if req.headers.get("HX-Request") == "true":
        return Response(
            status_code=204,
            headers={"HX-Redirect": f"/drafts/{parsed}"},
        )
    return RedirectResponse(url=f"/drafts/{parsed}", status_code=303)


# ---------------------------------------------------------------------------
# POST /drafts/{draft_id}/link-vtk — set or clear parent_vtk_id (#640)
# ---------------------------------------------------------------------------


async def link_vtk_handler(req: Request, draft_id: str):
    """POST /drafts/{draft_id}/link-vtk — set or clear ``parent_vtk_id``.

    Spec §3.3 — owner-only mutation. The body is a single
    ``parent_vtk_id`` field. An empty value unlinks; a valid UUID
    links (subject to the same FK validation as the upload flow).

    On success the handler writes the new lineage triple to Jena via
    :func:`write_doc_lineage` and returns the refreshed metadata
    fragment so the detail page can HTMX-swap ``#draft-metadata``
    in place.
    """
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect

    parsed = _parse_uuid(draft_id)
    if parsed is None:
        return _not_found_page(req)

    draft = fetch_draft(parsed)
    if draft is None:
        return _not_found_page(req)
    if not can_edit_draft(auth, draft):
        # 404 rather than 403 so we don't leak existence to cross-org
        # or non-owner callers (matches delete_draft_handler).
        return _not_found_page(req)

    form = await req.form()
    parent_vtk_raw = form.get("parent_vtk_id", "")
    parent_vtk_str = str(parent_vtk_raw).strip() if parent_vtk_raw else ""
    parent_vtk_uuid = _parse_uuid(parent_vtk_str) if parent_vtk_str else None

    # Validate: VTK docs cannot themselves carry a parent.
    if parent_vtk_uuid is not None and draft.doc_type == "vtk":
        return HTMLResponse(
            to_xml(Alert("VTK ei saa olla seotud teise VTKga.", variant="danger")),
            status_code=400,
        )
    if parent_vtk_str and parent_vtk_uuid is None:
        return HTMLResponse(
            to_xml(Alert("Valitud VTK ei ole kättesaadav.", variant="danger")),
            status_code=400,
        )

    # FK target must exist, be in the same org, and be a VTK.
    if parent_vtk_uuid is not None:
        try:
            with _connect() as conn:
                fk_error = _validate_parent_vtk_fk(conn, parent_vtk_uuid, str(draft.org_id))
        except Exception:
            logger.exception(
                "Failed to validate parent_vtk_id=%s for draft=%s",
                parent_vtk_uuid,
                parsed,
            )
            fk_error = "Valitud VTK ei ole kättesaadav."
        if fk_error is not None:
            return HTMLResponse(
                to_xml(Alert(fk_error, variant="danger")),
                status_code=400,
            )

    # Persist the new value.
    try:
        with _connect() as conn:
            update_draft_parent_vtk(conn, parsed, parent_vtk_uuid)
            conn.commit()
    except Exception:
        logger.exception("Failed to update parent_vtk_id for draft=%s", parsed)
        return HTMLResponse(
            to_xml(Alert("Seose salvestamine ebaõnnestus.", variant="danger")),
            status_code=500,
        )

    # Refresh the Draft snapshot so the metadata block renders the
    # post-update value.
    refreshed = fetch_draft(parsed) or draft
    parent_vtk: Draft | None = None
    if parent_vtk_uuid is not None:
        parent_vtk = fetch_draft(parent_vtk_uuid)
        if (
            parent_vtk is None
            or str(parent_vtk.org_id) != str(draft.org_id)
            or parent_vtk.doc_type != "vtk"
        ):
            parent_vtk = None

    # Write the lineage triple (idempotent; relink/unlink both handled).
    # Failure here is logged but not user-visible — the DB is already
    # authoritative and a later analyze run will reconcile.
    try:
        write_doc_lineage(refreshed, parent_vtk)
    except Exception:
        logger.exception(
            "write_doc_lineage failed for draft=%s parent_vtk=%s",
            parsed,
            parent_vtk_uuid,
        )

    log_action(
        auth.get("id"),
        "draft.link_vtk",
        {
            "draft_id": str(parsed),
            "parent_vtk_id": str(parent_vtk_uuid) if parent_vtk_uuid else None,
        },
    )

    # Return the refreshed metadata fragment so HTMX can swap
    # #draft-metadata in place.
    return _draft_metadata_block(
        refreshed,
        parent_vtk=parent_vtk,
        can_edit=True,
    )


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_draft_routes(rt) -> None:  # type: ignore[no-untyped-def]
    """Mount the draft upload routes on the FastHTML route decorator *rt*.

    The list/detail/new pages are behind the global auth ``Beforeware``,
    so **do not** add ``/drafts`` to ``SKIP_PATHS``.
    """
    rt("/drafts", methods=["GET"])(drafts_list_page)
    rt("/drafts/new", methods=["GET"])(new_draft_page)
    rt("/drafts", methods=["POST"])(create_draft_handler)
    rt("/drafts/{draft_id}", methods=["GET"])(draft_detail_page)
    rt("/drafts/{draft_id}/status", methods=["GET"])(draft_status_fragment)
    rt("/drafts/{draft_id}/actions", methods=["GET"])(draft_actions_fragment)
    rt("/drafts/{draft_id}/keep", methods=["POST"])(keep_draft_handler)
    rt("/drafts/{draft_id}/delete", methods=["POST"])(delete_draft_handler)
    rt("/drafts/{draft_id}/link-vtk", methods=["POST"])(link_vtk_handler)
    # #656: retry a failed draft's pipeline from the parse stage.
    from app.docs.retry_handler import retry_draft_handler

    rt("/drafts/{draft_id}/retry", methods=["POST"])(retry_draft_handler)
