"""Theme shim — the UI is dark-only.

The original design exposed a light/dark/system toggle. It was removed in
favour of a single permanent dark theme for a cleaner visual identity.

This module is kept as a thin shim so existing callers
(``get_theme_from_request(req)``) continue to type-check and return a
stable value instead of being ripped out across a dozen route files.
The value is only passed through to ``TopBar(theme=...)`` which now
ignores it.
"""

from typing import Literal

from starlette.requests import Request
from starlette.responses import Response

ThemeChoice = Literal["dark"]
DEFAULT_THEME: ThemeChoice = "dark"
COOKIE_NAME = "theme"  # retained for existing cookies; the app never reads them


def get_theme_from_request(request: Request) -> ThemeChoice:  # noqa: ARG001
    """Return ``"dark"`` unconditionally — the UI is dark-only."""
    return DEFAULT_THEME


def set_theme_cookie(response: Response, theme: ThemeChoice) -> None:  # noqa: ARG001
    """No-op; the toggle was removed. Signature kept for back-compat."""
    return


# Inline script injected into <head> to apply the dark theme before first
# paint. Hardcoded to ``dark`` so there is never a flash of light mode
# even though ``:root`` in tokens.css already defaults to the dark palette.
THEME_INIT_SCRIPT = """
(function() {
  try {
    document.documentElement.setAttribute('data-theme', 'dark');
  } catch (e) {}
})();
"""
