"""Modal and ConfirmModal — overlay dialog surfaces.

Per design system spec §4.3 and NFR §10.2:
    - Uses ``role="dialog"`` + ``aria-modal="true"`` + ``aria-labelledby`` pointing
      at the header title for assistive tech
    - Ships with a backdrop, dismiss button, header, scrollable body, and footer
    - Focus trap, Escape-to-close, and focus restoration are handled by the
      companion ``app/static/js/modal.js`` — pages must include it via
      ``ModalScript()`` or load it globally in the page shell

The component renders as a hidden ``<div>`` wrapper; JS flips ``data-open`` and
``hidden`` when ``Modal.open(id)`` / ``Modal.close(id)`` are called.
"""

from typing import Literal

from fasthtml.common import *  # noqa: F403

from app.ui.primitives.button import Button, IconButton

ModalSize = Literal["sm", "md", "lg", "full"]


def ModalHeader(title: str, *, id: str | None = None, dismissible: bool = True):
    """Header row with the title (used as ``aria-labelledby`` target) and close X."""
    parts: list = [H2(title, id=id, cls="modal-title")]  # noqa: F405
    if dismissible:
        parts.append(
            IconButton(
                "x",
                aria_label="Sulge",
                cls="modal-close",
                data_modal_close="",
            )
        )
    return Div(*parts, cls="modal-header")  # noqa: F405


def ModalBody(*children, cls: str = "", **kwargs):
    """Scrollable body container for the modal's main content."""
    classes = f"modal-body {cls}".strip()
    return Div(*children, cls=classes, **kwargs)  # noqa: F405


def ModalFooter(*children, cls: str = "", **kwargs):
    """Footer row for action buttons — aligned flex-end with a small gap."""
    classes = f"modal-footer {cls}".strip()
    return Div(*children, cls=classes, **kwargs)  # noqa: F405


def Modal(
    *children,
    title: str,
    id: str,
    size: ModalSize = "md",
    dismissible: bool = True,
    cls: str = "",
    **kwargs,
):
    """Overlay dialog surface.

    Renders as a hidden wrapper that contains the backdrop + dialog. Pages call
    ``Modal.open('<id>')`` (see ``modal.js``) to reveal it. The dialog is a
    ``role="dialog"`` element with ``aria-modal="true"`` and ``aria-labelledby``
    pointing at the header title element.
    """
    title_id = f"{id}-title"
    classes = f"modal modal-{size} {cls}".strip()
    dialog = Div(
        ModalHeader(title, id=title_id, dismissible=dismissible),
        *children,
        cls=classes,
        role="dialog",
        aria_modal="true",
        aria_labelledby=title_id,
        tabindex="-1",
    )  # noqa: F405
    backdrop = Div(cls="modal-backdrop", data_modal_close="" if dismissible else None)  # noqa: F405
    return Div(  # noqa: F405
        backdrop,
        dialog,
        id=id,
        cls="modal-root",
        hidden=True,
        data_modal_dismissible="true" if dismissible else "false",
        **kwargs,
    )


def ConfirmModal(
    title: str,
    message: str,
    *,
    id: str,
    confirm_label: str = "Kinnita",
    cancel_label: str = "Tühista",
    confirm_variant: Literal["primary", "secondary", "ghost", "danger"] = "primary",
    **kwargs,
):
    """Yes/No confirmation dialog with Estonian defaults."""
    return Modal(
        ModalBody(P(message)),  # noqa: F405
        ModalFooter(
            Button(cancel_label, variant="secondary", data_modal_close=""),
            Button(
                confirm_label,
                variant=confirm_variant,
                data_modal_confirm="",
                id=f"{id}-confirm",
            ),
        ),
        title=title,
        id=id,
        size="sm",
        **kwargs,
    )


def ModalScript():
    """Script tag that loads the modal JS helpers (focus trap, Esc/backdrop close)."""
    return Script(src="/static/js/modal.js", defer=True)  # noqa: F405
