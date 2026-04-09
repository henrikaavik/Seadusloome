"""Integration tests for /auth/login and /auth/logout.

These tests mock ``JWTAuthProvider`` so no real database is needed and
focus on the cookie/redirect contract: successful login sets HttpOnly
cookies, failure re-renders the form with an error, logout clears cookies.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

from app.main import app


def _user_dict() -> dict:
    return {
        "id": "uid-1",
        "email": "user@seadusloome.ee",
        "full_name": "Test Kasutaja",
        "role": "drafter",
        "org_id": None,
    }


@patch("app.auth.routes._provider")
def test_login_success_sets_cookies_and_redirects(mock_provider: MagicMock):
    mock_provider.authenticate.return_value = _user_dict()
    mock_provider.create_tokens.return_value = ("access-token-xyz", "refresh-token-xyz")

    client = TestClient(app, follow_redirects=False)
    resp = client.post(
        "/auth/login",
        data={"email": "user@seadusloome.ee", "password": "correct-horse"},
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/"

    set_cookie_headers = resp.headers.get_list("set-cookie")
    access_cookie = next(h for h in set_cookie_headers if h.startswith("access_token="))
    refresh_cookie = next(h for h in set_cookie_headers if h.startswith("refresh_token="))
    assert "access-token-xyz" in access_cookie
    assert "refresh-token-xyz" in refresh_cookie
    # Both cookies must be HttpOnly and SameSite=lax (Starlette normalises casing).
    assert "httponly" in access_cookie.lower()
    assert "samesite=lax" in access_cookie.lower()
    assert "httponly" in refresh_cookie.lower()
    assert "samesite=lax" in refresh_cookie.lower()


@patch("app.auth.routes._provider")
def test_login_failure_rerenders_form_with_error(mock_provider: MagicMock):
    mock_provider.authenticate.return_value = None

    client = TestClient(app, follow_redirects=False)
    resp = client.post(
        "/auth/login",
        data={"email": "user@seadusloome.ee", "password": "wrong"},
    )

    assert resp.status_code == 200
    assert "Vale e-post või parool." in resp.text
    # No cookies issued on failure.
    assert not resp.headers.get_list("set-cookie")
    # Form must still be present so the user can retry.
    assert 'action="/auth/login"' in resp.text
    # The email should be pre-filled.
    assert "user@seadusloome.ee" in resp.text


@patch("app.auth.middleware._get_provider")
@patch("app.auth.routes._provider")
def test_logout_clears_cookies(mock_route_provider: MagicMock, mock_get_mw_provider: MagicMock):
    # The middleware runs before the logout handler and needs a valid
    # authed user for the request to reach the logout handler at all.
    mw_provider = MagicMock()
    mw_provider.get_current_user.return_value = {
        "id": "uid-1",
        "email": "user@seadusloome.ee",
        "full_name": "Test Kasutaja",
        "role": "drafter",
        "org_id": None,
    }
    mock_get_mw_provider.return_value = mw_provider

    client = TestClient(app, follow_redirects=False)
    resp = client.post(
        "/auth/logout",
        cookies={"access_token": "at-123", "refresh_token": "rt-123"},
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"

    # Starlette's delete_cookie sets max-age=0 / expires=1970 on both tokens.
    set_cookie_headers = resp.headers.get_list("set-cookie")
    blob = "\n".join(set_cookie_headers).lower()
    assert "access_token=" in blob
    assert "refresh_token=" in blob
    # The old refresh token should have been deleted from DB.
    mock_route_provider.delete_refresh_token.assert_called_once_with("rt-123")


def test_login_page_renders_form():
    """GET /auth/login must render the login form without auth."""
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/auth/login")

    assert resp.status_code == 200
    assert 'action="/auth/login"' in resp.text
    assert 'method="post"' in resp.text
    assert 'name="email"' in resp.text
    assert 'name="password"' in resp.text
    assert "Sisselogimine" in resp.text
