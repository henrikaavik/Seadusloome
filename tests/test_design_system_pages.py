"""Smoke tests for the live /design-system reference pages.

Access to ``/design-system`` is restricted to admin users via the
``require_role('admin')`` decorator. The tests below verify that
unauthenticated visitors are redirected to ``/auth/login`` and that the
section handlers themselves render successfully when called directly with
an admin auth scope.
"""

from __future__ import annotations

from typing import Any

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

from app.main import app
from app.ui import design_system_pages as ds
from app.ui.design_system_pages import SECTIONS

_SECTION_SLUGS = [slug for slug, _label, _desc in SECTIONS]

_SLUG_TO_HANDLER = {
    "colors": ds._colors_page,
    "typography": ds._typography_page,
    "buttons": ds._buttons_page,
    "forms": ds._forms_page,
    "surfaces": ds._surfaces_page,
    "feedback": ds._feedback_page,
    "data": ds._data_page,
    "navigation": ds._navigation_page,
    "modals": ds._modals_page,
    "icons": ds._icons_page,
}


def _admin_request(path: str = "/design-system") -> Request:
    """Build a Starlette Request with an admin user already on ``scope['auth']``."""
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": b"",
        "headers": [],
        "root_path": "",
        "path_params": {},
        "auth": {
            "id": "admin-1",
            "email": "admin@example.ee",
            "full_name": "Admin Tester",
            "role": "admin",
            "org_id": None,
        },
    }
    return Request(scope)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, follow_redirects=False)


# ---------------------------------------------------------------------------
# Auth gating
# ---------------------------------------------------------------------------


def test_index_redirects_anonymous_to_login(client: TestClient):
    response = client.get("/design-system")
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


@pytest.mark.parametrize("slug", _SECTION_SLUGS)
def test_section_redirects_anonymous_to_login(client: TestClient, slug: str):
    response = client.get(f"/design-system/{slug}")
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


# ---------------------------------------------------------------------------
# Rendering — call handlers directly with an admin scope
# ---------------------------------------------------------------------------


def test_index_renders_with_admin_scope():
    rendered = ds.design_system_index(_admin_request())
    body = "".join(str(part) for part in rendered)
    assert "Disainisüsteem" in body
    for slug in _SECTION_SLUGS:
        assert f"/design-system/{slug}" in body


def test_index_uses_page_shell():
    rendered = ds.design_system_index(_admin_request())
    body = "".join(str(part) for part in rendered)
    assert 'class="top-bar"' in body
    assert "Seadusloome" in body


@pytest.mark.parametrize("slug", _SECTION_SLUGS)
def test_section_page_renders(slug: str):
    handler = _SLUG_TO_HANDLER[slug]
    rendered = handler(_admin_request(f"/design-system/{slug}"))
    body = "".join(str(part) for part in rendered)
    assert 'class="top-bar"' in body
    assert "Disainisüsteem" in body


def test_colors_page_shows_hex_codes():
    rendered = ds._colors_page(_admin_request("/design-system/colors"))
    body = "".join(str(part) for part in rendered)
    assert "#0030DE" in body
    assert "--estonian-blue" in body


def test_buttons_page_renders_variants():
    rendered = ds._buttons_page(_admin_request("/design-system/buttons"))
    body = "".join(str(part) for part in rendered)
    for variant in ("primary", "secondary", "ghost", "danger"):
        assert f"btn-{variant}" in body
