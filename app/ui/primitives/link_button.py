"""LinkButton primitive — anchor styled like a Button (#632).

A real ``<a>`` element with the same ``btn btn-{variant} btn-{size}`` class
contract as ``Button``. Use this when the action navigates to a URL; use
``Button`` when the action submits a form or fires HTMX.

Follows the design system spec §4.2 (component API conventions):
    - Content as positional ``*children``
    - Style variants via ``variant`` / ``size`` literals
    - Custom classes appended via ``cls``
    - Arbitrary HTMX / ARIA attributes passed through via ``**kwargs``
"""

from fasthtml.common import *  # noqa: F403

from app.ui.primitives.button import ButtonSize, ButtonVariant, _button_icon_size
from app.ui.primitives.icon import Icon


def LinkButton(
    *children,
    href: str,
    variant: ButtonVariant = "primary",
    size: ButtonSize = "md",
    icon: str | None = None,
    cls: str = "",
    **kwargs,
):
    """Anchor styled with the Button class contract."""
    classes = f"btn btn-{variant} btn-{size}"
    if cls:
        classes = f"{classes} {cls}"

    inner: list = []
    if icon:
        inner.append(Icon(icon, size=_button_icon_size(size)))
    inner.extend(children)

    return ft_hx(  # noqa: F405
        "a",
        *inner,
        href=href,
        cls=classes,
        **kwargs,
    )
