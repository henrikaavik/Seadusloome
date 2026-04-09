"""Container — max-width wrapper."""

from typing import Literal

from fasthtml.common import *  # noqa: F403

ContainerSize = Literal["sm", "md", "lg", "xl", "full"]


def Container(*children, size: ContainerSize = "lg", cls: str = "", **kwargs):  # noqa: F405
    """Max-width centered wrapper.

    Sizes map to tokens.css --container-* variables.
    """
    classes = f"container container-{size} {cls}".strip()
    return Div(*children, cls=classes, **kwargs)  # noqa: F405
