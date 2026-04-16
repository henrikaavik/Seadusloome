"""Tests for org-admin guards on same-org admin / org_admin targets (#634).

Pre-fix, ``org_user_role_update``, ``org_user_deactivate`` and
``org_user_role_form`` only validated the **new** role against
``ORG_ASSIGNABLE_ROLES`` — they never inspected the target user's
**current** role. An org_admin could therefore demote or deactivate
another org_admin (or even the seeded system admin, which has an org_id
per ``migrations/004_admin_seed_fix.sql``). Self-deactivation was also
unguarded.

These tests exercise the three handlers by stubbing ``get_user`` so no
DB is required and checking for the refusal path.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from starlette.responses import RedirectResponse

from app.auth.users import (
    org_user_deactivate,
    org_user_role_form,
    org_user_role_update,
)

_ORG_ID = "22222222-2222-2222-2222-222222222222"
_ORG_ADMIN_ID = "11111111-1111-1111-1111-111111111111"
_TARGET_ID = "33333333-3333-3333-3333-333333333333"


def _make_request(auth: dict[str, Any] | None = None) -> MagicMock:
    req = MagicMock()
    # PageShell (used by _error_page) reads ``full_name`` off the auth dict
    # to render the user menu — always include it.
    default = {
        "id": _ORG_ADMIN_ID,
        "role": "org_admin",
        "org_id": _ORG_ID,
        "email": "admin@org.ee",
        "full_name": "Org Admin",
    }
    req.scope = {"auth": auth or default}
    # Mimic Starlette's Request.cookies (used by the theme helper).
    req.cookies = {}
    return req


def _target(*, role: str, user_id: str = _TARGET_ID, org_id: str = _ORG_ID) -> dict:  # type: ignore[type-arg]
    return {
        "id": user_id,
        "email": "t@t.ee",
        "full_name": "T",
        "role": role,
        "org_id": org_id,
        "is_active": True,
        "org_name": "org",
    }


# ---------------------------------------------------------------------------
# org_user_role_update — must refuse protected targets
# ---------------------------------------------------------------------------


class TestOrgUserRoleUpdateRefusesProtectedTargets:
    @patch("app.auth.users.update_user_role")
    @patch("app.auth.users.get_user")
    def test_refuses_to_update_org_admin_target(
        self, mock_get_user: MagicMock, mock_update: MagicMock
    ):
        mock_get_user.return_value = _target(role="org_admin")

        req = _make_request()
        result = org_user_role_update(req, _TARGET_ID, "drafter")

        # Handler must NOT call update_user_role.
        mock_update.assert_not_called()
        # Must render an error page (not a 303 redirect to /org/users).
        assert not isinstance(result, RedirectResponse), (
            "org_admin target must not be updated — handler redirected as if it succeeded"
        )

    @patch("app.auth.users.update_user_role")
    @patch("app.auth.users.get_user")
    def test_refuses_to_update_admin_target(
        self, mock_get_user: MagicMock, mock_update: MagicMock
    ):
        mock_get_user.return_value = _target(role="admin")

        req = _make_request()
        result = org_user_role_update(req, _TARGET_ID, "drafter")

        mock_update.assert_not_called()
        assert not isinstance(result, RedirectResponse), (
            "admin target must not be updated — handler redirected as if it succeeded"
        )

    @patch("app.auth.users.log_action")
    @patch("app.auth.users.update_user_role")
    @patch("app.auth.users.get_user")
    def test_allows_updating_drafter_target(
        self,
        mock_get_user: MagicMock,
        mock_update: MagicMock,
        mock_log: MagicMock,
    ):
        """Existing allowed path must still work — drafter -> reviewer."""
        mock_get_user.return_value = _target(role="drafter")
        mock_update.return_value = True

        req = _make_request()
        result = org_user_role_update(req, _TARGET_ID, "reviewer")

        mock_update.assert_called_once_with(_TARGET_ID, "reviewer")
        assert isinstance(result, RedirectResponse)


# ---------------------------------------------------------------------------
# org_user_deactivate — must refuse protected targets and self
# ---------------------------------------------------------------------------


class TestOrgUserDeactivateGuards:
    @patch("app.auth.users.deactivate_user")
    @patch("app.auth.users.get_user")
    def test_refuses_to_deactivate_org_admin_target(
        self, mock_get_user: MagicMock, mock_deactivate: MagicMock
    ):
        mock_get_user.return_value = _target(role="org_admin")

        req = _make_request()
        result = org_user_deactivate(req, _TARGET_ID)

        mock_deactivate.assert_not_called()
        assert not isinstance(result, RedirectResponse), (
            "org_admin target must not be deactivated — handler redirected as if it succeeded"
        )

    @patch("app.auth.users.deactivate_user")
    @patch("app.auth.users.get_user")
    def test_refuses_to_deactivate_admin_target(
        self, mock_get_user: MagicMock, mock_deactivate: MagicMock
    ):
        mock_get_user.return_value = _target(role="admin")

        req = _make_request()
        result = org_user_deactivate(req, _TARGET_ID)

        mock_deactivate.assert_not_called()
        assert not isinstance(result, RedirectResponse), (
            "admin target must not be deactivated — handler redirected as if it succeeded"
        )

    @patch("app.auth.users.deactivate_user")
    @patch("app.auth.users.get_user")
    def test_refuses_self_deactivation(self, mock_get_user: MagicMock, mock_deactivate: MagicMock):
        """Org admin tries to deactivate themselves."""
        # The get_user for caller returns an org_admin in their own org.
        mock_get_user.return_value = _target(
            role="org_admin",
            user_id=_ORG_ADMIN_ID,
        )

        req = _make_request()
        result = org_user_deactivate(req, _ORG_ADMIN_ID)

        mock_deactivate.assert_not_called()
        assert not isinstance(result, RedirectResponse), (
            "Self-deactivation must not be allowed — handler redirected as if it succeeded"
        )

    @patch("app.auth.users.log_action")
    @patch("app.auth.users.deactivate_user")
    @patch("app.auth.users.get_user")
    def test_allows_deactivating_drafter_target(
        self,
        mock_get_user: MagicMock,
        mock_deactivate: MagicMock,
        mock_log: MagicMock,
    ):
        mock_get_user.return_value = _target(role="drafter")
        mock_deactivate.return_value = True

        req = _make_request()
        result = org_user_deactivate(req, _TARGET_ID)

        mock_deactivate.assert_called_once_with(_TARGET_ID)
        assert isinstance(result, RedirectResponse)


# ---------------------------------------------------------------------------
# org_user_role_form — must NOT render the role-picker for protected targets
# ---------------------------------------------------------------------------


class TestOrgUserRoleFormProtectedTargets:
    @patch("app.auth.users.get_user")
    def test_protected_target_rendered_without_select_action(self, mock_get_user: MagicMock):
        """A protected target must NOT receive a form with role select +
        Salvesta button. The page should render an 'ei saa muuta' state."""
        mock_get_user.return_value = _target(role="org_admin")

        req = _make_request()
        result = org_user_role_form(req, _TARGET_ID)

        # Convert the FT result to HTML text
        from fasthtml.common import to_xml

        html = to_xml(result) if result is not None else ""

        # The Estonian refusal message must be present.
        assert "ei saa muuta" in html.lower() or "keelatud" in html.lower(), (
            "Form must show a 'ei saa muuta' / 'keelatud' state for protected targets"
        )
        # The POST action to actually update the role must NOT be present
        # (or the form must be rendered read-only / disabled).
        assert f'action="/org/users/{_TARGET_ID}/role"' not in html, (
            "Protected-target form must not expose an active POST action"
        )
