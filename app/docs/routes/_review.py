"""POST /drafts/{draft_id}/review-outcome — reviewer outcome handler (#817).

Reviewer-only endpoint that persists a ``DraftReview`` row and returns an
HTMX fragment replacing the in-page review section so the user gets
immediate feedback without a full page reload.

Routes registered (wired by :func:`app.docs.routes.register_draft_routes`):

    POST /drafts/{draft_id}/review-outcome — submit_review_outcome_handler

Authorization gate: :func:`app.auth.policy.can_review_draft` — reviewer
(or system admin) on a draft they can view. Cross-org callers and
drafters get 404 (not 403) so existence is never leaked.

**Patch-path caveat (post-#704):** ``patch("app.docs.routes.X")`` rebinds
the symbol in the package namespace ONLY. This module imports its
dependencies at module load time. To intercept a dependency, patch where
it is USED:

  ``patch("app.docs.routes._review.fetch_draft")``
  ``patch("app.docs.routes._review._connect")``
  ``patch("app.docs.routes._review.create_review")``
  ``patch("app.docs.routes._review.list_reviews_for_draft")``
  ``patch("app.docs.routes._review.log_review_outcome")``
"""

from __future__ import annotations

import logging

from fasthtml.common import to_xml
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from app.auth.helpers import require_auth as _require_auth
from app.auth.policy import can_review_draft
from app.db import get_connection as _connect
from app.docs._helpers import _not_found_page, _parse_uuid
from app.docs.audit import log_review_outcome
from app.docs.draft_model import fetch_draft
from app.docs.review_model import (
    REVIEW_OUTCOMES,
    create_review,
    list_reviews_for_draft,
)
from app.ui.surfaces.alert import Alert

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# POST /drafts/{draft_id}/review-outcome
# ---------------------------------------------------------------------------


async def submit_review_outcome_handler(req: Request, draft_id: str):
    """POST /drafts/{draft_id}/review-outcome — persist a reviewer outcome.

    Validation:

    * Auth: reviewer role + can_view_draft, via
      :func:`app.auth.policy.can_review_draft`. Cross-org or non-reviewer
      callers get 404 (not 403) so existence is never leaked.
    * ``outcome`` must be one of :data:`REVIEW_OUTCOMES`; anything else
      returns 400 with a friendly Estonian error message.
    * ``comment`` is optional. Empty / whitespace-only input is treated
      as "no comment" (NULL).

    On success:

    * Insert one row into ``draft_reviews``.
    * Emit a ``draft.review_outcome.created`` audit event (comment body
      is NOT logged — only a ``comment_present`` flag).
    * Return the refreshed review section so HTMX can ``outerHTML`` swap
      the in-page ``#draft-review-section`` div without a full reload.
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
    if not can_review_draft(auth, draft):
        # 404 (not 403) so cross-org / non-reviewer probes cannot
        # enumerate draft ids — same convention as delete_draft_handler.
        return _not_found_page(req)

    # Read form data.
    form = await req.form()
    outcome_raw = form.get("outcome", "")
    outcome = str(outcome_raw).strip() if outcome_raw else ""
    comment_raw = form.get("comment", "")
    comment = str(comment_raw) if comment_raw else ""

    if outcome not in REVIEW_OUTCOMES:
        return HTMLResponse(
            to_xml(Alert("Palun valige ülevaatuse tulemus.", variant="danger")),
            status_code=400,
        )

    # Snapshot the reviewer's display name so the UI can render
    # "Anne Tamm (kustutatud kasutaja)" if their account is later
    # removed. Fallback chain: full_name → email → "Tundmatu kasutaja".
    reviewer_name: str | None = (
        str(auth.get("full_name") or auth.get("email") or "").strip() or None
    )
    reviewer_id = auth.get("id")

    try:
        with _connect() as conn:
            create_review(
                conn,
                draft_id=parsed,
                reviewer_id=str(reviewer_id) if reviewer_id else None,
                reviewer_name=reviewer_name,
                outcome=outcome,
                comment=comment,
            )
            conn.commit()
    except Exception:
        logger.exception("Failed to persist review outcome for draft=%s", parsed)
        return HTMLResponse(
            to_xml(Alert("Tulemuse salvestamine ebaõnnestus.", variant="danger")),
            status_code=500,
        )

    log_review_outcome(
        str(reviewer_id) if reviewer_id else None,
        parsed,
        outcome=outcome,
        comment_present=bool(comment.strip()) if comment else False,
    )

    # Re-render the section so HTMX can swap it in place. Local import
    # to avoid a circular: _detail imports _review_outcome_section from
    # this module's parent (_detail.py itself) — by importing the
    # renderer lazily here we keep the route module dependency-light.
    from app.docs.routes._detail import _review_outcome_section

    try:
        with _connect() as conn:
            reviews = list_reviews_for_draft(conn, parsed)
    except Exception:
        logger.exception("Failed to reload reviews after submit for draft=%s", parsed)
        reviews = []

    return _review_outcome_section(draft, auth=auth, reviews=reviews)
