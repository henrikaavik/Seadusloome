"""Retry handler for failed drafts (#656).

When a draft's pipeline run lands in ``status='failed'`` the user has
no way to re-run the pipeline short of re-uploading the original file.
This module exposes a single POST endpoint that re-enqueues the
pipeline from the very first stage (``parse_draft``) after clearing
the ``error_message`` / ``error_debug`` columns and flipping the draft
back to the ``uploaded`` state.

Why enqueue from ``parse_draft`` instead of the failed stage?

* The pipeline handlers (``parse_handler``, ``extract_handler``,
  ``analyze_handler``) each clear downstream rows as they run, so
  starting from ``parse_draft`` is always safe.
* The failing draft in #656 was created before the
  ``app.docs.error_mapping`` module classified failures, so we cannot
  reliably tell from ``error_message`` alone which stage produced the
  failure. Rather than guessing and risking an inconsistent partial
  re-run, we take the conservative "start over from the top" path.
* A smarter resume-from-failed-stage behaviour can layer in later
  once ``drafts.error_stage`` exists; today's implementation is
  intentionally dumb-but-correct.

Security:
* Owner / same-org viewer authorization mirrors
  :func:`app.docs.report_routes.reanalyze_report_handler`.
* Cross-org drafts resolve to the 404 page (never 403) so existence
  is never leaked.
* The DB mutation is the single UPDATE that resets status + clears
  both error columns. The enqueue runs only if the UPDATE succeeded;
  a failed enqueue leaves the draft in ``uploaded`` state which the
  operator can recover manually.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from app.auth.audit import log_action
from app.auth.helpers import require_auth as _require_auth
from app.auth.policy import can_edit_draft
from app.db import get_connection as _connect
from app.docs.draft_model import fetch_draft
from app.jobs.queue import JobQueue

logger = logging.getLogger(__name__)


def _reset_draft_for_retry(draft_id: str) -> bool:
    """Flip a failed draft back to ``uploaded`` and clear error columns.

    Returns ``True`` when the DB row was actually updated (i.e. the
    draft was in ``status='failed'`` when we looked). Returns ``False``
    when the row had already moved out of ``failed`` — typically a
    concurrent retry from another tab, or the operator ran one by
    hand. In that case the caller should still treat the retry as a
    best-effort no-op: the queue may already carry a pending job.

    The WHERE clause guards against double-resetting a draft whose
    pipeline is already running.
    """
    with _connect() as conn:
        result = conn.execute(
            """
            update drafts
            set status = 'uploaded',
                error_message = null,
                error_debug = null,
                updated_at = now()
            where id = %s
              and status = 'failed'
            """,
            (draft_id,),
        )
        conn.commit()
        return (result.rowcount or 0) > 0


def retry_draft_handler(req: Request, draft_id: str):
    """POST /drafts/{draft_id}/retry — re-enqueue the pipeline from parse.

    The button that fires this lives on the draft detail page and is
    only rendered when ``draft.status == 'failed'``. The handler is
    defensive and revalidates that condition server-side so a stale
    open tab cannot re-enqueue a pipeline that's already running.

    Authorization: same-org editor per :func:`can_edit_draft`. We
    cannot use ``can_view_draft`` because retry is a state-mutating
    action that should be gated on the edit policy, not the read one.
    """
    # Local imports avoid an import cycle at module-load time:
    # ``app.docs.routes`` registers this handler and we need the
    # helpers it defines without pulling them at import time.
    from app.docs.routes import _not_found_page, _parse_uuid

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
    # 404 (not 403) on cross-org / non-editor access so we never leak
    # the fact that someone else's draft exists (matches delete/link).
    if not can_edit_draft(auth, draft):
        return _not_found_page(req)

    # Revalidate terminal state server-side. A duplicate POST from a
    # stale tab after the pipeline already restarted must be a no-op —
    # not a second concurrent run that fights the first.
    if draft.status != "failed":
        logger.info(
            "retry_draft_handler: draft=%s is not in 'failed' state (status=%s) — ignoring",
            parsed,
            draft.status,
        )
        if req.headers.get("HX-Request") == "true":
            return Response(
                status_code=204,
                headers={"HX-Redirect": f"/drafts/{parsed}"},
            )
        return RedirectResponse(url=f"/drafts/{parsed}", status_code=303)

    # Reset the error columns in the same UPDATE as the status flip
    # so HTMX polls never observe ``status='uploaded'`` with a stale
    # ``error_message`` attached.
    try:
        reset_ok = _reset_draft_for_retry(str(parsed))
    except Exception:
        logger.exception("Failed to reset draft=%s for retry", parsed)
        return _not_found_page(req)

    if not reset_ok:
        # A concurrent writer beat us to the reset. Treat as a no-op.
        logger.info(
            "retry_draft_handler: draft=%s reset returned 0 rows (race) — redirecting",
            parsed,
        )
        if req.headers.get("HX-Request") == "true":
            return Response(
                status_code=204,
                headers={"HX-Redirect": f"/drafts/{parsed}"},
            )
        return RedirectResponse(url=f"/drafts/{parsed}", status_code=303)

    # Enqueue the first pipeline stage. Downstream handlers clear
    # their own rows on each run so we never carry stale entities or
    # reports forward.
    try:
        job_id = JobQueue().enqueue(
            "parse_draft",
            {"draft_id": str(parsed)},
            priority=5,
        )
    except Exception:
        logger.exception("Failed to enqueue parse_draft retry for draft=%s", parsed)
        # The row is already back in ``uploaded`` state; the operator
        # can trigger another retry. We avoid flipping it back to
        # ``failed`` because the original failure reason is gone.
        return _not_found_page(req)

    log_action(
        auth.get("id"),
        "draft.retry",
        {"draft_id": str(parsed), "job_id": job_id},
    )
    logger.info(
        "Retry pipeline enqueued draft=%s job_id=%s user=%s",
        parsed,
        job_id,
        auth.get("id"),
    )

    # HTMX / non-HTMX both redirect back to the detail page — the
    # status tracker there will pick up the freshly-enqueued run.
    if req.headers.get("HX-Request") == "true":
        return Response(
            status_code=204,
            headers={"HX-Redirect": f"/drafts/{parsed}"},
        )
    return RedirectResponse(url=f"/drafts/{parsed}", status_code=303)
