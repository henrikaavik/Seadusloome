"""Background job handler for ``export_report``.

Replaces the last Phase 2 stub in :mod:`app.jobs.worker`. The real
handler loads the draft + the latest ``impact_reports`` row, calls the
.docx builder in :mod:`app.docs.docx_export`, and returns the absolute
path to the generated file in its result dict.

Unlike the other Phase 2 handlers (``parse_draft``, ``extract_entities``,
``analyze_impact``) this one never transitions ``drafts.status`` —
exporting is a read-only user action and the draft row stays in
``ready``. A failure flips the *job* to ``failed`` (with the usual
retry-with-backoff semantics) so the user sees the error on the
export-status polling fragment, but the source draft remains intact.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from app.db import get_connection
from app.docs.docx_export import build_impact_report_docx
from app.docs.draft_model import get_draft
from app.jobs.worker import register_handler

logger = logging.getLogger(__name__)


# Column order used in the SELECT below; must stay aligned with
# ``app.docs.docx_export._REPORT_COLUMN_INDEX``.
_REPORT_SELECT_COLUMNS = (
    "id, draft_id, affected_count, conflict_count, gap_count, "
    "impact_score, report_data, ontology_version, generated_at"
)


def export_report(payload: dict[str, Any]) -> dict[str, Any]:
    """Render the impact report for *payload['draft_id']* as a ``.docx``.

    Args:
        payload: Must contain ``draft_id`` and ``report_id``, both
            UUID-serialisable strings. Any other keys are ignored.

    Returns:
        ``{"draft_id": ..., "report_id": ..., "docx_path": ...}``
        persisted in ``background_jobs.result`` by the worker. The UI
        polls for this row via ``GET /drafts/<id>/export-status/<job>``
        and reads ``docx_path`` to build the download link.

    Raises:
        ValueError: When ``draft_id`` or ``report_id`` is missing/invalid,
            or when either row no longer exists in Postgres.
    """
    raw_draft_id = payload.get("draft_id")
    if not raw_draft_id:
        raise ValueError("export_report payload missing required 'draft_id'")
    raw_report_id = payload.get("report_id")
    if not raw_report_id:
        raise ValueError("export_report payload missing required 'report_id'")

    try:
        draft_id = UUID(str(raw_draft_id))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"export_report: invalid draft_id {raw_draft_id!r}") from exc
    try:
        report_id = UUID(str(raw_report_id))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"export_report: invalid report_id {raw_report_id!r}") from exc

    logger.info("export_report: starting for draft=%s report=%s", draft_id, report_id)

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

    docx_path = build_impact_report_docx(draft, report_row)
    logger.info(
        "export_report: wrote docx draft=%s report=%s path=%s",
        draft_id,
        report_id,
        docx_path,
    )

    return {
        "draft_id": str(draft_id),
        "report_id": str(report_id),
        "docx_path": str(docx_path),
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
#
# Importing this module triggers the real handler registration with the
# worker's dispatch registry. ``app/docs/__init__.py`` imports us at
# startup so the worker sees the real handler before claiming any job.

register_handler("export_report")(export_report)
