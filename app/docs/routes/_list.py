"""Drafts-listing handler + filter helpers for /drafts (#704 PR-D extraction).

Pulled out of ``app/docs/routes/__init__.py`` so the workspace listing
page (filter bar, search, sort, pagination, table builders) lives next
to the route handler that renders it. The detail / lifecycle / upload
flows continue to live in their own submodules.

Routes registered (wired by :func:`app.docs.routes.register_draft_routes`):

    GET  /drafts   — drafts_list_page

Public-ish helpers re-exported by ``app.docs.routes.__init__`` for
back-compat:

    Constants:
        ``_DOC_TYPE_BADGE``        — badge variant per doc_type for the table
        ``_DOC_TYPE_CHOICES``      — ordered (value, label) pairs for the filter
        ``_DOC_TYPE_VALUES``       — frozenset of valid doc_type values
        ``_STATUS_VALUES``         — VALID_STATUSES from app.docs.status
        ``_SORT_CHOICES``          — ordered (value, label) pairs for the sort dropdown
        ``_SORT_VALUES``           — frozenset of valid sort values

    Helpers:
        ``_draft_rows``            — Draft → DataTable row dict shaping
        ``_draft_list_columns``    — DataTable column definitions
        ``_parse_date_param``      — YYYY-MM-DD tolerant parser
        ``_parse_filters_from_request`` — extract filter state from QueryParams
        ``_filter_querystring``    — render filters back into a querystring
        ``_has_active_filters``    — empty-state branch selector
        ``_filter_bar``            — HTMX-driven filter bar Form
        ``_drafts_table_section``  — table + pagination wrapper

**Patch-path caveat (post-#704):** ``patch("app.docs.routes.X")``
rebinds the symbol in the package namespace ONLY. This module imports
its dependencies at module load time, so a package-level patch does
NOT propagate here. To intercept a list-page dependency, patch where
it is USED:
``patch("app.docs.routes._list.list_drafts_for_org_filtered")``,
``patch("app.docs.routes._list.list_users")``, etc. Pinned by tests
in ``tests/test_docs_routes_patch_paths.py``.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request
from starlette.responses import Response

from app.auth.helpers import require_auth as _require_auth
from app.auth.users import list_users
from app.docs.draft_model import (
    DEFAULT_SORT,
    Draft,
    list_drafts_for_org_filtered,
)
from app.docs.routes._shared import (
    _PAGE_SIZE,
    _format_timestamp,
    _status_badge,
)
from app.docs.status import (
    STATUS_BY_VALUE,
)
from app.docs.status import (
    VALID_STATUSES as _VALID_STATUSES,
)
from app.docs.upload import max_upload_mb_display
from app.ui.data.data_table import Column, DataTable
from app.ui.data.pagination import Pagination
from app.ui.feedback.empty_state import EmptyState
from app.ui.layout import PageShell
from app.ui.primitives.badge import Badge, BadgeVariant
from app.ui.primitives.link_button import LinkButton
from app.ui.surfaces.alert import Alert
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.surfaces.info_box import InfoBox
from app.ui.theme import get_theme_from_request

# ---------------------------------------------------------------------------
# Table row + column shaping
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
            # #813: HTML4 string form survives FastHTML's HTTP renderer.
            attrs["checked"] = "checked"
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
            # #813: HTML4 string form survives FastHTML's HTTP renderer.
            attrs["checked"] = "checked"
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
            # #813: HTML4 string form survives FastHTML's HTTP renderer.
            opt_attrs["selected"] = "selected"
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
            # #813: HTML4 string form survives FastHTML's HTTP renderer.
            opt_attrs["selected"] = "selected"
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


# ---------------------------------------------------------------------------
# GET /drafts — listing handler
# ---------------------------------------------------------------------------


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
                f"Maksimaalne failisuurus on {max_upload_mb_display()}. "
                "Eelnõud säilivad kuni nende kustutamiseni; tundlikud failid "
                "on krüpteeritud puhkeolekus ja nähtavad ainult teie "
                "organisatsiooni liikmetele."
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
