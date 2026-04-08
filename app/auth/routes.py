"""Authentication routes for the Seadusloome FastHTML application."""

from __future__ import annotations

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from app.auth.jwt_provider import JWTAuthProvider

_provider = JWTAuthProvider()


def _set_cookie(response: Response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        key=name,
        value=value,
        httponly=True,
        samesite="lax",
        secure=False,  # Set True in production behind HTTPS
        max_age=max_age,
        path="/",
    )


def _clear_cookie(response: Response, name: str) -> None:
    response.delete_cookie(key=name, path="/")


# ---------------------------------------------------------------------------
# Route handlers — registered by the app via `register_auth_routes(app, rt)`
# ---------------------------------------------------------------------------


def login_page():
    """GET /auth/login — render the login form."""
    return Titled(
        "Sisselogimine",
        Form(
            Fieldset(
                Label("E-post", Input(name="email", type="email", required=True)),
                Label("Parool", Input(name="password", type="password", required=True)),
            ),
            Button("Logi sisse", type="submit"),
            method="post",
            action="/auth/login",
        ),
    )


def login_post(req: Request, email: str, password: str):
    """POST /auth/login — authenticate and set cookies."""
    user = _provider.authenticate(email, password)
    if user is None:
        return Titled(
            "Sisselogimine",
            P("Vale e-post v\u00f5i parool.", style="color:red"),
            Form(
                Fieldset(
                    Label("E-post", Input(name="email", type="email", required=True, value=email)),
                    Label("Parool", Input(name="password", type="password", required=True)),
                ),
                Button("Logi sisse", type="submit"),
                method="post",
                action="/auth/login",
            ),
        )

    access_token, refresh_token = _provider.create_tokens(user)
    response = RedirectResponse(url="/", status_code=303)
    _set_cookie(response, "access_token", access_token, max_age=3600)
    _set_cookie(response, "refresh_token", refresh_token, max_age=30 * 86400)
    return response


def logout_post(req: Request):
    """POST /auth/logout — clear cookies and delete session."""
    refresh_token = req.cookies.get("refresh_token")
    if refresh_token:
        _provider.delete_refresh_token(refresh_token)

    response = RedirectResponse(url="/auth/login", status_code=303)
    _clear_cookie(response, "access_token")
    _clear_cookie(response, "refresh_token")
    return response


def register_auth_routes(rt):  # type: ignore[no-untyped-def]
    """Register authentication routes on the FastHTML route decorator *rt*."""
    rt("/auth/login", methods=["GET"])(login_page)
    rt("/auth/login", methods=["POST"])(login_post)
    rt("/auth/logout", methods=["POST"])(logout_post)
