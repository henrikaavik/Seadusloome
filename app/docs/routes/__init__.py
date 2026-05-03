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
from datetime import date
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
    log_draft_upload,
    log_draft_view,
)
from app.docs.draft_model import (
    DEFAULT_SORT,
    Draft,
    fetch_draft,
    list_drafts_for_org_filtered,
    list_eelnous_for_vtk,
    list_versionable_drafts_for_org,
    list_vtks_for_org,
    touch_draft_access_conn,
    update_draft_parent_vtk,
)
from app.docs.graph_builder import write_doc_lineage
from app.docs.routes._lifecycle import delete_draft_handler, keep_draft_handler
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
from app.docs.similarity import list_similar_drafts_for_view
from app.docs.status import (
    STATUS_BY_VALUE,
)
from app.docs.status import (
    VALID_STATUSES as _VALID_STATUSES,
)
from app.docs.upload import DraftUploadError, handle_upload
from app.docs.version_diff import compute_diff, render_diff_table
from app.docs.version_model import (
    DraftVersion,
    list_versions_for_draft,
)
from app.ui.data.data_table import Column, DataTable
from app.ui.data.pagination import Pagination
from app.ui.feedback.empty_state import EmptyState
from app.ui.feedback.flash import push_flash
from app.ui.layout import PageShell
from app.ui.primitives.annotation_button import AnnotationButton
from app.ui.primitives.badge import Badge, BadgeVariant
from app.ui.primitives.button import Button
from app.ui.primitives.link_button import LinkButton
from app.ui.surfaces.alert import Alert
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.surfaces.info_box import InfoBox
from app.ui.surfaces.modal import ConfirmModal, Modal, ModalBody, ModalFooter, ModalScript
from app.ui.theme import get_theme_from_request

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Re-exports (#704 PR-B)
# ---------------------------------------------------------------------------
#
# The cross-cutting status / timestamp / pipeline helpers
# (``_status_badge``, ``_format_timestamp``, ``_is_draft_stale``,
# ``_elapsed_seconds``, ``_format_elapsed`` and friends) plus the
# ``_PAGE_SIZE`` / ``_DELETE_CONFIRM`` / ``_STALE_THRESHOLD_DAYS`` /
# ``_POLLING_TIMEOUT_SECONDS`` / ``_TYPICAL_STAGE_SECONDS`` /
# ``_STATUS_STAGES`` constants now live in
# :mod:`app.docs.routes._shared` so the upcoming ``_list``/``_upload``
# /``_detail`` submodules can import them without re-pulling the
# package's full dependency graph (#704 PR-B). The big
# ``_status_tracker`` renderer moved to
# :mod:`app.docs.routes._status_tracker`.  Both modules are re-exported
# from this package so existing test patches and imports
# (``from app.docs.routes import _format_elapsed``,
# ``patch("app.docs.routes._is_draft_stale")``) keep working without
# any patch-path swap.

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
        return LinkButton(
            "Vaata",
            href=f"/drafts/{row['id']}",
            variant="secondary",
            size="sm",
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

# Status checkbox group -- same six values used by the pipeline.
# Sourced from :data:`app.docs.status.VALID_STATUSES` (#625 §4.2 SSOT)
# so the filter bar picks up new statuses automatically.
_STATUS_VALUES: tuple[str, ...] = _VALID_STATUSES

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
        spec = STATUS_BY_VALUE.get(value)
        status_inputs.append(
            Label(  # noqa: F405
                Input(**attrs),  # noqa: F405
                Span(spec.label_et if spec else value),  # noqa: F405
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
                action=LinkButton(
                    "Lähtesta filtrid",
                    href="/drafts",
                    variant="secondary",
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
                action=LinkButton(
                    "Laadi üles uus eelnõu",
                    href="/drafts/new",
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
            LinkButton(
                "Laadi üles uus eelnõu",
                href="/drafts/new",
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


def _version_picker(
    versionable_drafts: list[Draft],
    *,
    selected: str | None = None,
):
    """Render the "Versioon olemasolevast eelnõust" picker (#618 PR-B).

    Optional select that lets the uploader create a NEW version of an
    existing ``ready``-status draft instead of a brand-new draft.  When
    the picker is left at the default empty option, the upload follows
    the legacy "new draft" branch.

    Empty list -> the picker still renders but only shows the "Uus
    eelnõu (pole versioon)" sentinel; this keeps the DOM structure
    deterministic regardless of how many parents are eligible.
    """
    selected_str = str(selected) if selected else ""
    options: list = [
        Option(  # noqa: F405
            "Uus eelnõu (pole versioon)",
            value="",
            selected=(selected_str == ""),
        )
    ]
    for existing in versionable_drafts:
        existing_id = str(existing.id)
        # Truncate long titles so the dropdown stays readable.  60 chars
        # matches the brief; the trailing ellipsis is added when truncation
        # actually fires.
        title = existing.title or existing.filename or existing_id
        display_title = title if len(title) <= 60 else f"{title[:60]}…"
        options.append(
            Option(  # noqa: F405
                f"Versioon eelnõust: {display_title}",
                value=existing_id,
                selected=(existing_id == selected_str),
            )
        )
    return Div(  # noqa: F405
        Label(  # noqa: F405
            "Versioneerimine",
            fr="field-parent-draft",
            cls="form-field-label",
        ),
        Select(  # noqa: F405
            *options,
            name="parent_draft_id",
            id="field-parent-draft",
            cls="input input-select",
        ),
        Small(  # noqa: F405
            "Kui valid olemasoleva eelnõu, salvestatakse fail uue versioonina, "
            "mitte uue eelnõuna.",
            cls="form-field-help",
        ),
        cls="form-field",
    )


def _upload_form(
    *,
    title_value: str = "",
    error: str | None = None,
    vtks: list[Draft] | None = None,
    versionable_drafts: list[Draft] | None = None,
    doc_type_value: str = "eelnou",
    parent_vtk_id_value: str | None = None,
    parent_draft_id_value: str | None = None,
):
    """Render the multipart upload form.

    IMPORTANT: this form uses the raw ``Form`` primitive from
    ``fasthtml.common`` rather than :class:`AppForm` because file uploads
    **must** use ``enctype="multipart/form-data"``. AppForm defaults to
    ``application/x-www-form-urlencoded`` and would silently drop the file.

    #640: adds a "Dokumendi tüüp" radio group and a "Seotud VTK"
    ``<select>`` populated with the caller's org's VTKs. Validation of
    both fields happens server-side in ``create_draft_handler``.

    #618 PR-B: adds the optional "Versioneerimine" picker so the
    uploader can create a new version of an existing ``ready`` draft
    instead of a brand-new draft.  When the picker is empty the form
    behaves exactly like before; when populated the route handler
    routes the upload through the new-version branch in
    :func:`app.docs.upload.handle_upload`.
    """
    error_alert = Alert(error, variant="danger") if error else None
    picker_disabled = doc_type_value == "vtk"
    vtk_list = vtks or []
    versionable_list = versionable_drafts or []

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
                "Kuni 200 tähemärki. Uue versiooni puhul päritakse pealkiri vanemalt eelnõult.",
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
        _version_picker(
            versionable_list,
            selected=parent_draft_id_value,
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
            LinkButton("Tühista", href="/drafts", variant="ghost"),
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
    #
    # #618 PR-B: also populate the "Versioneerimine" picker with every
    # ``ready``-status draft in the same org so the uploader can target
    # an existing draft for a follow-on version.
    org_id_str = str(auth["org_id"])
    vtks = list_vtks_for_org(org_id_str)
    try:
        with _connect() as conn:
            versionable = list_versionable_drafts_for_org(conn, org_id_str)
    except Exception:
        logger.exception("Failed to load versionable drafts for org=%s", org_id_str)
        versionable = []
    form, error_alert = _upload_form(vtks=vtks, versionable_drafts=versionable)
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

    #618 PR-B: additionally accepts an optional ``parent_draft_id`` --
    when supplied the upload becomes a NEW VERSION of the targeted
    draft (a new ``draft_versions`` row, no new ``drafts`` row).  The
    full validation (existence, same-org ownership, ``status='ready'``)
    happens inside :func:`app.docs.upload.handle_upload` and surfaces
    as a Estonian :class:`DraftUploadError` -- this handler just parses
    the form value and forwards it.
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

    # #618 PR-B: parse the new "Versioneerimine" picker value.  Empty /
    # missing means "create a new draft, not a version".  A malformed
    # UUID is rejected up front so we never call ``handle_upload`` with
    # garbage; same-org / status validation lives downstream because it
    # needs the connection.
    parent_draft_raw = form.get("parent_draft_id", "")
    parent_draft_str = str(parent_draft_raw).strip() if parent_draft_raw else ""
    parent_draft_uuid = _parse_uuid(parent_draft_str) if parent_draft_str else None

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
    elif parent_draft_str and parent_draft_uuid is None:
        # The user submitted something in the version picker but it wasn't
        # a valid UUID -- reject before we touch the DB.
        error_message = "Vanem-eelnõu ei ole kättesaadav."
        status_code = 400
    elif parent_draft_uuid is not None and doc_type_value == "vtk":
        # A VTK cannot itself be a follow-on version of another draft --
        # reading-stage progression is an eelnõu lifecycle concept.
        error_message = "VTK ei saa olla teise eelnõu versioon."
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
                    parent_draft_id=parent_draft_uuid,
                )
            except DraftUploadError as exc:
                error_message = str(exc)
                # Keep the legacy 200-with-banner shape so existing
                # tests (and the HTMX form rerender) continue to behave
                # the same; only the upfront-validation branches above
                # set 400.
            else:
                log_draft_upload(
                    auth.get("id"),
                    draft.id,
                    filename=draft.filename,
                    content_type=draft.content_type,
                    file_size=draft.file_size,
                )
                # #598: queue a success toast for the detail page.
                # The Estonian copy differs slightly between the
                # "new draft" and "new version" branches so users see
                # the right narrative.
                if parent_draft_uuid is not None:
                    push_flash(
                        req,
                        "Uus versioon üles laaditud, analüüs algas.",
                        kind="success",
                    )
                else:
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

    org_id_for_pickers = str(auth["org_id"]) if auth.get("org_id") else None
    vtks = list_vtks_for_org(org_id_for_pickers) if org_id_for_pickers else []
    versionable: list[Draft] = []
    if org_id_for_pickers:
        try:
            with _connect() as conn:
                versionable = list_versionable_drafts_for_org(conn, org_id_for_pickers)
        except Exception:
            logger.exception("Failed to load versionable drafts for org=%s", org_id_for_pickers)
    form_el, _ = _upload_form(
        title_value=title_value,
        error=error_message,
        vtks=vtks,
        versionable_drafts=versionable,
        doc_type_value=doc_type_value if doc_type_value in _VALID_DOC_TYPES else "eelnou",
        parent_vtk_id_value=parent_vtk_str or None,
        parent_draft_id_value=parent_draft_str or None,
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
