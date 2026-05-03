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
from typing import Any

from fasthtml.common import *  # noqa: F403
from fasthtml.common import to_xml
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from app.auth.audit import log_action
from app.auth.helpers import require_auth as _require_auth
from app.auth.policy import can_delete_draft, can_edit_draft, can_view_draft
from app.auth.users import list_users
from app.db import get_connection as _connect
from app.docs._helpers import _not_found_page, _parse_uuid
from app.docs.audit import (
    log_draft_view,
)
from app.docs.draft_model import (
    Draft,
    fetch_draft,
    list_eelnous_for_vtk,
    list_vtks_for_org,
    touch_draft_access_conn,
    update_draft_parent_vtk,
)
from app.docs.graph_builder import write_doc_lineage
from app.docs.routes._lifecycle import delete_draft_handler, keep_draft_handler
from app.docs.routes._list import (
    _DOC_TYPE_BADGE,
    _DOC_TYPE_CHOICES,
    _DOC_TYPE_VALUES,
    _SORT_CHOICES,
    _SORT_VALUES,
    _STATUS_VALUES,
    _draft_list_columns,
    _draft_rows,
    _drafts_table_section,
    _filter_bar,
    _filter_querystring,
    _has_active_filters,
    _parse_date_param,
    _parse_filters_from_request,
    drafts_list_page,
    list_drafts_for_org_filtered,
)
from app.docs.routes._shared import (
    _DELETE_CONFIRM,
    _PAGE_SIZE,
    _POLLING_TIMEOUT_SECONDS,
    _STALE_THRESHOLD_DAYS,
    _STATUS_STAGES,
    _TYPICAL_STAGE_SECONDS,
    _elapsed_seconds,
    _format_elapsed,
    _format_elapsed_final,
    _format_timestamp,
    _is_draft_stale,
    _is_status_polling_stale,
    _poll_interval_seconds,
    _processing_duration_seconds,
    _status_badge,
)
from app.docs.routes._status_tracker import _status_tracker
from app.docs.routes._upload import (
    _DOC_TYPE_TOGGLE_SCRIPT,
    _FILE_PICKER_SCRIPT,
    _UPLOAD_MAX_BYTES,
    _VALID_DOC_TYPES,
    _doc_type_radio,
    _upload_form,
    _validate_parent_vtk_fk,
    _version_picker,
    _vtk_picker,
    create_draft_handler,
    new_draft_page,
)
from app.docs.similarity import list_similar_drafts_for_view
from app.docs.version_diff import compute_diff, render_diff_table
from app.docs.version_model import (
    DraftVersion,
    list_versions_for_draft,
)
from app.ui.data.data_table import Column, DataTable
from app.ui.feedback.empty_state import EmptyState
from app.ui.layout import PageShell
from app.ui.primitives.annotation_button import AnnotationButton
from app.ui.primitives.badge import Badge
from app.ui.primitives.button import Button
from app.ui.primitives.link_button import LinkButton
from app.ui.surfaces.alert import Alert
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.surfaces.info_box import InfoBox
from app.ui.surfaces.modal import ConfirmModal, Modal, ModalBody, ModalFooter, ModalScript
from app.ui.theme import get_theme_from_request

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Re-exports (#704 PR-B / PR-C / PR-D)
# ---------------------------------------------------------------------------
#
# The cross-cutting status / timestamp / pipeline helpers
# (``_status_badge``, ``_format_timestamp``, ``_is_draft_stale``,
# ``_elapsed_seconds``, ``_format_elapsed`` and friends) plus the
# ``_PAGE_SIZE`` / ``_DELETE_CONFIRM`` / ``_STALE_THRESHOLD_DAYS`` /
# ``_POLLING_TIMEOUT_SECONDS`` / ``_TYPICAL_STAGE_SECONDS`` /
# ``_STATUS_STAGES`` constants now live in
# :mod:`app.docs.routes._shared`. The big ``_status_tracker`` renderer
# moved to :mod:`app.docs.routes._status_tracker`.  PR-C moved the
# upload form helpers (``_doc_type_radio``, ``_vtk_picker``,
# ``_version_picker``, ``_upload_form``, ``_validate_parent_vtk_fk``),
# the upload-form constants (``_UPLOAD_MAX_BYTES``,
# ``_FILE_PICKER_SCRIPT``, ``_DOC_TYPE_TOGGLE_SCRIPT``,
# ``_VALID_DOC_TYPES``), and the two route handlers (``new_draft_page``,
# ``create_draft_handler``) into :mod:`app.docs.routes._upload`.  PR-D
# moved the listing-page surface — ``drafts_list_page`` (GET /drafts),
# the row shapers (``_draft_rows`` / ``_draft_list_columns`` /
# ``_DOC_TYPE_BADGE``), the filter-bar state machinery
# (``_DOC_TYPE_CHOICES`` / ``_DOC_TYPE_VALUES`` / ``_STATUS_VALUES``
# / ``_SORT_CHOICES`` / ``_SORT_VALUES``, ``_parse_date_param``,
# ``_parse_filters_from_request``, ``_filter_querystring``,
# ``_has_active_filters``), the rendered ``_filter_bar``, and the
# ``_drafts_table_section`` HTMX wrapper — into
# :mod:`app.docs.routes._list`.  All four submodules are re-exported
# here so direct imports (``from app.docs.routes import _format_elapsed``)
# keep working.
#
# **Patch-path caveat (post-#704):** ``patch("app.docs.routes.X")``
# rebinds the symbol in this package's namespace ONLY. Submodules
# import their dependencies directly from ``_shared`` /
# ``app.docs.upload`` / etc. at module load time — so a package-level
# patch does NOT propagate to e.g. ``_status_tracker``'s internal
# calls or ``_upload``'s ``handle_upload`` / ``_connect`` references,
# or ``_list``'s ``list_drafts_for_org_filtered`` / ``list_users``
# references. To intercept a submodule dependency, patch where it is
# USED:
#
#   ``patch("app.docs.routes._status_tracker._poll_interval_seconds")``
#   ``patch("app.docs.routes._upload.handle_upload")``
#   ``patch("app.docs.routes._upload._connect")``
#   ``patch("app.docs.routes._upload._validate_parent_vtk_fk")``
#   ``patch("app.docs.routes._list.list_drafts_for_org_filtered")``
#   ``patch("app.docs.routes._list.list_users")``
#
# Same rule applies to any future submodule extracted in PR-E.
# This is the standard "patch where used" rule from the Python testing
# docs; the ``__all__`` block below is for direct-import convenience,
# not for patch-path equivalence. Pinned by regression tests in
# ``tests/test_docs_routes_patch_paths.py``.

__all__ = [
    # _shared.py constants
    "_DELETE_CONFIRM",
    "_PAGE_SIZE",
    "_POLLING_TIMEOUT_SECONDS",
    "_STALE_THRESHOLD_DAYS",
    "_STATUS_STAGES",
    "_TYPICAL_STAGE_SECONDS",
    # _shared.py helpers
    "_elapsed_seconds",
    "_format_elapsed",
    "_format_elapsed_final",
    "_format_timestamp",
    "_is_draft_stale",
    "_is_status_polling_stale",
    "_poll_interval_seconds",
    "_processing_duration_seconds",
    "_status_badge",
    # _status_tracker.py
    "_status_tracker",
    # _upload.py constants
    "_DOC_TYPE_TOGGLE_SCRIPT",
    "_FILE_PICKER_SCRIPT",
    "_UPLOAD_MAX_BYTES",
    "_VALID_DOC_TYPES",
    # _upload.py helpers
    "_doc_type_radio",
    "_upload_form",
    "_validate_parent_vtk_fk",
    "_version_picker",
    "_vtk_picker",
    # _upload.py handlers
    "create_draft_handler",
    "new_draft_page",
    # _list.py constants
    "_DOC_TYPE_BADGE",
    "_DOC_TYPE_CHOICES",
    "_DOC_TYPE_VALUES",
    "_SORT_CHOICES",
    "_SORT_VALUES",
    "_STATUS_VALUES",
    # _list.py helpers
    "_draft_list_columns",
    "_draft_rows",
    "_drafts_table_section",
    "_filter_bar",
    "_filter_querystring",
    "_has_active_filters",
    "_parse_date_param",
    "_parse_filters_from_request",
    # _list.py handler
    "drafts_list_page",
    # _list.py back-compat re-exports (was imported into __init__ pre-#704)
    "list_drafts_for_org_filtered",
    # _lifecycle.py (re-exported for symmetry; #703 already moved these)
    "delete_draft_handler",
    "keep_draft_handler",
    # public registration entry-point
    "register_draft_routes",
]


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
# Helpers
# ---------------------------------------------------------------------------
# ``_parse_uuid`` and ``_not_found_page`` now live in
# :mod:`app.docs._helpers` so :mod:`app.docs.retry_handler` can import
# them without triggering an import cycle with this module (#671).


# ---------------------------------------------------------------------------
# GET /drafts — listing
# ---------------------------------------------------------------------------
#
# The listing handler ``drafts_list_page`` together with the row
# shapers (``_draft_rows``, ``_draft_list_columns``,
# ``_DOC_TYPE_BADGE``), the filter-bar state machinery
# (``_DOC_TYPE_CHOICES`` / ``_DOC_TYPE_VALUES`` / ``_STATUS_VALUES``
# / ``_SORT_CHOICES`` / ``_SORT_VALUES``, ``_parse_date_param``,
# ``_parse_filters_from_request``, ``_filter_querystring``,
# ``_has_active_filters``), the rendered ``_filter_bar`` form, and
# the ``_drafts_table_section`` HTMX wrapper now all live in
# :mod:`app.docs.routes._list` (#704 PR-D).  They are imported above
# and re-exported via ``__all__`` so direct callers (and pre-#704
# ``patch("app.docs.routes.X")`` test sites) keep working — see the
# patch-path caveat in the re-export comment above.


# ---------------------------------------------------------------------------
# GET /drafts/new — upload form
# POST /drafts — create handler
# ---------------------------------------------------------------------------
#
# Both handlers, the multipart upload form, the inline JS payloads,
# the VTK / version pickers, and the upfront ``parent_vtk_id``
# validation all live in :mod:`app.docs.routes._upload` (#704 PR-C).
# The two helpers ``_vtk_picker`` and ``_validate_parent_vtk_fk`` are
# also called by the link-vtk modal + handler that still live below
# in this module — they are imported above and re-exported via
# ``__all__`` so direct callers (and pre-#704 ``patch("app.docs.
# routes.X")`` test sites) keep working.


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


def _similar_drafts_card(similar: list[dict]) -> Any:
    """Render the "Sarnased eelnõud" card from ``list_similar_drafts_for_view`` output.

    Within-org rows show a link and a score badge.
    Cross-org rows are masked: only an aggregate count is shown with no
    titles or links.

    The renderer asserts ``title is None`` before rendering masked rows
    as a defence-in-depth guard (the DB already returns NULL for
    cross-org titles, so this assertion should never fire).
    """
    within_org = [r for r in similar if not r.get("masked")]
    cross_org = [r for r in similar if r.get("masked")]

    items: list[Any] = []

    for row in within_org:
        # Sanity-check: the DB masking query should never give us a
        # cross-org row with a title.  If it does, treat it as masked.
        assert row["title"] is not None, (
            "similar_drafts: expected title for within-org row but got None"
        )
        pct = f"{row['score'] * 100:.0f}%"
        items.append(
            Div(  # noqa: F405
                A(  # noqa: F405
                    row["title"],
                    href=f"/drafts/{row['similar_draft_id']}",
                    cls="similar-draft-link",
                ),
                Badge(pct, variant="default", cls="score-badge"),
                cls="similar-draft-row",
            )
        )

    if cross_org:
        count = len(cross_org)
        label = (
            f"{count} sarnast eelnõu teistes ministeeriumites"
            if count > 1
            else "1 sarnane eelnõu teises ministeeriumis"
        )
        items.append(
            P(  # noqa: F405
                label,
                cls="similar-draft-cross-org-note",
            )
        )

    if not items:
        return ""

    return Card(
        CardHeader(H3("Sarnased eelnõud", cls="card-title")),  # noqa: F405
        CardBody(*items),
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
            LinkButton(
                "Vaata mõjuaruannet",
                href=f"/drafts/{draft.id}/report",
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

    # #621: "Sarnased eelnõud" — best-effort; empty list on any DB error.
    similar: list[dict] = []
    viewer_org_id = auth.get("org_id") if auth else None
    if viewer_org_id:
        try:
            with _connect() as conn:
                similar = list_similar_drafts_for_view(conn, str(draft.id), str(viewer_org_id))
            if similar:
                log_action(
                    str(auth.get("id")) if auth else None,
                    "draft.similar.view",
                    {"draft_id": str(draft.id), "count": len(similar)},
                )
        except Exception:
            logger.warning(
                "draft_detail_page: failed to load similar drafts for draft=%s",
                draft.id,
                exc_info=True,
            )

    # #618 PR-C: "Versioonide ajalugu" — best-effort; an empty list
    # collapses to a friendly empty-state card so a dead DB never
    # bricks the detail page. Org-scoped via the parent draft's
    # ``org_id``, which transitively scopes the version rows.
    version_timeline_rows: list[dict[str, Any]] = []
    try:
        with _connect() as conn:
            version_timeline_rows = _version_timeline_rows(conn, draft.id, org_id=draft.org_id)
    except Exception:
        logger.warning(
            "draft_detail_page: failed to load version timeline for draft=%s",
            draft.id,
            exc_info=True,
        )

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
        # #621: similar-drafts card; hidden (returns "") when no results.
        _similar_drafts_card(similar),
        # #618 PR-C: "Versioonide ajalugu" — one row per draft_versions
        # entry, ordered v1 → latest. Always rendered (even with one
        # version) so the user has a stable reference for the diff
        # button once a v2 lands.
        _version_timeline_section(draft, version_timeline_rows),
        # #643: VTK-only card listing follow-on eelnõud. Skipped on
        # eelnõu detail since VTKs are the only doc_type that can have
        # children in our model.
        _vtk_children_card(draft, children=vtk_children, uploader_index=uploader_index)
        if draft.doc_type == "vtk"
        else "",
        # #608: client-side WS listener for live status pushes.
        # Self-initialises off the data-draft-id marker on the
        # status-tracker Div; gracefully no-ops if the WS fails so
        # the existing 3s polling continues unaffected.
        Script(src="/static/js/draft-status.js", defer=True),  # noqa: F405
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
# Versioning UI (#618 PR-C) — timeline section + diff page
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
    # #618 PR-C: side-by-side diff between two versions of a draft.
    rt("/drafts/{draft_id}/diff", methods=["GET"])(draft_diff_page)
    # #656: retry a failed draft's pipeline from the parse stage.
    from app.docs.retry_handler import retry_draft_handler

    rt("/drafts/{draft_id}/retry", methods=["POST"])(retry_draft_handler)
