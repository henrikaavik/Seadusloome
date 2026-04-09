"""EmptyState — centered placeholder shown when a list/table has no items."""

from fasthtml.common import *  # noqa: F403


def EmptyState(
    title: str,
    *,
    message: str | None = None,
    icon: str | None = None,
    action=None,
    cls: str = "",
    **kwargs,
):
    """Friendly 'no data' placeholder.

    ``icon`` is an icon name placeholder (rendered as text until the
    Lucide sprite lands in Issue #55). ``action`` is an optional CTA
    element, e.g. a ``Button(...)``, appended beneath the message.
    """
    classes = f"empty-state {cls}".strip()
    children: list = []
    if icon:
        children.append(
            Div(icon, cls="empty-state-icon", aria_hidden="true"),
        )
    children.append(H3(title, cls="empty-state-title"))
    if message:
        children.append(P(message, cls="empty-state-message"))
    if action is not None:
        children.append(Div(action, cls="empty-state-action"))
    return Div(*children, cls=classes, role="status", **kwargs)
