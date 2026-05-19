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

from app.annotations.row_keys import target_dom_id
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

    # #773: derive the same hashed CSS id as the AnnotationButton primitive
    # so the trigger's ``hx-target`` and the popover's outer id stay in
    # lockstep.  Raw ontology URIs as target_id would otherwise blow up
    # the ``#annotation-popover-...`` selector.
    popover_id = target_dom_id(target_type, target_id)

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

    # New annotation form. The hidden inputs still carry the RAW target_id
    # — the POST handler reads them as form fields, not as URL segments.
    new_form = Form(  # noqa: F405
        Input(type="hidden", name="target_type", value=target_type),  # noqa: F405
        Input(type="hidden", name="target_id", value=target_id),  # noqa: F405
        Textarea(  # noqa: F405
            name="content",
            placeholder="Lisa markus...",
            rows="3",
            cls="annotation-form-input",
            # #813: HTML4 string form survives FastHTML's HTTP renderer.
            required="required",
        ),
        Button(  # noqa: F405
            "Lisa markus",
            type="submit",
            cls="btn btn-primary btn-sm",
        ),
        hx_post="/api/annotations",
        hx_target=f"#{popover_id}",
        hx_swap="outerHTML",
        cls="annotation-form",
    )

    return Div(  # noqa: F405
        header,
        annotation_list,
        Hr(cls="annotation-divider"),  # noqa: F405
        new_form,
        id=popover_id,
        cls="annotation-popover",
        # Data attrs preserve the original URI for the round-trip identity.
        data_target_type=target_type,
        data_target_id=target_id,
    )
