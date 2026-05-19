"""Detail-page handlers + renderers for /drafts (#704 PR-E extraction).

Pulled out of ``app/docs/routes/__init__.py`` so the draft-detail
page (status tracker, metadata, similar drafts, version timeline,
VTK children card, link-vtk handler) lives next to its route
handlers. Modal scripts + builders sit in the companion
:mod:`app.docs.routes._detail_modals`; the version-timeline section
+ diff page live in :mod:`app.docs.routes._detail_versions`.

Routes registered (wired by :func:`app.docs.routes.register_draft_routes`):

    GET  /drafts/{draft_id}          — draft_detail_page
    GET  /drafts/{draft_id}/status   — draft_status_fragment
    GET  /drafts/{draft_id}/actions  — draft_actions_fragment
    POST /drafts/{draft_id}/link-vtk — link_vtk_handler

Public-ish helpers re-exported by ``app.docs.routes.__init__`` for
back-compat:

    Helpers:
        ``_seotud_vtk_row``           — "Seotud VTK" metadata <dt>/<dd>
        ``_draft_metadata_block``     — full metadata <dl> wrapped for HTMX
        ``_vtk_children_card``        — "Sellest VTKst tulenevad eelnõud"
        ``_similar_drafts_card``      — "Sarnased eelnõud" (cross-org masked)
        ``_draft_detail_body``        — metadata + actions container

**Patch-path caveat (post-#704):** ``patch("app.docs.routes.X")``
rebinds the symbol in the package namespace ONLY. This module imports
its dependencies at module load time, so a package-level patch does
NOT propagate here. To intercept a detail-handler dependency, patch
where it is USED:
``patch("app.docs.routes._detail.fetch_draft")``,
``patch("app.docs.routes._detail._connect")``,
``patch("app.docs.routes._detail.log_draft_view")``,
``patch("app.docs.routes._detail.list_users")``,
``patch("app.docs.routes._detail.list_eelnous_for_vtk")``,
``patch("app.docs.routes._detail.list_vtks_for_org")``,
``patch("app.docs.routes._detail.write_doc_lineage")``,
``patch("app.docs.routes._detail.update_draft_parent_vtk")``,
``patch("app.docs.routes._detail._version_timeline_rows")``, etc.
Pinned by tests in ``tests/test_docs_routes_patch_paths.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from fasthtml.common import *  # noqa: F403
from fasthtml.common import to_xml
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from app.auth.audit import log_action
from app.auth.helpers import require_auth as _require_auth
from app.auth.policy import (
    can_delete_draft,
    can_edit_draft,
    can_review_draft,
    can_view_draft,
)
from app.auth.users import list_users
from app.db import get_connection as _connect
from app.docs._helpers import _not_found_page, _parse_uuid
from app.docs.audit import log_draft_view
from app.docs.draft_model import (
    Draft,
    fetch_draft,
    list_eelnous_for_vtk,
    list_vtks_for_org,
    touch_draft_access_conn,
    update_draft_parent_vtk,
)
from app.docs.graph_builder import write_doc_lineage
from app.docs.report_routes import explorer_draft_url
from app.docs.review_model import (
    REVIEW_OUTCOME_LABELS_ET,
    DraftReview,
    list_reviews_for_draft,
)
from app.docs.routes._detail_modals import (
    _DELETE_FORM_ID,
    _DELETE_MODAL_ID,
    _DELETE_MODAL_SCRIPT,
    _DELETE_TRIGGER_ID,
    _DRAFT_METADATA_ID,
    _LINK_VTK_MODAL_ID,
    _LINK_VTK_TRIGGER_ID,
    _link_vtk_modal,
)
from app.docs.routes._detail_versions import (
    _version_timeline_rows,
    _version_timeline_section,
)
from app.docs.routes._shared import (
    _DELETE_CONFIRM,
    _format_timestamp,
    _is_draft_stale,
    _status_badge,
)
from app.docs.routes._status_tracker import _status_tracker
from app.docs.routes._upload import _validate_parent_vtk_fk
from app.docs.similarity import list_similar_drafts_for_view
from app.ui.feedback.empty_state import EmptyState
from app.ui.layout import PageShell
from app.ui.primitives.annotation_button import AnnotationButton
from app.ui.primitives.badge import Badge
from app.ui.primitives.button import Button
from app.ui.primitives.link_button import LinkButton
from app.ui.surfaces.alert import Alert
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.surfaces.info_box import InfoBox
from app.ui.surfaces.modal import ConfirmModal, ModalScript
from app.ui.theme import get_theme_from_request

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Detail-page renderers
# ---------------------------------------------------------------------------


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
            children.append(Dd("— ", trigger))  # noqa: F405
        else:
            children.append(Dd("—"))  # noqa: F405
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
            icon="\U0001f4c4",
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


# ---------------------------------------------------------------------------
# Review outcome section (#817) — reviewer-only UI
# ---------------------------------------------------------------------------


# Stable DOM id used as the HTMX swap target for review-section refreshes.
_REVIEW_SECTION_ID = "draft-review-section"


# Badge variants per outcome — keeps the colour code consistent across
# the detail page and the reviewer Töölaud. Mirrors the design-system
# vocabulary used for impact-band badges.
_OUTCOME_BADGE_VARIANT: dict[str, str] = {
    "no_issue": "success",
    "issue_found": "danger",
    "needs_discussion": "warning",
}


def _review_chip(outcome: str) -> Any:
    """Render a single outcome badge using the canonical Estonian label."""
    label = REVIEW_OUTCOME_LABELS_ET.get(outcome, outcome)
    variant = _OUTCOME_BADGE_VARIANT.get(outcome, "default")
    return Badge(label, variant=variant)  # type: ignore[arg-type]


def _review_history_item(review: DraftReview) -> Any:
    """One row in the chronological review history.

    Renders reviewer name with a "(kustutatud kasutaja)" placeholder when
    ``reviewer_id`` is NULL but the snapshot is present (the reviewer's
    account was deleted but the review record is preserved).
    """
    if review.reviewer_id is None:
        if review.reviewer_name_snapshot:
            reviewer_label: str = f"{review.reviewer_name_snapshot} (kustutatud kasutaja)"
        else:
            reviewer_label = "Kustutatud kasutaja"
    else:
        reviewer_label = review.reviewer_name_snapshot or "Tundmatu kasutaja"

    parts: list[Any] = [
        Div(  # noqa: F405
            Span(reviewer_label, cls="review-history__reviewer"),  # noqa: F405
            _review_chip(review.outcome),
            Span(  # noqa: F405
                _format_timestamp(review.created_at),
                cls="review-history__timestamp",
            ),
            cls="review-history__meta",
        ),
    ]
    if review.comment:
        parts.append(
            P(  # noqa: F405
                review.comment,
                cls="review-history__comment",
            )
        )
    return Li(*parts, cls="review-history__item")  # noqa: F405


def _review_history(reviews: list[DraftReview]) -> Any:
    """Render the chronological list of review outcomes for the draft."""
    if not reviews:
        return P(  # noqa: F405
            "Selle eelnõu kohta pole veel ülevaatuse tulemusi.",
            cls="muted-text",
        )
    items = [_review_history_item(r) for r in reviews]
    return Ul(*items, cls="review-history")  # noqa: F405


def _review_outcome_form(draft: Draft) -> Any:
    """Render the reviewer-only outcome form with three buttons + optional comment.

    Three outcome buttons share one ``<form>`` and disambiguate via the
    ``name="outcome"`` value on each ``<button>``. HTMX posts the form and
    swaps the section in place using ``hx-target`` / ``hx-swap``.
    """
    # The three outcome buttons. Submit-by-name is the cleanest way to
    # send the chosen value without an extra hidden radio set.
    no_issue_btn = Button(
        REVIEW_OUTCOME_LABELS_ET["no_issue"],
        type="submit",
        name="outcome",
        value="no_issue",
        variant="primary",
        size="md",
    )
    issue_found_btn = Button(
        REVIEW_OUTCOME_LABELS_ET["issue_found"],
        type="submit",
        name="outcome",
        value="issue_found",
        variant="danger",
        size="md",
    )
    needs_discussion_btn = Button(
        REVIEW_OUTCOME_LABELS_ET["needs_discussion"],
        type="submit",
        name="outcome",
        value="needs_discussion",
        variant="secondary",
        size="md",
    )

    return Form(  # noqa: F405
        Label(  # noqa: F405
            "Lisa märkus (valikuline)",
            For="review-comment",
            cls="form-label",
        ),
        Textarea(  # noqa: F405
            "",
            name="comment",
            id="review-comment",
            rows="3",
            placeholder="Selgitage oma järeldust või tooge esile põhilised tähelepanekud.",
            cls="form-textarea",
        ),
        Div(  # noqa: F405
            no_issue_btn,
            issue_found_btn,
            needs_discussion_btn,
            cls="review-outcome__actions",
        ),
        method="post",
        action=f"/drafts/{draft.id}/review-outcome",
        enctype="application/x-www-form-urlencoded",
        hx_post=f"/drafts/{draft.id}/review-outcome",
        hx_target=f"#{_REVIEW_SECTION_ID}",
        hx_swap="outerHTML",
        cls="review-outcome__form",
    )


def _review_outcome_section(
    draft: Draft,
    auth: Mapping[str, Any] | None = None,
    *,
    reviews: list[DraftReview] | None = None,
) -> Any:
    """Render the reviewer-outcome card (#817).

    Only visible to users whose ``auth`` satisfies
    :func:`app.auth.policy.can_review_draft` — reviewer role + draft view
    rights. Returns an empty placeholder div for everyone else so HTMX
    swaps don't fail when the section is hidden.

    Layout:

    * Three outcome buttons + optional comment textarea (top).
    * Chronological list of previous reviews (below the form).

    Wrapped in a stable ``#draft-review-section`` div so the POST
    handler can ``outerHTML``-swap the section after a new review lands
    without rebuilding the entire detail page.
    """
    # Placeholder div for non-reviewers so the layout doesn't shift.
    if not can_review_draft(auth, draft):
        return Div("", id=_REVIEW_SECTION_ID)  # noqa: F405

    review_rows: list[DraftReview] = reviews if reviews is not None else []
    card = Card(
        CardHeader(H3("Ülevaatus", cls="card-title")),  # noqa: F405
        CardBody(
            P(  # noqa: F405
                "Märkige eelnõu ülevaatuse tulemus. Saate hiljem oma järelduse "
                "uuendada — kõik tulemused jäävad ajalukku alles.",
                cls="muted-text",
            ),
            _review_outcome_form(draft),
            H4("Varasemad tulemused", cls="card-subtitle"),  # noqa: F405
            _review_history(review_rows),
        ),
    )
    return Div(card, id=_REVIEW_SECTION_ID)  # noqa: F405


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
        # #759: deep-link into Õiguskaart centred on this draft's impact
        # subgraph (``/explorer?draft=<id>`` — the ``?draft=`` handling is
        # issue #755; this just mints the URL via the shared helper). Same
        # ``ready`` guard as the report CTA — no impact subgraph exists
        # before the analyse pipeline completes.
        actions.append(
            LinkButton(
                "Vaata mõjukaarti",
                href=explorer_draft_url(str(draft.id)),
                variant="secondary",
                title="Visualiseeri eelnõu ja mõjutatud sätted Õiguskaardil.",
            )
        )
        # #724: cross-link into the Analüüsikeskus "Normi mõjuahel" workflow,
        # which accepts a draft UUID as ``sisend`` and reuses this draft's
        # ``impact_reports`` row (no recomputation). Same ``ready`` guard as
        # the "Vaata mõjuaruannet" CTA above.
        actions.append(
            LinkButton(
                "Ava analüüsikeskuses →",
                href=f"/analyysikeskus/normi-mojuahel?sisend={draft.id}",
                variant="secondary",
                title="Ava selle eelnõu mõjuahel Analüüsikeskuses.",
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
                # #813: HTML4 string form survives FastHTML's HTTP renderer.
                hidden="hidden",
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


# ---------------------------------------------------------------------------
# GET /drafts/{draft_id} — detail page
# ---------------------------------------------------------------------------


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

    # #817: load existing review outcomes for this draft. Only fetched
    # when the caller actually has reviewer rights — drafters never see
    # the section so spending an extra query for them would be waste.
    # Best-effort: any DB error degrades to an empty list and the form
    # still renders with no history below it.
    reviews: list[DraftReview] = []
    if can_review_draft(auth, draft):
        try:
            with _connect() as conn:
                reviews = list_reviews_for_draft(conn, draft.id)
        except Exception:
            logger.warning(
                "draft_detail_page: failed to load draft_reviews for draft=%s",
                draft.id,
                exc_info=True,
            )

    return PageShell(
        H1(draft.title, cls="page-title"),  # noqa: F405
        P(A("← Tagasi eelnõude nimekirja", href="/drafts"), cls="back-link"),  # noqa: F405
        InfoBox(
            P(
                "Eelnõu läbib automaatselt mitu etappi: "
                "teksti eraldamine → viidete tuvastamine → "
                "mõjuanalüüs. "
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
            CardHeader(H3("Üksikasjad", cls="card-title")),  # noqa: F405
            # #603: the old CardFooter rendered ``draft.graph_uri`` — an
            # internal Jena named-graph URI — to the user. That leaked
            # implementation detail with no operational value; audit
            # logs and admin tools still have the URI, only the
            # user-facing detail page omits it.
            CardBody(*detail_body),
        ),
        # #817: reviewer-only outcome section. Renders an empty stub div
        # for non-reviewers so the in-place HTMX swap target stays valid
        # but no UI surfaces. The stable ``#draft-review-section`` id is
        # also the HTMX target for the POST handler's reload.
        _review_outcome_section(draft, auth=auth, reviews=reviews),
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
