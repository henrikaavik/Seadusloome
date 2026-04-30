"""Integration tests for ``/profile`` and ``/profile/password``.

The handlers reach into:

* ``app.auth.middleware._get_provider`` for ``get_current_user``
  (so we can mark the request as authenticated);
* ``app.auth.profile.get_connection`` for the password-hash lookup
  and the ``change_password`` call;
* ``app.auth.profile.change_password`` itself.

We monkey-patch the provider and the DB connection so no real DB is
needed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import bcrypt
from starlette.testclient import TestClient

from app.main import app


def _user_dict(must_change: bool = False) -> dict[str, Any]:
    return {
        "id": "uid-1",
        "email": "kasutaja@seadusloome.ee",
        "full_name": "Test Kasutaja",
        "role": "drafter",
        "org_id": None,
        "must_change_password": must_change,
    }


def _set_authenticated(mock_get_provider: MagicMock, user: dict[str, Any]) -> None:
    """Wire the middleware to consider the request authenticated as *user*."""
    provider = MagicMock()
    provider.get_current_user.return_value = user
    mock_get_provider.return_value = provider


# ---------------------------------------------------------------------------
# /profile (GET)
# ---------------------------------------------------------------------------


@patch("app.auth.middleware._get_provider")
def test_profile_get_renders_card_with_change_password_link(mock_get_provider: MagicMock):
    _set_authenticated(mock_get_provider, _user_dict())

    client = TestClient(app, follow_redirects=False)
    resp = client.get("/profile", cookies={"access_token": "valid"})

    assert resp.status_code == 200
    assert "Profiil" in resp.text
    assert "Vaheta parool" in resp.text
    assert 'href="/profile/password"' in resp.text


# ---------------------------------------------------------------------------
# /profile/password (GET)
# ---------------------------------------------------------------------------


@patch("app.auth.middleware._get_provider")
def test_password_get_renders_three_password_fields(mock_get_provider: MagicMock):
    _set_authenticated(mock_get_provider, _user_dict())

    client = TestClient(app, follow_redirects=False)
    resp = client.get("/profile/password", cookies={"access_token": "valid"})

    assert resp.status_code == 200
    assert 'name="current_password"' in resp.text
    assert 'name="new_password"' in resp.text
    assert 'name="new_password_confirm"' in resp.text
    # No forced-change banner for a normal user.
    assert "Administraator on lähtestanud" not in resp.text


@patch("app.auth.middleware._get_provider")
def test_password_get_shows_forced_change_banner_when_flagged(
    mock_get_provider: MagicMock,
):
    _set_authenticated(mock_get_provider, _user_dict(must_change=True))

    client = TestClient(app, follow_redirects=False)
    resp = client.get("/profile/password", cookies={"access_token": "valid"})

    assert resp.status_code == 200
    assert "Administraator on lähtestanud teie parooli." in resp.text


# ---------------------------------------------------------------------------
# /profile/password (POST) — validation errors
# ---------------------------------------------------------------------------


@patch("app.auth.profile.get_connection")
@patch("app.auth.middleware._get_provider")
def test_post_wrong_current_password_rejected(
    mock_get_provider: MagicMock,
    mock_get_conn: MagicMock,
):
    _set_authenticated(mock_get_provider, _user_dict())

    # The DB lookup of password_hash returns a hash that does NOT
    # match what the user submits.
    real_hash = bcrypt.hashpw(b"actual-password", bcrypt.gensalt()).decode()
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = (real_hash,)
    mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
    mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

    client = TestClient(app, follow_redirects=False)
    resp = client.post(
        "/profile/password",
        cookies={"access_token": "valid"},
        data={
            "current_password": "WRONG",
            "new_password": "NewPassword1",
            "new_password_confirm": "NewPassword1",
        },
    )

    assert resp.status_code == 200  # form re-render, NOT redirect
    assert "Praegune parool on vale." in resp.text


@patch("app.auth.profile.get_connection")
@patch("app.auth.middleware._get_provider")
def test_post_password_mismatch_rejected(
    mock_get_provider: MagicMock,
    mock_get_conn: MagicMock,
):
    _set_authenticated(mock_get_provider, _user_dict())

    real_hash = bcrypt.hashpw(b"correct-pw", bcrypt.gensalt()).decode()
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = (real_hash,)
    mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
    mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

    client = TestClient(app, follow_redirects=False)
    resp = client.post(
        "/profile/password",
        cookies={"access_token": "valid"},
        data={
            "current_password": "correct-pw",
            "new_password": "NewPassword1",
            "new_password_confirm": "DIFFERENT1",
        },
    )

    assert resp.status_code == 200
    assert "Paroolid ei kattu." in resp.text


@patch("app.auth.profile.get_connection")
@patch("app.auth.middleware._get_provider")
def test_post_weak_new_password_rejected(
    mock_get_provider: MagicMock,
    mock_get_conn: MagicMock,
):
    _set_authenticated(mock_get_provider, _user_dict())

    real_hash = bcrypt.hashpw(b"correct-pw", bcrypt.gensalt()).decode()
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = (real_hash,)
    mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
    mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

    client = TestClient(app, follow_redirects=False)
    resp = client.post(
        "/profile/password",
        cookies={"access_token": "valid"},
        data={
            "current_password": "correct-pw",
            "new_password": "short",
            "new_password_confirm": "short",
        },
    )

    assert resp.status_code == 200
    assert "Parool peab olema vähemalt 8 tähemärki pikk" in resp.text


# ---------------------------------------------------------------------------
# /profile/password (POST) — success path
# ---------------------------------------------------------------------------


@patch("app.auth.profile.change_password")
@patch("app.auth.profile.get_connection")
@patch("app.auth.middleware._get_provider")
def test_post_success_redirects_to_login_with_cleared_cookies(
    mock_get_provider: MagicMock,
    mock_get_conn: MagicMock,
    mock_change_password: MagicMock,
):
    _set_authenticated(mock_get_provider, _user_dict())

    real_hash = bcrypt.hashpw(b"correct-pw", bcrypt.gensalt()).decode()
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = (real_hash,)
    mock_get_conn.return_value.__enter__ = MagicMock(return_value=conn)
    mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

    client = TestClient(app, follow_redirects=False)
    resp = client.post(
        "/profile/password",
        cookies={"access_token": "valid"},
        data={
            "current_password": "correct-pw",
            "new_password": "BrandNew2",
            "new_password_confirm": "BrandNew2",
        },
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"

    # ``change_password`` must have been called with must_change=False
    # so this self-service rotation does not re-flag the user.
    mock_change_password.assert_called_once()
    _args, kwargs = mock_change_password.call_args
    assert kwargs.get("must_change") is False

    # Both auth cookies must be cleared.
    set_cookie_blob = "\n".join(resp.headers.get_list("set-cookie")).lower()
    assert "access_token=" in set_cookie_blob
    assert "refresh_token=" in set_cookie_blob
    # Starlette's delete_cookie sets max-age=0 to expire the cookie.
    assert "max-age=0" in set_cookie_blob


# ---------------------------------------------------------------------------
# must_change_password redirect enforcement (middleware)
# ---------------------------------------------------------------------------


@patch("app.auth.middleware._get_provider")
def test_must_change_user_redirected_from_dashboard(mock_get_provider: MagicMock):
    """A user with ``must_change_password=True`` is bounced from any
    other authenticated route to /profile/password."""
    _set_authenticated(mock_get_provider, _user_dict(must_change=True))

    client = TestClient(app, follow_redirects=False)
    resp = client.get("/dashboard", cookies={"access_token": "valid"})

    assert resp.status_code == 303
    assert resp.headers["location"] == "/profile/password"


@patch("app.auth.middleware._get_provider")
def test_must_change_user_redirected_from_root(mock_get_provider: MagicMock):
    _set_authenticated(mock_get_provider, _user_dict(must_change=True))

    client = TestClient(app, follow_redirects=False)
    resp = client.get("/", cookies={"access_token": "valid"})

    assert resp.status_code == 303
    assert resp.headers["location"] == "/profile/password"


@patch("app.auth.middleware._get_provider")
def test_must_change_user_can_reach_password_page(mock_get_provider: MagicMock):
    """The user MUST be able to reach /profile/password to escape the loop."""
    _set_authenticated(mock_get_provider, _user_dict(must_change=True))

    client = TestClient(app, follow_redirects=False)
    resp = client.get("/profile/password", cookies={"access_token": "valid"})

    assert resp.status_code == 200
    assert "Administraator on lähtestanud teie parooli." in resp.text


@patch("app.auth.routes._provider")
@patch("app.auth.middleware._get_provider")
def test_must_change_user_can_reach_logout(
    mock_get_mw_provider: MagicMock,
    mock_route_provider: MagicMock,
):
    """An authenticated user with must_change=True must still be able
    to log out instead of being trapped on /profile/password forever.

    The middleware must let POST /auth/logout through; the logout
    handler then deletes the refresh session and clears cookies. We
    additionally mock the routes-level provider so the handler does
    not attempt a real DB connection during the delete.
    """
    _set_authenticated(mock_get_mw_provider, _user_dict(must_change=True))

    client = TestClient(app, follow_redirects=False)
    resp = client.post(
        "/auth/logout",
        cookies={"access_token": "valid", "refresh_token": "rt-1"},
    )
    # logout_post returns a 303 to /auth/login, NOT /profile/password.
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"
    # The logout handler must have asked the provider to delete the
    # refresh token — confirms the middleware did NOT short-circuit
    # the request to /profile/password.
    mock_route_provider.delete_refresh_token.assert_called_once_with("rt-1")


@patch("app.auth.middleware._get_provider")
def test_must_change_false_user_can_reach_dashboard(mock_get_provider: MagicMock):
    """Sanity check: when must_change is False, the redirect does NOT fire."""
    _set_authenticated(mock_get_provider, _user_dict(must_change=False))

    client = TestClient(app, follow_redirects=False)
    resp = client.get("/dashboard", cookies={"access_token": "valid"})

    # Could be 200 or another status depending on the dashboard
    # handler, but it MUST NOT be the must-change redirect.
    assert not (resp.status_code == 303 and resp.headers.get("location") == "/profile/password")
