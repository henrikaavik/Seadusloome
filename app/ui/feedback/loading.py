"""Loading indicators: LoadingSpinner and Skeleton placeholders."""

from typing import Literal

from fasthtml.common import *  # noqa: F403

SpinnerSize = Literal["sm", "md", "lg"]
SkeletonVariant = Literal["text", "card", "avatar"]


def LoadingSpinner(
    *,
    size: SpinnerSize = "md",
    cls: str = "",
    aria_label: str = "Laadimine...",
    **kwargs,
):
    """Rotating circular spinner.

    Renders a ``role="status"`` element with a visually-hidden label so
    assistive tech announces the loading state.
    """
    classes = f"loading-spinner loading-spinner-{size} {cls}".strip()
    return Span(
        Span(cls="loading-spinner-circle", aria_hidden="true"),
        Span(aria_label, cls="sr-only"),
        cls=classes,
        role="status",
        **kwargs,
    )


def Skeleton(*, variant: SkeletonVariant = "text", cls: str = "", **kwargs):
    """Shimmer placeholder shown while content loads.

    Marked with ``aria-busy="true"`` + ``aria-live="polite"`` so screen
    readers know the region is loading and will be updated.
    """
    classes = f"skeleton skeleton-{variant} {cls}".strip()
    return Div(
        cls=classes,
        aria_busy="true",
        aria_live="polite",
        aria_label="Sisu laadimine",
        **kwargs,
    )
