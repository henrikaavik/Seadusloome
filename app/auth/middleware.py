"""FastHTML Beforeware for JWT cookie-based authentication."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from app.auth.cookies import set_auth_cookie
from app.auth.jwt_provider import JWTAuthProvider

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_provider = JWTAuthProvider()

# Paths that never require authentication.
SKIP_PATHS: list[str] = [
    r"/auth/login",
    r"/static/.*",
    r"/favicon\.ico",
    r"/explorer",
    r"/api/explorer/.*",
    r"/api/health",
    r"/ws/explorer",
    r"/webhooks/github",
    r"/api/validate/.*",
]


def auth_before(req: Request) -> Response | None:
    """Beforeware: authenticate via JWT cookies, redirect to login if needed.

    - Reads ``access_token`` HttpOnly cookie and populates ``req.scope['auth']``.
    - When the access token is expired but a valid ``refresh_token`` cookie exists,
      transparently issues new tokens and attaches Set-Cookie headers to the
      redirect-to-self response so the browser stores the fresh cookies.
    - Returns ``None`` when the user is authenticated (lets the request through).
    - Returns a ``RedirectResponse`` to ``/auth/login`` when unauthenticated.
    """
    access_token: str | None = req.cookies.get("access_token")
    refresh_token: str | None = req.cookies.get("refresh_token")

    # Try the access token first.
    if access_token:
        user = _provider.get_current_user(access_token)
        if user is not None:
            req.scope["auth"] = user
            return None

    # Access token missing or invalid -- attempt silent refresh.
    if refresh_token:
        user = _provider.verify_refresh_token(refresh_token)
        if user is not None:
            # Rotate tokens: delete old refresh session, create new pair.
            _provider.delete_refresh_token(refresh_token)
            new_access, new_refresh = _provider.create_tokens(user)

            # We cannot simply set cookies on the current response because the
            # Beforeware runs *before* the handler and the response object does
            # not exist yet.  Instead we redirect the browser to the same URL
            # with fresh cookies.  The next request will carry the new tokens.
            redirect = RedirectResponse(url=str(req.url), status_code=307)
            set_auth_cookie(redirect, "access_token", new_access, max_age=3600)
            set_auth_cookie(redirect, "refresh_token", new_refresh, max_age=30 * 86400)
            return redirect

    # No valid credentials -- redirect to login.
    return RedirectResponse(url="/auth/login", status_code=303)
