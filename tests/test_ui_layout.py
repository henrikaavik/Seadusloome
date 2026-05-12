"""Smoke tests for layout primitives: Container, Sidebar, TopBar.

These tests render components directly via `to_xml()` and assert on the
resulting HTML. They intentionally avoid importing `app.main` or using
TestClient so they remain fast and isolated from the rest of the app.
"""

from typing import cast, get_args

import pytest
from fasthtml.common import Link, Script, to_xml

from app.auth.provider import UserDict
from app.ui.layout.container import Container, ContainerSize
from app.ui.layout.page_shell import PageShell
from app.ui.layout.sidebar import NAV_ITEMS, Sidebar
from app.ui.layout.top_bar import TopBar

# ---------------------------------------------------------------------------
# Container
# ---------------------------------------------------------------------------

_SIZES: tuple[ContainerSize, ...] = cast(tuple[ContainerSize, ...], get_args(ContainerSize))


@pytest.mark.parametrize("size", _SIZES)
def test_container_size_renders_class(size: ContainerSize):
    html = to_xml(Container("hello", size=size))
    assert f"container-{size}" in html
    assert "container" in html
    assert "hello" in html


def test_container_default_size_is_lg():
    html = to_xml(Container("body"))
    assert "container-lg" in html


def test_container_custom_cls_is_appended():
    html = to_xml(Container("body", cls="my-extra"))
    assert "container" in html
    assert "my-extra" in html


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------


def _user(role: str) -> UserDict:
    return {
        "id": "u-1",
        "email": "u@example.ee",
        "full_name": "Test User",
        "role": role,
        "org_id": None,
        "must_change_password": False,
    }


def test_sidebar_returns_none_for_no_user():
    assert Sidebar(user=None) is None


def test_sidebar_drafter_sees_subset():
    sidebar = Sidebar(user=_user("drafter"))
    assert sidebar is not None
    html = to_xml(sidebar)
    # Drafter visible items (#714: legal-work labels, URLs unchanged)
    assert "Töölaud" in html
    assert "Analüüsikeskus" in html
    assert "Eelnõud" in html
    assert "Õiguskaart" in html
    assert "Koostaja" in html
    assert "Nõustaja" in html
    # Old module names no longer used as nav labels
    assert "Uurija" not in html
    assert "Vestlus" not in html
    # Hidden from drafter
    assert "Kasutajad" not in html
    assert "Administraator" not in html


def test_sidebar_admin_sees_all():
    sidebar = Sidebar(user=_user("admin"))
    assert sidebar is not None
    html = to_xml(sidebar)
    for label, _href, _icon, _roles in NAV_ITEMS:
        assert label in html


def test_sidebar_org_admin_sees_users_but_not_admin():
    sidebar = Sidebar(user=_user("org_admin"))
    assert sidebar is not None
    html = to_xml(sidebar)
    assert "Kasutajad" in html
    assert "Administraator" not in html


def test_sidebar_active_item_marked_aria_current():
    sidebar = Sidebar(user=_user("drafter"), active="/explorer")
    assert sidebar is not None
    html = to_xml(sidebar)
    assert 'aria-current="page"' in html
    assert "sidebar-link active" in html


def test_sidebar_inactive_items_have_no_aria_current():
    sidebar = Sidebar(user=_user("drafter"), active=None)
    assert sidebar is not None
    html = to_xml(sidebar)
    assert 'aria-current="page"' not in html


def test_sidebar_subpath_marks_parent_active():
    """``/admin/audit`` should still highlight the ``/admin`` nav item (#420)."""
    sidebar = Sidebar(user=_user("admin"), active="/admin/audit")
    assert sidebar is not None
    html = to_xml(sidebar)
    # Exactly one active link.
    assert html.count("sidebar-link active") == 1
    # ``Administraator`` is the only admin-only item, so its href is /admin.
    assert 'aria-current="page"' in html


def test_sidebar_root_only_matches_root():
    """A non-root active path must NOT highlight the dashboard via prefix."""
    sidebar = Sidebar(user=_user("drafter"), active="/explorer/foo")
    assert sidebar is not None
    html = to_xml(sidebar)
    assert html.count("sidebar-link active") == 1  # only /explorer
    # /dashboard, /chat etc must not also be marked active
    assert html.count('aria-current="page"') == 1


# ---------------------------------------------------------------------------
# TopBar
# ---------------------------------------------------------------------------


def test_topbar_anonymous_shows_login_link():
    html = to_xml(TopBar(user=None))
    assert "Logi sisse" in html
    assert 'href="/auth/login"' in html
    # No notification bell when no user
    assert "notification-bell" not in html
    # No top-nav when no user
    assert "top-nav" not in html


def test_topbar_with_user_shows_user_menu_and_nav():
    user = _user("drafter")
    user["full_name"] = "Mari Maasikas"
    html = to_xml(TopBar(user=user))
    assert "Mari Maasikas" in html
    assert "user-menu" in html
    # Logout form posts to /auth/logout
    assert 'action="/auth/logout"' in html
    # Top nav links visible (#714 labels)
    assert "Analüüsikeskus" in html
    assert "Eelnõud" in html
    assert "Õiguskaart" in html
    assert "Koostaja" in html
    assert "Nõustaja" in html
    # Notification bell rendered
    assert "notification-bell" in html


def test_topbar_with_user_renders_initials():
    user = _user("drafter")
    user["full_name"] = "Jaan Tamm"
    html = to_xml(TopBar(user=user))
    assert "JT" in html


def test_topbar_has_no_theme_toggle():
    """The theme toggle was removed (#658); the UI is dark-only now."""
    html = to_xml(TopBar(user=None, theme="dark"))
    assert "theme-toggle" not in html
    assert "Teema:" not in html


# ---------------------------------------------------------------------------
# Head tags hoisted by fast_app(hdrs=...) — #400
# ---------------------------------------------------------------------------


def test_head_contains_theme_init_script_and_stylesheets():
    """The FOUC-guarding theme init script and all stylesheets must land in ``<head>``.

    Inline ``Script(...)`` / ``Link(...)`` returned from a handler end up
    in ``<body>`` unless they are passed through ``fast_app(hdrs=...)``,
    so this test safeguards the wiring in ``app/main.py``.
    """
    from bs4 import BeautifulSoup
    from starlette.testclient import TestClient

    from app.main import app

    client = TestClient(app, follow_redirects=False)
    # /auth/login is unauthenticated so we can fetch a full page without
    # faking cookies.
    resp = client.get("/auth/login")
    assert resp.status_code == 200

    soup = BeautifulSoup(resp.text, "html.parser")
    head = soup.find("head")
    assert head is not None, "Rendered page has no <head> element"

    # The theme init script must be an inline script inside <head>.
    inline_scripts = [s for s in head.find_all("script") if s.string]
    assert any("data-theme" in (s.string or "") for s in inline_scripts), (
        "THEME_INIT_SCRIPT missing from <head>"
    )

    # Required stylesheets must all be linked inside <head>.
    head_stylesheets: set[str] = set()
    for link in head.find_all("link"):
        if link.get("rel") == ["stylesheet"]:
            href = link.get("href")
            if isinstance(href, str):
                head_stylesheets.add(href)
    for href in (
        "/static/css/fonts.css",
        "/static/css/tokens.css",
        "/static/css/ui.css",
    ):
        assert href in head_stylesheets, f"{href} missing from <head>"

    # And the per-page <title> must still be present.
    assert head.find("title") is not None


# ---------------------------------------------------------------------------
# PageShell — default container wrapping vs full_bleed; extra_head (#746)
# ---------------------------------------------------------------------------


def test_page_shell_default_wraps_content_in_container():
    html = to_xml(PageShell("hello", title="Test"))
    # The standard shell centres content in a .container (size-lg by default).
    assert "container container-lg" in html
    assert "main-content" in html
    # Default mode is NOT full-bleed.
    assert "main-content--full" not in html
    assert "hello" in html


def test_page_shell_full_bleed_skips_container_and_marks_main():
    html = to_xml(PageShell("canvas-goes-here", title="Õiguskaart", full_bleed=True))
    # full_bleed=True: the <main> carries the modifier class…
    assert "main-content main-content--full" in html
    # …and the content is NOT wrapped in a centring .container.
    assert "container container-lg" not in html
    assert "canvas-goes-here" in html


def test_page_shell_extra_head_elements_appended_after_title():
    """``extra_head=`` FT elements are appended into the head fragment, after
    the standard ``<title>`` — FastHTML hoists everything a handler returns
    that belongs in ``<head>`` (``Script``/``Link``/``Title``) at response
    time, so emitting them here keeps them out of ``<body>``."""
    html = to_xml(
        PageShell(
            "body",
            title="WithExtra",
            extra_head=[
                Script(src="https://example.test/x.js"),
                Link(rel="stylesheet", href="/x.css"),
            ],
        )
    )
    # The standard <title> is still present…
    assert "WithExtra — Seadusloome" in html
    # …and the extra head resources are emitted (before the body landmarks).
    assert "https://example.test/x.js" in html
    assert "/x.css" in html
    title_pos = html.index("WithExtra — Seadusloome")
    skip_pos = html.index("skip-to-content")
    assert title_pos < html.index("https://example.test/x.js") < skip_pos
    assert title_pos < html.index("/x.css") < skip_pos


def test_page_shell_no_extra_head_is_back_compat():
    """Default ``PageShell(...)`` (no ``extra_head``) still emits just the
    standard <title> at the top — the explorer is the only caller passing
    ``extra_head=``."""
    html = to_xml(PageShell("body", title="Plain"))
    assert "Plain — Seadusloome" in html
