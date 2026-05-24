"""TopBar — site header with logo, nav, user menu.

Layout contract (post-B1, epic #784)
------------------------------------
Authenticated pages render a **two-row** TopBar on desktop and tablet:

* Row 1 — logo (left) · global search bar (centre) · notifications + user
  menu (right). The search bar is the visible-bar B1 affordance; see
  :mod:`app.ui.components.global_search` for the why.
* Row 2 — primary nav (Analüüsikeskus, Eelnõud, Õiguskaart, Koostaja,
  Nõustaja). Sits directly under the search bar so the nav stays prominent
  even with the new search.

On viewports ``≤768px`` the layout collapses to a single row: logo · search
icon button · user menu. The icon button links to ``/search`` (full-screen
input page); the nav drops into the sidebar (already mobile-hidden).
Unauthenticated visits (no ``user``) render only the logo + user menu —
no search bar, no nav row.
"""

from fasthtml.common import *  # noqa: F403

from app.auth.provider import UserDict
from app.ui.components.global_search import GlobalSearchBar, GlobalSearchMobileButton
from app.ui.forms.app_form import AppForm

# Inline JS for the notification bell. Extracted to a module-level
# constant so the FT component stays readable. The script runs once on
# DOM ready and opens a WebSocket to ``/ws/notifications`` for
# real-time badge updates (#180); the existing 30 s polling endpoint is
# kept as a fallback. The dropdown toggle is handled here too because
# it depends on the same DOM subtree.
_NOTIFICATION_BELL_JS = """
document.addEventListener('click', function (e) {
    var bell = document.querySelector('.notification-bell');
    var dropdown = document.getElementById('notification-dropdown');
    if (dropdown && bell && !bell.contains(e.target)) {
        dropdown.replaceChildren();
        dropdown.classList.remove('notification-dropdown--open');
    }
    if (dropdown && bell && bell.contains(e.target)) {
        dropdown.classList.toggle('notification-dropdown--open');
    }
});

(function () {
    if (typeof window === 'undefined' || !('WebSocket' in window)) return;
    var reconnectDelay = 5000;
    var ws = null;
    function bumpBadge() {
        var badge = document.getElementById('bell-badge');
        if (!badge) return;
        var current = parseInt(badge.textContent || '0', 10);
        if (isNaN(current)) current = 0;
        var next = current + 1;
        badge.textContent = next < 100 ? String(next) : '99+';
        badge.classList.remove('bell-badge--hidden');
    }
    function connect() {
        var proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        try {
            ws = new WebSocket(proto + '//' + window.location.host + '/ws/notifications');
        } catch (e) {
            setTimeout(connect, reconnectDelay);
            return;
        }
        ws.addEventListener('message', function (ev) {
            var data;
            try { data = JSON.parse(ev.data); } catch (e) { return; }
            if (!data || data.type !== 'notification') return;
            bumpBadge();
            var poll = document.getElementById('bell-poll');
            if (poll && window.htmx) { window.htmx.trigger(poll, 'refresh'); }
        });
        ws.addEventListener('close', function () {
            ws = null;
            setTimeout(connect, reconnectDelay);
        });
        ws.addEventListener('error', function () {
            try { if (ws) ws.close(); } catch (e) {}
        });
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', connect);
    } else {
        connect();
    }
})();
"""


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

    - Opens ``/ws/notifications`` for real-time badge updates (#180).
    - Polls ``/api/notifications/unread-count`` every 30s as a fallback
      so a dropped WS does not leave the badge stale.
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
        # Badge poll: swap just the badge span every 30 seconds. Kept
        # as a fallback so a dropped or proxied-down WebSocket does
        # not leave the badge stale (#180).
        Span(  # noqa: F405
            hx_get="/api/notifications/unread-count",
            hx_trigger="load, every 30s, refresh",
            hx_swap="none",
            # The endpoint returns an OOB-swapped badge span that
            # replaces #bell-badge in-place regardless of hx_swap.
            id="bell-poll",
            cls="hidden",
        ),
        Script(_NOTIFICATION_BELL_JS),  # noqa: F405
        cls="notification-bell",
    )


def TopBar(  # noqa: ANN201
    user: UserDict | None = None,
    theme: str = "dark",  # retained for caller compatibility; the UI is dark-only now
    unread_count: int = 0,
):
    """Site topbar with logo, nav, notifications, user menu."""
    del theme  # dark-only UI; accepted for back-compat with existing callers

    logo = A(  # noqa: F405
        Span("Seadusloome", cls="logo-text"),  # noqa: F405
        href="/",
        cls="logo",
    )

    if not user:
        # Unauthenticated TopBar — only logo + login link, single row.
        return Header(  # noqa: F405
            Div(  # noqa: F405
                logo,
                Div(  # noqa: F405
                    UserMenu(user),
                    cls="top-actions",
                ),
                cls="top-bar-inner",
            ),
            cls="top-bar",
        )

    return Header(  # noqa: F405
        # Row 1 — logo · search bar · actions. Search slots in for tablet
        # and up; on mobile the inline bar is hidden via CSS and the
        # mobile button (lives in .top-actions) takes its place.
        Div(  # noqa: F405
            logo,
            Div(  # noqa: F405
                GlobalSearchBar(),
                cls="top-bar-search",
            ),
            Div(  # noqa: F405
                GlobalSearchMobileButton(),
                NotificationBell(unread_count),
                UserMenu(user),
                cls="top-actions",
            ),
            cls="top-bar-inner top-bar-inner--row1",
        ),
        # Row 2 — primary nav. Drops on mobile (sidebar already hidden too;
        # the mobile nav story lives outside this issue).
        Div(  # noqa: F405
            Nav(  # noqa: F405
                A("Analüüsikeskus", href="/analyysikeskus"),  # noqa: F405
                A("Eelnõud", href="/drafts"),  # noqa: F405
                A("Õiguskaart", href="/explorer"),  # noqa: F405
                A("Koostaja", href="/drafter"),  # noqa: F405
                A("Nõustaja", href="/chat"),  # noqa: F405
                cls="top-nav",
                aria_label="Põhinavigatsioon",
            ),
            cls="top-bar-inner top-bar-inner--row2",
        ),
        cls="top-bar top-bar--two-row",
    )
