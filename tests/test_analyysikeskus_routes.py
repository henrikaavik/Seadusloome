"""Tests for the Analüüsikeskus placeholder route (#714, PR-A).

Follows the auth-mocking pattern from ``tests/test_chat_routes.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch


def _authed_user() -> dict[str, Any]:
    return {
        "id": "33333333-3333-3333-3333-333333333333",
        "email": "kasutaja@seadusloome.ee",
        "full_name": "Test Kasutaja",
        "role": "drafter",
        "org_id": "11111111-1111-1111-1111-111111111111",
    }


def _stub_provider() -> MagicMock:
    provider = MagicMock()
    provider.get_current_user.return_value = _authed_user()
    return provider


def _authed_client():
    from starlette.testclient import TestClient

    client = TestClient(
        __import__("app.main", fromlist=["app"]).app,
        follow_redirects=False,
    )
    client.cookies.set("access_token", "stub-token")
    return client


def test_analyysikeskus_redirects_unauthenticated():
    from starlette.testclient import TestClient

    from app.main import app

    client = TestClient(app, follow_redirects=False)
    resp = client.get("/analyysikeskus")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


@patch("app.auth.middleware._get_provider")
def test_analyysikeskus_renders_placeholder(mock_provider: MagicMock):
    mock_provider.return_value = _stub_provider()
    client = _authed_client()
    resp = client.get("/analyysikeskus")
    assert resp.status_code == 200
    assert "Analüüsikeskus" in resp.text
    assert "Tulekul" in resp.text
    # Sidebar marks the new nav item active.
    assert 'aria-current="page"' in resp.text
