"""Detail-page modal scripts + builders (#704 PR-E extraction).

Companion to :mod:`app.docs.routes._detail`. Split off so the inline
JS payloads + the link-vtk modal builder live next to each other,
keeping ``_detail.py`` focused on the page-level renderers and route
handlers.

Public-ish helpers re-exported by ``app.docs.routes.__init__`` for
back-compat:

    Constants:
        ``_DELETE_MODAL_ID``         — id of the delete-confirm modal
        ``_DELETE_TRIGGER_ID``       — id of the visible "Kustuta" button
        ``_DELETE_FORM_ID``          — id of the hidden HTMX delete form
        ``_LINK_VTK_MODAL_ID``       — id of the "Seo VTKga" modal
        ``_LINK_VTK_TRIGGER_ID``     — id of the visible "Seo VTKga" button
        ``_LINK_VTK_FORM_ID``        — id of the link-vtk form inside the modal
        ``_DRAFT_METADATA_ID``       — id of the metadata block (HTMX swap target)
        ``_LINK_VTK_MODAL_SCRIPT``   — inline JS that wires the trigger + form
        ``_DELETE_MODAL_SCRIPT``     — inline JS that wires the delete trigger

    Helpers:
        ``_link_vtk_modal``          — builds Modal + ModalScript + Script trio

**Patch-path caveat (post-#704):** ``patch("app.docs.routes.X")``
rebinds the symbol in the package namespace ONLY. This module imports
its dependencies at module load time, so a package-level patch does
NOT propagate here. To intercept a modal-builder dependency, patch
where it is USED:
``patch("app.docs.routes._detail_modals._vtk_picker")``, etc.
"""

from __future__ import annotations

import uuid
from typing import Any

from fasthtml.common import *  # noqa: F403

from app.docs.draft_model import Draft
from app.docs.routes._upload import _vtk_picker
from app.ui.primitives.button import Button
from app.ui.surfaces.modal import Modal, ModalBody, ModalFooter, ModalScript

# ---------------------------------------------------------------------------
# Modal identifiers (delete + link-vtk)
# ---------------------------------------------------------------------------

_DELETE_MODAL_ID = "delete-draft-modal"
_DELETE_TRIGGER_ID = "delete-draft-trigger"
_DELETE_FORM_ID = "delete-draft-form"

# #640: identifiers for the "Seo VTKga" modal + its embedded form.
_LINK_VTK_MODAL_ID = "link-vtk-modal"
_LINK_VTK_TRIGGER_ID = "link-vtk-trigger"
_LINK_VTK_FORM_ID = "link-vtk-form"
_DRAFT_METADATA_ID = "draft-metadata"

# #306: identifiers for the "Analüüsi uuesti" confirm modal + its hidden form.
_REANALYZE_MODAL_ID = "reanalyze-draft-modal"
_REANALYZE_TRIGGER_ID = "reanalyze-draft-trigger"
_REANALYZE_FORM_ID = "reanalyze-draft-form"


# ---------------------------------------------------------------------------
# Inline JS payloads
# ---------------------------------------------------------------------------

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

# #306: same shape as ``_DELETE_MODAL_SCRIPT`` — the "Analüüsi uuesti"
# trigger opens the confirm modal, the modal's confirm button fires the
# hidden HTMX form's submit. Kept as a separate script so the IDs are
# scoped per-action and the two flows can co-exist on the same page
# without colliding event listeners.
_REANALYZE_MODAL_SCRIPT = (
    "(function () {\n"
    f"  var trigger = document.getElementById('{_REANALYZE_TRIGGER_ID}');\n"
    f"  var confirmBtn = document.getElementById('{_REANALYZE_MODAL_ID}-confirm');\n"
    f"  var form = document.getElementById('{_REANALYZE_FORM_ID}');\n"
    "  if (!trigger || !confirmBtn || !form || !window.Modal) return;\n"
    "  trigger.addEventListener('click', function (evt) {\n"
    "    evt.preventDefault();\n"
    f"    window.Modal.open('{_REANALYZE_MODAL_ID}');\n"
    "  });\n"
    "  confirmBtn.addEventListener('click', function () {\n"
    f"    window.Modal.close('{_REANALYZE_MODAL_ID}');\n"
    "    if (window.htmx && typeof window.htmx.trigger === 'function') {\n"
    "      window.htmx.trigger(form, 'submit');\n"
    "    } else {\n"
    "      form.submit();\n"
    "    }\n"
    "  });\n"
    "})();\n"
)


# ---------------------------------------------------------------------------
# Modal builders
# ---------------------------------------------------------------------------


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
