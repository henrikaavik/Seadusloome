"""User profile routes — `/profile` and `/profile/password`.

Implements the minimum viable profile UI from
``docs/superpowers/specs/2026-04-28-password-management-design.md`` §7.4
plus the P1 fix from
``docs/2026-04-29-ui-review-seadusloome-live.md``: the user-menu
``Minu profiil`` link in the topbar (``app/ui/layout/top_bar.py:24``)
previously 404'd because no ``/profile`` route was registered.

Out of scope (future work — see spec §5.1, §5.3, §5.4):
* self-service forgot password (``/auth/forgot``, ``/auth/reset/...``);
* admin-initiated reset emails / temp passwords;
* Postmark email integration.

This module ships the authenticated change-password flow only, which
is the smallest surface that lets the seeded admin escape the forced
``must_change_password=TRUE`` redirect and lets every user reach
``/profile`` without a 404.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.auth.audit import log_action
from app.auth.cookies import clear_auth_cookie
from app.auth.jwt_provider import verify_password
from app.auth.password import change_password, validate_password
from app.auth.provider import UserDict
from app.db import get_connection
from app.ui.feedback.flash import push_flash
from app.ui.forms.app_form import AppForm
from app.ui.forms.form_field import FormField
from app.ui.layout import PageShell
from app.ui.primitives.button import Button
from app.ui.surfaces.alert import Alert
from app.ui.surfaces.card import Card, CardBody, CardHeader

logger = logging.getLogger(__name__)


def _shell_user(req: Request) -> UserDict | None:
    """Return the authenticated UserDict (or None) for PageShell.

    ``req.scope['auth']`` is typed as ``Any | None`` because the
    middleware writes the dict via ``req.scope["auth"] = user``. We
    cast it for the type checker and return ``None`` when missing so
    PageShell renders the unauthenticated state.
    """
    auth = req.scope.get("auth")
    if not auth:
        return None
    return cast(UserDict, auth)


# ---------------------------------------------------------------------------
# View helpers
# ---------------------------------------------------------------------------


def _password_form(*, error: str | None = None, must_change: bool = False):
    """Render the change-password form.

    The forced-change banner is shown only when the authenticated user
    arrived here because middleware redirected them. After they
    successfully change the password the flag is cleared and they will
    not see the banner again.
    """
    children: list = []
    if must_change:
        children.append(
            Alert(
                "Administraator on lähtestanud teie parooli. "
                "Palun määrake uus parool jätkamiseks.",
                variant="warning",
            )
        )
    if error:
        children.append(Alert(error, variant="danger"))
    children.append(
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
                help=("Vähemalt 8 tähemärki, sealhulgas üks suurtäht ja üks number."),
            ),
            FormField(
                name="new_password_confirm",
                label="Korda uut parooli",
                type="password",
                required=True,
            ),
            Div(  # noqa: F405
                Button("Salvesta uus parool", type="submit", variant="primary"),
                # If we are inside the forced-change flow, hide the
                # ``Tühista`` link — the user has nowhere else to go
                # until the password is changed (middleware will just
                # bounce them back). Otherwise let them return to
                # ``/profile``.
                A("Tühista", href="/profile", cls="btn btn-ghost btn-md")  # noqa: F405
                if not must_change
                else None,
                cls="form-actions",
            ),
            method="post",
            action="/profile/password",
        )
    )
    return Card(
        CardHeader(H2("Vaheta parool", cls="card-title")),  # noqa: F405
        CardBody(*children),
    )


def _profile_card(user: dict[str, Any]):
    """Render the profile landing card."""
    return Card(
        CardHeader(H2("Profiil", cls="card-title")),  # noqa: F405
        CardBody(
            P(  # noqa: F405
                Strong(user.get("full_name") or user.get("email", "")),  # noqa: F405
                cls="muted-text",
            ),
            P(user.get("email", ""), cls="muted-text"),  # noqa: F405
            Div(  # noqa: F405
                A(  # noqa: F405
                    "Vaheta parool",
                    href="/profile/password",
                    cls="btn btn-primary btn-md",
                ),
                cls="form-actions",
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def profile_page(req: Request):
    """GET /profile — landing page with a link to change password."""
    user = _shell_user(req)
    return PageShell(
        _profile_card(dict(user) if user else {}),
        title="Profiil",
        user=user,
        request=req,
        container_size="sm",
    )


def password_page(req: Request):
    """GET /profile/password — render the change-password form."""
    user = _shell_user(req)
    must_change = bool(user.get("must_change_password")) if user else False
    return PageShell(
        _password_form(must_change=must_change),
        title="Vaheta parool",
        user=user,
        request=req,
        container_size="sm",
    )


def password_post(  # noqa: ANN201
    req: Request,
    current_password: str = "",
    new_password: str = "",
    new_password_confirm: str = "",
):
    """POST /profile/password — verify, validate, rotate.

    On any validation error we re-render the form with an
    ``Alert(variant="danger")`` so the user can correct it without
    losing context. On success we audit-log, push a flash, clear both
    auth cookies, and redirect to ``/auth/login`` so the user signs
    back in with the new password (every refresh session for the user
    has been deleted by ``change_password`` so the cookies would be
    dead anyway — clearing them explicitly avoids one extra round-trip
    through silent refresh).
    """
    auth = req.scope.get("auth") or {}
    user_id = auth.get("id")
    user_email = auth.get("email") or ""
    must_change = bool(auth.get("must_change_password"))

    if not user_id:
        # auth_before guards every non-skip path so this branch is
        # defensive; if it ever fires, just bounce to login.
        return RedirectResponse(url="/auth/login", status_code=303)

    # 1. Verify current password against the DB hash. We deliberately
    # do not reuse ``JWTAuthProvider.authenticate`` because that
    # additionally re-checks ``is_active`` etc; here we already know
    # the user is authenticated and active (otherwise auth_before
    # would have rejected the request).
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT password_hash FROM users WHERE id = %s",
                (user_id,),
            ).fetchone()
    except Exception:
        logger.exception("Failed to read password hash for user %s", user_id)
        return _render_error(req, "Parooli muutmine ebaõnnestus. Palun proovige uuesti.")

    if row is None or not verify_password(current_password, row[0]):
        return _render_error(req, "Praegune parool on vale.", must_change=must_change)

    # 2. Validate the new password (length / upper / digit / no-email-substring).
    pw_error = validate_password(new_password, email=user_email)
    if pw_error:
        return _render_error(req, pw_error, must_change=must_change)

    # 3. Confirm the two new-password inputs match.
    if new_password != new_password_confirm:
        return _render_error(req, "Paroolid ei kattu.", must_change=must_change)

    # 4. Rotate atomically. ``change_password`` bumps token_version,
    # deletes sessions, sets password_changed_at, and clears
    # must_change_password (must_change=False here — this is the
    # self-service change flow, not the admin-temp flow).
    try:
        with get_connection() as conn:
            change_password(user_id, new_password, conn=conn, must_change=False)
    except Exception:
        logger.exception("change_password failed for user %s", user_id)
        return _render_error(req, "Parooli muutmine ebaõnnestus. Palun proovige uuesti.")

    log_action(user_id, "user.password_change", {"forced": must_change})

    push_flash(
        req,
        "Parool on muudetud. Palun logi uuesti sisse.",
        kind="success",
    )
    response = RedirectResponse(url="/auth/login", status_code=303)
    clear_auth_cookie(response, "access_token")
    clear_auth_cookie(response, "refresh_token")
    return response


def _render_error(req: Request, message: str, *, must_change: bool = False):
    """Re-render the change-password form with an inline error alert."""
    return PageShell(
        _password_form(error=message, must_change=must_change),
        title="Vaheta parool",
        user=_shell_user(req),
        request=req,
        container_size="sm",
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_profile_routes(rt) -> None:  # type: ignore[no-untyped-def]
    """Register ``/profile`` + ``/profile/password`` on the route decorator."""
    rt("/profile", methods=["GET"])(profile_page)
    rt("/profile/password", methods=["GET"])(password_page)
    rt("/profile/password", methods=["POST"])(password_post)
