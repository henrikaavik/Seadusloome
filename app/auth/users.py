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

logger = logging.getLogger(__name__)

VALID_ROLES = ("drafter", "reviewer", "org_admin", "admin")
ORG_ASSIGNABLE_ROLES = ("drafter", "reviewer")


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
        logger.exception("Failed to create user email=%s", email)
        return None


def update_user_role(user_id: str, role: str) -> bool:
    """Update a user's role. Returns True on success."""
    if role not in VALID_ROLES:
        logger.error("Invalid role: %s", role)
        return False
    try:
        with _connect() as conn:
            conn.execute("UPDATE users SET role = %s WHERE id = %s", (role, user_id))
            conn.commit()
        return True
    except Exception:
        logger.exception("Failed to update role for user %s", user_id)
        return False


def deactivate_user(user_id: str) -> bool:
    """Deactivate a user by setting is_active to false and revoking sessions.

    Returns True on success.
    """
    try:
        with _connect() as conn:
            conn.execute("UPDATE users SET is_active = FALSE WHERE id = %s", (user_id,))
            conn.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
            conn.commit()
        return True
    except Exception:
        logger.exception("Failed to deactivate user %s", user_id)
        return False


# ---------------------------------------------------------------------------
# Route handlers — System admin (/admin/users)
# ---------------------------------------------------------------------------


def _user_table(users: list[dict], show_org: bool = True) -> Table | P:  # type: ignore[type-arg]
    """Render a user table."""
    if not users:
        return P("Kasutajaid ei leitud.")

    header_cells = [Th("Nimi"), Th("E-post"), Th("Roll"), Th("Staatus")]
    if show_org:
        header_cells.insert(2, Th("Organisatsioon"))
    header_cells.append(Th("Tegevused"))

    rows = []
    for u in users:
        cells = [
            Td(u["full_name"]),
            Td(u["email"]),
            Td(u["role"]),
            Td("Aktiivne" if u.get("is_active", True) else "Deaktiveeritud"),
        ]
        if show_org:
            cells.insert(2, Td(u.get("org_name", "—")))

        actions = []
        actions.append(A("Muuda rolli", href=f"/admin/users/{u['id']}/role"))
        if u.get("is_active", True):
            actions.append(
                Form(
                    Button("Deaktiveeri", type="submit", cls="button secondary"),
                    method="post",
                    action=f"/admin/users/{u['id']}/deactivate",
                    style="display:inline",
                )
            )
        cells.append(Td(*actions))
        rows.append(Tr(*cells))

    return Table(Thead(Tr(*header_cells)), Tbody(*rows))


def admin_user_list(req: Request):
    """GET /admin/users — list all users (system admin)."""
    users = list_users()
    return Titled(
        "Kasutajad",
        A("Lisa uus kasutaja", href="/admin/users/new", cls="button"),
        _user_table(users, show_org=True),
    )


def admin_user_new_form(req: Request):
    """GET /admin/users/new — create user form (system admin)."""
    orgs = list_orgs()
    org_options = [Option("— Ei kuulu —", value="")] + [
        Option(o["name"], value=o["id"]) for o in orgs
    ]
    role_options = [Option(r, value=r) for r in VALID_ROLES]

    return Titled(
        "Uus kasutaja",
        Form(
            Fieldset(
                Label("E-post", Input(name="email", type="email", required=True)),
                Label("Parool", Input(name="password", type="password", required=True)),
                Label("Täisnimi", Input(name="full_name", type="text", required=True)),
                Label("Roll", Select(*role_options, name="role")),
                Label("Organisatsioon", Select(*org_options, name="org_id")),
            ),
            Button("Loo kasutaja", type="submit"),
            " ",
            A("Tühista", href="/admin/users"),
            method="post",
            action="/admin/users",
        ),
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
        return Titled(
            "Viga",
            P(pw_error, style="color:red"),
            A("Tagasi", href="/admin/users/new"),
        )
    actual_org_id = org_id if org_id else None
    user = create_user(email.strip(), password, full_name.strip(), role, actual_org_id)
    if user is None:
        return Titled(
            "Viga",
            P("Kasutaja loomine ebaõnnestus. E-posti aadress võib olla juba kasutusel."),
            A("Tagasi", href="/admin/users/new"),
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
    user = get_user(user_id)
    if user is None:
        return Titled("Viga", P("Kasutajat ei leitud."), A("Tagasi", href="/admin/users"))

    role_options = [Option(r, value=r, selected=(r == user["role"])) for r in VALID_ROLES]
    return Titled(
        "Muuda rolli",
        P(f"Kasutaja: {user['full_name']} ({user['email']})"),
        Form(
            Fieldset(Label("Roll", Select(*role_options, name="role"))),
            Button("Salvesta", type="submit"),
            " ",
            A("Tühista", href="/admin/users"),
            method="post",
            action=f"/admin/users/{user_id}/role",
        ),
    )


def admin_user_role_update(req: Request, user_id: str, role: str):
    """POST /admin/users/{user_id}/role — update user role (system admin)."""
    auth = req.scope.get("auth", {})

    # Prevent admin from changing their own role
    if str(user_id) == str(auth.get("id")):
        return Titled(
            "Viga",
            P("Te ei saa oma rolli muuta."),
            A("Tagasi", href="/admin/users"),
        )

    # If demoting an admin, ensure at least 1 admin remains
    target_user = get_user(user_id)
    if target_user and target_user["role"] == "admin" and role != "admin":
        if count_admins() <= 1:
            return Titled(
                "Viga",
                P("Süsteemis peab olema vähemalt üks administraator."),
                A("Tagasi", href="/admin/users"),
            )

    success = update_user_role(user_id, role)
    if not success:
        return Titled("Viga", P("Rolli muutmine ebaõnnestus."), A("Tagasi", href="/admin/users"))
    log_action(auth.get("id"), "user.role_update", {"user_id": user_id, "new_role": role})
    return RedirectResponse(url="/admin/users", status_code=303)


def admin_user_deactivate(req: Request, user_id: str):
    """POST /admin/users/{user_id}/deactivate — deactivate user (system admin)."""
    auth = req.scope.get("auth", {})
    if str(user_id) == str(auth.get("id")):
        return Titled(
            "Viga", P("Te ei saa ennast deaktiveerida."), A("Tagasi", href="/admin/users")
        )
    target = get_user(user_id)
    if target and target.get("role") == "admin" and count_admins() <= 1:
        return Titled(
            "Viga",
            P("Viimast administraatorit ei saa deaktiveerida."),
            A("Tagasi", href="/admin/users"),
        )
    success = deactivate_user(user_id)
    if not success:
        return Titled(
            "Viga", P("Kasutaja deaktiveerimine ebaõnnestus."), A("Tagasi", href="/admin/users")
        )
    log_action(auth.get("id"), "user.deactivate", {"user_id": user_id})
    return RedirectResponse(url="/admin/users", status_code=303)


# ---------------------------------------------------------------------------
# Route handlers — Org admin (/org/users)
# ---------------------------------------------------------------------------


def org_user_list(req: Request):
    """GET /org/users — list own org users (org admin)."""
    auth = req.scope.get("auth", {})
    org_id = auth.get("org_id")
    if not org_id:
        return Titled("Viga", P("Te ei kuulu ühtegi organisatsiooni."), A("Tagasi", href="/"))
    users = list_users(org_id=org_id)
    return Titled(
        "Organisatsiooni kasutajad",
        A("Kutsu uus kasutaja", href="/org/users/new", cls="button"),
        _user_table(users, show_org=False),
    )


def org_user_new_form(req: Request):
    """GET /org/users/new — invite/create user form (org admin)."""
    role_options = [Option(r, value=r) for r in ORG_ASSIGNABLE_ROLES]
    return Titled(
        "Uus kasutaja",
        Form(
            Fieldset(
                Label("E-post", Input(name="email", type="email", required=True)),
                Label("Parool", Input(name="password", type="password", required=True)),
                Label("Täisnimi", Input(name="full_name", type="text", required=True)),
                Label("Roll", Select(*role_options, name="role")),
            ),
            Button("Loo kasutaja", type="submit"),
            " ",
            A("Tühista", href="/org/users"),
            method="post",
            action="/org/users",
        ),
    )


def org_user_create(req: Request, email: str, password: str, full_name: str, role: str):
    """POST /org/users — create user in own org (org admin)."""
    auth = req.scope.get("auth", {})
    org_id = auth.get("org_id")
    if not org_id:
        return Titled("Viga", P("Te ei kuulu ühtegi organisatsiooni."), A("Tagasi", href="/"))

    pw_error = validate_password(password)
    if pw_error:
        return Titled(
            "Viga",
            P(pw_error, style="color:red"),
            A("Tagasi", href="/org/users/new"),
        )

    if role not in ORG_ASSIGNABLE_ROLES:
        return Titled(
            "Viga",
            P(f"Lubamatu roll: {role}. Lubatud: {', '.join(ORG_ASSIGNABLE_ROLES)}"),
            A("Tagasi", href="/org/users/new"),
        )

    user = create_user(email.strip(), password, full_name.strip(), role, org_id)
    if user is None:
        return Titled(
            "Viga",
            P("Kasutaja loomine ebaõnnestus. E-posti aadress võib olla juba kasutusel."),
            A("Tagasi", href="/org/users/new"),
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
    org_id = auth.get("org_id")

    user = get_user(user_id)
    if user is None or user.get("org_id") != org_id:
        return Titled("Viga", P("Kasutajat ei leitud."), A("Tagasi", href="/org/users"))

    role_options = [Option(r, value=r, selected=(r == user["role"])) for r in ORG_ASSIGNABLE_ROLES]
    return Titled(
        "Muuda rolli",
        P(f"Kasutaja: {user['full_name']} ({user['email']})"),
        Form(
            Fieldset(Label("Roll", Select(*role_options, name="role"))),
            Button("Salvesta", type="submit"),
            " ",
            A("Tühista", href="/org/users"),
            method="post",
            action=f"/org/users/{user_id}/role",
        ),
    )


def org_user_role_update(req: Request, user_id: str, role: str):
    """POST /org/users/{user_id}/role — update user role (org admin)."""
    auth = req.scope.get("auth", {})
    org_id = auth.get("org_id")

    user = get_user(user_id)
    if user is None or user.get("org_id") != org_id:
        return Titled("Viga", P("Kasutajat ei leitud."), A("Tagasi", href="/org/users"))

    if role not in ORG_ASSIGNABLE_ROLES:
        return Titled(
            "Viga",
            P(f"Lubamatu roll: {role}. Lubatud: {', '.join(ORG_ASSIGNABLE_ROLES)}"),
            A("Tagasi", href="/org/users"),
        )

    success = update_user_role(user_id, role)
    if not success:
        return Titled("Viga", P("Rolli muutmine ebaõnnestus."), A("Tagasi", href="/org/users"))
    log_action(auth.get("id"), "user.role_update", {"user_id": user_id, "new_role": role})
    return RedirectResponse(url="/org/users", status_code=303)


def org_user_deactivate(req: Request, user_id: str):
    """POST /org/users/{user_id}/deactivate — deactivate user (org admin)."""
    auth = req.scope.get("auth", {})
    org_id = auth.get("org_id")

    user = get_user(user_id)
    if user is None or user.get("org_id") != org_id:
        return Titled("Viga", P("Kasutajat ei leitud."), A("Tagasi", href="/org/users"))

    success = deactivate_user(user_id)
    if not success:
        return Titled(
            "Viga", P("Kasutaja deaktiveerimine ebaõnnestus."), A("Tagasi", href="/org/users")
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
