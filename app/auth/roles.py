"""Role-based access control decorators for FastHTML routes."""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

logger = logging.getLogger(__name__)


def _get_auth(req: Request) -> dict | None:  # type: ignore[type-arg]
    """Extract the auth dict from the request scope, or None."""
    return req.scope.get("auth")  # type: ignore[return-value]


def require_role(*roles: str) -> Callable[..., Any]:
    """Decorator that restricts a route handler to users with one of the given roles.

    If the user is not authenticated, redirects to ``/auth/login``.
    If authenticated but lacking the required role, returns a 403 page.

    Usage::

        @rt("/admin/dashboard")
        @require_role("admin")
        def admin_dashboard(req: Request):
            ...
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Find the Request object in args or kwargs
            req = _find_request(args, kwargs)
            if req is None:
                logger.error("require_role: no Request found in handler arguments")
                return RedirectResponse(url="/auth/login", status_code=303)

            auth = _get_auth(req)
            if auth is None:
                return RedirectResponse(url="/auth/login", status_code=303)

            user_role = auth.get("role", "")
            if user_role not in roles:
                return Titled(
                    "Ligipääs keelatud",
                    P("Teil puudub õigus selle lehe vaatamiseks."),
                    A("Tagasi avalehele", href="/"),
                )

            return fn(*args, **kwargs)

        return wrapper

    return decorator


def require_org_member(org_id_param: str = "org_id") -> Callable[..., Any]:
    """Decorator that checks the authenticated user belongs to the given organization.

    The organization ID is read from the route parameter named *org_id_param*.
    System admins bypass this check.

    If the user is not authenticated, redirects to ``/auth/login``.
    If the user's ``org_id`` does not match, returns a 403 page.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            req = _find_request(args, kwargs)
            if req is None:
                logger.error("require_org_member: no Request found in handler arguments")
                return RedirectResponse(url="/auth/login", status_code=303)

            auth = _get_auth(req)
            if auth is None:
                return RedirectResponse(url="/auth/login", status_code=303)

            # System admins can access any organization
            if auth.get("role") == "admin":
                return fn(*args, **kwargs)

            route_org_id = kwargs.get(org_id_param)
            if route_org_id is None:
                route_org_id = req.path_params.get(org_id_param)

            user_org_id = auth.get("org_id")
            if not user_org_id or str(user_org_id) != str(route_org_id):
                return Titled(
                    "Ligipääs keelatud",
                    P("Teil puudub õigus selle organisatsiooni andmete vaatamiseks."),
                    A("Tagasi avalehele", href="/"),
                )

            return fn(*args, **kwargs)

        return wrapper

    return decorator


def _find_request(args: tuple, kwargs: dict) -> Request | None:  # type: ignore[type-arg]
    """Locate a Starlette Request in the handler's positional or keyword args."""
    for arg in args:
        if isinstance(arg, Request):
            return arg
    if "req" in kwargs and isinstance(kwargs["req"], Request):
        return kwargs["req"]
    if "request" in kwargs and isinstance(kwargs["request"], Request):
        return kwargs["request"]
    return None
