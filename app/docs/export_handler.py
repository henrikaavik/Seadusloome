"""Background job handler for ``export_report``.

Replaces the last Phase 2 stub in :mod:`app.jobs.worker`. The real
handler loads the draft + the latest ``impact_reports`` row, calls the
.docx builder in :mod:`app.docs.docx_export`, and returns the absolute
path to the generated file in its result dict.

The optional ``format`` payload key (#613) selects the output. For
``"docx"`` (the default, also the implicit value for legacy jobs
written before #613) the handler returns ``{"docx_path": ...}``. For
``"pdf"`` the handler still builds the .docx (so PDF and .docx content
can never diverge) and then shells out to LibreOffice headless via
:func:`app.docs.docx_export.convert_docx_to_pdf` to convert it; the
result dict gains a ``pdf_path`` and the ``format`` echo. The download
handler in :mod:`app.docs.report_routes` picks the right artefact +
MIME from the job payload.

Unlike the other Phase 2 handlers (``parse_draft``, ``extract_entities``,
``analyze_impact``) this one never transitions ``drafts.status`` —
exporting is a read-only user action and the draft row stays in
``ready``. A failure flips the *job* to ``failed`` (with the usual
retry-with-backoff semantics) so the user sees the error on the
export-status polling fragment, but the source draft remains intact.

Progress reporting (#610)
-------------------------

For long .docx renders the handler publishes a ``{"current": N, "total": M}``
payload to ``background_jobs.progress`` after each major report section
plus every ``_PROGRESS_BATCH`` table rows. The export-progress WebSocket
endpoint (``app/docs/ws_export_progress.py``) reads from this column and
pushes updates to the browser so the UI shows a real ``<progress>`` bar
instead of the indeterminate "Eksport käimas..." spinner.

The publish path is best-effort: a failed UPDATE is logged at DEBUG and
swallowed, so the .docx build always finishes regardless of progress
channel health.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db import get_connection
from app.docs.docx_export import build_impact_report_docx, convert_docx_to_pdf
from app.docs.draft_model import get_draft
from app.jobs.worker import register_handler

_VALID_FORMATS = ("docx", "pdf")

logger = logging.getLogger(__name__)


# Column order used in the SELECT below; must stay aligned with
# ``app.docs.docx_export._REPORT_COLUMN_INDEX``.
_REPORT_SELECT_COLUMNS = (
    "id, draft_id, affected_count, conflict_count, gap_count, "
    "impact_score, report_data, ontology_version, generated_at"
)


def _publish_progress(job_id: int, *, current: int, total: int) -> None:
    """Write the latest ``{current, total}`` payload to ``background_jobs.progress``.

    A short-lived connection per call keeps the .docx render from
    holding a transaction open for the entire export — workers run
    concurrently and one stuck transaction would block the queue's
    ``FOR UPDATE SKIP LOCKED`` claims.

    Best-effort: a failed UPDATE is logged at DEBUG and swallowed so a
    stuck progress channel never aborts the .docx build. The export
    polling fallback in ``app/docs/report_routes.py`` still drives the
    UI to the success/failure terminal state regardless of the WS push.
    """
    payload = {"current": current, "total": total}
    try:
        with get_connection() as conn:
            conn.execute(
                "UPDATE background_jobs SET progress = %s WHERE id = %s",
                (Jsonb(payload), job_id),
            )
            conn.commit()
    except Exception:
        logger.debug(
            "export_report: progress UPDATE failed job=%s payload=%s",
            job_id,
            payload,
            exc_info=True,
        )


def export_report(
    payload: dict[str, Any],
    *,
    attempt: int = 1,
    max_attempts: int = 3,
    job_id: int | None = None,
) -> dict[str, Any]:
    """Render the impact report for *payload['draft_id']* as a ``.docx``.

    Args:
        payload: Must contain ``draft_id`` and ``report_id``, both
            UUID-serialisable strings. Any other keys are ignored.
        attempt: 1-based current attempt counter. Accepted for handler
            signature compatibility (#448); export does not transition
            any domain row state on failure so we don't actually branch
            on it, but other handlers do.
        max_attempts: Total retry budget for this job.
        job_id: ``background_jobs.id`` for this invocation. When
            present we hand a progress callback to the docx builder so
            the export-progress WebSocket can push real progress to the
            browser (#610). When ``None`` (e.g. older worker, direct
            call from a test) the callback is omitted and the build
            runs without progress reporting — same behaviour as before
            the migration.

    Returns:
        ``{"draft_id": ..., "report_id": ..., "docx_path": ...}``
        persisted in ``background_jobs.result`` by the worker. The UI
        polls for this row via ``GET /drafts/<id>/export-status/<job>``
        and reads ``docx_path`` to build the download link.

    Raises:
        ValueError: When ``draft_id`` or ``report_id`` is missing/invalid,
            or when either row no longer exists in Postgres.
    """
    # ``attempt``/``max_attempts`` are part of the handler contract
    # (#448) but export doesn't gate domain state on them.
    del attempt, max_attempts
    raw_draft_id = payload.get("draft_id")
    if not raw_draft_id:
        raise ValueError("export_report payload missing required 'draft_id'")
    raw_report_id = payload.get("report_id")
    if not raw_report_id:
        raise ValueError("export_report payload missing required 'report_id'")

    fmt = str(payload.get("format") or "docx")
    if fmt not in _VALID_FORMATS:
        raise ValueError(
            f"export_report: unsupported format {fmt!r}; expected one of {_VALID_FORMATS}"
        )

    try:
        draft_id = UUID(str(raw_draft_id))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"export_report: invalid draft_id {raw_draft_id!r}") from exc
    try:
        report_id = UUID(str(raw_report_id))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"export_report: invalid report_id {raw_report_id!r}") from exc

    logger.info(
        "export_report: starting for draft=%s report=%s format=%s job=%s",
        draft_id,
        report_id,
        fmt,
        job_id,
    )

    with get_connection() as conn:
        draft = get_draft(conn, draft_id)
        report_row = conn.execute(
            f"SELECT {_REPORT_SELECT_COLUMNS} FROM impact_reports WHERE id = %s",
            (str(report_id),),
        ).fetchone()

    if draft is None or report_row is None:
        raise ValueError(f"Draft {draft_id} or report {report_id} not found")

    # Sanity-guard: the report must belong to the draft we were asked
    # to export. The join column lives at index 1 of the SELECT above.
    if str(report_row[1]) != str(draft_id):
        raise ValueError(
            f"Report {report_id} does not belong to draft {draft_id}; refusing to export"
        )

    # Build the per-call progress callback. We bind ``job_id`` via a
    # closure so ``build_impact_report_docx`` doesn't need to know
    # anything about the queue schema. ``None`` skips progress entirely.
    progress_callback: Callable[[int, int], None] | None = None
    if job_id is not None:
        captured_job_id = job_id

        def _publish(current: int, total: int) -> None:
            _publish_progress(captured_job_id, current=current, total=total)

        progress_callback = _publish

    docx_path = build_impact_report_docx(
        draft,
        report_row,
        progress_callback=progress_callback,
    )
    logger.info(
        "export_report: wrote docx draft=%s report=%s path=%s",
        draft_id,
        report_id,
        docx_path,
    )

    result: dict[str, Any] = {
        "draft_id": str(draft_id),
        "report_id": str(report_id),
        "format": fmt,
        "docx_path": str(docx_path),
    }

    if fmt == "pdf":
        # The .docx is the source of truth for content; PDF is a pure
        # visual rendering of the same file via headless LibreOffice.
        # Conversion typically takes 2-3 s on the prod VPS for a typical
        # 5-section report (verified during the §4.3 base-image probe).
        # The progress channel is left at its terminal "1.0" tick from
        # the docx render — adding a "converting…" sub-stage would mean
        # invalidating the WS protocol and re-shaping the JS shim, which
        # is more change than the user experience justifies.
        pdf_path = convert_docx_to_pdf(docx_path)
        result["pdf_path"] = str(pdf_path)
        logger.info(
            "export_report: wrote pdf draft=%s report=%s path=%s",
            draft_id,
            report_id,
            pdf_path,
        )

    return result


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
#
# Importing this module triggers the real handler registration with the
# worker's dispatch registry. ``app/docs/__init__.py`` imports us at
# startup so the worker sees the real handler before claiming any job.

register_handler("export_report")(export_report)
