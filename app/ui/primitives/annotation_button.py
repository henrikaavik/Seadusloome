"""AnnotationButton — small trigger button for loading annotation popovers.

Shows an annotation icon + count badge. On click, loads the annotation
popover via HTMX into a sibling container.
"""

from __future__ import annotations

from fasthtml.common import *  # noqa: F403

from app.ui.primitives.badge import Badge


def AnnotationButton(
    target_type: str,
    target_id: str,
    count: int = 0,
) -> Any:  # noqa: F405
    """Small button that loads the annotation popover via HTMX.

    Parameters
    ----------
    target_type:
        The type of the annotated target (e.g. ``"draft"``, ``"provision"``).
    target_id:
        The ID of the annotated target.
    count:
        Number of existing annotations to display in the badge.
    """
    popover_id = f"annotation-popover-{target_type}-{target_id}"

    badge = Badge(str(count), variant="primary", cls="annotation-count-badge") if count > 0 else ""

    btn = Button(  # noqa: F405
        NotStr("&#128172;"),  # speech balloon character
        badge,
        type="button",
        hx_get=f"/api/annotations?target_type={target_type}&target_id={target_id}",
        hx_target=f"#{popover_id}",
        hx_swap="innerHTML",
        cls="annotation-button",
        aria_label=f"Markused ({count})",
        title="Markused",
    )

    container = Div(id=popover_id, cls="annotation-popover-container")  # noqa: F405

    return Div(  # noqa: F405
        btn,
        container,
        cls="annotation-button-wrapper",
    )
