"""Authentication routes for the Seadusloome FastHTML application."""

from __future__ import annotations

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.auth.cookies import clear_auth_cookie, set_auth_cookie
from app.auth.jwt_provider import JWTAuthProvider
from app.ui.forms.form_field import FormField
from app.ui.layout import PageShell
from app.ui.primitives.button import Button
from app.ui.surfaces.alert import Alert
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.theme import get_theme_from_request

_provider = JWTAuthProvider()


# ---------------------------------------------------------------------------
# View helpers
# ---------------------------------------------------------------------------


def _login_form(email: str = "", error: str | None = None):
    """Render the login form, optionally with an error message."""
    return Card(
        CardHeader(H2("Sisselogimine", cls="card-title")),
        CardBody(
            Alert("Vale e-post või parool.", variant="danger") if error else None,
            Form(
                FormField(
                    name="email",
                    label="E-post",
                    type="email",
                    value=email,
                    required=True,
                    validator="email",
                ),
                FormField(
                    name="password",
                    label="Parool",
                    type="password",
                    required=True,
                ),
                Button("Logi sisse", type="submit", variant="primary"),
                method="post",
                action="/auth/login",
                cls="auth-form",
            ),
        ),
        cls="auth-card",
    )


# ---------------------------------------------------------------------------
# Route handlers — registered by the app via `register_auth_routes(app, rt)`
# ---------------------------------------------------------------------------


def login_page(req: Request):
    """GET /auth/login — render the login form."""
    theme = get_theme_from_request(req)
    return PageShell(
        _login_form(),
        title="Sisselogimine",
        user=None,
        theme=theme,
        container_size="sm",
    )


def login_post(req: Request, email: str, password: str):
    """POST /auth/login — authenticate and set cookies."""
    user = _provider.authenticate(email, password)
    if user is None:
        theme = get_theme_from_request(req)
        return PageShell(
            _login_form(email=email, error="Vale e-post või parool."),
            title="Sisselogimine",
            user=None,
            theme=theme,
            container_size="sm",
        )

    access_token, refresh_token = _provider.create_tokens(user)
    response = RedirectResponse(url="/", status_code=303)
    set_auth_cookie(response, "access_token", access_token, max_age=3600)
    set_auth_cookie(response, "refresh_token", refresh_token, max_age=30 * 86400)
    return response


def logout_post(req: Request):
    """POST /auth/logout — clear cookies and delete session."""
    refresh_token = req.cookies.get("refresh_token")
    if refresh_token:
        _provider.delete_refresh_token(refresh_token)

    response = RedirectResponse(url="/auth/login", status_code=303)
    clear_auth_cookie(response, "access_token")
    clear_auth_cookie(response, "refresh_token")
    return response


def register_auth_routes(rt):  # type: ignore[no-untyped-def]
    """Register authentication routes on the FastHTML route decorator *rt*."""
    rt("/auth/login", methods=["GET"])(login_page)
    rt("/auth/login", methods=["POST"])(login_post)
    rt("/auth/logout", methods=["POST"])(logout_post)
