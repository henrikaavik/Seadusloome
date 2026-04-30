"""Shared helpers for admin sub-modules."""

from __future__ import annotations

from fasthtml.common import *  # noqa: F403

from app.auth.provider import UserDict
from app.ui.layout import PageShell
from app.ui.surfaces.alert import Alert


def _tooltip(text: str):
    """Return a small (?) icon with a CSS-only hover tooltip."""
    return Span("?", cls="admin-tooltip", data_tooltip=text)  # noqa: F405


def _render_admin_error_page(
    *,
    title: str,
    user: UserDict | None,
    theme: str = "dark",
    message: str = "Andmete laadimine ebaõnnestus",
):
    """Return a styled PageShell error fallback for failed admin pages.

    Used by every admin page handler's outermost try/except so that an
    unexpected exception (missing helper, broken backend dependency,
    malformed DB row) renders a banner with a Tagasi link instead of
    leaking a raw 500 to the user. Callers should ``logger.exception``
    BEFORE calling this so the traceback is not lost.
    """
    return PageShell(
        H1(title, cls="page-title"),  # noqa: F405
        Alert(
            P(message),  # noqa: F405
            P(  # noqa: F405
                A("← Tagasi adminipaneelile", href="/admin"),  # noqa: F405
                cls="back-link",
            ),
            variant="danger",
            title="Viga",
        ),
        title=title,
        user=user,
        theme=theme,
        active_nav="/admin",
    )
