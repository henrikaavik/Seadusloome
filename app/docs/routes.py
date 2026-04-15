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
from datetime import UTC, datetime
from typing import Any

from fasthtml.common import *  # noqa: F403
from fasthtml.common import to_xml
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from app.auth.audit import log_action
from app.auth.helpers import require_auth as _require_auth
from app.auth.policy import can_delete_draft, can_view_draft
from app.db import get_connection as _connect
from app.docs.audit import (
    log_draft_delete,
    log_draft_upload,
    log_draft_view,
)
from app.docs.draft_model import (
    Draft,
    count_drafts_for_org_conn,
    delete_draft,
    fetch_draft,
    fetch_drafts_for_org,
    touch_draft_access,
    touch_draft_access_conn,
)
from app.docs.upload import DraftUploadError, handle_upload
from app.rag.retriever import delete_chunks_for_draft
from app.storage import delete_file as delete_encrypted_file
from app.sync.jena_loader import delete_named_graph
from app.ui.data.data_table import Column, DataTable
from app.ui.data.pagination import Pagination
from app.ui.feedback.flash import push_flash
from app.ui.layout import PageShell
from app.ui.primitives.annotation_button import AnnotationButton
from app.ui.primitives.badge import Badge, BadgeVariant
from app.ui.primitives.button import Button
from app.ui.surfaces.alert import Alert
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.surfaces.info_box import InfoBox
from app.ui.surfaces.modal import ConfirmModal, ModalScript
from app.ui.theme import get_theme_from_request
from app.ui.time import format_tallinn

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Status display helpers
# ---------------------------------------------------------------------------

# Public pipeline stages in order. "failed" is a terminal branch rendered
# separately so the tracker reads left-to-right during normal operation.
_STATUS_STAGES: tuple[tuple[str, str], ...] = (
    ("uploaded", "Üles laaditud"),
    ("parsing", "Töötlemine"),
    ("extracting", "Olemite eraldamine"),
    ("analyzing", "Mõjude analüüs"),
    ("ready", "Valmis"),
)

_STATUS_LABELS: dict[str, str] = dict(_STATUS_STAGES)
_STATUS_LABELS["failed"] = "Ebaõnnestus"

_TERMINAL_STATUSES: frozenset[str] = frozenset({"ready", "failed"})

_PAGE_SIZE = 25

_DELETE_CONFIRM = (
    "Kas olete kindel, et soovite selle eelnõu kustutada? Seda tegevust ei saa tagasi võtta."
)

# #572: drafts whose ``last_accessed_at`` is older than this are stale
# and the UI surfaces a "Hoia alles" (Keep) button alongside the delete
# button. The same threshold is used by the archive-warning scan job.
_STALE_THRESHOLD_DAYS = 90


def _is_draft_stale(draft: Draft) -> bool:
    """Return True when the draft has not been accessed for 90+ days (#572)."""
    last = getattr(draft, "last_accessed_at", None)
    if last is None:
        return False
    try:
        elapsed = (datetime.now(UTC) - last).total_seconds()
    except (TypeError, ValueError):
        return False
    return elapsed > _STALE_THRESHOLD_DAYS * 24 * 60 * 60


_STATUS_KEY_MAP: dict[str, str] = {
    "uploaded": "pending",
    "parsing": "running",
    "extracting": "running",
    "analyzing": "running",
    "ready": "ok",
    "failed": "failed",
}

_STATUS_VARIANT_MAP: dict[str, BadgeVariant] = {
    "uploaded": "default",
    "parsing": "primary",
    "extracting": "primary",
    "analyzing": "primary",
    "ready": "success",
    "failed": "danger",
}


def _status_badge(status: str):
    """Return a Badge for a draft status.

    We use plain ``Badge`` instead of ``StatusBadge`` because the latter
    ships its own English-ish label set and our domain statuses
    (uploaded/parsing/extracting/analyzing) need Estonian copy.
    """
    key = _STATUS_KEY_MAP.get(status, "pending")
    variant: BadgeVariant = _STATUS_VARIANT_MAP.get(status, "default")
    label = _STATUS_LABELS.get(status, status)
    return Badge(label, variant=variant, cls=f"draft-status draft-status-{key}")


def _format_timestamp(value: Any) -> str:
    """Render a ``datetime`` in Europe/Tallinn (see app.ui.time)."""
    return format_tallinn(value)


# #457: stop polling after this many seconds since the draft was
# created. Without an upper bound the page hammers /status forever
# whenever a worker hangs (or the queue is paused), and the user has
# no actionable signal.
_POLLING_TIMEOUT_SECONDS = 300


def _is_status_polling_stale(draft: Draft) -> bool:
    """Return True if we should stop polling and surface a warning.

    #470: we use ``updated_at`` (bumped by every handler on each
    pipeline transition) rather than ``created_at``. A long-running
    draft whose pipeline is still making progress will keep bumping
    ``updated_at``, so the polling budget resets on each transition.
    A pipeline that's genuinely hung leaves ``updated_at`` frozen, and
    the polling window elapses against that frozen timestamp. If
    ``updated_at`` is missing for any reason (older rows, DB race),
    fall back to ``created_at`` so we still honour the timeout.
    """
    reference = draft.updated_at or draft.created_at
    if reference is None:
        return False
    try:
        elapsed = (datetime.now(UTC) - reference).total_seconds()
    except (TypeError, ValueError):
        return False
    return elapsed > _POLLING_TIMEOUT_SECONDS


def _status_tracker(draft: Draft):
    """Render the 6-stage horizontal status tracker.

    Wrapped in a polling Div so HTMX can refresh it every 3 seconds
    until the draft reaches a terminal state OR the polling timeout
    elapses (#457). After the timeout we drop the polling attributes
    and surface a yellow alert nudging the user to check the admin
    dashboard so they don't sit on the page forever.
    """
    items: list = []
    current_index = -1
    for idx, (key, _) in enumerate(_STATUS_STAGES):
        if key == draft.status:
            current_index = idx
            break

    for idx, (key, label) in enumerate(_STATUS_STAGES):
        classes = ["draft-stage"]
        if draft.status == "failed":
            # On failure every stage past the last successful one is dim.
            classes.append("draft-stage-idle")
        elif current_index >= 0 and idx < current_index:
            classes.append("draft-stage-done")
        elif current_index >= 0 and idx == current_index:
            classes.append("draft-stage-active")
        else:
            classes.append("draft-stage-idle")
        items.append(
            Li(  # noqa: F405
                Span(str(idx + 1), cls="draft-stage-number", aria_hidden="true"),  # noqa: F405
                Span(label, cls="draft-stage-label"),  # noqa: F405
                cls=" ".join(classes),
            )
        )

    tracker = Ol(*items, cls="draft-status-tracker", aria_label="Töötluse staatus")  # noqa: F405

    # Build the poll attributes only while the draft is still
    # progressing AND we haven't blown the polling timeout (#457).
    polling_stale = _is_status_polling_stale(draft)
    poll_attrs: dict[str, Any] = {}
    if draft.status not in _TERMINAL_STATUSES and not polling_stale:
        poll_attrs = {
            "hx_get": f"/drafts/{draft.id}/status",
            "hx_trigger": "every 3s",
            "hx_target": "this",
            "hx_swap": "outerHTML",
        }

    header = Div(  # noqa: F405
        Span("Staatus:", cls="draft-status-label-text"),  # noqa: F405
        _status_badge(draft.status),
        cls="draft-status-header",
    )

    children: list = [header, tracker]
    if draft.status == "failed" and draft.error_message:
        children.append(
            Alert(
                draft.error_message,
                variant="danger",
                title="Töötlemine ebaõnnestus",
            )
        )
    elif polling_stale and draft.status not in _TERMINAL_STATUSES:
        # The pipeline has been running longer than the polling
        # timeout. Surface a yellow alert and stop polling so the
        # user knows to escalate instead of waiting indefinitely.
        children.append(
            Alert(
                "Vajab tähelepanu — töötlemine võtab oodatust kauem aega. "
                "Kontrollige administreerimispaneelilt, kas taustajob on kinni jäänud.",
                variant="warning",
                title="Töötlemine venib",
            )
        )

    # #604: announce stage transitions to screen readers. ``polite``
    # avoids interrupting the user mid-task; ``aria_atomic=false`` means
    # assistive tech reads just the changed node instead of the whole
    # tracker. The existing failed-state Alert still carries
    # ``role="alert"`` for the more urgent announcement.
    return Div(  # noqa: F405
        *children,
        id=f"draft-status-{draft.id}",
        cls="draft-status-wrapper",
        aria_live="polite",
        aria_atomic="false",
        **poll_attrs,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_uuid(raw: str) -> uuid.UUID | None:
    """Return a ``UUID`` parsed from *raw*, or ``None`` if invalid."""
    try:
        return uuid.UUID(raw)
    except (ValueError, TypeError):
        return None


def _not_found_page(req: Request):
    """Render the 404 page used whenever a draft is missing or out of scope."""
    auth = req.scope.get("auth")
    theme = get_theme_from_request(req)
    return PageShell(
        H1("Eelnõu ei leitud", cls="page-title"),  # noqa: F405
        Alert(
            "Otsitud eelnõu ei ole olemas või Te ei oma selle vaatamise õigust.",
            variant="warning",
        ),
        P(A("← Tagasi eelnõude nimekirja", href="/drafts"), cls="back-link"),  # noqa: F405
        title="Eelnõu ei leitud",
        user=auth,
        theme=theme,
        active_nav="/drafts",
        request=req,
    )


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
                "title": draft.title,
                "filename": draft.filename,
                "status_raw": draft.status,
                "created_at": _format_timestamp(draft.created_at),
            }
        )
    return rows


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
        return A(  # noqa: F405
            "Vaata",
            href=f"/drafts/{row['id']}",
            cls="btn btn-secondary btn-sm",
        )

    return [
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


def drafts_list_page(req: Request):
    """GET /drafts — paginated list of the caller's org's drafts."""
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
        body: Any = Alert(
            "Te ei kuulu ühtegi organisatsiooni, seega ei saa Te eelnõusid näha ega üles laadida.",
            variant="warning",
        )
        pagination = None
        total = 0
    else:
        drafts = fetch_drafts_for_org(org_id, limit=_PAGE_SIZE, offset=offset)
        total = count_drafts_for_org_conn(org_id)
        total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

        if total == 0:
            body = Div(
                InfoBox(
                    P(
                        "Laadige \u00fcles .docx v\u00f5i .pdf eeln\u00f5u, "
                        "et n\u00e4ha selle m\u00f5ju olemasolevatele seadustele. "
                        "S\u00fcsteem anal\u00fc\u00fcsib automaatselt viiteid, "
                        "konflikte ja EL-i vastavust."
                    ),
                    variant="info",
                    dismissible=True,
                ),
                P(
                    "Teie organisatsioon ei ole veel \u00fchtegi eeln\u00f5u \u00fcles laadinud.",
                    cls="muted-text",
                ),
                A(
                    "Laadi \u00fcles uus eeln\u00f5u",
                    href="/drafts/new",
                    cls="btn btn-primary btn-md",
                ),
                cls="empty-state",
            )
            pagination = None
        else:
            body = DataTable(
                columns=_draft_list_columns(),
                rows=_draft_rows(drafts),
                empty_message="Eelnõusid ei leitud.",
            )
            pagination = Pagination(
                current_page=page,
                total_pages=total_pages,
                base_url="/drafts",
                page_size=_PAGE_SIZE,
                total=total,
            )

    header_children: list = [H1("Eelnõud", cls="page-title")]  # noqa: F405
    if org_id:
        header_children.append(
            Div(
                A(
                    "Laadi üles uus eelnõu",
                    href="/drafts/new",
                    cls="btn btn-primary btn-md",
                ),
                cls="page-actions",
            )
        )

    card_body_children: list = [body]
    if pagination is not None:
        card_body_children.append(pagination)

    return PageShell(
        *header_children,
        Card(
            CardHeader(H3("Minu organisatsiooni eelnõud", cls="card-title")),  # noqa: F405
            CardBody(*card_body_children),
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


def _upload_form(*, title_value: str = "", error: str | None = None):
    """Render the multipart upload form.

    IMPORTANT: this form uses the raw ``Form`` primitive from
    ``fasthtml.common`` rather than :class:`AppForm` because file uploads
    **must** use ``enctype="multipart/form-data"``. AppForm defaults to
    ``application/x-www-form-urlencoded`` and would silently drop the file.
    """
    error_alert = Alert(error, variant="danger") if error else None

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
                "Kuni 200 tähemärki.",
                cls="form-field-help",
            ),
            cls="form-field",
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
            cls="form-field",
        ),
        Div(
            Button("Laadi üles", type="submit", variant="primary"),
            A("Tühista", href="/drafts", cls="btn btn-ghost btn-md"),  # noqa: F405
            cls="form-actions",
        ),
        method="post",
        action="/drafts",
        enctype="multipart/form-data",
        cls="upload-form",
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

    form, error_alert = _upload_form()
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


async def create_draft_handler(req: Request):
    """POST /drafts — accept a multipart upload and create a draft row."""
    auth_or_redirect = _require_auth(req)
    if isinstance(auth_or_redirect, Response):
        return auth_or_redirect
    auth = auth_or_redirect
    theme = get_theme_from_request(req)

    form = await req.form()
    title_raw = form.get("title", "")
    upload = form.get("file")
    title_value = str(title_raw) if title_raw is not None else ""

    if upload is None or not hasattr(upload, "read"):
        error_message = "Palun valige üleslaaditav fail."
    else:
        try:
            draft = await handle_upload(auth, title_value, upload)  # type: ignore[arg-type]
        except DraftUploadError as exc:
            error_message = str(exc)
        else:
            log_draft_upload(
                auth.get("id"),
                draft.id,
                filename=draft.filename,
                content_type=draft.content_type,
                file_size=draft.file_size,
            )
            # #598: queue a success toast for the detail page.
            push_flash(
                req,
                "Eelnõu üles laaditud, analüüs algas.",
                kind="success",
            )
            return RedirectResponse(url=f"/drafts/{draft.id}", status_code=303)

    # #598: also surface the validation error as a danger toast so the
    # banner + toast pattern is consistent with the happy-path redirect.
    push_flash(req, error_message, kind="danger")

    form_el, _ = _upload_form(title_value=title_value, error=error_message)
    return PageShell(
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


# ---------------------------------------------------------------------------
# GET /drafts/{draft_id} — detail page
# ---------------------------------------------------------------------------


_DELETE_MODAL_ID = "delete-draft-modal"
_DELETE_TRIGGER_ID = "delete-draft-trigger"
_DELETE_FORM_ID = "delete-draft-form"

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


def _draft_detail_body(draft: Draft, auth: Mapping[str, Any] | None = None) -> list[Any]:
    """Build the metadata + actions body of the draft detail page.

    The delete form is only rendered when ``auth`` is allowed to delete
    per ``app.auth.policy.can_delete_draft`` (issue #568). Before this
    check the button was shown to every same-org viewer, which made the
    route handler's stricter owner-only check surprising for reviewers
    and org admins who could click and get a 404.
    """
    metadata = Dl(  # noqa: F405
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
        cls="info-list",
    )

    actions: list = []
    # #600: the CTA block is rendered here but the wrapping container
    # is always present so it can listen for the ``draft-ready`` event
    # and re-fetch itself once the pipeline transitions. Only add the
    # "Vaata mõjuaruannet" link when the draft has reached ``ready``.
    if draft.status == "ready":
        actions.append(
            A(  # noqa: F405
                "Vaata mõjuaruannet",
                href=f"/drafts/{draft.id}/report",
                cls="btn btn-primary btn-md",
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
                method="post",
                action=f"/drafts/{draft.id}/keep",
                enctype="application/x-www-form-urlencoded",
                hx_post=f"/drafts/{draft.id}/keep",
                hx_target="body",
                hx_swap="outerHTML",
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
                id=_DELETE_FORM_ID,
                method="post",
                action=f"/drafts/{draft.id}/delete",
                enctype="application/x-www-form-urlencoded",
                hx_post=f"/drafts/{draft.id}/delete",
                hx_target="body",
                hx_swap="outerHTML",
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
    return [metadata, actions_container]


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

    detail_body = _draft_detail_body(draft, auth=auth)
    tracker = _status_tracker(draft)

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
    # ``_draft_detail_body`` returns ``[metadata_dl, actions_container]``;
    # we only swap the actions container here.
    return body[-1]


# ---------------------------------------------------------------------------
# POST /drafts/{draft_id}/delete — delete handler
# ---------------------------------------------------------------------------


def delete_draft_handler(req: Request, draft_id: str):
    """POST /drafts/{draft_id}/delete — remove the draft + encrypted file.

    Owner-only per NFR §5 matrix (fixed by #568). Any same-org colleague
    used to be able to delete another user's draft because the handler
    authorized on ``org_id`` alone. The helper in ``app.auth.policy``
    enforces the full rule: owner OR system admin.
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
    if not can_delete_draft(auth, draft):
        # Return 404 rather than 403 so we don't leak existence of the
        # draft to cross-org or non-owner callers.
        return _not_found_page(req)

    storage_path: str | None = None
    try:
        with _connect() as conn:
            storage_path = delete_draft(conn, parsed)
            # #576: polymorphic soft reference — clear any rag_chunks rows
            # tied to this draft inside the same transaction as the row
            # delete so either both land or neither does. Today no private
            # draft ingestion exists so this is a no-op, but wiring it now
            # means future ingestion code can't forget.
            try:
                delete_chunks_for_draft(conn, parsed)
            except Exception:
                logger.exception("Failed to delete rag_chunks for draft id=%s", parsed)
            conn.commit()
    except Exception:
        logger.exception("Failed to delete draft id=%s", parsed)
        return _not_found_page(req)

    # #454/#478: cancel any pending/claimed/running/retrying background
    # jobs that still reference this draft. The handlers all
    # early-return on a missing draft row, but leaving the rows on the
    # queue keeps a stale ``Ebaõnnestus``-style error appearing on the
    # admin job dashboard once the worker fails to find the row. Doing
    # this *after* the row delete is intentional: it's idempotent and
    # means any job claimed by the worker between the row delete and
    # this cleanup is also covered. #478 added ``running`` because a
    # worker that picked up the job just before deletion would
    # otherwise leave the row behind and produce a spurious failure.
    try:
        with _connect() as conn:
            conn.execute(
                """
                DELETE FROM background_jobs
                WHERE payload->>'draft_id' = %s
                  AND status IN ('pending', 'claimed', 'running', 'retrying')
                """,
                (str(parsed),),
            )
            conn.commit()
    except Exception:
        logger.exception(
            "Failed to cancel pending background jobs for draft id=%s",
            parsed,
        )

    if storage_path:
        try:
            delete_encrypted_file(storage_path)
        except Exception:
            logger.exception(
                "Failed to delete encrypted file for draft id=%s path=%s",
                parsed,
                storage_path,
            )

    # Purge the draft's named graph from Jena (idempotent — a 404 from
    # Fuseki is treated as success, which covers drafts deleted before
    # the analyze_impact handler ever loaded the graph).
    try:
        delete_named_graph(draft.graph_uri)
    except Exception:
        logger.exception(
            "Failed to delete named graph for draft id=%s uri=%s",
            parsed,
            draft.graph_uri,
        )

    log_draft_delete(
        auth.get("id"),
        parsed,
        filename=draft.filename,
    )

    # #598: queue a success toast for the drafts listing page.
    push_flash(req, "Eelnõu kustutatud.", kind="success")

    # #467: when the browser drives the delete via HTMX (the form has
    # ``hx_post`` + ``hx_target='body'`` + ``hx_swap='outerHTML'`` — see
    # ``_draft_detail_body``), returning a plain 303 here makes HTMX
    # follow the redirect as an AJAX GET, fetch the drafts-list partial
    # (whose first element is a ``<title>`` tag from ``PageShell``), and
    # swap that entire partial into ``<body>``. The rendered page ends
    # up with a ``<title>`` inside the body, which browsers treat as
    # invalid HTML and render as visible text — corrupting the layout.
    #
    # The fix is to detect HTMX requests and return an empty 204 with an
    # ``HX-Redirect`` header so HTMX performs a **real** browser
    # navigation to ``/drafts`` instead of swapping. Non-HTMX clients
    # (JS-disabled users hitting the native form action) still get the
    # 303 redirect.
    if req.headers.get("HX-Request") == "true":
        return Response(
            status_code=204,
            headers={"HX-Redirect": "/drafts"},
        )
    return RedirectResponse(url="/drafts", status_code=303)


# ---------------------------------------------------------------------------
# POST /drafts/{draft_id}/keep — reset last_accessed_at (#572)
# ---------------------------------------------------------------------------


def keep_draft_handler(req: Request, draft_id: str):
    """POST /drafts/{draft_id}/keep — reset the 90-day archive clock.

    Owner-only per the same policy as delete — resetting the archive
    clock is a governance action that re-commits the org to retaining
    the draft for another 90 days. Same-org reviewers and admins MUST
    NOT be able to bypass the owner's intent to let a stale draft
    auto-warn.
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
    if not can_delete_draft(auth, draft):
        # 404 (not 403) — see delete_draft_handler for the reasoning.
        return _not_found_page(req)

    try:
        with _connect() as conn:
            touch_draft_access(conn, parsed)
            conn.commit()
    except Exception:
        logger.exception("Failed to reset last_accessed_at for draft=%s", parsed)
        return _not_found_page(req)

    log_action(
        auth.get("id"),
        "draft.keep",
        {"draft_id": str(parsed)},
    )

    # #598: queue a success toast for the detail page redirect target.
    push_flash(req, "90-päevane loendur lähtestatud.", kind="success")

    # HTMX-driven submits get an HX-Redirect so the browser performs a
    # real navigation rather than swapping a partial into <body>.
    if req.headers.get("HX-Request") == "true":
        return Response(
            status_code=204,
            headers={"HX-Redirect": f"/drafts/{parsed}"},
        )
    return RedirectResponse(url=f"/drafts/{parsed}", status_code=303)


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
