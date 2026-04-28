"""User profile routes — /profile (hub) and /profile/password (change pw form)."""

from __future__ import annotations

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.auth.audit import log_action
from app.auth.cookies import clear_auth_cookie
from app.auth.jwt_provider import verify_password
from app.auth.password import change_password, validate_password
from app.db import get_connection
from app.ui.forms.app_form import AppForm
from app.ui.forms.form_field import FormField
from app.ui.layout import PageShell
from app.ui.primitives.button import Button
from app.ui.surfaces.alert import Alert
from app.ui.surfaces.card import Card, CardBody, CardHeader


def _password_form(error: str | None = None, *, force: bool = False):
    return Card(
        CardHeader(H2("Vaheta parool", cls="card-title")),
        CardBody(
            (
                Alert(
                    "Administraator on lähtestanud teie parooli. "
                    "Palun määrake uus parool jätkamiseks.",
                    variant="warning",
                )
                if force
                else None
            ),
            Alert(error, variant="danger") if error else None,
            AppForm(
                FormField(
                    name="current_password",
                    label="Praegune parool",
                    type="password",
                    required=True,
                ),
                FormField(
                    name="new_password",
                    label="Uus parool",
                    type="password",
                    required=True,
                ),
                FormField(
                    name="new_password_confirm",
                    label="Korda uut parooli",
                    type="password",
                    required=True,
                ),
                Div(
                    Button("Salvesta", type="submit", variant="primary"),
                    A("Tühista", href="/profile", cls="btn btn-ghost btn-md"),
                    cls="form-actions",
                ),
                method="post",
                action="/profile/password",
            ),
        ),
    )


def profile_page(req: Request):
    auth = req.scope.get("auth")
    return PageShell(
        H1("Profiil", cls="page-title"),
        Card(
            CardHeader(H3("Konto", cls="card-title")),
            CardBody(
                P(f"E-post: {auth['email']}", cls="muted-text") if auth else None,
                P(f"Nimi: {auth['full_name']}", cls="muted-text") if auth else None,
                Hr(),
                P(A("Vaheta parool", href="/profile/password", cls="btn btn-secondary")),
            ),
        ),
        title="Profiil",
        user=auth,
        active_nav="/profile",
    )


def profile_password_page(req: Request):
    auth = req.scope.get("auth")
    return PageShell(
        H1("Vaheta parool", cls="page-title"),
        _password_form(force=bool(auth and auth.get("must_change_password"))),
        title="Vaheta parool",
        user=auth,
        active_nav="/profile",
    )


def profile_password_post(
    req: Request,
    current_password: str,
    new_password: str,
    new_password_confirm: str,
):
    auth = req.scope.get("auth")
    if auth is None:
        return RedirectResponse(url="/auth/login", status_code=303)

    user_id = auth["id"]
    email = auth["email"]
    forced = bool(auth.get("must_change_password"))

    if new_password != new_password_confirm:
        return PageShell(
            H1("Vaheta parool", cls="page-title"),
            _password_form(error="Paroolid ei kattu.", force=forced),
            title="Vaheta parool",
            user=auth,
            active_nav="/profile",
        )

    pw_error = validate_password(new_password, email=email)
    if pw_error:
        return PageShell(
            H1("Vaheta parool", cls="page-title"),
            _password_form(error=pw_error, force=forced),
            title="Vaheta parool",
            user=auth,
            active_nav="/profile",
        )

    with get_connection() as conn:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE id = %s",
            (user_id,),
        ).fetchone()
        if row is None or not verify_password(current_password, row[0]):
            return PageShell(
                H1("Vaheta parool", cls="page-title"),
                _password_form(error="Praegune parool on vale.", force=forced),
                title="Vaheta parool",
                user=auth,
                active_nav="/profile",
            )

        change_password(user_id, new_password, conn=conn)
        conn.commit()

    log_action(user_id, "user.password_change", {"forced": forced})

    response = RedirectResponse(url="/auth/login", status_code=303)
    clear_auth_cookie(response, "access_token")
    clear_auth_cookie(response, "refresh_token")
    return response


def register_profile_routes(rt):  # type: ignore[no-untyped-def]
    rt("/profile", methods=["GET"])(profile_page)
    rt("/profile/password", methods=["GET"])(profile_password_page)
    rt("/profile/password", methods=["POST"])(profile_password_post)
