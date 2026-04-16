"""TopBar — site header with logo, nav, user menu."""

from fasthtml.common import *  # noqa: F403

from app.auth.provider import UserDict
from app.ui.forms.app_form import AppForm


def UserMenu(user: UserDict | None):  # noqa: ANN201
    """User dropdown menu in the topbar."""
    if user is None:
        return A("Logi sisse", href="/auth/login", cls="user-menu-login")  # noqa: F405

    initials = "".join(p[:1] for p in user["full_name"].split()[:2]).upper() or "U"
    return Div(  # noqa: F405
        Div(  # noqa: F405
            Span(initials, cls="user-avatar"),  # noqa: F405
            Span(user["full_name"], cls="user-name"),  # noqa: F405
            cls="user-menu-trigger",
            tabindex="0",
        ),
        Div(  # noqa: F405
            A("Töölaud", href="/dashboard"),  # noqa: F405
            A("Minu profiil", href="/profile"),  # noqa: F405
            Hr(),  # noqa: F405
            AppForm(
                Button("Logi välja", type="submit", cls="user-menu-logout"),  # noqa: F405
                method="post",
                action="/auth/logout",
            ),
            cls="user-menu-dropdown",
        ),
        cls="user-menu",
    )


def _bell_badge(unread_count: int):  # noqa: ANN201
    """Inner badge element — returned by the polling endpoint too."""
    if unread_count > 0:
        return Span(  # noqa: F405
            str(unread_count if unread_count < 100 else "99+"),
            cls="bell-badge",
            id="bell-badge",
        )
    return Span(id="bell-badge", cls="bell-badge bell-badge--hidden")  # noqa: F405


def NotificationBell(unread_count: int = 0):  # noqa: ANN201
    """Bell icon with unread count badge and HTMX-powered dropdown.

    - Polls ``/api/notifications/unread-count`` every 30s to update the badge.
    - Click toggles a dropdown loaded via ``hx_get="/api/notifications?limit=5"``.
    """
    return Div(  # noqa: F405
        Button(  # noqa: F405
            Span("\U0001f514", cls="bell-icon", aria_hidden="true"),  # noqa: F405
            _bell_badge(unread_count),
            Span("Teavitused", cls="sr-only"),  # noqa: F405
            type="button",
            cls="notification-bell-trigger",
            aria_label=(f"Teavitused ({unread_count} lugemata)" if unread_count else "Teavitused"),
            hx_get="/api/notifications?limit=5",
            hx_target="#notification-dropdown",
            hx_swap="innerHTML",
            hx_trigger="click",
        ),
        Div(id="notification-dropdown", cls="notification-dropdown"),  # noqa: F405
        # Badge poll: swap just the badge span every 30 seconds.
        Span(  # noqa: F405
            hx_get="/api/notifications/unread-count",
            hx_trigger="load, every 30s",
            hx_swap="none",
            # The endpoint returns an OOB-swapped badge span that
            # replaces #bell-badge in-place regardless of hx_swap.
            id="bell-poll",
            cls="hidden",
        ),
        Script(  # noqa: F405
            """
            document.addEventListener('click', function(e) {
                var bell = document.querySelector('.notification-bell');
                var dropdown = document.getElementById('notification-dropdown');
                if (dropdown && bell && !bell.contains(e.target)) {
                    dropdown.innerHTML = '';
                    dropdown.classList.remove('notification-dropdown--open');
                }
                if (dropdown && bell && bell.contains(e.target)) {
                    dropdown.classList.toggle('notification-dropdown--open');
                }
            });
            """
        ),
        cls="notification-bell",
    )


def TopBar(  # noqa: ANN201
    user: UserDict | None = None,
    theme: str = "dark",  # retained for caller compatibility; the UI is dark-only now
    unread_count: int = 0,
):
    """Site topbar with logo, nav, notifications, user menu."""
    del theme  # dark-only UI; accepted for back-compat with existing callers
    return Header(  # noqa: F405
        Div(  # noqa: F405
            A(  # noqa: F405
                Span("Seadusloome", cls="logo-text"),  # noqa: F405
                href="/",
                cls="logo",
            ),
            Nav(  # noqa: F405
                A("Uurija", href="/explorer"),  # noqa: F405
                A("Eelnõud", href="/drafts"),  # noqa: F405
                A("Vestlus", href="/chat"),  # noqa: F405
                cls="top-nav",
            )
            if user
            else None,
            Div(  # noqa: F405
                NotificationBell(unread_count) if user else None,
                UserMenu(user),
                cls="top-actions",
            ),
            cls="top-bar-inner",
        ),
        cls="top-bar",
    )
