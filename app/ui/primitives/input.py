"""Form input primitives: Input, Textarea, Select, Checkbox, Radio.

Follows the design system spec §4.2 + §5 (form system) and NFR §10
(accessibility — focus rings, contrast, labels, ``aria-invalid`` error state).

All components:
    - Apply ``.input`` base class (or the checkbox/radio variant)
    - Accept a ``cls`` string appended to the defaults
    - Pass arbitrary HTMX / ARIA / data attributes via ``**kwargs``
    - Set ``aria-invalid="true"`` when ``error=True``
"""

from typing import Literal

from fasthtml.common import *  # noqa: F403

InputType = Literal["text", "email", "password", "number", "search", "url", "tel", "date"]


def _input_classes(base: str, error: bool, cls: str) -> str:
    parts = [base]
    if error:
        parts.append("input-error")
    if cls:
        parts.append(cls)
    return " ".join(parts)


def Input(
    name: str,
    *,
    type: InputType = "text",
    value: str | None = None,
    placeholder: str | None = None,
    required: bool = False,
    disabled: bool = False,
    readonly: bool = False,
    error: bool = False,
    cls: str = "",
    **kwargs,
):
    """Single-line text input. ``error`` toggles ``.input-error`` + ``aria-invalid``."""
    attrs: dict = {
        "name": name,
        "type": type,
        "cls": _input_classes("input", error, cls),
    }
    if value is not None:
        attrs["value"] = value
    if placeholder is not None:
        attrs["placeholder"] = placeholder
    if required:
        attrs["required"] = True
    if disabled:
        attrs["disabled"] = True
    if readonly:
        attrs["readonly"] = True
    if error:
        attrs["aria_invalid"] = "true"
    attrs.update(kwargs)
    return ft_hx("input", **attrs)


def Textarea(
    name: str,
    *,
    value: str | None = None,
    placeholder: str | None = None,
    rows: int = 4,
    required: bool = False,
    disabled: bool = False,
    error: bool = False,
    cls: str = "",
    **kwargs,
):
    """Multi-line text input (resize vertical only via ``.input`` CSS)."""
    attrs: dict = {
        "name": name,
        "rows": rows,
        "cls": _input_classes("input input-textarea", error, cls),
    }
    if placeholder is not None:
        attrs["placeholder"] = placeholder
    if required:
        attrs["required"] = True
    if disabled:
        attrs["disabled"] = True
    if error:
        attrs["aria_invalid"] = "true"
    attrs.update(kwargs)
    return ft_hx("textarea", value or "", **attrs)


def Select(
    name: str,
    options: list[tuple[str, str]] | list[str],
    *,
    value: str | None = None,
    required: bool = False,
    disabled: bool = False,
    error: bool = False,
    cls: str = "",
    **kwargs,
):
    """Dropdown select. ``options`` accepts ``[(value, label), ...]`` or ``[str, ...]``."""
    option_tags = []
    for opt in options:
        if isinstance(opt, tuple):
            opt_value, opt_label = opt
        else:
            opt_value = opt_label = opt
        option_tags.append(
            Option(opt_label, value=opt_value, selected=(value is not None and opt_value == value))
        )

    attrs: dict = {
        "name": name,
        "cls": _input_classes("input input-select", error, cls),
    }
    if required:
        attrs["required"] = True
    if disabled:
        attrs["disabled"] = True
    if error:
        attrs["aria_invalid"] = "true"
    attrs.update(kwargs)
    return ft_hx("select", *option_tags, **attrs)


def Checkbox(
    name: str,
    *,
    value: str = "1",
    checked: bool = False,
    label: str | None = None,
    disabled: bool = False,
    cls: str = "",
    **kwargs,
):
    """Checkbox input. If ``label`` is given, wrap in a ``.check-label`` element."""
    attrs: dict = {
        "type": "checkbox",
        "name": name,
        "value": value,
        "cls": f"check-input {cls}".strip(),
    }
    if checked:
        attrs["checked"] = True
    if disabled:
        attrs["disabled"] = True
    attrs.update(kwargs)
    box = ft_hx("input", **attrs)
    if label is None:
        return box
    return Label(box, Span(label, cls="check-label-text"), cls="check-label")


def Radio(
    name: str,
    value: str,
    *,
    checked: bool = False,
    label: str | None = None,
    disabled: bool = False,
    cls: str = "",
    **kwargs,
):
    """Radio input. If ``label`` is given, wrap in a ``.check-label`` element."""
    attrs: dict = {
        "type": "radio",
        "name": name,
        "value": value,
        "cls": f"check-input {cls}".strip(),
    }
    if checked:
        attrs["checked"] = True
    if disabled:
        attrs["disabled"] = True
    attrs.update(kwargs)
    box = ft_hx("input", **attrs)
    if label is None:
        return box
    return Label(box, Span(label, cls="check-label-text"), cls="check-label")
