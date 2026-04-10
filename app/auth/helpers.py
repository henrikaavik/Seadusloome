"""Shared auth helper functions used across route modules."""

from __future__ import annotations

from typing import cast

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from app.auth.provider import UserDict


def require_auth(req: Request) -> Response | UserDict:
    """Return the auth dict or a 303 redirect to the login page.

    ``auth_before`` already guards every non-SKIP_PATHS route in the
    middleware layer, but defensive handlers short-circuit the typing
    concerns around a missing ``org_id`` and make the unit tests simpler.
    """
    auth = req.scope.get("auth")
    if not auth or not auth.get("id"):
        return RedirectResponse(url="/auth/login", status_code=303)
    return cast(UserDict, auth)
