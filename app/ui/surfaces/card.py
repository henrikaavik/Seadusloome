"""Card — container surface with optional header, body, and footer sections."""

from typing import Literal

from fasthtml.common import *  # noqa: F403

CardVariant = Literal["default", "bordered", "flat"]


def Card(*children, variant: CardVariant = "default", cls: str = "", **kwargs):
    """Surface container with padding, radius, and subtle shadow.

    Use CardHeader / CardBody / CardFooter as children for sectioned layouts,
    or pass arbitrary content directly for a simple card.
    """
    classes = f"card card-{variant} {cls}".strip()
    return Div(*children, cls=classes, **kwargs)  # noqa: F405


def CardHeader(*children, cls: str = "", **kwargs):
    """Top section of a Card — typically holds a title and actions."""
    classes = f"card-header {cls}".strip()
    return Div(*children, cls=classes, **kwargs)  # noqa: F405


def CardBody(*children, cls: str = "", **kwargs):
    """Main content section of a Card."""
    classes = f"card-body {cls}".strip()
    return Div(*children, cls=classes, **kwargs)  # noqa: F405


def CardFooter(*children, cls: str = "", **kwargs):
    """Bottom section of a Card — typically holds actions or metadata."""
    classes = f"card-footer {cls}".strip()
    return Div(*children, cls=classes, **kwargs)  # noqa: F405
