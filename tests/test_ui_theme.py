"""Tests for theme detection cookie helpers (no TestClient required)."""

from unittest.mock import MagicMock

from starlette.responses import Response

from app.ui.theme import (
    COOKIE_NAME,
    DEFAULT_THEME,
    get_theme_from_request,
    set_theme_cookie,
)

# ---------------------------------------------------------------------------
# get_theme_from_request
# ---------------------------------------------------------------------------


def _request_with_cookie(value: str | None) -> MagicMock:
    """Build a fake Starlette Request whose .cookies behaves like a dict."""
    req = MagicMock()
    req.cookies = {COOKIE_NAME: value} if value is not None else {}
    return req


def test_get_theme_returns_default_when_no_cookie():
    req = _request_with_cookie(None)
    assert get_theme_from_request(req) == DEFAULT_THEME
    assert get_theme_from_request(req) == "system"


def test_get_theme_returns_light_when_cookie_set():
    req = _request_with_cookie("light")
    assert get_theme_from_request(req) == "light"


def test_get_theme_returns_dark_when_cookie_set():
    req = _request_with_cookie("dark")
    assert get_theme_from_request(req) == "dark"


def test_get_theme_returns_system_for_invalid_cookie():
    req = _request_with_cookie("solarized")
    assert get_theme_from_request(req) == "system"


def test_get_theme_returns_system_for_empty_cookie():
    req = _request_with_cookie("")
    assert get_theme_from_request(req) == "system"


# ---------------------------------------------------------------------------
# set_theme_cookie
# ---------------------------------------------------------------------------


def test_set_theme_cookie_writes_with_correct_attrs():
    response = Response()
    set_theme_cookie(response, "dark")
    set_cookie_header = response.headers.get("set-cookie") or ""
    assert f"{COOKIE_NAME}=dark" in set_cookie_header
    assert "Path=/" in set_cookie_header
    # samesite=lax is normalized to "Lax" in the header
    assert "lax" in set_cookie_header.lower()
    # 1-year max-age
    assert "Max-Age=31536000" in set_cookie_header
    # httponly is False (readable by JS)
    assert "HttpOnly" not in set_cookie_header


def test_set_theme_cookie_accepts_light():
    response = Response()
    set_theme_cookie(response, "light")
    assert f"{COOKIE_NAME}=light" in (response.headers.get("set-cookie") or "")


def test_set_theme_cookie_accepts_system():
    response = Response()
    set_theme_cookie(response, "system")
    assert f"{COOKIE_NAME}=system" in (response.headers.get("set-cookie") or "")


def test_set_theme_cookie_normalizes_invalid_value():
    response = Response()
    set_theme_cookie(response, "solarized")  # type: ignore[arg-type]
    set_cookie_header = response.headers.get("set-cookie") or ""
    assert f"{COOKIE_NAME}={DEFAULT_THEME}" in set_cookie_header
