"""Badge — small pill-shaped status marker and semantic StatusBadge."""

from typing import Literal

from fasthtml.common import *  # noqa: F403

BadgeVariant = Literal["default", "primary", "success", "warning", "danger"]

StatusKey = Literal["ok", "running", "pending", "failed", "warning"]

_STATUS_META: dict[str, tuple[BadgeVariant, str]] = {
    "ok": ("success", "OK"),
    "running": ("primary", "Töötab"),
    "pending": ("default", "Ootel"),
    "failed": ("danger", "Ebaõnnestus"),
    "warning": ("warning", "Hoiatus"),
}


def Badge(*children, variant: BadgeVariant = "default", cls: str = "", **kwargs):
    """Inline pill-shaped label used for counts, tags, and quick status."""
    classes = f"badge badge-{variant} {cls}".strip()
    return Span(*children, cls=classes, **kwargs)  # noqa: F405


def StatusBadge(status: StatusKey, cls: str = "", **kwargs):
    """Semantic status marker with a colored dot and Estonian label."""
    variant, label = _STATUS_META[status]
    classes = f"badge badge-{variant} status-badge status-{status} {cls}".strip()
    return Span(  # noqa: F405
        Span("", cls="status-dot", aria_hidden="true"),  # noqa: F405
        label,
        cls=classes,
        role="status",
        **kwargs,
    )
