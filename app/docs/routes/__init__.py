"""FastHTML routes for the Phase 2 Document Upload module.

Route map:

    GET  /drafts                     — list the caller's org's drafts
    GET  /drafts/new                 — upload form
    POST /drafts                     — multipart upload handler
    GET  /drafts/{draft_id}          — draft detail page with status tracker
    GET  /drafts/{draft_id}/status   — HTMX polling fragment (status only)
    GET  /drafts/{draft_id}/actions  — HTMX fragment for the action row
    POST /drafts/{draft_id}/keep     — reset 90-day archive clock (#572)
    POST /drafts/{draft_id}/delete   — delete draft + encrypted file
    POST /drafts/{draft_id}/link-vtk — set or clear ``parent_vtk_id`` (#640)
    GET  /drafts/{draft_id}/diff     — side-by-side version diff (#618 PR-C)
    POST /drafts/{draft_id}/retry    — retry a failed draft's pipeline (#656)

All routes require authentication (they are **not** in ``SKIP_PATHS``).
The listing and detail pages additionally enforce ``draft.org_id ==
user.org_id`` for every returned record. Single-draft lookups that fail
that check return a 404 rather than a 403 so we never leak the fact
that a draft from another org exists.

Module layout (post-#704 PR-A → PR-E):

    _shared.py          — cross-cutting status / timestamp helpers (PR-B)
    _status_tracker.py  — pipeline-stage tracker renderer (PR-B)
    _upload.py          — GET /drafts/new + POST /drafts + form helpers (PR-C)
    _list.py            — GET /drafts + filter/sort state machinery (PR-D)
    _lifecycle.py       — POST /drafts/{id}/delete + /keep handlers (#703 PR-A)
    _detail.py          — GET /drafts/{id} + status / actions / link-vtk (PR-E)
    _detail_modals.py   — modal scripts + link-vtk modal builder (PR-E)
    _detail_versions.py — version timeline + diff page (PR-E)

All re-exported here so direct imports
(``from app.docs.routes import _format_elapsed``) keep working.

**Patch-path caveat (post-#704):** ``patch("app.docs.routes.X")``
rebinds the symbol in this package's namespace ONLY. Submodules import
their dependencies directly from ``_shared`` / ``app.docs.upload`` /
etc. at module load time — so a package-level patch does NOT propagate
to e.g. ``_status_tracker``'s internal calls or ``_upload``'s
``handle_upload`` / ``_connect`` references. To intercept a submodule
dependency, patch where it is USED:

  ``patch("app.docs.routes._status_tracker._poll_interval_seconds")``
  ``patch("app.docs.routes._upload.handle_upload")``
  ``patch("app.docs.routes._upload._connect")``
  ``patch("app.docs.routes._upload._validate_parent_vtk_fk")``
  ``patch("app.docs.routes._list.list_drafts_for_org_filtered")``
  ``patch("app.docs.routes._list.list_users")``
  ``patch("app.docs.routes._detail.fetch_draft")``
  ``patch("app.docs.routes._detail._connect")``
  ``patch("app.docs.routes._detail.log_draft_view")``
  ``patch("app.docs.routes._detail.list_users")``
  ``patch("app.docs.routes._detail.list_eelnous_for_vtk")``
  ``patch("app.docs.routes._detail.list_vtks_for_org")``
  ``patch("app.docs.routes._detail.update_draft_parent_vtk")``
  ``patch("app.docs.routes._detail.write_doc_lineage")``
  ``patch("app.docs.routes._detail._version_timeline_rows")``
  ``patch("app.docs.routes._detail_versions.fetch_draft")``
  ``patch("app.docs.routes._detail_versions._connect")``
  ``patch("app.docs.routes._detail_versions.list_versions_for_draft")``
  ``patch("app.docs.routes._detail_versions.log_draft_view")``

This is the standard "patch where used" rule from the Python testing
docs; the ``__all__`` block below is for direct-import convenience,
not for patch-path equivalence. Pinned by regression tests in
``tests/test_docs_routes_patch_paths.py``.
"""

from __future__ import annotations

from app.docs.routes._detail import (
    _draft_detail_body,
    _draft_metadata_block,
    _seotud_vtk_row,
    _similar_drafts_card,
    _vtk_children_card,
    draft_actions_fragment,
    draft_detail_page,
    draft_status_fragment,
    link_vtk_handler,
)
from app.docs.routes._detail_modals import (
    _DELETE_FORM_ID,
    _DELETE_MODAL_ID,
    _DELETE_MODAL_SCRIPT,
    _DELETE_TRIGGER_ID,
    _DRAFT_METADATA_ID,
    _LINK_VTK_FORM_ID,
    _LINK_VTK_MODAL_ID,
    _LINK_VTK_MODAL_SCRIPT,
    _LINK_VTK_TRIGGER_ID,
    _link_vtk_modal,
)
from app.docs.routes._detail_versions import (
    _EELNOU_INITIAL_LABEL_ET,
    _READING_STAGE_LABELS_ET,
    _diff_not_found_response,
    _format_reading_stage,
    _format_reading_stage_for_draft,
    _version_timeline_rows,
    _version_timeline_section,
    draft_diff_page,
)
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
    _VALID_DOC_TYPES,
    _doc_type_radio,
    _file_picker_script,
    _upload_form,
    _validate_parent_vtk_fk,
    _version_picker,
    _vtk_picker,
    create_draft_handler,
    new_draft_page,
)

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
    "_VALID_DOC_TYPES",
    # _upload.py helpers
    "_doc_type_radio",
    "_file_picker_script",
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
    # _detail_modals.py constants (#704 PR-E)
    "_DELETE_FORM_ID",
    "_DELETE_MODAL_ID",
    "_DELETE_MODAL_SCRIPT",
    "_DELETE_TRIGGER_ID",
    "_DRAFT_METADATA_ID",
    "_LINK_VTK_FORM_ID",
    "_LINK_VTK_MODAL_ID",
    "_LINK_VTK_MODAL_SCRIPT",
    "_LINK_VTK_TRIGGER_ID",
    # _detail_modals.py helpers
    "_link_vtk_modal",
    # _detail.py renderers (#704 PR-E)
    "_draft_detail_body",
    "_draft_metadata_block",
    "_seotud_vtk_row",
    "_similar_drafts_card",
    "_vtk_children_card",
    # _detail.py handlers
    "draft_actions_fragment",
    "draft_detail_page",
    "draft_status_fragment",
    "link_vtk_handler",
    # _detail_versions.py (#704 PR-E)
    "_EELNOU_INITIAL_LABEL_ET",
    "_READING_STAGE_LABELS_ET",
    "_diff_not_found_response",
    "_format_reading_stage",
    "_format_reading_stage_for_draft",
    "_version_timeline_rows",
    "_version_timeline_section",
    "draft_diff_page",
    # public registration entry-point
    "register_draft_routes",
]


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
