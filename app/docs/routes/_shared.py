"""Cross-cutting helpers for the /drafts package (#704 PR-B extraction).

Pure helpers extracted from ``app/docs/routes/__init__.py`` so the
upcoming ``_list.py`` / ``_upload.py`` / ``_detail.py`` submodules
can import them without dragging the package's full dependency
graph back through ``__init__.py``. Every function here has NO
route-handler-specific dependencies — they take a :class:`Draft`
or a primitive value, return a string / int / bool / FT node, and
nothing else.

The package's ``__init__.py`` re-exports each name so existing test
code (``from app.docs.routes import _format_elapsed`` /
``patch("app.docs.routes._is_draft_stale")``) keeps working without
any patch-path swap.

Constants:
    ``_PAGE_SIZE``                  — drafts per page on the listing
    ``_DELETE_CONFIRM``             — Estonian confirm-modal copy
    ``_STALE_THRESHOLD_DAYS``       — 90-day archive-warning threshold
    ``_POLLING_TIMEOUT_SECONDS``    — HTMX polling budget per draft
    ``_TYPICAL_STAGE_SECONDS``      — typical wall-clock per pipeline stage
    ``_STATUS_STAGES``              — ordered (value, label) pairs from
                                      :data:`app.docs.status.PIPELINE_STAGES`

Helpers:
    ``_is_draft_stale``             — list + detail
    ``_status_badge``               — list + detail + status_tracker
    ``_format_timestamp``           — used everywhere
    ``_is_status_polling_stale``    — detail + status fragment + tracker
    ``_poll_interval_seconds``      — detail + status fragment + tracker
    ``_elapsed_seconds``            — tracker + detail
    ``_processing_duration_seconds`` — tracker + detail
    ``_format_elapsed``             — tracker + detail
    ``_format_elapsed_final``       — tracker + detail
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.docs.draft_model import Draft
from app.docs.status import (
    PIPELINE_STAGES,
    STATUS_BY_VALUE,
)
from app.ui.primitives.badge import Badge
from app.ui.time import format_tallinn

# ---------------------------------------------------------------------------
# Status display constants
# ---------------------------------------------------------------------------

# Pipeline stages in order. "failed" is a terminal branch rendered
# separately so the tracker reads left-to-right during normal operation.
# Sourced from :data:`app.docs.status.PIPELINE_STAGES` (#625 §4.2 SSOT)
# so a new status only needs to be added in ``status.py``.
_STATUS_STAGES: tuple[tuple[str, str], ...] = tuple((s.value, s.label_et) for s in PIPELINE_STAGES)

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


def _status_badge(status: str):
    """Return a Badge for a draft status.

    We use plain ``Badge`` instead of ``StatusBadge`` because the latter
    ships its own English-ish label set and our domain statuses
    (uploaded/parsing/extracting/analyzing) need Estonian copy.

    Lookup goes through :data:`app.docs.status.STATUS_BY_VALUE` so the
    label, variant, and CSS key all stay synchronised with the SSOT
    (#625 §4.2). Unknown statuses fall back to a neutral pill so a
    legacy row from before a label was added cannot crash the page.
    """
    spec = STATUS_BY_VALUE.get(status)
    if spec is None:
        return Badge(status, variant="default", cls="draft-status draft-status-pending")
    return Badge(
        spec.label_et,
        variant=spec.badge_variant,
        cls=f"draft-status draft-status-{spec.css_key}",
    )


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
