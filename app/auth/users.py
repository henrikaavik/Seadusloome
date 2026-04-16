"""User management CRUD operations and admin routes."""

from __future__ import annotations

import logging

import bcrypt
from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.auth.audit import log_action
from app.auth.organizations import list_orgs
from app.auth.roles import require_role
from app.db import get_connection as _connect
from app.ui.data.data_table import Column, DataTable
from app.ui.forms.app_form import AppForm
from app.ui.forms.form_field import FormField, FormSelectField
from app.ui.layout import PageShell
from app.ui.primitives.badge import Badge, StatusBadge
from app.ui.primitives.button import Button
from app.ui.surfaces.alert import Alert
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.theme import get_theme_from_request

logger = logging.getLogger(__name__)

VALID_ROLES = ("drafter", "reviewer", "org_admin", "admin")
ORG_ASSIGNABLE_ROLES = ("drafter", "reviewer")

_ROLE_LABELS = {
    "admin": "Administraator",
    "org_admin": "Organisatsiooni admin",
    "reviewer": "Ülevaataja",
    "drafter": "Koostaja",
}


def _hash_password(password: str) -> str:
    """Hash *password* with bcrypt."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def validate_password(password: str) -> str | None:
    """Return an error message if *password* is too weak, or ``None`` if valid."""
    if len(password) < 8:
        return "Parool peab olema vähemalt 8 tähemärki pikk"
    if not any(c.isupper() for c in password):
        return "Parool peab sisaldama vähemalt ühte suurtähte"
    if not any(c.isdigit() for c in password):
        return "Parool peab sisaldama vähemalt ühte numbrit"
    return None


def count_admins() -> int:
    """Return the number of active admin users."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = TRUE"
        ).fetchone()
        return row[0] if row else 0


# ---------------------------------------------------------------------------
# DB functions
# ---------------------------------------------------------------------------


def list_users(org_id: str | None = None) -> list[dict]:  # type: ignore[type-arg]
    """Return users, optionally filtered by *org_id*."""
    try:
        with _connect() as conn:
            if org_id:
                rows = conn.execute(
                    "SELECT u.id, u.email, u.full_name, u.role, u.org_id, u.is_active, "
                    "o.name AS org_name "
                    "FROM users u LEFT JOIN organizations o ON o.id = u.org_id "
                    "WHERE u.org_id = %s ORDER BY u.full_name",
                    (org_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT u.id, u.email, u.full_name, u.role, u.org_id, u.is_active, "
                    "o.name AS org_name "
                    "FROM users u LEFT JOIN organizations o ON o.id = u.org_id "
                    "ORDER BY u.full_name"
                ).fetchall()
        return [
            {
                "id": str(r[0]),
                "email": r[1],
                "full_name": r[2],
                "role": r[3],
                "org_id": str(r[4]) if r[4] else None,
                "is_active": r[5],
                "org_name": r[6] or "—",
            }
            for r in rows
        ]
    except Exception:
        # TODO: Phase 4 — let exceptions propagate to route layer
        logger.exception("Failed to list users")
        return []


def get_user(user_id: str) -> dict | None:  # type: ignore[type-arg]
    """Return a single user by ID, or None."""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT u.id, u.email, u.full_name, u.role, u.org_id, u.is_active, "
                "o.name AS org_name "
                "FROM users u LEFT JOIN organizations o ON o.id = u.org_id "
                "WHERE u.id = %s",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": str(row[0]),
            "email": row[1],
            "full_name": row[2],
            "role": row[3],
            "org_id": str(row[4]) if row[4] else None,
            "is_active": row[5],
            "org_name": row[6] or "—",
        }
    except Exception:
        # TODO: Phase 4 — let exceptions propagate to route layer
        logger.exception("Failed to get user %s", user_id)
        return None


def create_user(
    email: str,
    password: str,
    full_name: str,
    role: str,
    org_id: str | None = None,
) -> dict | None:  # type: ignore[type-arg]
    """Create a new user. Returns the user dict or None on failure."""
    if role not in VALID_ROLES:
        logger.error("Invalid role: %s", role)
        return None
    try:
        password_hash = _hash_password(password)
        with _connect() as conn:
            row = conn.execute(
                "INSERT INTO users (email, password_hash, full_name, role, org_id) "
                "VALUES (%s, %s, %s, %s, %s) "
                "RETURNING id, email, full_name, role, org_id, is_active",
                (email, password_hash, full_name, role, org_id or None),
            ).fetchone()
            conn.commit()
        if row is None:
            return None
        return {
            "id": str(row[0]),
            "email": row[1],
            "full_name": row[2],
            "role": row[3],
            "org_id": str(row[4]) if row[4] else None,
            "is_active": row[5],
        }
    except Exception:
        # TODO: Phase 4 — let exceptions propagate to route layer
        logger.exception("Failed to create user email=%s", email)
        return None


def update_user_role(user_id: str, role: str) -> bool:
    """Update a user's role. Returns True on success.

    Bumps ``token_version`` in the same UPDATE so every previously-issued
    access token is invalidated immediately (#635).
    """
    if role not in VALID_ROLES:
        logger.error("Invalid role: %s", role)
        return False
    try:
        with _connect() as conn:
            conn.execute(
                "UPDATE users SET role = %s, token_version = token_version + 1 WHERE id = %s",
                (role, user_id),
            )
            conn.commit()
        return True
    except Exception:
        # TODO: Phase 4 — let exceptions propagate to route layer
        logger.exception("Failed to update role for user %s", user_id)
        return False


def deactivate_user(user_id: str) -> bool:
    """Deactivate a user by setting is_active to false and revoking sessions.

    Bumps ``token_version`` so any outstanding access token is rejected
    on its next use (#635), and deletes the user's refresh sessions.

    Returns True on success.
    """
    try:
        with _connect() as conn:
            conn.execute(
                "UPDATE users "
                "SET is_active = FALSE, token_version = token_version + 1 "
                "WHERE id = %s",
                (user_id,),
            )
            conn.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
            conn.commit()
        return True
    except Exception:
        # TODO: Phase 4 — let exceptions propagate to route layer
        logger.exception("Failed to deactivate user %s", user_id)
        return False


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _error_page(req: Request, message: str, back_href: str, active_nav: str):
    """Render an error page wrapped in PageShell."""
    auth = req.scope.get("auth")
    theme = get_theme_from_request(req)
    return PageShell(
        H1("Viga", cls="page-title"),
        Alert(message, variant="danger"),
        P(A("← Tagasi", href=back_href), cls="back-link"),
        title="Viga",
        user=auth,
        theme=theme,
        active_nav=active_nav,
    )


def _user_table(users: list[dict], *, show_org: bool, base_path: str):  # type: ignore[type-arg]
    """Render a user DataTable.

    Args:
        users: User dicts to display.
        show_org: Whether to include the organization column.
        base_path: URL prefix for actions (``/admin/users`` or ``/org/users``).
    """
    if not users:
        return P("Kasutajaid ei leitud.", cls="muted-text")

    def _status_cell(row: dict) -> object:  # type: ignore[type-arg]
        if row["_is_active"]:
            return StatusBadge("ok")
        return Badge("Deaktiveeritud", variant="danger")

    def _role_cell(row: dict) -> object:  # type: ignore[type-arg]
        return Badge(
            _ROLE_LABELS.get(row["role"], row["role"]),
            variant="primary",
        )

    def _actions_cell(row: dict) -> object:  # type: ignore[type-arg]
        actions: list = [
            A(
                "Muuda rolli",
                href=f"{base_path}/{row['id']}/role",
                cls="btn btn-secondary btn-sm",
            )
        ]
        if row["_is_active"]:
            actions.append(
                AppForm(
                    Button(
                        "Deaktiveeri",
                        type="submit",
                        variant="danger",
                        size="sm",
                    ),
                    method="post",
                    action=f"{base_path}/{row['id']}/deactivate",
                    cls="inline-form",
                )
            )
        return Div(*actions, cls="table-actions")

    columns: list[Column] = [
        Column(key="full_name", label="Nimi", sortable=False),
        Column(key="email", label="E-post", sortable=False),
    ]
    if show_org:
        columns.append(Column(key="org_name", label="Organisatsioon", sortable=False))
    columns.extend(
        [
            Column(key="role", label="Roll", sortable=False, render=_role_cell),
            Column(key="status", label="Staatus", sortable=False, render=_status_cell),
            Column(
                key="actions",
                label="Tegevused",
                sortable=False,
                render=_actions_cell,
            ),
        ]
    )

    rows = [
        {
            "id": u["id"],
            "full_name": u["full_name"],
            "email": u["email"],
            "org_name": u.get("org_name", "—"),
            "role": u["role"],
            "_is_active": u.get("is_active", True),
        }
        for u in users
    ]

    return DataTable(columns=columns, rows=rows, empty_message="Kasutajaid ei leitud.")


# ---------------------------------------------------------------------------
# Route handlers — System admin (/admin/users)
# ---------------------------------------------------------------------------


def admin_user_list(req: Request):
    """GET /admin/users — list all users (system admin)."""
    auth = req.scope.get("auth")
    theme = get_theme_from_request(req)
    users = list_users()

    content = (
        H1("Kasutajad", cls="page-title"),
        Div(
            A(
                "Lisa uus kasutaja",
                href="/admin/users/new",
                cls="btn btn-primary btn-md",
            ),
            cls="page-actions",
        ),
        Card(
            CardHeader(H3("Kõik kasutajad", cls="card-title")),
            CardBody(_user_table(users, show_org=True, base_path="/admin/users")),
        ),
    )

    return PageShell(
        *content,
        title="Kasutajad",
        user=auth,
        theme=theme,
        active_nav="/admin",
    )


def admin_user_new_form(req: Request):
    """GET /admin/users/new — create user form (system admin)."""
    auth = req.scope.get("auth")
    theme = get_theme_from_request(req)

    orgs = list_orgs()
    org_options: list = [("", "— Ei kuulu —")] + [(o["id"], o["name"]) for o in orgs]
    role_options = [(r, _ROLE_LABELS.get(r, r)) for r in VALID_ROLES]

    form = AppForm(
        FormField(name="email", label="E-post", type="email", required=True),
        FormField(name="password", label="Parool", type="password", required=True),
        FormField(name="full_name", label="Täisnimi", type="text", required=True),
        FormSelectField(name="role", label="Roll", options=role_options),
        FormSelectField(name="org_id", label="Organisatsioon", options=org_options),
        Div(
            Button("Loo kasutaja", type="submit", variant="primary"),
            A("Tühista", href="/admin/users", cls="btn btn-ghost btn-md"),
            cls="form-actions",
        ),
        method="post",
        action="/admin/users",
    )

    return PageShell(
        H1("Uus kasutaja", cls="page-title"),
        Card(CardBody(form)),
        title="Uus kasutaja",
        user=auth,
        theme=theme,
        active_nav="/admin",
    )


def admin_user_create(
    req: Request,
    email: str,
    password: str,
    full_name: str,
    role: str,
    org_id: str = "",
):
    """POST /admin/users — create a new user (system admin)."""
    pw_error = validate_password(password)
    if pw_error:
        return _error_page(req, pw_error, "/admin/users/new", "/admin")
    actual_org_id = org_id if org_id else None
    user = create_user(email.strip(), password, full_name.strip(), role, actual_org_id)
    if user is None:
        return _error_page(
            req,
            "Kasutaja loomine ebaõnnestus. E-posti aadress võib olla juba kasutusel.",
            "/admin/users/new",
            "/admin",
        )
    auth = req.scope.get("auth", {})
    log_action(
        auth.get("id"),
        "user.create",
        {"user_id": user["id"], "email": email, "role": role, "org_id": actual_org_id},
    )
    return RedirectResponse(url="/admin/users", status_code=303)


def admin_user_role_form(req: Request, user_id: str):
    """GET /admin/users/{user_id}/role — change role form (system admin)."""
    auth = req.scope.get("auth")
    theme = get_theme_from_request(req)

    user = get_user(user_id)
    if user is None:
        return _error_page(req, "Kasutajat ei leitud.", "/admin/users", "/admin")

    role_options = [(r, _ROLE_LABELS.get(r, r)) for r in VALID_ROLES]

    form = AppForm(
        FormSelectField(
            name="role",
            label="Roll",
            options=role_options,
            value=user["role"],
        ),
        Div(
            Button("Salvesta", type="submit", variant="primary"),
            A("Tühista", href="/admin/users", cls="btn btn-ghost btn-md"),
            cls="form-actions",
        ),
        method="post",
        action=f"/admin/users/{user_id}/role",
    )

    return PageShell(
        H1("Muuda rolli", cls="page-title"),
        Card(
            CardHeader(P(f"Kasutaja: {user['full_name']} ({user['email']})", cls="card-subtitle")),
            CardBody(form),
        ),
        title="Muuda rolli",
        user=auth,
        theme=theme,
        active_nav="/admin",
    )


def admin_user_role_update(req: Request, user_id: str, role: str):
    """POST /admin/users/{user_id}/role — update user role (system admin)."""
    auth = req.scope.get("auth", {})

    # Prevent admin from changing their own role
    if str(user_id) == str(auth.get("id")):
        return _error_page(req, "Te ei saa oma rolli muuta.", "/admin/users", "/admin")

    # If demoting an admin, ensure at least 1 admin remains
    target_user = get_user(user_id)
    if target_user and target_user["role"] == "admin" and role != "admin":
        if count_admins() <= 1:
            return _error_page(
                req,
                "Süsteemis peab olema vähemalt üks administraator.",
                "/admin/users",
                "/admin",
            )

    success = update_user_role(user_id, role)
    if not success:
        return _error_page(req, "Rolli muutmine ebaõnnestus.", "/admin/users", "/admin")
    log_action(auth.get("id"), "user.role_update", {"user_id": user_id, "new_role": role})
    return RedirectResponse(url="/admin/users", status_code=303)


def admin_user_deactivate(req: Request, user_id: str):
    """POST /admin/users/{user_id}/deactivate — deactivate user (system admin)."""
    auth = req.scope.get("auth", {})
    if str(user_id) == str(auth.get("id")):
        return _error_page(req, "Te ei saa ennast deaktiveerida.", "/admin/users", "/admin")
    target = get_user(user_id)
    if target and target.get("role") == "admin" and count_admins() <= 1:
        return _error_page(
            req,
            "Viimast administraatorit ei saa deaktiveerida.",
            "/admin/users",
            "/admin",
        )
    success = deactivate_user(user_id)
    if not success:
        return _error_page(req, "Kasutaja deaktiveerimine ebaõnnestus.", "/admin/users", "/admin")
    log_action(auth.get("id"), "user.deactivate", {"user_id": user_id})
    return RedirectResponse(url="/admin/users", status_code=303)


# ---------------------------------------------------------------------------
# Route handlers — Org admin (/org/users)
# ---------------------------------------------------------------------------


def org_user_list(req: Request):
    """GET /org/users — list own org users (org admin)."""
    auth = req.scope.get("auth", {})
    theme = get_theme_from_request(req)
    org_id = auth.get("org_id")
    if not org_id:
        return _error_page(req, "Te ei kuulu ühtegi organisatsiooni.", "/", "/org/users")
    users = list_users(org_id=org_id)

    content = (
        H1("Organisatsiooni kasutajad", cls="page-title"),
        Div(
            A(
                "Kutsu uus kasutaja",
                href="/org/users/new",
                cls="btn btn-primary btn-md",
            ),
            cls="page-actions",
        ),
        Card(
            CardHeader(H3("Organisatsiooni liikmed", cls="card-title")),
            CardBody(_user_table(users, show_org=False, base_path="/org/users")),
        ),
    )

    return PageShell(
        *content,
        title="Organisatsiooni kasutajad",
        user=auth or None,
        theme=theme,
        active_nav="/org/users",
    )


def org_user_new_form(req: Request):
    """GET /org/users/new — invite/create user form (org admin)."""
    auth = req.scope.get("auth")
    theme = get_theme_from_request(req)

    role_options = [(r, _ROLE_LABELS.get(r, r)) for r in ORG_ASSIGNABLE_ROLES]

    form = AppForm(
        FormField(name="email", label="E-post", type="email", required=True),
        FormField(name="password", label="Parool", type="password", required=True),
        FormField(name="full_name", label="Täisnimi", type="text", required=True),
        FormSelectField(name="role", label="Roll", options=role_options),
        Div(
            Button("Loo kasutaja", type="submit", variant="primary"),
            A("Tühista", href="/org/users", cls="btn btn-ghost btn-md"),
            cls="form-actions",
        ),
        method="post",
        action="/org/users",
    )

    return PageShell(
        H1("Uus kasutaja", cls="page-title"),
        Card(CardBody(form)),
        title="Uus kasutaja",
        user=auth,
        theme=theme,
        active_nav="/org/users",
    )


def org_user_create(req: Request, email: str, password: str, full_name: str, role: str):
    """POST /org/users — create user in own org (org admin)."""
    auth = req.scope.get("auth", {})
    org_id = auth.get("org_id")
    if not org_id:
        return _error_page(req, "Te ei kuulu ühtegi organisatsiooni.", "/", "/org/users")

    pw_error = validate_password(password)
    if pw_error:
        return _error_page(req, pw_error, "/org/users/new", "/org/users")

    if role not in ORG_ASSIGNABLE_ROLES:
        return _error_page(
            req,
            f"Lubamatu roll: {role}. Lubatud: {', '.join(ORG_ASSIGNABLE_ROLES)}",
            "/org/users/new",
            "/org/users",
        )

    user = create_user(email.strip(), password, full_name.strip(), role, org_id)
    if user is None:
        return _error_page(
            req,
            "Kasutaja loomine ebaõnnestus. E-posti aadress võib olla juba kasutusel.",
            "/org/users/new",
            "/org/users",
        )
    log_action(
        auth.get("id"),
        "user.create",
        {"user_id": user["id"], "email": email, "role": role, "org_id": org_id},
    )
    return RedirectResponse(url="/org/users", status_code=303)


def org_user_role_form(req: Request, user_id: str):
    """GET /org/users/{user_id}/role — change role form (org admin)."""
    auth = req.scope.get("auth", {})
    theme = get_theme_from_request(req)
    org_id = auth.get("org_id")

    user = get_user(user_id)
    if user is None or user.get("org_id") != org_id:
        return _error_page(req, "Kasutajat ei leitud.", "/org/users", "/org/users")

    role_options = [(r, _ROLE_LABELS.get(r, r)) for r in ORG_ASSIGNABLE_ROLES]

    form = AppForm(
        FormSelectField(
            name="role",
            label="Roll",
            options=role_options,
            value=user["role"],
        ),
        Div(
            Button("Salvesta", type="submit", variant="primary"),
            A("Tühista", href="/org/users", cls="btn btn-ghost btn-md"),
            cls="form-actions",
        ),
        method="post",
        action=f"/org/users/{user_id}/role",
    )

    return PageShell(
        H1("Muuda rolli", cls="page-title"),
        Card(
            CardHeader(P(f"Kasutaja: {user['full_name']} ({user['email']})", cls="card-subtitle")),
            CardBody(form),
        ),
        title="Muuda rolli",
        user=auth or None,
        theme=theme,
        active_nav="/org/users",
    )


def org_user_role_update(req: Request, user_id: str, role: str):
    """POST /org/users/{user_id}/role — update user role (org admin)."""
    auth = req.scope.get("auth", {})
    org_id = auth.get("org_id")

    user = get_user(user_id)
    if user is None or user.get("org_id") != org_id:
        return _error_page(req, "Kasutajat ei leitud.", "/org/users", "/org/users")

    if role not in ORG_ASSIGNABLE_ROLES:
        return _error_page(
            req,
            f"Lubamatu roll: {role}. Lubatud: {', '.join(ORG_ASSIGNABLE_ROLES)}",
            "/org/users",
            "/org/users",
        )

    success = update_user_role(user_id, role)
    if not success:
        return _error_page(req, "Rolli muutmine ebaõnnestus.", "/org/users", "/org/users")
    log_action(auth.get("id"), "user.role_update", {"user_id": user_id, "new_role": role})
    return RedirectResponse(url="/org/users", status_code=303)


def org_user_deactivate(req: Request, user_id: str):
    """POST /org/users/{user_id}/deactivate — deactivate user (org admin)."""
    auth = req.scope.get("auth", {})
    org_id = auth.get("org_id")

    user = get_user(user_id)
    if user is None or user.get("org_id") != org_id:
        return _error_page(req, "Kasutajat ei leitud.", "/org/users", "/org/users")

    success = deactivate_user(user_id)
    if not success:
        return _error_page(
            req,
            "Kasutaja deaktiveerimine ebaõnnestus.",
            "/org/users",
            "/org/users",
        )
    log_action(auth.get("id"), "user.deactivate", {"user_id": user_id})
    return RedirectResponse(url="/org/users", status_code=303)


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

# System admin routes
_admin_user_list = require_role("admin")(admin_user_list)
_admin_user_new_form = require_role("admin")(admin_user_new_form)
_admin_user_create = require_role("admin")(admin_user_create)
_admin_user_role_form = require_role("admin")(admin_user_role_form)
_admin_user_role_update = require_role("admin")(admin_user_role_update)
_admin_user_deactivate = require_role("admin")(admin_user_deactivate)

# Org admin routes
_org_user_list = require_role("org_admin", "admin")(org_user_list)
_org_user_new_form = require_role("org_admin", "admin")(org_user_new_form)
_org_user_create = require_role("org_admin", "admin")(org_user_create)
_org_user_role_form = require_role("org_admin", "admin")(org_user_role_form)
_org_user_role_update = require_role("org_admin", "admin")(org_user_role_update)
_org_user_deactivate = require_role("org_admin", "admin")(org_user_deactivate)


def register_user_routes(rt) -> None:  # type: ignore[no-untyped-def]
    """Register user management routes on the FastHTML route decorator *rt*."""
    # System admin routes
    rt("/admin/users", methods=["GET"])(_admin_user_list)
    rt("/admin/users/new", methods=["GET"])(_admin_user_new_form)
    rt("/admin/users", methods=["POST"])(_admin_user_create)
    rt("/admin/users/{user_id}/role", methods=["GET"])(_admin_user_role_form)
    rt("/admin/users/{user_id}/role", methods=["POST"])(_admin_user_role_update)
    rt("/admin/users/{user_id}/deactivate", methods=["POST"])(_admin_user_deactivate)

    # Org admin routes
    rt("/org/users", methods=["GET"])(_org_user_list)
    rt("/org/users/new", methods=["GET"])(_org_user_new_form)
    rt("/org/users", methods=["POST"])(_org_user_create)
    rt("/org/users/{user_id}/role", methods=["GET"])(_org_user_role_form)
    rt("/org/users/{user_id}/role", methods=["POST"])(_org_user_role_update)
    rt("/org/users/{user_id}/deactivate", methods=["POST"])(_org_user_deactivate)
