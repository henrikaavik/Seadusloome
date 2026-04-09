"""PageShell — standard page wrapper used by every route."""

from fasthtml.common import *  # noqa: F403

from app.auth.provider import UserDict
from app.ui.layout.container import Container, ContainerSize
from app.ui.layout.sidebar import Sidebar
from app.ui.layout.top_bar import TopBar
from app.ui.theme import THEME_INIT_SCRIPT


def _head_tags(title: str):  # noqa: ANN202
    """Standard <head> tags for every page."""
    return (
        Title(f"{title} — Seadusloome"),  # noqa: F405
        Meta(charset="utf-8"),  # noqa: F405
        Meta(name="viewport", content="width=device-width, initial-scale=1"),  # noqa: F405
        Meta(name="color-scheme", content="light dark"),  # noqa: F405
        Link(rel="stylesheet", href="/static/css/fonts.css"),  # noqa: F405
        Link(rel="stylesheet", href="/static/css/tokens.css"),  # noqa: F405
        Link(rel="stylesheet", href="/static/css/ui.css"),  # noqa: F405
        Script(THEME_INIT_SCRIPT),  # noqa: F405
    )


def PageShell(  # noqa: ANN201
    *content,
    title: str,
    user: UserDict | None = None,
    theme: str = "system",
    active_nav: str | None = None,
    unread_count: int = 0,
    container_size: ContainerSize = "lg",
):
    """Wrap page content with topbar, sidebar, and main container.

    Every application page should return PageShell(...) to ensure consistent
    layout and accessibility landmarks.
    """
    return (
        *_head_tags(title),
        A(  # noqa: F405
            "Mine põhisisu juurde",
            href="#main-content",
            cls="skip-to-content",
        ),
        Div(  # noqa: F405
            TopBar(user=user, theme=theme, unread_count=unread_count),
            Div(  # noqa: F405
                Sidebar(user=user, active=active_nav),
                Main(  # noqa: F405
                    Container(*content, size=container_size),
                    cls="main-content",
                    id="main-content",
                ),
                cls="app-body",
            ),
            Div(id="toast-container", cls="toast-container"),  # noqa: F405
            cls="app-shell",
        ),
    )
