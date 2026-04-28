"""Authentication routes for the Seadusloome FastHTML application."""

from __future__ import annotations

import logging
import os

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.auth.audit import log_action
from app.auth.cookies import clear_auth_cookie, set_auth_cookie
from app.auth.jwt_provider import JWTAuthProvider
from app.auth.password import (
    change_password,
    claim_reset_token,
    hash_email,
    hash_token,
    issue_reset_token,
    validate_password,
)
from app.db import get_connection
from app.email.service import get_email_provider
from app.email.templates import password_reset
from app.ui.forms.app_form import AppForm
from app.ui.forms.form_field import FormField
from app.ui.layout import PageShell
from app.ui.primitives.button import Button
from app.ui.surfaces.alert import Alert
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.theme import get_theme_from_request

logger = logging.getLogger(__name__)

EMAIL_RATE_LIMIT_PER_HOUR = 3
IP_RATE_LIMIT_PER_HOUR = 10

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
            AppForm(
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


# ---------------------------------------------------------------------------
# Forgot-password handlers
# ---------------------------------------------------------------------------


def _forgot_form(error: str | None = None):
    return Card(
        CardHeader(H2("Parooli lähtestamine", cls="card-title")),
        CardBody(
            Alert(error, variant="danger") if error else None,
            P(
                "Sisestage oma e-posti aadress ja saadame teile parooli lähtestamise lingi.",
                cls="muted-text",
            ),
            AppForm(
                FormField(
                    name="email",
                    label="E-post",
                    type="email",
                    required=True,
                    validator="email",
                ),
                Button("Saada lähtestamise link", type="submit", variant="primary"),
                method="post",
                action="/auth/forgot",
                cls="auth-form",
            ),
            P(
                A("← Tagasi sisselogimisele", href="/auth/login"),
                cls="back-link",
            ),
        ),
        cls="auth-card",
    )


def _forgot_sent_page(req: Request):
    """Generic post-submit page — same response for known and unknown emails."""
    return PageShell(
        Card(
            CardHeader(H2("Kontrollige e-posti", cls="card-title")),
            CardBody(
                P(
                    "Kui see e-post on registreeritud, saatsime parooli "
                    "lähtestamise lingi. Vaata e-postist."
                ),
                P(
                    A("← Tagasi sisselogimisele", href="/auth/login"),
                    cls="back-link",
                ),
            ),
            cls="auth-card",
        ),
        title="Parooli lähtestamine",
        user=None,
        theme=get_theme_from_request(req),
        container_size="sm",
    )


def forgot_page(req: Request):
    return PageShell(
        _forgot_form(),
        title="Parooli lähtestamine",
        user=None,
        theme=get_theme_from_request(req),
        container_size="sm",
    )


def forgot_post(req: Request, email: str):
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return forgot_page(req)

    email_h = hash_email(email)
    ip = (req.client.host if req.client else "unknown") or "unknown"

    with get_connection() as conn:
        conn.execute(
            "INSERT INTO password_reset_attempts (email_hash, ip) VALUES (%s, %s)",
            (email_h, ip),
        )
        conn.commit()

        # Rate-limit checks BEFORE user lookup → unknown emails throttled identically.
        email_count_row = conn.execute(
            "SELECT COUNT(*) FROM password_reset_attempts "
            "WHERE email_hash = %s AND attempted_at > now() - interval '1 hour'",
            (email_h,),
        ).fetchone()
        n_email: int = email_count_row[0] if email_count_row is not None else 0
        ip_count_row = conn.execute(
            "SELECT COUNT(*) FROM password_reset_attempts "
            "WHERE ip = %s AND attempted_at > now() - interval '1 hour'",
            (ip,),
        ).fetchone()
        n_ip: int = ip_count_row[0] if ip_count_row is not None else 0
        if n_email > EMAIL_RATE_LIMIT_PER_HOUR or n_ip > IP_RATE_LIMIT_PER_HOUR:
            logger.info("forgot rate-limited email_hash=%s ip=%s", email_h, ip)
            return _forgot_sent_page(req)

        row = conn.execute(
            "SELECT id, full_name FROM users WHERE email = %s AND is_active = TRUE",
            (email,),
        ).fetchone()

        if row is not None:
            user_id, full_name = row
            raw = issue_reset_token(user_id=user_id, created_by=None, conn=conn)
            conn.commit()
            base = os.environ.get("APP_BASE_URL", "http://localhost:8000").rstrip("/")
            reset_url = f"{base}/auth/reset/{raw}"
            subject, html, text = password_reset(full_name=full_name, reset_url=reset_url)
            try:
                get_email_provider().send(to=email, subject=subject, html=html, text=text)
            except Exception:
                logger.exception("password reset email failed to send")

    return _forgot_sent_page(req)


# ---------------------------------------------------------------------------
# Reset-password handlers
# ---------------------------------------------------------------------------


def _reset_form(token: str, error: str | None = None):
    return Card(
        CardHeader(H2("Määra uus parool", cls="card-title")),
        CardBody(
            Alert(error, variant="danger") if error else None,
            AppForm(
                FormField(name="new_password", label="Uus parool", type="password", required=True),
                FormField(
                    name="new_password_confirm",
                    label="Korda uut parooli",
                    type="password",
                    required=True,
                ),
                Button("Salvesta uus parool", type="submit", variant="primary"),
                method="post",
                action=f"/auth/reset/{token}",
                cls="auth-form",
            ),
        ),
        cls="auth-card",
    )


def _reset_invalid_page(req: Request):
    return PageShell(
        Card(
            CardHeader(H2("Lähtestamise link on aegunud või vigane", cls="card-title")),
            CardBody(
                P("Palun taotlege uus parooli lähtestamise link."),
                P(A("Taotle uus link", href="/auth/forgot"), cls="back-link"),
            ),
            cls="auth-card",
        ),
        title="Lähtestamise link",
        user=None,
        theme=get_theme_from_request(req),
        container_size="sm",
    )


def reset_page(req: Request, token: str):
    digest = hash_token(token)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM password_reset_tokens "
            "WHERE token_hash = %s AND used_at IS NULL AND expires_at > now()",
            (digest,),
        ).fetchone()
    if row is None:
        return _reset_invalid_page(req)

    return PageShell(
        _reset_form(token),
        title="Määra uus parool",
        user=None,
        theme=get_theme_from_request(req),
        container_size="sm",
    )


def reset_post(req: Request, token: str, new_password: str, new_password_confirm: str):
    if new_password != new_password_confirm:
        return PageShell(
            _reset_form(token, error="Paroolid ei kattu."),
            title="Määra uus parool",
            user=None,
            theme=get_theme_from_request(req),
            container_size="sm",
        )

    pw_error = validate_password(new_password)
    if pw_error:
        return PageShell(
            _reset_form(token, error=pw_error),
            title="Määra uus parool",
            user=None,
            theme=get_theme_from_request(req),
            container_size="sm",
        )

    with get_connection() as conn:
        with conn.transaction():
            claimed = claim_reset_token(token, conn=conn)
            if claimed is None:
                # Token already used / expired / never existed.
                return _reset_invalid_page(req)
            user_id, _created_by = claimed
            change_password(user_id, new_password, conn=conn)
        conn.commit()

    log_action(user_id, "user.password_reset", {"self_service": True})

    response = RedirectResponse(url="/auth/login", status_code=303)
    clear_auth_cookie(response, "access_token")
    clear_auth_cookie(response, "refresh_token")
    return response


def register_auth_routes(rt):  # type: ignore[no-untyped-def]
    """Register authentication routes on the FastHTML route decorator *rt*."""
    rt("/auth/login", methods=["GET"])(login_page)
    rt("/auth/login", methods=["POST"])(login_post)
    rt("/auth/logout", methods=["POST"])(logout_post)
    rt("/auth/forgot", methods=["GET"])(forgot_page)
    rt("/auth/forgot", methods=["POST"])(forgot_post)
    rt("/auth/reset/{token}", methods=["GET"])(reset_page)
    rt("/auth/reset/{token}", methods=["POST"])(reset_post)
