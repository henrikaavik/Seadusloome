"""Unit tests for app/auth/policy.py (issues #568, #569, #575).

The helpers must behave consistently across the three resource types so
routes can adopt the same pattern without each reinventing the rules.
"""

from __future__ import annotations

import types

import pytest

from app.auth.policy import (
    ROLE_DRAFTER,
    ROLE_ORG_ADMIN,
    ROLE_REVIEWER,
    ROLE_SYSTEM_ADMIN,
    can_access_conversation,
    can_access_drafter_session,
    can_delete_draft,
    can_view_draft,
    is_org_admin,
    is_system_admin,
)


def _resource(
    user_id: str | None = "owner-1", org_id: str | None = "org-a"
) -> types.SimpleNamespace:
    return types.SimpleNamespace(user_id=user_id, org_id=org_id)


def _auth(user_id: str = "owner-1", org_id: str = "org-a", role: str = ROLE_DRAFTER) -> dict:
    return {"id": user_id, "org_id": org_id, "role": role}


# ---------------------------------------------------------------------------
# Drafts: view is org-wide, delete is owner-only
# ---------------------------------------------------------------------------


class TestCanViewDraft:
    def test_owner_can_view(self):
        assert can_view_draft(_auth(), _resource()) is True

    def test_same_org_non_owner_can_view(self):
        assert can_view_draft(_auth(user_id="other"), _resource()) is True

    def test_other_org_cannot_view(self):
        assert can_view_draft(_auth(org_id="org-b"), _resource()) is False

    def test_system_admin_cross_org_can_view(self):
        assert can_view_draft(_auth(org_id="org-b", role=ROLE_SYSTEM_ADMIN), _resource()) is True

    @pytest.mark.parametrize("auth", [None, {}])
    def test_missing_auth_denied(self, auth):
        assert can_view_draft(auth, _resource()) is False


class TestCanDeleteDraft:
    def test_owner_can_delete(self):
        assert can_delete_draft(_auth(), _resource()) is True

    def test_same_org_non_owner_cannot_delete(self):
        """The bug fixed by #568: same-org colleagues must not delete."""
        assert can_delete_draft(_auth(user_id="other"), _resource()) is False

    def test_reviewer_cannot_delete(self):
        assert can_delete_draft(_auth(user_id="other", role=ROLE_REVIEWER), _resource()) is False

    def test_org_admin_cannot_delete_others(self):
        assert can_delete_draft(_auth(user_id="other", role=ROLE_ORG_ADMIN), _resource()) is False

    def test_other_org_drafter_cannot_delete(self):
        assert can_delete_draft(_auth(org_id="org-b"), _resource()) is False

    def test_system_admin_cross_org_can_delete(self):
        """Matrix grants system admin a cross-org delete override."""
        assert (
            can_delete_draft(
                _auth(user_id="admin", org_id="org-b", role=ROLE_SYSTEM_ADMIN), _resource()
            )
            is True
        )

    def test_none_resource_denied(self):
        assert can_delete_draft(_auth(), None) is False


# ---------------------------------------------------------------------------
# Chat conversations: owner-only, no admin override
# ---------------------------------------------------------------------------


class TestCanAccessConversation:
    def test_owner_allowed(self):
        assert can_access_conversation(_auth(), _resource()) is True

    def test_same_org_non_owner_denied(self):
        """Bug from #569: same-org users must not access another's chat."""
        assert can_access_conversation(_auth(user_id="other"), _resource()) is False

    def test_other_org_non_owner_denied(self):
        # Different org, different user — both reasons to deny.
        assert (
            can_access_conversation(_auth(user_id="other", org_id="org-b"), _resource()) is False
        )

    def test_system_admin_denied(self):
        """System admin gets no backdoor into chat transcripts."""
        assert (
            can_access_conversation(_auth(user_id="admin", role=ROLE_SYSTEM_ADMIN), _resource())
            is False
        )

    @pytest.mark.parametrize("auth,resource", [(None, _resource()), (_auth(), None)])
    def test_none_inputs_denied(self, auth, resource):
        assert can_access_conversation(auth, resource) is False


# ---------------------------------------------------------------------------
# Drafter sessions: same policy as conversations
# ---------------------------------------------------------------------------


class TestCanAccessDrafterSession:
    def test_owner_allowed(self):
        assert can_access_drafter_session(_auth(), _resource()) is True

    def test_same_org_non_owner_denied(self):
        assert can_access_drafter_session(_auth(user_id="other"), _resource()) is False

    def test_system_admin_denied(self):
        assert (
            can_access_drafter_session(_auth(user_id="admin", role=ROLE_SYSTEM_ADMIN), _resource())
            is False
        )


# ---------------------------------------------------------------------------
# Role shortcuts
# ---------------------------------------------------------------------------


class TestRoleShortcuts:
    def test_is_system_admin(self):
        assert is_system_admin({"role": ROLE_SYSTEM_ADMIN}) is True
        assert is_system_admin({"role": ROLE_ORG_ADMIN}) is False
        assert is_system_admin(None) is False

    def test_is_org_admin(self):
        assert is_org_admin({"role": ROLE_ORG_ADMIN}) is True
        assert is_org_admin({"role": ROLE_SYSTEM_ADMIN}) is False
