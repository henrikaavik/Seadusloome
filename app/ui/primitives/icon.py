"""Icon primitive — SVG sprite reference wrapper for Lucide icons.

Icons are delivered as a single self-hosted SVG sprite at
``/static/icons/sprite.svg``. Each icon inside the sprite is a ``<symbol>``
with an ``id`` matching the Lucide icon name (e.g. ``check-circle``).

Usage::

    Icon("check")                             # decorative (aria-hidden)
    Icon("alert-circle", size="lg",
         aria_label="Viga")                    # semantic (aria-label set)

Accessibility (NFR §10):
    - Icons are decorative by default (``aria-hidden="true"``).
    - Pass ``aria_label`` to promote an icon to a semantic image.
"""

from typing import Literal

from fasthtml.common import *  # noqa: F403

IconSize = Literal["sm", "md", "lg"]

SPRITE_URL = "/static/icons/sprite.svg"


def Icon(
    name: str,
    *,
    size: IconSize = "md",
    cls: str = "",
    aria_label: str | None = None,
    aria_hidden: bool | None = None,
    **kwargs,
):
    """Render a Lucide icon from the self-hosted sprite.

    If ``aria_label`` is supplied, the icon is treated as semantic
    (role=img, labelled). Otherwise it is decorative and hidden from
    assistive technology.
    """
    classes = f"icon icon-{size}"
    if cls:
        classes = f"{classes} {cls}"

    attrs: dict = {"cls": classes}
    if aria_label:
        attrs["role"] = "img"
        attrs["aria_label"] = aria_label
    else:
        # Default to decorative unless caller explicitly overrode aria_hidden.
        attrs["aria_hidden"] = "false" if aria_hidden is False else "true"

    attrs.update(kwargs)

    return ft_hx(
        "svg",
        ft_hx("use", href=f"{SPRITE_URL}#{name}"),
        **attrs,
    )
