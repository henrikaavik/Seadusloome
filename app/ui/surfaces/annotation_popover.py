"""AnnotationPopover — thread popover surface for inline annotation display.

Renders a popover containing:
    - Header with annotation count badge
    - List of existing annotations with replies (pre-rendered items)
    - New annotation form at bottom (textarea + submit)

All interactions are HTMX-powered: submit swaps the popover content.
"""

from __future__ import annotations

from typing import Any

from fasthtml.common import *  # noqa: F403

from app.ui.primitives.badge import Badge


def AnnotationPopover(
    target_type: str,
    target_id: str,
    annotations: list[Any],
    auth: dict[str, Any] | Any = None,
) -> Any:
    """Render an annotation thread popover for inline display.

    Parameters
    ----------
    target_type:
        The type of the annotated target (e.g. ``"draft"``, ``"provision"``).
    target_id:
        The ID of the annotated target.
    annotations:
        Pre-rendered annotation items (FT elements from the route layer).
    auth:
        The authenticated user dict.
    """
    count = len(annotations)

    # Header
    header = Div(  # noqa: F405
        Span("Markused", cls="annotation-popover-title"),  # noqa: F405
        Badge(str(count), variant="primary", cls="annotation-count-badge"),
        cls="annotation-popover-header",
    )

    # Annotation list
    if annotations:
        annotation_list = Div(  # noqa: F405
            *annotations,
            cls="annotation-list",
        )
    else:
        annotation_list = Div(  # noqa: F405
            P("Markuseid pole lisatud.", cls="muted-text"),  # noqa: F405
            cls="annotation-list annotation-list-empty",
        )

    # New annotation form
    new_form = Form(  # noqa: F405
        Input(type="hidden", name="target_type", value=target_type),  # noqa: F405
        Input(type="hidden", name="target_id", value=target_id),  # noqa: F405
        Textarea(  # noqa: F405
            name="content",
            placeholder="Lisa markus...",
            rows="3",
            cls="annotation-form-input",
            required=True,
        ),
        Button(  # noqa: F405
            "Lisa markus",
            type="submit",
            cls="btn btn-primary btn-sm",
        ),
        hx_post="/api/annotations",
        hx_target=f"#annotation-popover-{target_type}-{target_id}",
        hx_swap="outerHTML",
        cls="annotation-form",
    )

    return Div(  # noqa: F405
        header,
        annotation_list,
        Hr(cls="annotation-divider"),  # noqa: F405
        new_form,
        id=f"annotation-popover-{target_type}-{target_id}",
        cls="annotation-popover",
    )
