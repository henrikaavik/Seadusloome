"""Unit tests for the FastHTML auth Beforeware (SKIP_PATHS + auth_before).

These tests run ``auth_before`` directly against a stubbed
``JWTAuthProvider`` so we can exercise every code path without a real
database. They also verify that each SKIP_PATHS regex matches via
``re.fullmatch`` — the semantics FastHTML actually uses.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock

import pytest
from starlette.responses import RedirectResponse

from app.auth import middleware as mw
from app.auth.middleware import SKIP_PATHS, auth_before

# ---------------------------------------------------------------------------
# SKIP_PATHS regex coverage (re.fullmatch semantics)
# ---------------------------------------------------------------------------


PUBLIC_PATHS = [
    "/auth/login",
    "/static/css/tokens.css",
    "/static/js/explorer.js",
    "/favicon.ico",
    "/api/explorer/overview",
    "/api/explorer/category/ABC",
    "/api/health",
    "/api/ping",
    "/ws/explorer",
    "/webhooks/github",
    "/api/validate/email",
    "/api/validate/password",
]

PROTECTED_PATHS = [
    "/dashboard",
    "/admin",
    "/admin/audit",
    "/admin/users",
    "/org/users",
    "/api/bookmarks",
    # #442 — explorer page must require auth so the draft overlay can
    # read req.scope['auth'] and scope by org. The JSON APIs under
    # /api/explorer/... are still public.
    "/explorer",
    "/explorer/foo",
    "/explorer/foo/bar",
]


def _matches_any(path: str) -> bool:
    return any(re.fullmatch(p, path) for p in SKIP_PATHS)


@pytest.mark.parametrize("path", PUBLIC_PATHS)
def test_public_path_is_skipped(path: str):
    assert _matches_any(path), f"Public path {path!r} was not matched by SKIP_PATHS"


@pytest.mark.parametrize("path", PROTECTED_PATHS)
def test_protected_path_is_not_skipped(path: str):
    assert not _matches_any(path), f"Protected path {path!r} was matched by SKIP_PATHS"


# ---------------------------------------------------------------------------
# auth_before() branch tests with a stubbed provider
# ---------------------------------------------------------------------------


def _make_request(
    cookies: dict[str, str] | None = None,
    path: str = "/dashboard",
) -> MagicMock:
    """Return a Starlette-Request-like mock carrying ``cookies`` and a url."""
    req = MagicMock()
    req.cookies = cookies or {}
    req.url = f"http://testserver{path}"
    req.scope = {}
    return req


@pytest.fixture
def stub_provider(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace the cached ``_provider`` with a MagicMock."""
    provider = MagicMock()
    monkeypatch.setattr(mw, "_provider", provider)
    # also patch the lazy getter so no real JWTAuthProvider() is built
    monkeypatch.setattr(mw, "_get_provider", lambda: provider)
    return provider


def test_valid_access_token_populates_scope(stub_provider: MagicMock):
    user: dict[str, Any] = {
        "id": "u-1",
        "email": "a@b.ee",
        "full_name": "A B",
        "role": "drafter",
        "org_id": None,
    }
    stub_provider.get_current_user.return_value = user

    req = _make_request(cookies={"access_token": "valid"})
    result = auth_before(req)

    assert result is None
    assert req.scope["auth"] == user
    stub_provider.get_current_user.assert_called_once_with("valid")


def test_expired_access_with_valid_refresh_rotates_tokens(stub_provider: MagicMock):
    user: dict[str, Any] = {
        "id": "u-1",
        "email": "a@b.ee",
        "full_name": "A B",
        "role": "drafter",
        "org_id": None,
    }
    stub_provider.get_current_user.return_value = None
    stub_provider.verify_refresh_token.return_value = user
    stub_provider.create_tokens.return_value = ("new-access", "new-refresh")

    req = _make_request(cookies={"access_token": "expired", "refresh_token": "ok"})
    result = auth_before(req)

    assert isinstance(result, RedirectResponse)
    assert result.status_code == 307
    # Rotation cleared the old refresh token.
    stub_provider.delete_refresh_token.assert_called_once_with("ok")
    stub_provider.create_tokens.assert_called_once_with(user)
    set_cookies = [h for h in result.raw_headers if h[0].lower() == b"set-cookie"]
    cookie_blob = b"\n".join(v for _, v in set_cookies).decode()
    assert "access_token=new-access" in cookie_blob
    assert "refresh_token=new-refresh" in cookie_blob


def test_both_tokens_invalid_redirects_to_login(stub_provider: MagicMock):
    stub_provider.get_current_user.return_value = None
    stub_provider.verify_refresh_token.return_value = None

    req = _make_request(cookies={"access_token": "bad", "refresh_token": "bad"})
    result = auth_before(req)

    assert isinstance(result, RedirectResponse)
    assert result.status_code == 303
    assert result.headers["location"] == "/auth/login"


def test_no_cookies_redirects_to_login(stub_provider: MagicMock):
    stub_provider.get_current_user.return_value = None
    stub_provider.verify_refresh_token.return_value = None

    req = _make_request(cookies={})
    result = auth_before(req)

    assert isinstance(result, RedirectResponse)
    assert result.status_code == 303
    assert result.headers["location"] == "/auth/login"


def test_only_refresh_cookie_still_refreshes(stub_provider: MagicMock):
    """No access_token at all but a valid refresh_token should also rotate."""
    user: dict[str, Any] = {
        "id": "u-2",
        "email": "c@d.ee",
        "full_name": "C D",
        "role": "admin",
        "org_id": None,
    }
    stub_provider.get_current_user.return_value = None
    stub_provider.verify_refresh_token.return_value = user
    stub_provider.create_tokens.return_value = ("aa", "rr")

    req = _make_request(cookies={"refresh_token": "ok"})
    result = auth_before(req)

    assert isinstance(result, RedirectResponse)
    assert result.status_code == 307
    stub_provider.get_current_user.assert_not_called()
    stub_provider.verify_refresh_token.assert_called_once_with("ok")
