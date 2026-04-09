"""Button and IconButton primitives.

Follows the design system spec §4.2 (component API conventions):
    - Content as positional ``*children``
    - Style variants via ``variant`` / ``size`` literals
    - Custom classes appended via ``cls``
    - Arbitrary HTMX / ARIA attributes passed through via ``**kwargs``

Accessibility (NFR §10):
    - ``:focus-visible`` ring via ``.btn`` base class in ``ui.css``
    - ``IconButton`` requires an ``aria_label`` for screen readers
    - Loading state disables interaction so assistive tech sees ``disabled``
"""

from typing import Literal

from fasthtml.common import *  # noqa: F403

ButtonVariant = Literal["primary", "secondary", "ghost", "danger"]
ButtonSize = Literal["sm", "md", "lg"]


def _spinner():
    """Tiny rotating spinner shown inside buttons in the loading state."""
    return Span(cls="btn-spinner", aria_hidden="true")


def _icon_placeholder(name: str):
    """Placeholder for Lucide icon integration — rendered as a span for now."""
    return Span(cls=f"btn-icon-glyph icon-{name}", data_icon=name, aria_hidden="true")


def Button(
    *children,
    variant: ButtonVariant = "primary",
    size: ButtonSize = "md",
    type: str = "button",
    disabled: bool = False,
    loading: bool = False,
    icon: str | None = None,
    cls: str = "",
    **kwargs,
):
    """Styled button with variant + size options and optional loading state."""
    classes = f"btn btn-{variant} btn-{size}"
    if cls:
        classes = f"{classes} {cls}"
    if disabled or loading:
        classes = f"{classes} btn-disabled"

    inner: list = []
    if loading:
        inner.append(_spinner())
    elif icon:
        inner.append(_icon_placeholder(icon))
    inner.extend(children)

    return ft_hx(
        "button",
        *inner,
        cls=classes,
        type=type,
        disabled=(disabled or loading),
        **kwargs,
    )


def IconButton(
    icon: str,
    *,
    variant: ButtonVariant = "ghost",
    size: ButtonSize = "md",
    aria_label: str,
    cls: str = "",
    **kwargs,
):
    """Icon-only button. ``aria_label`` is required for accessibility (NFR §10)."""
    if not aria_label:
        raise ValueError("IconButton requires a non-empty aria_label for accessibility.")

    classes = f"btn btn-{variant} btn-{size} btn-icon"
    if cls:
        classes = f"{classes} {cls}"

    return ft_hx(
        "button",
        _icon_placeholder(icon),
        cls=classes,
        type=kwargs.pop("type", "button"),
        aria_label=aria_label,
        **kwargs,
    )
