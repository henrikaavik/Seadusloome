"""FastHTML Beforeware for JWT cookie-based authentication."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from app.auth.cookies import set_auth_cookie
from app.auth.jwt_provider import JWTAuthProvider

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_provider: JWTAuthProvider | None = None


def _get_provider() -> JWTAuthProvider:
    """Return a cached JWTAuthProvider, instantiated on first use.

    Lazy construction lets ``import app.main`` succeed in test/dev
    environments that do not export SECRET_KEY / DATABASE_URL at import
    time, as long as tests never actually exercise the middleware.
    """
    global _provider
    if _provider is None:
        _provider = JWTAuthProvider()
    return _provider


# Paths that never require authentication. FastHTML's Beforeware uses
# ``re.fullmatch`` for skip-pattern matching.
#
# Note (#442): ``/explorer`` is intentionally **not** in this list. The
# explorer page reads the optional ``?draft=<id>`` query param and uses
# ``req.scope['auth']`` to scope the overlay to the caller's org; if
# the page were public the overlay would always come back empty. The
# explorer JSON APIs under ``/api/explorer/...`` also require auth so
# that ontology data queries are not publicly accessible.
SKIP_PATHS: list[str] = [
    r"/auth/login",
    r"/static/.*",
    r"/favicon\.ico",
    r"/api/health",
    r"/api/ping",
    r"/ws/explorer",
    r"/webhooks/github",
    r"/api/validate/.*",
]

# Paths an authenticated user with ``must_change_password=True`` may
# still reach without being bounced to ``/profile/password``. Anything
# else gets a 303 to ``/profile/password``. Matched with ``re.fullmatch``
# (same semantics as ``SKIP_PATHS``). ``/auth/logout`` lets the user
# escape the forced-change flow by signing out; ``/profile/password``
# (GET + POST) is the page that releases the flag; ``/static/.*`` and
# ``/api/health`` are infrastructure paths that should never be
# redirected even mid-flow.
_MUST_CHANGE_ALLOW_PATHS: list[str] = [
    r"/profile/password",
    r"/auth/logout",
    r"/static/.*",
    r"/api/health",
]


def verify_refresh_token_user(
    refresh_token: str,
    provider: JWTAuthProvider | None = None,
) -> dict[str, Any] | None:
    """Verify a refresh token without consuming it (#637, review).

    Verify the refresh token's signature, confirm the DB session row
    exists and is not expired, and confirm the user is still active.
    Return the associated user dict on success, ``None`` otherwise.

    Unlike :func:`try_refresh_access_token`, this helper does **not**
    mutate session state — the session row is not deleted and no new
    tokens are minted. It exists for transports that cannot persist
    rotated cookies on their response, most notably the ``/ws/chat``
    WebSocket handshake: the upgrade response has no Set-Cookie hook,
    so consuming the refresh token would leave the browser with a
    dead cookie and break the next HTTP request's silent-refresh.

    Security note: refresh-token rotation defends against reuse by
    ensuring every refresh mints a brand-new pair and invalidates the
    old one. The rotation only matters when NEW tokens are minted
    (HTTP middleware does that). Read-only verification of a
    presented refresh token does not enable reuse attacks because no
    new token is minted here; the refresh token still gets rotated
    atomically on the next HTTP request that goes through
    :func:`auth_before`.
    """
    if not refresh_token:
        return None

    provider = provider or _get_provider()
    user = provider.verify_refresh_token(refresh_token)
    if user is None:
        return None
    return dict(user)


def try_refresh_access_token(
    refresh_token: str,
    provider: JWTAuthProvider | None = None,
) -> tuple[str, str, dict[str, Any]] | None:
    """Rotate a refresh token and mint a fresh access/refresh pair.

    Returns ``(new_access, new_refresh, user_dict)`` on success,
    ``None`` when the refresh token is absent, expired, or the user
    has been deactivated. The old refresh session is removed from the
    DB so it cannot be replayed.

    Factored out of :func:`auth_before` (#637) so non-HTTP transports
    — notably the ``/ws/chat`` WebSocket handshake — can mirror the
    HTTP silent-refresh contract without re-implementing it.

    Note: the ``/ws/chat`` handshake uses
    :func:`verify_refresh_token_user` (verify-only) instead of this
    function because it cannot persist rotated cookies on the upgrade
    response. Consuming the refresh token there without being able to
    set replacement cookies would break the browser's subsequent HTTP
    silent-refresh (#637, review).
    """
    if not refresh_token:
        return None

    provider = provider or _get_provider()
    user = provider.verify_refresh_token(refresh_token)
    if user is None:
        return None

    # Rotate: invalidate the old refresh session, mint a fresh pair.
    provider.delete_refresh_token(refresh_token)
    new_access, new_refresh = provider.create_tokens(user)
    return new_access, new_refresh, dict(user)


def _must_change_redirect(req: Request, user: dict[str, Any]) -> Response | None:
    """Return a 303 to ``/profile/password`` when the user must change their pw.

    Returns ``None`` when the user is allowed to proceed: either the
    flag is not set, or the request targets one of the
    ``_MUST_CHANGE_ALLOW_PATHS`` (the password-change page itself,
    logout, static assets, healthchecks).

    Implements §4.8 of the password-management design and the P0
    mitigation described in
    ``docs/2026-04-29-ui-review-seadusloome-live.md``: the seeded
    admin (``admin@seadusloome.ee``) has ``must_change_password=TRUE``
    set by migration 024 so they cannot do anything else until they
    pick a real password.
    """
    if not user.get("must_change_password"):
        return None
    path = req.url.path
    for pattern in _MUST_CHANGE_ALLOW_PATHS:
        if re.fullmatch(pattern, path):
            return None
    return RedirectResponse(url="/profile/password", status_code=303)


def auth_before(req: Request) -> Response | None:
    """Beforeware: authenticate via JWT cookies, redirect to login if needed.

    - Reads ``access_token`` HttpOnly cookie and populates ``req.scope['auth']``.
    - When the access token is expired but a valid ``refresh_token`` cookie exists,
      transparently issues new tokens and attaches Set-Cookie headers to the
      redirect-to-self response so the browser stores the fresh cookies.
    - When the authenticated user has ``must_change_password=True`` and the
      request is not for ``/profile/password`` / ``/auth/logout`` / static /
      healthchecks, redirects to ``/profile/password``.
    - Returns ``None`` when the user is authenticated (lets the request through).
    - Returns a ``RedirectResponse`` to ``/auth/login`` when unauthenticated.
    """
    access_token: str | None = req.cookies.get("access_token")
    refresh_token: str | None = req.cookies.get("refresh_token")

    provider = _get_provider()

    # Try the access token first.
    if access_token:
        user = provider.get_current_user(access_token)
        if user is not None:
            req.scope["auth"] = user
            forced = _must_change_redirect(req, dict(user))
            if forced is not None:
                return forced
            return None

    # Access token missing or invalid -- attempt silent refresh.
    if refresh_token:
        rotated = try_refresh_access_token(refresh_token, provider=provider)
        if rotated is not None:
            new_access, new_refresh, _user = rotated

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
