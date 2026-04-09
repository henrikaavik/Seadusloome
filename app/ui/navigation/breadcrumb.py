"""Breadcrumb hierarchical navigation component.

Design system spec §4.3 + NFR §10 (accessibility):
    - Wrapped in ``<nav aria-label="Breadcrumb">`` for landmark semantics.
    - Ordered list ``<ol>`` expresses hierarchy for screen readers.
    - Last item uses ``aria-current="page"`` and is not linked.
    - Chevron separators (``›``) are ``aria-hidden`` so SR users hear only labels.
"""

from fasthtml.common import *  # noqa: F403

BreadcrumbItem = tuple[str, str] | str


def Breadcrumb(*items: BreadcrumbItem, cls: str = "", **kwargs):
    """Render a hierarchical breadcrumb trail.

    Each positional ``item`` is either:
        - ``(label, href)`` — linked ancestor crumb
        - ``str`` — current page (use for the last item)

    The last item is always rendered as ``aria-current="page"`` regardless of
    its shape (tuple or string) to guarantee correct semantics.
    """
    classes = f"breadcrumb {cls}".strip()
    total = len(items)
    li_nodes: list = []

    for index, item in enumerate(items):
        is_last = index == total - 1
        label = item[0] if isinstance(item, tuple) else item
        href = item[1] if isinstance(item, tuple) else None

        if is_last:
            crumb = Li(label, aria_current="page", cls="breadcrumb-current")
        else:
            link = A(label, href=href or "#")
            crumb = Li(link, cls="breadcrumb-item")
        li_nodes.append(crumb)

        if not is_last:
            li_nodes.append(
                Li("\u203a", aria_hidden="true", cls="breadcrumb-separator"),
            )

    return ft_hx(
        "nav",
        Ol(*li_nodes, cls="breadcrumb-list"),
        aria_label="Breadcrumb",
        cls=classes,
        **kwargs,
    )
