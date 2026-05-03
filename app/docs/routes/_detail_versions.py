"""Versioning UI for the detail page (#704 PR-E split).

Companion to :mod:`app.docs.routes._detail`. Holds the
"Versioonide ajalugu" (version timeline) row resolver + section
renderer plus the side-by-side diff page handler. Split off so the
main detail module stays focused on the page-level renderers /
metadata block / status + link-vtk handlers.

Routes registered (wired by :func:`app.docs.routes.register_draft_routes`):

    GET  /drafts/{draft_id}/diff   — draft_diff_page

Public-ish helpers re-exported by ``app.docs.routes.__init__`` for
back-compat:

    Constants:
        ``_READING_STAGE_LABELS_ET`` — reading_stage → Estonian label

    Helpers:
        ``_format_reading_stage``     — single-stage Estonian label lookup
        ``_version_timeline_rows``    — DB → timeline-ready row dicts
        ``_version_timeline_section`` — "Versioonide ajalugu" card
        ``_diff_not_found_response``  — shared diff-route 404 path

**Patch-path caveat (post-#704):** ``patch("app.docs.routes.X")``
rebinds the symbol in the package namespace ONLY. This module imports
its dependencies at module load time, so a package-level patch does
NOT propagate here. To intercept a versions-handler dependency, patch
where it is USED:
``patch("app.docs.routes._detail_versions.fetch_draft")``,
``patch("app.docs.routes._detail_versions._connect")``,
``patch("app.docs.routes._detail_versions.list_versions_for_draft")``,
``patch("app.docs.routes._detail_versions.log_draft_view")``, etc.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request
from starlette.responses import Response

from app.auth.helpers import require_auth as _require_auth
from app.auth.policy import can_view_draft
from app.auth.users import list_users
from app.db import get_connection as _connect
from app.docs._helpers import _not_found_page, _parse_uuid
from app.docs.audit import log_draft_view
from app.docs.draft_model import (
    Draft,
    fetch_draft,
)
from app.docs.routes._shared import (
    _format_timestamp,
    _status_badge,
)
from app.docs.version_diff import compute_diff, render_diff_table
from app.docs.version_model import (
    DraftVersion,
    list_versions_for_draft,
)
from app.ui.data.data_table import Column, DataTable
from app.ui.layout import PageShell
from app.ui.primitives.badge import Badge
from app.ui.primitives.link_button import LinkButton
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.theme import get_theme_from_request

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Reading-stage label table (#618 PR-C)
# ---------------------------------------------------------------------------


# #618 PR-C: human-readable Estonian labels for the
# ``draft_versions.reading_stage`` CHECK-constraint values.  Sourced
# from :data:`app.docs.version_model.READING_STAGES` so a missing
# label here surfaces as the raw stage key (still legible) rather
# than a KeyError.
_READING_STAGE_LABELS_ET: dict[str, str] = {
    "vtk": "VTK",
    "reading_1": "1. lugemine",
    "reading_2": "2. lugemine",
    "reading_3": "3. lugemine",
    "enacted": "Vastu võetud",
}


def _format_reading_stage(stage: str) -> str:
    """Return the Estonian label for a ``reading_stage`` value (#618 PR-C)."""
    return _READING_STAGE_LABELS_ET.get(stage, stage)


# ---------------------------------------------------------------------------
# Version timeline (rows + section renderer)
# ---------------------------------------------------------------------------


def _version_timeline_rows(
    conn: Any,
    draft_id: uuid.UUID,
    *,
    org_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Resolve ``draft_versions`` rows into timeline-ready row dicts (#618 PR-C).

    Returned rows are ordered by ``version_number ASC`` (the timeline
    reads top-down: v1 first, latest last) — the opposite of what
    :func:`list_versions_for_draft` returns. Sorting in the helper
    keeps the order in one place rather than every caller having to
    remember to reverse.

    ``created_by`` user IDs are resolved in a single ``list_users``
    call so the timeline doesn't fan out into N+1 ``get_user`` lookups
    for drafts with many readings. The lookup is org-scoped because
    ``draft_versions`` lives under the same org as the parent draft —
    cross-org user IDs simply fall through to the email-style fallback.

    Args:
        conn: Open psycopg connection (re-used by the caller's
            transaction so the read sits in the same snapshot as the
            preceding ``fetch_draft``).
        draft_id: Parent draft id.
        org_id: Org id of the parent draft. Used to scope the user
            lookup so cross-org name resolution stays impossible
            even if a stale ``created_by`` survived a user move.

    Returns:
        List of ``{"version": DraftVersion, "uploader_label": str,
        "is_first": bool}`` dicts in ``version_number ASC`` order.
        ``uploader_label`` falls back to the user's email then the raw
        UUID if the full name is missing.
    """
    versions = list_versions_for_draft(conn, draft_id)
    versions_asc = sorted(versions, key=lambda v: v.version_number)
    if not versions_asc:
        return []

    # Bulk-resolve uploader names. Org-scoped so the lookup matches the
    # invariant that draft_versions inherit org from the parent draft.
    uploaders: dict[str, dict[str, Any]] = {}
    try:
        uploaders = {str(u["id"]): u for u in list_users(org_id=str(org_id))}
    except Exception:
        # list_users already swallows DB errors and returns []; this
        # except is a defensive belt-and-braces in case the helper
        # signature changes. The timeline degrades to "uuid only".
        logger.warning(
            "version timeline: list_users failed for org=%s",
            org_id,
            exc_info=True,
        )

    rows: list[dict[str, Any]] = []
    for idx, version in enumerate(versions_asc):
        uploader = uploaders.get(str(version.created_by))
        if uploader and uploader.get("full_name"):
            uploader_label = uploader["full_name"]
        elif uploader and uploader.get("email"):
            uploader_label = uploader["email"]
        else:
            uploader_label = str(version.created_by)
        rows.append(
            {
                "version": version,
                "uploader_label": uploader_label,
                "is_first": idx == 0,
            }
        )
    return rows


def _version_timeline_section(
    draft: Draft,
    timeline_rows: list[dict[str, Any]],
) -> Any:
    """Render the "Versioonide ajalugu" card (#618 PR-C).

    Returns an empty string when no versions exist for the draft. In
    practice migration 030's backfill guarantees every draft has at
    least one version, but the empty-state branch keeps the helper
    safe to call from tests that omit the version row.
    """
    if not timeline_rows:
        return Card(
            CardHeader(H3("Versioonide ajalugu", cls="card-title")),  # noqa: F405
            CardBody(
                P(  # noqa: F405
                    "Sellel eelnõul ei ole salvestatud versioone.",
                    cls="version-timeline-empty",
                ),
            ),
            cls="version-timeline-card",
        )

    def _version_number_cell(row: dict[str, Any]) -> Any:
        version: DraftVersion = row["version"]
        return Span(f"v{version.version_number}", cls="version-number")  # noqa: F405

    def _stage_cell(row: dict[str, Any]) -> Any:
        version: DraftVersion = row["version"]
        return Badge(
            _format_reading_stage(version.reading_stage),
            variant="primary",
            cls=f"reading-stage reading-stage-{version.reading_stage}",
        )

    def _created_cell(row: dict[str, Any]) -> Any:
        version: DraftVersion = row["version"]
        return _format_timestamp(version.created_at)

    def _uploader_cell(row: dict[str, Any]) -> Any:
        return row["uploader_label"]

    def _status_cell(row: dict[str, Any]) -> Any:
        version: DraftVersion = row["version"]
        return _status_badge(version.status)

    def _actions_cell(row: dict[str, Any]) -> Any:
        version: DraftVersion = row["version"]
        # "Ava" — link to the report scoped to this version. The
        # report route ignores the unknown query param today; PR-D
        # (out of scope here) will wire it through to per-version
        # rendering. Surfacing the link now keeps the timeline
        # actionable from day one.
        actions: list[Any] = [
            LinkButton(
                "Ava",
                href=f"/drafts/{draft.id}/report?version={version.id}",
                variant="secondary",
                size="sm",
            ),
        ]
        # "Erinevus" — diff this version against the previous one.
        # Spec: from=v-1 to=v, addressed by version_number not UUID.
        # v1 has no predecessor so we omit the button; the markup
        # also reads cleaner than a disabled-button stub.
        if not row["is_first"]:
            actions.append(
                LinkButton(
                    "Erinevus",
                    href=(
                        f"/drafts/{draft.id}/diff"
                        f"?from={version.version_number - 1}"
                        f"&to={version.version_number}"
                    ),
                    variant="secondary",
                    size="sm",
                )
            )
        return Span(*actions, cls="version-timeline-actions")  # noqa: F405

    columns = [
        Column(
            key="version_number", label="Versioon", sortable=False, render=_version_number_cell
        ),
        Column(key="stage", label="Lugemine", sortable=False, render=_stage_cell),
        Column(key="created_at", label="Üles laaditud", sortable=False, render=_created_cell),
        Column(key="uploader", label="Üleslaadija", sortable=False, render=_uploader_cell),
        Column(key="status", label="Staatus", sortable=False, render=_status_cell),
        Column(key="actions", label="Tegevused", sortable=False, render=_actions_cell),
    ]

    return Card(
        CardHeader(H3("Versioonide ajalugu", cls="card-title")),  # noqa: F405
        CardBody(DataTable(columns, timeline_rows)),
        cls="version-timeline-card",
    )


# ---------------------------------------------------------------------------
# GET /drafts/{draft_id}/diff — side-by-side diff page (#618 PR-C)
# ---------------------------------------------------------------------------


def _diff_not_found_response(req: Request) -> Any:
    """404 page used whenever the diff route can't resolve a version (#618 PR-C).

    Re-uses :func:`_not_found_page` so the rendered shell and copy
    match the rest of the drafts module — no version-specific
    "not found" leak hints at whether a missing slot exists.
    """
    return _not_found_page(req)


def draft_diff_page(req: Request, draft_id: str):
    """GET /drafts/{draft_id}/diff?from=<v1>&to=<v2> — side-by-side diff.

    The ``from`` and ``to`` query parameters are **version numbers**
    (1-based ints), not UUIDs — easier to type, link, and bookmark.
    Out-of-range or non-numeric values render the not-found page so
    we never leak whether a particular version ever existed.

    Org scoping is enforced by ``can_view_draft`` against the parent
    draft (which is the source of truth for org membership;
    ``draft_versions`` inherits org from its parent). Cross-org
    callers receive a 404 page rather than a 403 so the route never
    reveals draft existence.

    The handler decrypts both versions' parsed text via
    :func:`app.storage.encrypted.decrypt_text`, runs them through
    :func:`compute_diff`, and renders the result with
    :func:`render_diff_table` inside a :func:`PageShell` so the
    diff opens directly from the timeline as a full page.
    """
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect
    theme = get_theme_from_request(req)

    parsed = _parse_uuid(draft_id)
    if parsed is None:
        return _diff_not_found_response(req)

    draft = fetch_draft(parsed)
    if draft is None or not can_view_draft(auth, draft):
        # 404 (not 403) so cross-org probing can't enumerate ids.
        return _diff_not_found_response(req)

    # Parse + validate the version-number query params. Both must
    # exist, both must parse as positive ints, and from < to. We
    # silently swap a reversed pair so deep links from the old
    # timeline shape (where the action read "v2 vs v1") still work;
    # this matches the spec's "from=v2&to=v1 swap" branch.
    qp = req.query_params
    from_raw = qp.get("from", "")
    to_raw = qp.get("to", "")
    try:
        from_num = int(from_raw)
        to_num = int(to_raw)
    except (TypeError, ValueError):
        return _diff_not_found_response(req)
    if from_num <= 0 or to_num <= 0:
        return _diff_not_found_response(req)
    if from_num == to_num:
        # Diff against itself: nothing useful to render. Redirect
        # callers back to the detail page rather than serving an
        # all-unchanged table that just wastes a page-load.
        return _diff_not_found_response(req)
    if from_num > to_num:
        from_num, to_num = to_num, from_num

    # Resolve both versions inside one connection so the lookups
    # share a snapshot. ``list_versions_for_draft`` is cheaper than
    # two version-number-keyed queries and gives us the existence
    # check + decrypt source in a single pass.
    try:
        with _connect() as conn:
            versions = list_versions_for_draft(conn, draft.id)
    except Exception:
        logger.exception(
            "draft_diff_page: failed to list versions for draft=%s",
            draft.id,
        )
        return _diff_not_found_response(req)

    by_number: dict[int, DraftVersion] = {v.version_number: v for v in versions}
    left_version = by_number.get(from_num)
    right_version = by_number.get(to_num)
    if left_version is None or right_version is None:
        return _diff_not_found_response(req)

    # Decrypt both texts. A missing ``parsed_text_encrypted`` (the
    # parse pipeline hasn't run yet for this version) collapses to
    # an empty string so the diff degrades gracefully — the UI
    # surfaces the version's status badge anyway in the timeline.
    from app.storage.encrypted import decrypt_text

    def _decrypt_or_empty(version: DraftVersion) -> str:
        if version.parsed_text_encrypted is None:
            return ""
        try:
            return decrypt_text(version.parsed_text_encrypted)
        except Exception:
            logger.warning(
                "draft_diff_page: decrypt failed for version=%s draft=%s",
                version.id,
                draft.id,
                exc_info=True,
            )
            return ""

    left_text = _decrypt_or_empty(left_version)
    right_text = _decrypt_or_empty(right_version)
    diff_rows = compute_diff(left_text, right_text)

    # Audit log: surfacing an old version of a sensitive draft is a
    # meaningful access event. Reuses the existing draft-view logger
    # rather than minting a new audit code so dashboards keep working.
    try:
        log_draft_view(auth.get("id"), draft.id)
    except Exception:
        logger.warning("draft_diff_page: log_draft_view failed", exc_info=True)

    title = f"{draft.title} — versioonide erinevus v{from_num} → v{to_num}"

    return PageShell(
        H1(  # noqa: F405
            f"Versioonide erinevus v{from_num} → v{to_num}",
            cls="page-title",
        ),
        P(  # noqa: F405
            A(  # noqa: F405
                f"← Tagasi eelnõu juurde: {draft.title}",
                href=f"/drafts/{draft.id}",
            ),
            cls="back-link",
        ),
        Card(
            CardHeader(
                H3(  # noqa: F405
                    (
                        f"v{from_num} ({_format_reading_stage(left_version.reading_stage)}) "
                        f"võrreldes v{to_num} "
                        f"({_format_reading_stage(right_version.reading_stage)})"
                    ),
                    cls="card-title",
                ),
            ),
            CardBody(render_diff_table(diff_rows)),
        ),
        title=title,
        user=auth,
        theme=theme,
        active_nav="/drafts",
        request=req,
    )
