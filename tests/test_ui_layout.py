"""Smoke tests for layout primitives: Container, Sidebar, TopBar.

These tests render components directly via `to_xml()` and assert on the
resulting HTML. They intentionally avoid importing `app.main` or using
TestClient so they remain fast and isolated from the rest of the app.
"""

from typing import cast, get_args

import pytest
from fasthtml.common import to_xml

from app.auth.provider import UserDict
from app.ui.layout.container import Container, ContainerSize
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
    }


def test_sidebar_returns_none_for_no_user():
    assert Sidebar(user=None) is None


def test_sidebar_drafter_sees_subset():
    sidebar = Sidebar(user=_user("drafter"))
    assert sidebar is not None
    html = to_xml(sidebar)
    # Drafter visible items
    assert "Töölaud" in html
    assert "Uurija" in html
    assert "Eelnõud" in html
    assert "Vestlus" in html
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
    # Top nav links visible
    assert "Uurija" in html
    assert "Eelnõud" in html
    assert "Vestlus" in html
    # Notification bell rendered
    assert "notification-bell" in html


def test_topbar_with_user_renders_initials():
    user = _user("drafter")
    user["full_name"] = "Jaan Tamm"
    html = to_xml(TopBar(user=user))
    assert "JT" in html


def test_topbar_theme_toggle_present():
    html = to_xml(TopBar(user=None, theme="dark"))
    assert "theme-toggle" in html
