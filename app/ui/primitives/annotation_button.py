"""AnnotationButton — small trigger button for loading annotation popovers.

Shows an annotation icon + count badge. On click, loads the annotation
popover via HTMX into a sibling container.
"""

from __future__ import annotations

from urllib.parse import urlencode

from fasthtml.common import *  # noqa: F403

from app.annotations.row_keys import target_dom_id
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
        The ID of the annotated target.  Ontology URIs are accepted as-is —
        they are hashed via :func:`target_dom_id` for the CSS id and
        URL-encoded via :func:`urlencode` for the HTMX query string.
    count:
        Number of existing annotations to display in the badge.
    """
    # #773: derive a CSS-safe DOM id from a sha256-truncated hash of the
    # raw target_id so URIs (with ``/``, ``:``, ``#``) don't break HTMX's
    # ``hx-target="#..."`` selector. The original target_id is exposed as
    # a data attribute on the wrapper for round-trip identification.
    popover_id = target_dom_id(target_type, target_id)

    # #773: build the HTMX URL with structured encoding so reserved
    # characters in target_type / target_id are escaped exactly once.
    qs = urlencode({"target_type": target_type, "target_id": target_id})
    hx_get_url = f"/api/annotations?{qs}"

    badge = Badge(str(count), variant="primary", cls="annotation-count-badge") if count > 0 else ""

    btn = Button(  # noqa: F405
        NotStr("&#128172;"),  # speech balloon character
        badge,
        type="button",
        hx_get=hx_get_url,
        hx_target=f"#{popover_id}",
        hx_swap="innerHTML",
        cls="annotation-button",
        aria_label=f"Lisa märkus sellele reale ({count})",
        # #615: spell out what the button does on hover. The old
        # "Markused" tooltip didn't convey that clicking lets the user
        # add a new annotation for the team.
        title="Lisa märkus sellele reale",
    )

    container = Div(id=popover_id, cls="annotation-popover-container")  # noqa: F405

    return Div(  # noqa: F405
        btn,
        container,
        cls="annotation-button-wrapper",
        # The original raw target_id stays here so JS / test code can find
        # the wrapper by the entity URI without re-hashing.
        data_target_type=target_type,
        data_target_id=target_id,
    )
