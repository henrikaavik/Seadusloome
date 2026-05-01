"""Unit tests for ``app.docs._helpers.resolve_draft`` (#624).

Each branch (auth fail / bad UUID / draft missing / authz fail / success)
is exercised independently with mocked dependencies. These are pure
unit tests — no DB, no FastHTML routing, no auth middleware.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock, patch

from starlette.responses import RedirectResponse

from app.docs._helpers import ResolvedDraft, resolve_draft


def _make_request(auth_scope: dict[str, Any] | None = None) -> Any:
    """Build a minimal request stub that ``require_auth`` and
    ``_not_found_page`` both accept."""
    req = MagicMock()
    req.scope = {"auth": auth_scope}
    req.headers = {}
    return req


def _make_draft(*, org_id: str = "org-1") -> Any:
    draft = MagicMock()
    draft.id = uuid.uuid4()
    draft.org_id = org_id
    return draft


class TestResolveDraftAuth:
    def test_returns_redirect_when_unauthenticated(self):
        """If require_auth returns a Response, resolve_draft passes it
        through verbatim — never tries to load the draft."""
        redirect = RedirectResponse(url="/auth/login", status_code=303)
        req = _make_request(auth_scope=None)

        with (
            patch("app.docs._helpers.require_auth", return_value=redirect),
            patch("app.docs._helpers._dm.fetch_draft") as mock_fetch,
        ):
            result = resolve_draft(req, str(uuid.uuid4()))

        assert result is redirect
        mock_fetch.assert_not_called()


class TestResolveDraftParse:
    def test_returns_404_for_non_uuid_path_param(self):
        """Bad UUID strings short-circuit to _not_found_page."""
        auth = {"id": "user-1", "org_id": "org-1", "full_name": "Test User", "role": "drafter"}
        req = _make_request(auth_scope=auth)

        with (
            patch("app.docs._helpers.require_auth", return_value=auth),
            patch("app.docs._helpers._dm.fetch_draft") as mock_fetch,
        ):
            result = resolve_draft(req, "not-a-uuid")

        # Not a ResolvedDraft → caller returns it as a 404 page.
        assert not isinstance(result, ResolvedDraft)
        mock_fetch.assert_not_called()


class TestResolveDraftLoad:
    def test_returns_404_when_draft_missing(self):
        """A valid UUID with no matching draft returns the 404 page."""
        auth = {"id": "user-1", "org_id": "org-1", "full_name": "Test User", "role": "drafter"}
        req = _make_request(auth_scope=auth)

        with (
            patch("app.docs._helpers.require_auth", return_value=auth),
            patch("app.docs._helpers._dm.fetch_draft", return_value=None),
        ):
            result = resolve_draft(req, str(uuid.uuid4()))

        assert not isinstance(result, ResolvedDraft)


class TestResolveDraftAuthz:
    def test_returns_404_when_view_authz_denies(self):
        """Cross-org access returns the 404 page (not 403) so we don't
        leak existence of out-of-scope drafts."""
        auth = {"id": "user-1", "org_id": "org-1", "full_name": "Test User", "role": "drafter"}
        draft = _make_draft(org_id="org-2")  # different org
        req = _make_request(auth_scope=auth)

        with (
            patch("app.docs._helpers.require_auth", return_value=auth),
            patch("app.docs._helpers._dm.fetch_draft", return_value=draft),
            patch("app.docs._helpers.can_view_draft", return_value=False),
        ):
            result = resolve_draft(req, str(draft.id))

        assert not isinstance(result, ResolvedDraft)

    def test_uses_can_edit_for_action_edit(self):
        """``action='edit'`` switches the gate from can_view to can_edit."""
        auth = {"id": "user-1", "org_id": "org-1", "full_name": "Test User", "role": "drafter"}
        draft = _make_draft()
        req = _make_request(auth_scope=auth)

        with (
            patch("app.docs._helpers.require_auth", return_value=auth),
            patch("app.docs._helpers._dm.fetch_draft", return_value=draft),
            patch("app.docs._helpers.can_view_draft", return_value=True) as mock_view,
            patch("app.docs._helpers.can_edit_draft", return_value=True) as mock_edit,
        ):
            result = resolve_draft(req, str(draft.id), action="edit")

        assert isinstance(result, ResolvedDraft)
        mock_edit.assert_called_once_with(auth, draft)
        mock_view.assert_not_called()


class TestResolveDraftSuccess:
    def test_returns_resolved_draft_on_success(self):
        """All checks passing returns a ResolvedDraft with both fields."""
        auth = {"id": "user-1", "org_id": "org-1", "full_name": "Test User", "role": "drafter"}
        draft = _make_draft()
        req = _make_request(auth_scope=auth)

        with (
            patch("app.docs._helpers.require_auth", return_value=auth),
            patch("app.docs._helpers._dm.fetch_draft", return_value=draft),
            patch("app.docs._helpers.can_view_draft", return_value=True),
        ):
            result = resolve_draft(req, str(draft.id))

        assert isinstance(result, ResolvedDraft)
        assert result.draft is draft
        assert result.auth is auth

    def test_resolved_draft_supports_attribute_and_tuple_unpack(self):
        """ResolvedDraft is a NamedTuple — both forms must work."""
        auth = {"id": "user-1", "org_id": "org-1", "full_name": "Test User", "role": "drafter"}
        draft = _make_draft()
        req = _make_request(auth_scope=auth)

        with (
            patch("app.docs._helpers.require_auth", return_value=auth),
            patch("app.docs._helpers._dm.fetch_draft", return_value=draft),
            patch("app.docs._helpers.can_view_draft", return_value=True),
        ):
            result = resolve_draft(req, str(draft.id))

        assert isinstance(result, ResolvedDraft)
        # Attribute access
        assert result.draft is draft
        # Tuple unpack — important because callers may use either form.
        unpacked_draft, unpacked_auth = result
        assert unpacked_draft is draft
        assert unpacked_auth is auth
