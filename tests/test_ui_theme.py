"""Tests for the theme shim (dark-only UI).

The light/dark/system toggle was removed; ``app.ui.theme`` is now a thin
shim whose ``get_theme_from_request`` always returns ``"dark"`` and
whose ``set_theme_cookie`` is a no-op. These tests lock that contract
in so future callers don't accidentally reintroduce cookie-driven
branching.
"""

from unittest.mock import MagicMock

from starlette.responses import Response

from app.ui.theme import (
    DEFAULT_THEME,
    get_theme_from_request,
    set_theme_cookie,
)


def _request_with_cookie(value: str | None) -> MagicMock:
    req = MagicMock()
    req.cookies = {"theme": value} if value is not None else {}
    return req


def test_default_theme_is_dark():
    assert DEFAULT_THEME == "dark"


def test_get_theme_returns_dark_with_no_cookie():
    assert get_theme_from_request(_request_with_cookie(None)) == "dark"


def test_get_theme_ignores_cookie_value():
    # Even if a stale cookie is present from the previous toggle, we
    # always render dark now.
    for stale in ("light", "system", "dark", "solarized", ""):
        assert get_theme_from_request(_request_with_cookie(stale)) == "dark"


def test_set_theme_cookie_is_noop():
    response = Response()
    set_theme_cookie(response, "dark")
    assert response.headers.get("set-cookie") is None
