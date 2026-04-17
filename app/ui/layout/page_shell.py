"""PageShell — standard page wrapper used by every route."""

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request

from app.auth.provider import UserDict
from app.ui.feedback.flash import render_flash_toasts
from app.ui.layout.container import Container, ContainerSize
from app.ui.layout.sidebar import Sidebar
from app.ui.layout.top_bar import TopBar

# Inline dismisser — reads ``data-duration`` off each toast that was
# server-seeded into #toast-container and removes it after that many
# milliseconds. Runs once per page load; HTMX swaps into the container
# are re-scanned by re-invoking the function on ``htmx:afterSettle``.
_TOAST_DISMISS_SCRIPT = """
(function () {
  function bind(container) {
    if (!container) return;
    container.querySelectorAll('.toast[data-duration]').forEach(function (toast) {
      if (toast.dataset.bound === '1') return;
      toast.dataset.bound = '1';
      var d = parseInt(toast.getAttribute('data-duration'), 10);
      if (!isNaN(d) && d > 0) {
        setTimeout(function () { toast.remove(); }, d);
      }
    });
  }
  function init() { bind(document.getElementById('toast-container')); }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
  document.body && document.body.addEventListener('htmx:afterSettle', init);
})();
"""


def _head_tags(title: str):  # noqa: ANN202
    """Per-page ``<head>`` tags.

    Only ``<title>`` varies per page — the theme init script, charset,
    viewport, color-scheme and stylesheet ``<link>`` elements live on
    ``fast_app(hdrs=...)`` in ``app.main`` so they land inside ``<head>``
    regardless of which handler runs. Inline ``Script(...)`` / ``Link(...)``
    returned from a handler end up in ``<body>`` and would defeat the FOUC
    guard.
    """
    return (Title(f"{title} — Seadusloome"),)  # noqa: F405


def PageShell(  # noqa: ANN201
    *content,
    title: str,
    user: UserDict | None = None,
    theme: str = "dark",
    active_nav: str | None = None,
    unread_count: int = 0,
    container_size: ContainerSize = "lg",
    request: Request | None = None,
):
    """Wrap page content with topbar, sidebar, and main container.

    Every application page should return PageShell(...) to ensure consistent
    layout and accessibility landmarks. Pass ``request=req`` so any pending
    session-flashed toast messages are drained and rendered into
    ``#toast-container`` (see :mod:`app.ui.feedback.flash`).

    The ``theme`` parameter is retained for caller back-compat after the
    2026-04-16 dark-only migration but is no longer consumed internally —
    TopBar ignores its ``theme`` kwarg too, so we don't forward it.
    """
    del theme  # dark-only UI; accepted for back-compat with existing callers
    flash_toasts = render_flash_toasts(request) if request is not None else []
    return (
        *_head_tags(title),
        A(  # noqa: F405
            "Mine põhisisu juurde",
            href="#main-content",
            cls="skip-to-content",
        ),
        Div(  # noqa: F405
            TopBar(user=user, unread_count=unread_count),
            Div(  # noqa: F405
                Sidebar(user=user, active=active_nav),
                Main(  # noqa: F405
                    Container(*content, size=container_size),
                    cls="main-content",
                    id="main-content",
                ),
                cls="app-body",
            ),
            Div(  # noqa: F405
                *flash_toasts,
                id="toast-container",
                cls="toast-container",
                aria_live="polite",
                aria_atomic="false",
            ),
            Script(_TOAST_DISMISS_SCRIPT),  # noqa: F405
            cls="app-shell",
        ),
    )
