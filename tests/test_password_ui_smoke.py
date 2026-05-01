"""Regression: the password-flow UI entry points must remain wired.

Background: in 2026-04 the admin reset button and login forgot link were
silently dropped from main during a "UI review follow-ups" sweep. These
smoke tests fail loudly if anyone removes them again. They render the FT
builders directly (no DB, no TestClient, no auth fixture) so the only
thing they can fail for is a missing string in the rendered output —
which is exactly the regression we're guarding against.
"""

from __future__ import annotations

from fasthtml.common import to_xml

from app.auth.routes import _login_form
from app.auth.users import _user_table


def test_login_form_has_forgot_link():
    """`_login_form()` must render an anchor to /auth/forgot with the
    Estonian label "Unustasid parooli?". If this fails, restore the line
        P(A("Unustasid parooli?", href="/auth/forgot"), cls="forgot-link"),
    in `_login_form` (app/auth/routes.py) between the password field and
    the submit button.
    """
    html = to_xml(_login_form())
    assert 'href="/auth/forgot"' in html, "login form missing /auth/forgot anchor"
    assert "Unustasid parooli" in html, "login form missing Estonian forgot label"


def test_admin_user_table_renders_reset_button_for_active_admin_target():
    """`_user_table` with base_path='/admin/users' must render
    'Lähtesta parool' for an active user row. If this fails, restore the
    `actions.append(A("Lähtesta parool", ...))` block in `_user_table.
    _actions_cell` (app/auth/users.py)."""
    users = [
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "full_name": "Test Drafter",
            "email": "drafter@example.com",
            "org_name": "Acme",
            "role": "drafter",
            "is_active": True,
        }
    ]
    html = to_xml(_user_table(users, show_org=True, base_path="/admin/users"))
    assert "Lähtesta parool" in html, "admin user table missing reset action label"
    assert "/admin/users/00000000-0000-0000-0000-000000000001/reset" in html, (
        "admin user table missing reset action href"
    )


def test_org_user_table_renders_reset_button_for_assignable_role():
    """`_user_table` with base_path='/org/users' must show the reset
    button for an active drafter (a role in ORG_ASSIGNABLE_ROLES)."""
    users = [
        {
            "id": "00000000-0000-0000-0000-000000000002",
            "full_name": "Org Drafter",
            "email": "org-drafter@example.com",
            "org_name": "Acme",
            "role": "drafter",
            "is_active": True,
        }
    ]
    html = to_xml(_user_table(users, show_org=False, base_path="/org/users"))
    assert "Lähtesta parool" in html
    assert "/org/users/00000000-0000-0000-0000-000000000002/reset" in html


def test_org_user_table_omits_reset_button_for_admin_target():
    """Org admin must NOT be able to reset another admin / org_admin."""
    users = [
        {
            "id": "00000000-0000-0000-0000-000000000003",
            "full_name": "Other Org Admin",
            "email": "other-org-admin@example.com",
            "org_name": "Acme",
            "role": "org_admin",
            "is_active": True,
        }
    ]
    html = to_xml(_user_table(users, show_org=False, base_path="/org/users"))
    assert "Lähtesta parool" not in html, (
        "org admin must not see reset button for another org_admin row"
    )


def test_user_table_omits_reset_button_for_inactive_user():
    """Inactive users must not show the reset button (covered by
    `if row['_is_active'] and show_reset:` guard)."""
    users = [
        {
            "id": "00000000-0000-0000-0000-000000000004",
            "full_name": "Inactive Drafter",
            "email": "inactive@example.com",
            "org_name": "Acme",
            "role": "drafter",
            "is_active": False,
        }
    ]
    html = to_xml(_user_table(users, show_org=True, base_path="/admin/users"))
    assert "Lähtesta parool" not in html
