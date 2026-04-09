"""Theme detection and cookie helpers for light/dark mode.

Design:
- User preference stored in 'theme' cookie (values: 'light', 'dark', 'system').
- Default: 'system' — respects prefers-color-scheme media query.
- Theme is applied via data-theme attribute on <html>.
- An inline script in <head> reads the cookie and sets data-theme before
  first paint to prevent FOUC.
"""

from typing import Literal

from starlette.requests import Request
from starlette.responses import Response

ThemeChoice = Literal["light", "dark", "system"]
VALID_THEMES: set[ThemeChoice] = {"light", "dark", "system"}
DEFAULT_THEME: ThemeChoice = "system"
COOKIE_NAME = "theme"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year


def get_theme_from_request(request: Request) -> ThemeChoice:
    """Return the user's theme preference from the cookie, defaulting to 'system'."""
    value = request.cookies.get(COOKIE_NAME, DEFAULT_THEME)
    if value in VALID_THEMES:
        return value  # type: ignore[return-value]
    return DEFAULT_THEME


def set_theme_cookie(response: Response, theme: ThemeChoice) -> None:
    """Write the theme preference cookie on the response."""
    if theme not in VALID_THEMES:
        theme = DEFAULT_THEME
    response.set_cookie(
        key=COOKIE_NAME,
        value=theme,
        max_age=COOKIE_MAX_AGE,
        path="/",
        samesite="lax",
        httponly=False,  # readable by JS for instant toggle
    )


# Inline script injected into <head> to apply theme before first paint.
# Reads the cookie synchronously, sets data-theme attribute, prevents FOUC.
THEME_INIT_SCRIPT = """
(function() {
  try {
    var match = document.cookie.match(/(?:^|;\\s*)theme=([^;]+)/);
    var theme = match ? match[1] : 'system';
    if (theme === 'light' || theme === 'dark') {
      document.documentElement.setAttribute('data-theme', theme);
    }
  } catch (e) {}
})();
"""
