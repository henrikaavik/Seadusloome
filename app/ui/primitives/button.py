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

from app.ui.primitives.icon import Icon, IconSize

ButtonVariant = Literal["primary", "secondary", "ghost", "danger"]
ButtonSize = Literal["sm", "md", "lg"]


def _spinner():
    """Tiny rotating spinner shown inside buttons in the loading state."""
    return Span(cls="btn-spinner", aria_hidden="true")


def _button_icon_size(size: ButtonSize) -> IconSize:
    """Map a button size to the matching Icon size."""
    return "sm" if size == "sm" else "md"


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
        inner.append(Icon(icon, size=_button_icon_size(size)))
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
        Icon(icon, size=_button_icon_size(size)),
        cls=classes,
        type=kwargs.pop("type", "button"),
        aria_label=aria_label,
        **kwargs,
    )
