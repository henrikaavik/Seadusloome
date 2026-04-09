"""Toast notifications — transient feedback messages.

Variants: info, success, warning, danger.

Accessibility: uses role="status" + aria-live="polite" so screen readers
announce messages without stealing focus. Dismiss button is keyboard-
accessible via the button element.

TODO(phase-3): Wire up real-time toasts via WebSocket — the current
auto-dismiss uses a simple setTimeout in the page. Future work will
integrate with FastHTML's setup_toasts() for session-flashed messages
and push new toasts into #toast-container from WS events.
"""

from typing import Literal

from fasthtml.common import Button, Div, Strong

ToastVariant = Literal["info", "success", "warning", "danger"]


def Toast(
    message: str,
    *,
    variant: ToastVariant = "info",
    title: str | None = None,
    duration: int = 5000,
    cls: str = "",
    **kwargs,
):
    """A single toast notification.

    ``duration`` is milliseconds; 0 disables auto-dismiss. The value is
    emitted as ``data-duration`` for the page-level JS dismisser.
    """
    classes = f"toast toast-{variant} {cls}".strip()
    body: list = []
    if title:
        body.append(Strong(title, cls="toast-title"))
    body.append(Div(message, cls="toast-message"))
    return Div(
        Div(*body, cls="toast-body"),
        Button(
            "\u00d7",
            type="button",
            cls="toast-dismiss",
            aria_label="Sulge teade",
            onclick="this.closest('.toast').remove()",
        ),
        cls=classes,
        role="status",
        aria_live="polite",
        data_duration=str(duration),
        **kwargs,
    )


def ToastContainer(*toasts, cls: str = "", **kwargs):
    """Fixed-position container that holds toast notifications.

    PageShell already renders an empty ``#toast-container`` div; this
    helper is used when you want to seed it with initial toasts (e.g.
    flashed messages from a redirect).
    """
    classes = f"toast-container {cls}".strip()
    return Div(
        *toasts,
        id="toast-container",
        cls=classes,
        aria_live="polite",
        aria_atomic="false",
        **kwargs,
    )
