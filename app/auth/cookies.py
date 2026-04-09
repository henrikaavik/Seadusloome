"""Shared cookie helpers for authentication.

Centralises the secure-cookie configuration so that ``routes.py`` and
``middleware.py`` use the same settings.  The ``COOKIE_SECURE`` flag
defaults to ``True`` (production) and can be overridden via the
``COOKIE_SECURE`` environment variable for local development.
"""

import os

from starlette.responses import Response

COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() == "true"


def set_auth_cookie(response: Response, key: str, value: str, max_age: int) -> None:
    """Set an HttpOnly, SameSite=Lax authentication cookie on *response*."""
    response.set_cookie(
        key=key,
        value=value,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        max_age=max_age,
        path="/",
    )


def clear_auth_cookie(response: Response, key: str) -> None:
    """Remove an authentication cookie from *response*."""
    response.delete_cookie(key=key, path="/")
