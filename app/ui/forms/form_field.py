"""FormField — wraps a label, input, help text, and error into a single row.

Supports live validation via HTMX: the field hits
`/api/validate/{validator_name}` on blur and swaps the error div with the
result. Server-side validation on submit uses the same validator functions
(see validators.py) and re-renders the form with errors populated.
"""

from __future__ import annotations

from typing import Literal

from fasthtml.common import *  # noqa: F403

from app.ui.primitives.input import Input, Select, Textarea

InputType = Literal["text", "email", "password", "number", "search", "url", "tel", "date"]


def FormField(
    name: str,
    label: str,
    *,
    type: InputType = "text",  # noqa: A002 - matches HTML attribute name
    value: str | None = None,
    placeholder: str | None = None,
    required: bool = False,
    disabled: bool = False,
    help: str | None = None,  # noqa: A002
    error: str | None = None,
    validator: str | None = None,
    cls: str = "",
    **kwargs,
):
    """Full form row: label + input + help + error, all linked via IDs.

    When ``validator`` is set, the input triggers an HTMX request on blur
    to `/api/validate/{validator}` which returns a div replacing
    ``#{name}-error`` with either the error message or an empty div.

    The wrapper div gets ``.form-field--error`` when ``error`` is passed,
    so you can style the whole row on validation failure.
    """
    field_id = f"field-{name}"
    error_id = f"{name}-error"
    help_id = f"{name}-help"

    # Link input to help and error for a11y. The error id is referenced
    # only when there is an error currently rendered or a live validator
    # may stream one in via HTMX. Without either, exposing an empty error
    # div via ``aria-describedby`` makes screen readers announce a stray
    # blank label on every focus, which we do not want (#421).
    described_by = []
    if help:
        described_by.append(help_id)
    if error or validator:
        described_by.append(error_id)
    aria_describedby = " ".join(described_by) if described_by else None

    # HTMX live validation hooks
    htmx_attrs: dict = {}
    if validator:
        htmx_attrs = {
            "hx_post": f"/api/validate/{validator}",
            "hx_trigger": "blur",
            "hx_target": f"#{error_id}",
            "hx_swap": "outerHTML",
        }

    input_el = Input(
        name=name,
        type=type,
        id=field_id,
        value=value,
        placeholder=placeholder,
        required=required,
        disabled=disabled,
        error=bool(error),
        aria_describedby=aria_describedby,
        **htmx_attrs,
    )

    wrapper_cls = f"form-field {cls}".strip()
    if error:
        wrapper_cls += " form-field--error"

    label_children = [label]
    if required:
        label_children.append(Span(" *", cls="form-field-required", aria_hidden="true"))

    # Hide the error placeholder from assistive tech when no validator is
    # attached and no error is present, so screen readers do not announce
    # an empty alert region (#421).
    error_div = Div(
        error or "",
        id=error_id,
        cls="form-field-error",
        role="alert" if error else None,
        hidden=(not error and not validator),
    )

    return Div(
        Label(*label_children, fr=field_id, cls="form-field-label"),
        input_el,
        Small(help, id=help_id, cls="form-field-help") if help else None,
        error_div,
        cls=wrapper_cls,
        **kwargs,
    )


def FormTextareaField(
    name: str,
    label: str,
    *,
    value: str | None = None,
    rows: int = 4,
    placeholder: str | None = None,
    required: bool = False,
    disabled: bool = False,
    help: str | None = None,  # noqa: A002
    error: str | None = None,
    cls: str = "",
    **kwargs,
):
    """FormField variant for a Textarea."""
    field_id = f"field-{name}"
    error_id = f"{name}-error"
    help_id = f"{name}-help"
    described_by = []
    if help:
        described_by.append(help_id)
    if error:
        described_by.append(error_id)
    aria_describedby = " ".join(described_by) if described_by else None

    textarea = Textarea(
        name=name,
        id=field_id,
        value=value,
        rows=rows,
        placeholder=placeholder,
        required=required,
        disabled=disabled,
        error=bool(error),
        aria_describedby=aria_describedby,
    )

    wrapper_cls = f"form-field {cls}".strip()
    if error:
        wrapper_cls += " form-field--error"

    label_children: list = [label]
    if required:
        label_children.append(Span(" *", cls="form-field-required", aria_hidden="true"))

    return Div(
        Label(*label_children, fr=field_id, cls="form-field-label"),
        textarea,
        Small(help, id=help_id, cls="form-field-help") if help else None,
        Div(error or "", id=error_id, cls="form-field-error", role="alert" if error else None),
        cls=wrapper_cls,
        **kwargs,
    )


def FormSelectField(
    name: str,
    label: str,
    options: list,
    *,
    value: str | None = None,
    required: bool = False,
    disabled: bool = False,
    help: str | None = None,  # noqa: A002
    error: str | None = None,
    cls: str = "",
    **kwargs,
):
    """FormField variant for a Select."""
    field_id = f"field-{name}"
    error_id = f"{name}-error"
    help_id = f"{name}-help"
    described_by = []
    if help:
        described_by.append(help_id)
    if error:
        described_by.append(error_id)
    aria_describedby = " ".join(described_by) if described_by else None

    select = Select(
        name=name,
        options=options,
        id=field_id,
        value=value,
        required=required,
        disabled=disabled,
        error=bool(error),
        aria_describedby=aria_describedby,
    )

    wrapper_cls = f"form-field {cls}".strip()
    if error:
        wrapper_cls += " form-field--error"

    label_children: list = [label]
    if required:
        label_children.append(Span(" *", cls="form-field-required", aria_hidden="true"))

    return Div(
        Label(*label_children, fr=field_id, cls="form-field-label"),
        select,
        Small(help, id=help_id, cls="form-field-help") if help else None,
        Div(error or "", id=error_id, cls="form-field-error", role="alert" if error else None),
        cls=wrapper_cls,
        **kwargs,
    )
