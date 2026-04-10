"""InfoBox -- contextual help box for onboarding and page guidance.

Unlike ``Alert`` (which communicates transient feedback), InfoBox is
designed for *persistent* instructional text that helps users understand
what a page does and what actions are available.  Three visual variants
map to different information tones:

    info (blue)    -- general explanations, welcome text
    tip  (green)   -- hints, pro-tips, keyboard shortcuts
    warning (yellow) -- caveats, limitations, things to watch out for

An optional ``dismissible=True`` flag adds a close button that removes
the element from the DOM.  Dismissal is JS-only (no server round-trip).
"""

from typing import Literal

from fasthtml.common import *  # noqa: F403

InfoBoxVariant = Literal["info", "tip", "warning"]

_ICONS: dict[str, str] = {
    "info": "\u2139\ufe0f",  # information source
    "tip": "\U0001f4a1",  # light bulb
    "warning": "\u26a0\ufe0f",  # warning sign
}


def InfoBox(
    *children,
    variant: InfoBoxVariant = "info",
    dismissible: bool = False,
    cls: str = "",
    **kwargs,
):
    """Contextual help box with icon, content, and optional dismiss button.

    Positional args are treated as children of the content area.
    """
    icon = Span(_ICONS.get(variant, _ICONS["info"]), cls="info-box-icon", aria_hidden="true")  # noqa: F405
    body = Div(*children, cls="info-box-content")  # noqa: F405
    parts: list = [icon, body]
    if dismissible:
        parts.append(
            Button(  # noqa: F405
                "\u00d7",
                type="button",
                cls="info-box-dismiss",
                aria_label="Sulge",
                onclick="this.parentElement.remove()",
            )
        )
    classes = f"info-box info-box-{variant} {cls}".strip()
    return Div(*parts, cls=classes, role="note", **kwargs)  # noqa: F405
