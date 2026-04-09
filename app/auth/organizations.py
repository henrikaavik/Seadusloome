"""Organization CRUD operations and admin routes."""

from __future__ import annotations

import logging
import re

from fasthtml.common import *
from starlette.requests import Request

from app.auth.audit import log_action
from app.auth.roles import require_role
from app.db import get_connection as _connect
from app.ui.data.data_table import Column, DataTable
from app.ui.forms.form_field import FormField
from app.ui.layout import PageShell
from app.ui.primitives.button import Button
from app.ui.surfaces.alert import Alert
from app.ui.surfaces.card import Card, CardBody, CardHeader
from app.ui.theme import get_theme_from_request

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def slugify(name: str) -> str:
    """Convert *name* to a URL-safe slug.

    Lowercase, replace whitespace with hyphens, strip non-alphanumeric
    characters (except hyphens), and collapse multiple hyphens.
    """
    slug = name.lower().strip()
    slug = re.sub(r"\s+", "-", slug)
    slug = re.sub(r"[^a-z0-9\-]", "", slug)
    slug = re.sub(r"-{2,}", "-", slug)
    slug = slug.strip("-")
    return slug


# ---------------------------------------------------------------------------
# DB functions
# ---------------------------------------------------------------------------


def list_orgs() -> list[dict]:  # type: ignore[type-arg]
    """Return all organizations ordered by name."""
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT id, name, slug, created_at FROM organizations ORDER BY name"
            ).fetchall()
        return [{"id": str(r[0]), "name": r[1], "slug": r[2], "created_at": r[3]} for r in rows]
    except Exception:
        logger.exception("Failed to list organizations")
        return []


def get_org(org_id: str) -> dict | None:  # type: ignore[type-arg]
    """Return a single organization by ID, or None."""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT id, name, slug, created_at FROM organizations WHERE id = %s",
                (org_id,),
            ).fetchone()
        if row is None:
            return None
        return {"id": str(row[0]), "name": row[1], "slug": row[2], "created_at": row[3]}
    except Exception:
        logger.exception("Failed to get organization %s", org_id)
        return None


def create_org(name: str, slug: str) -> dict | None:  # type: ignore[type-arg]
    """Insert a new organization. Returns the created org dict or None on failure."""
    try:
        with _connect() as conn:
            row = conn.execute(
                "INSERT INTO organizations (name, slug) VALUES (%s, %s) "
                "RETURNING id, name, slug, created_at",
                (name, slug),
            ).fetchone()
            conn.commit()
        if row is None:
            return None
        return {"id": str(row[0]), "name": row[1], "slug": row[2], "created_at": row[3]}
    except Exception:
        logger.exception("Failed to create organization name=%s", name)
        return None


def update_org(org_id: str, name: str, slug: str) -> dict | None:  # type: ignore[type-arg]
    """Update an organization's name and slug. Returns updated org dict or None."""
    try:
        with _connect() as conn:
            row = conn.execute(
                "UPDATE organizations SET name = %s, slug = %s WHERE id = %s "
                "RETURNING id, name, slug, created_at",
                (name, slug, org_id),
            ).fetchone()
            conn.commit()
        if row is None:
            return None
        return {"id": str(row[0]), "name": row[1], "slug": row[2], "created_at": row[3]}
    except Exception:
        logger.exception("Failed to update organization %s", org_id)
        return None


def delete_org(org_id: str) -> bool:
    """Delete an organization if it has no users. Returns True on success."""
    try:
        user_count = get_org_user_count(org_id)
        if user_count > 0:
            return False
        with _connect() as conn:
            conn.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
            conn.commit()
        return True
    except Exception:
        logger.exception("Failed to delete organization %s", org_id)
        return False


def get_org_user_count(org_id: str) -> int:
    """Return the number of users in the given organization."""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM users WHERE org_id = %s", (org_id,)
            ).fetchone()
        return row[0] if row else 0
    except Exception:
        logger.exception("Failed to count users for org %s", org_id)
        return 0


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _error_page(req: Request, message: str, back_href: str = "/admin/organizations"):
    """Render an error page wrapped in the PageShell."""
    auth = req.scope.get("auth")
    theme = get_theme_from_request(req)
    return PageShell(
        H1("Viga", cls="page-title"),
        Alert(message, variant="danger"),
        P(A("← Tagasi", href=back_href), cls="back-link"),
        title="Viga",
        user=auth,
        theme=theme,
        active_nav="/admin",
    )


def _org_list_page(req: Request, orgs: list[dict], message: str | None = None):  # type: ignore[type-arg]
    """Render the organization list page."""
    auth = req.scope.get("auth")
    theme = get_theme_from_request(req)

    org_rows = []
    for org in orgs:
        count = get_org_user_count(org["id"])
        org_rows.append(
            {
                "id": org["id"],
                "name": org["name"],
                "slug": org["slug"],
                "user_count": str(count),
                "_user_count": count,
            }
        )

    def _render_actions(row: dict) -> object:  # type: ignore[type-arg]
        actions: list = [
            A(
                "Muuda",
                href=f"/admin/organizations/{row['id']}/edit",
                cls="btn btn-secondary btn-sm",
            )
        ]
        if row["_user_count"] == 0:
            actions.append(
                Form(
                    Button(
                        "Kustuta",
                        type="submit",
                        variant="danger",
                        size="sm",
                    ),
                    method="post",
                    action=f"/admin/organizations/{row['id']}/delete",
                    cls="inline-form",
                )
            )
        return Div(*actions, cls="table-actions")

    columns = [
        Column(key="name", label="Nimi", sortable=False),
        Column(key="slug", label="Lühitunnus", sortable=False),
        Column(key="user_count", label="Kasutajaid", sortable=False, align="right"),
        Column(key="actions", label="Tegevused", sortable=False, render=_render_actions),
    ]

    table_or_empty: object
    if org_rows:
        table_or_empty = DataTable(
            columns=columns,
            rows=org_rows,
            empty_message="Organisatsioone ei leitud.",
        )
    else:
        table_or_empty = P("Organisatsioone ei leitud.", cls="muted-text")

    card_body_children: list = []
    if message:
        card_body_children.append(Alert(message, variant="success"))
    card_body_children.append(table_or_empty)

    content = (
        H1("Organisatsioonid", cls="page-title"),
        Div(
            A(
                "Lisa uus organisatsioon",
                href="/admin/organizations/new",
                cls="btn btn-primary btn-md",
            ),
            cls="page-actions",
        ),
        Card(
            CardHeader(H3("Kõik organisatsioonid", cls="card-title")),
            CardBody(*card_body_children),
        ),
    )

    return PageShell(
        *content,
        title="Organisatsioonid",
        user=auth,
        theme=theme,
        active_nav="/admin",
    )


def _org_form_page(req: Request, org: dict | None = None):  # type: ignore[type-arg]
    """Render create/edit form for an organization."""
    auth = req.scope.get("auth")
    theme = get_theme_from_request(req)

    is_edit = org is not None
    title = "Muuda organisatsiooni" if is_edit else "Uus organisatsioon"
    action = f"/admin/organizations/{org['id']}" if is_edit else "/admin/organizations"
    name_val = org["name"] if is_edit else ""
    slug_val = org["slug"] if is_edit else ""

    form = Form(
        FormField(
            name="name",
            label="Nimi",
            type="text",
            required=True,
            value=name_val,
        ),
        FormField(
            name="slug",
            label="Lühitunnus (slug)",
            type="text",
            required=True,
            value=slug_val,
        ),
        Div(
            Button("Salvesta", type="submit", variant="primary"),
            A("Tühista", href="/admin/organizations", cls="btn btn-ghost btn-md"),
            cls="form-actions",
        ),
        method="post",
        action=action,
    )

    return PageShell(
        H1(title, cls="page-title"),
        Card(
            CardBody(form),
        ),
        title=title,
        user=auth,
        theme=theme,
        active_nav="/admin",
    )


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def org_list(req: Request):
    """GET /admin/organizations — list all organizations."""
    return _org_list_page(req, list_orgs())


def org_new_form(req: Request):
    """GET /admin/organizations/new — show create form."""
    return _org_form_page(req)


def org_create(req: Request, name: str, slug: str):
    """POST /admin/organizations — create a new organization."""
    slug = slug.strip() or slugify(name)
    org = create_org(name.strip(), slug)
    if org is None:
        return _error_page(
            req,
            "Organisatsiooni loomine ebaõnnestus. Nimi või lühitunnus võib olla juba kasutusel.",
        )
    auth = req.scope.get("auth", {})
    log_action(auth.get("id"), "org.create", {"org_id": org["id"], "name": name, "slug": slug})
    return RedirectResponse(url="/admin/organizations", status_code=303)


def org_edit_form(req: Request, org_id: str):
    """GET /admin/organizations/{org_id}/edit — show edit form."""
    org = get_org(org_id)
    if org is None:
        return _error_page(req, "Organisatsiooni ei leitud.")
    return _org_form_page(req, org)


def org_update(req: Request, org_id: str, name: str, slug: str):
    """POST /admin/organizations/{org_id} — update an organization."""
    slug = slug.strip() or slugify(name)
    org = update_org(org_id, name.strip(), slug)
    if org is None:
        return _error_page(req, "Organisatsiooni muutmine ebaõnnestus.")
    auth = req.scope.get("auth", {})
    log_action(auth.get("id"), "org.update", {"org_id": org_id, "name": name, "slug": slug})
    return RedirectResponse(url="/admin/organizations", status_code=303)


def org_delete(req: Request, org_id: str):
    """POST /admin/organizations/{org_id}/delete — delete an organization."""
    success = delete_org(org_id)
    if not success:
        return _error_page(
            req,
            "Organisatsiooni kustutamine ebaõnnestus. "
            "Veenduge, et organisatsioonil pole kasutajaid.",
        )
    auth = req.scope.get("auth", {})
    log_action(auth.get("id"), "org.delete", {"org_id": org_id})
    return RedirectResponse(url="/admin/organizations", status_code=303)


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

# Apply admin role decorator to all route handlers
_org_list = require_role("admin")(org_list)
_org_new_form = require_role("admin")(org_new_form)
_org_create = require_role("admin")(org_create)
_org_edit_form = require_role("admin")(org_edit_form)
_org_update = require_role("admin")(org_update)
_org_delete = require_role("admin")(org_delete)


def register_org_routes(rt) -> None:  # type: ignore[no-untyped-def]
    """Register organization admin routes on the FastHTML route decorator *rt*."""
    rt("/admin/organizations", methods=["GET"])(_org_list)
    rt("/admin/organizations/new", methods=["GET"])(_org_new_form)
    rt("/admin/organizations", methods=["POST"])(_org_create)
    rt("/admin/organizations/{org_id}/edit", methods=["GET"])(_org_edit_form)
    rt("/admin/organizations/{org_id}", methods=["POST"])(_org_update)
    rt("/admin/organizations/{org_id}/delete", methods=["POST"])(_org_delete)
