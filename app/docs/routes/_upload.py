"""Upload-flow handlers + form helpers for /drafts (#704 PR-C extraction).

Pulled out of ``app/docs/routes/__init__.py`` so the upload form, its
JS payloads, the VTK / version pickers, and the upfront validation
of ``parent_vtk_id`` / ``parent_draft_id`` live next to the two
handlers that drive them: ``new_draft_page`` (GET /drafts/new) and
``create_draft_handler`` (POST /drafts).

Routes registered (wired by :func:`app.docs.routes.register_draft_routes`):

    GET  /drafts/new   — new_draft_page
    POST /drafts       — create_draft_handler

Public-ish helpers re-exported by ``app.docs.routes.__init__`` for
back-compat:

    ``_DOC_TYPE_TOGGLE_SCRIPT``
    ``_VALID_DOC_TYPES``
    ``_doc_type_radio``
    ``_file_picker_script`` — renders the upload-size-aware picker JS (#776)
    ``_vtk_picker``         — also used by the link-vtk modal in __init__.py
    ``_version_picker``
    ``_upload_form``
    ``_validate_parent_vtk_fk`` — also used by ``link_vtk_handler``

**Patch-path caveat (post-#704):** ``patch("app.docs.routes.X")``
rebinds the symbol in the package namespace ONLY. This module imports
its dependencies at module load time, so a package-level patch does
NOT propagate here. To intercept an upload-handler dependency, patch
where it is USED:
``patch("app.docs.routes._upload.handle_upload")``,
``patch("app.docs.routes._upload._connect")``, etc. Pinned by tests
in ``tests/test_docs_routes_patch_paths.py``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fasthtml.common import *  # noqa: F403
from fasthtml.common import to_xml
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from app.auth.helpers import require_auth as _require_auth
from app.db import get_connection as _connect
from app.docs._helpers import _parse_uuid
from app.docs.audit import log_draft_upload
from app.docs.draft_model import (
    Draft,
    list_versionable_drafts_for_org,
    list_vtks_for_org,
)
from app.docs.upload import (
    DraftUploadError,
    handle_upload,
    max_upload_bytes,
    max_upload_mb_display,
)
from app.ui.feedback.flash import push_flash
from app.ui.layout import PageShell
from app.ui.primitives.button import Button
from app.ui.primitives.link_button import LinkButton
from app.ui.surfaces.alert import Alert
from app.ui.surfaces.card import Card, CardBody
from app.ui.surfaces.info_box import InfoBox
from app.ui.theme import get_theme_from_request

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Client-side upload helpers (#602, #640, #776)
# ---------------------------------------------------------------------------


def _file_picker_script() -> str:
    """Render the file-picker JS with the *current* upload-size limit (#776).

    #602: client-side size cap matches the server-side limit in
    ``app/docs/upload.py`` so users don't wait for a large upload to
    transfer before being told it's too big. The inline script also
    renders "filename — 12.3 MB" below the picker so there is immediate
    visual confirmation of the selection.

    The byte constant and the user-facing error string both derive from
    the same ``MAX_UPLOAD_SIZE_MB`` read (#776) so the JS gate and the
    server-side validator are guaranteed to agree.
    """
    max_bytes = max_upload_bytes()
    max_label = max_upload_mb_display()
    return (
        "(function () {\n"
        "  var input = document.getElementById('field-file');\n"
        "  if (!input) return;\n"
        "  var info = document.getElementById('field-file-info');\n"
        "  var err = document.getElementById('field-file-error');\n"
        "  var submit = document.getElementById('upload-submit');\n"
        f"  var MAX = {max_bytes};\n"
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
        f"+ '). Maksimaalne suurus on {max_label}.';\n"
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

    #808: when the caller's org has zero VTKs, render the select as
    disabled with just the empty sentinel option and an inline help
    message explaining that the user can upload without linking a VTK.
    The field is optional server-side either way.
    """
    selected_str = str(selected) if selected else ""

    # #808: empty-state branch — disabled select + explanatory help text.
    if not vtks:
        return Div(  # noqa: F405
            Label(label, fr=field_id, cls="form-field-label"),  # noqa: F405
            Select(  # noqa: F405
                Option("— vali —", value="", selected=True),  # noqa: F405
                name=name,
                id=field_id,
                cls="input input-select",
                disabled=True,
            ),
            Small(  # noqa: F405
                "Organisatsioonis pole veel VTKsid — saate eelnõu üles laadida ilma VTK-ta.",
                cls="form-field-help",
            ),
            cls="form-field",
        )

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
                f"Toetatud failitüübid: .docx, .pdf. "
                f"Maksimaalne suurus {max_upload_mb_display()}.",
                cls="form-field-help",
            ),
            # #602: client-side picker feedback — filename + formatted
            # size, plus an inline error when the picked file exceeds
            # the configured limit so the user is not forced to wait
            # for a large upload to transfer before being told it's too
            # big. The actual byte cap is derived at render time so a
            # change to ``MAX_UPLOAD_SIZE_MB`` propagates without a
            # redeploy of the routes module (#776).
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
        Script(_file_picker_script()),  # noqa: F405
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
        H1("Uus eelnõu", cls="page-title"),  # noqa: F405
        InfoBox(
            P(
                f"Valige fail (.docx või .pdf, kuni {max_upload_mb_display()}) ja andke sellele "
                "pealkiri. Pärast üleslaadimist analüüsib "
                "süsteem eelnõu automaatselt."
            ),
            variant="info",
            dismissible=True,
        ),
        Card(CardBody(*card_children)),
        P(A("← Tagasi eelnõude nimekirja", href="/drafts"), cls="back-link"),  # noqa: F405
        title="Uus eelnõu",
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
