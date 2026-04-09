"""Alert — inline message box for info/success/warning/danger feedback."""

from typing import Literal

from fasthtml.common import *  # noqa: F403

AlertVariant = Literal["info", "success", "warning", "danger"]

_ALERT_ICONS: dict[str, str] = {
    "info": "ℹ",
    "success": "✓",
    "warning": "⚠",
    "danger": "✕",
}


def Alert(
    *children,
    variant: AlertVariant = "info",
    title: str | None = None,
    dismissible: bool = False,
    cls: str = "",
    **kwargs,
):
    """Inline message with colored background, icon, and optional title."""
    classes = f"alert alert-{variant} {cls}".strip()
    icon = Span(_ALERT_ICONS[variant], cls="alert-icon", aria_hidden="true")  # noqa: F405
    body_children: list = []
    if title is not None:
        body_children.append(Div(title, cls="alert-title"))  # noqa: F405
    body_children.extend(children)
    body = Div(*body_children, cls="alert-body")  # noqa: F405
    parts: list = [icon, body]
    if dismissible:
        parts.append(
            Button(  # noqa: F405
                "×",
                type="button",
                cls="alert-dismiss",
                aria_label="Sulge",
            )
        )
    return Div(*parts, cls=classes, role="alert", **kwargs)  # noqa: F405
