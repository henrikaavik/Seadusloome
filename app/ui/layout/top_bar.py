"""TopBar — site header with logo, nav, user menu, theme toggle."""

from fasthtml.common import *  # noqa: F403

from app.auth.provider import UserDict
from app.ui.forms.app_form import AppForm

_THEME_CYCLE_JS = (
    "const t = (document.cookie.match(/theme=([^;]+)/) || [])[1] || 'system';"
    "const next = {system:'light', light:'dark', dark:'system'}[t];"
    "document.cookie = 'theme=' + next + '; path=/; max-age=31536000; SameSite=Lax';"
    "if (next === 'system') document.documentElement.removeAttribute('data-theme');"
    "else document.documentElement.setAttribute('data-theme', next);"
    "const icons = {light: '\u2600', dark: '\u263e', system: '\u25d0'};"
    "const labels = {light: 'Hele', dark: 'Tume', system: 'S\u00fcsteem'};"
    "const iconEl = this.querySelector('.theme-toggle-icon');"
    "if (iconEl) iconEl.textContent = icons[next];"
    "this.setAttribute('aria-label', 'Teema: ' + labels[next]);"
)


def ThemeToggle(current_theme: str = "system"):  # noqa: ANN201
    """Theme toggle button cycling light/dark/system entirely on the client.

    The toggle writes the ``theme`` cookie via JavaScript and updates the
    ``data-theme`` attribute on ``<html>`` directly. No request is made to
    the server, which avoids the CSRF surface and the page reload that the
    HTMX-based version required.
    """
    icons = {"light": "\u2600", "dark": "\u263e", "system": "\u25d0"}
    labels = {"light": "Hele", "dark": "Tume", "system": "Süsteem"}
    return Button(  # noqa: F405
        Span(icons.get(current_theme, "\u25d0"), cls="theme-toggle-icon"),  # noqa: F405
        Span(labels.get(current_theme, "Süsteem"), cls="sr-only"),  # noqa: F405
        type="button",
        cls="theme-toggle",
        aria_label=f"Teema: {labels.get(current_theme, 'Süsteem')}",
        onclick=_THEME_CYCLE_JS,
    )


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


def NotificationBell(unread_count: int = 0):  # noqa: ANN201
    """Bell icon with unread count badge. Actual logic added in Phase 2."""
    badge = (
        Span(str(unread_count if unread_count < 100 else "99+"), cls="bell-badge")  # noqa: F405
        if unread_count > 0
        else None
    )
    return A(  # noqa: F405
        Span("🔔", cls="bell-icon", aria_hidden="true"),  # noqa: F405
        badge,
        Span("Teavitused", cls="sr-only"),  # noqa: F405
        href="/notifications",
        cls="notification-bell",
        aria_label=f"Teavitused ({unread_count} lugemata)" if unread_count else "Teavitused",
    )


def TopBar(  # noqa: ANN201
    user: UserDict | None = None,
    theme: str = "system",
    unread_count: int = 0,
):
    """Site topbar with logo, nav, notifications, theme toggle, user menu."""
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
                ThemeToggle(theme),
                UserMenu(user),
                cls="top-actions",
            ),
            cls="top-bar-inner",
        ),
        cls="top-bar",
    )
