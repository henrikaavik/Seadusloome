"""Smoke tests for the live /design-system reference pages."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.main import app
from app.ui.design_system_pages import SECTIONS

_SECTION_SLUGS = [slug for slug, _label, _desc in SECTIONS]


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, follow_redirects=False)


def test_index_returns_200(client: TestClient):
    response = client.get("/design-system")
    assert response.status_code == 200


def test_index_contains_links_to_all_sections(client: TestClient):
    response = client.get("/design-system")
    assert response.status_code == 200
    body = response.text
    for slug in _SECTION_SLUGS:
        assert f"/design-system/{slug}" in body


def test_index_shows_admin_only_notice(client: TestClient):
    response = client.get("/design-system")
    assert "admin-rollile" in response.text


def test_index_uses_page_shell(client: TestClient):
    """PageShell always renders a <header class="top-bar">."""
    response = client.get("/design-system")
    assert 'class="top-bar"' in response.text
    assert "Seadusloome" in response.text


@pytest.mark.parametrize("slug", _SECTION_SLUGS)
def test_section_page_returns_200(client: TestClient, slug: str):
    response = client.get(f"/design-system/{slug}")
    assert response.status_code == 200, f"{slug} returned {response.status_code}"


@pytest.mark.parametrize("slug", _SECTION_SLUGS)
def test_section_page_uses_page_shell(client: TestClient, slug: str):
    response = client.get(f"/design-system/{slug}")
    assert 'class="top-bar"' in response.text
    assert "Disainisüsteem" in response.text


def test_colors_page_shows_hex_codes(client: TestClient):
    response = client.get("/design-system/colors")
    assert "#0030DE" in response.text
    assert "--estonian-blue" in response.text


def test_buttons_page_renders_variants(client: TestClient):
    response = client.get("/design-system/buttons")
    for variant in ("primary", "secondary", "ghost", "danger"):
        assert f"btn-{variant}" in response.text
