"""Unit tests for the role-based access control decorators."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.testclient import TestClient


def _make_request(
    auth: dict | None = None,  # type: ignore[type-arg]
    path: str = "/test",
    path_params: dict | None = None,  # type: ignore[type-arg]
) -> Request:
    """Create a minimal Starlette Request with optional auth scope."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": b"",
        "headers": [],
        "root_path": "",
        "path_params": path_params or {},
    }
    if auth is not None:
        scope["auth"] = auth
    return Request(scope)


# ---------------------------------------------------------------------------
# require_role
# ---------------------------------------------------------------------------


class TestRequireRole:
    def test_allows_matching_role(self):
        from app.auth.roles import require_role

        @require_role("admin")
        def handler(req: Request):
            return "ok"

        req = _make_request(auth={"id": "u1", "role": "admin", "org_id": None})
        result = handler(req)
        assert result == "ok"

    def test_allows_any_of_multiple_roles(self):
        from app.auth.roles import require_role

        @require_role("admin", "org_admin")
        def handler(req: Request):
            return "ok"

        req = _make_request(auth={"id": "u1", "role": "org_admin", "org_id": "o1"})
        result = handler(req)
        assert result == "ok"

    def test_rejects_wrong_role(self):
        from app.auth.roles import require_role

        @require_role("admin")
        def handler(req: Request):
            return "ok"

        req = _make_request(auth={"id": "u1", "role": "drafter", "org_id": None})
        result = handler(req)
        # #739: a role denial returns an HTMLResponse with status 403 —
        # not a bare FT element (which FastHTML would serve as 200 OK).
        assert isinstance(result, HTMLResponse)
        assert result.status_code == 403
        assert b"Ligip\xc3\xa4\xc3\xa4s keelatud" in result.body

    def test_redirects_unauthenticated(self):
        from app.auth.roles import require_role

        @require_role("admin")
        def handler(req: Request):
            return "ok"

        # No auth in scope
        req = _make_request(auth=None)
        result = handler(req)
        assert isinstance(result, RedirectResponse)
        assert result.headers.get("location") == "/auth/login"

    def test_redirects_when_no_request(self):
        from app.auth.roles import require_role

        @require_role("admin")
        def handler(value: str):
            return value

        # No Request argument at all
        result = handler("test")
        assert isinstance(result, RedirectResponse)


# ---------------------------------------------------------------------------
# require_org_member
# ---------------------------------------------------------------------------


class TestRequireOrgMember:
    def test_allows_matching_org(self):
        from app.auth.roles import require_org_member

        @require_org_member("org_id")
        def handler(req: Request, org_id: str):
            return "ok"

        req = _make_request(
            auth={"id": "u1", "role": "drafter", "org_id": "org-123"},
            path_params={"org_id": "org-123"},
        )
        result = handler(req, org_id="org-123")
        assert result == "ok"

    def test_admin_bypasses_org_check(self):
        from app.auth.roles import require_org_member

        @require_org_member("org_id")
        def handler(req: Request, org_id: str):
            return "ok"

        req = _make_request(
            auth={"id": "u1", "role": "admin", "org_id": None},
            path_params={"org_id": "org-999"},
        )
        result = handler(req, org_id="org-999")
        assert result == "ok"

    def test_rejects_wrong_org(self):
        from app.auth.roles import require_org_member

        @require_org_member("org_id")
        def handler(req: Request, org_id: str):
            return "ok"

        req = _make_request(
            auth={"id": "u1", "role": "drafter", "org_id": "org-111"},
            path_params={"org_id": "org-222"},
        )
        result = handler(req, org_id="org-222")
        # #739: an org-membership denial returns an HTMLResponse with
        # status 403, not a bare FT element served as 200 OK.
        assert isinstance(result, HTMLResponse)
        assert result.status_code == 403
        assert b"Ligip\xc3\xa4\xc3\xa4s keelatud" in result.body

    def test_redirects_unauthenticated(self):
        from app.auth.roles import require_org_member

        @require_org_member("org_id")
        def handler(req: Request, org_id: str):
            return "ok"

        req = _make_request(auth=None, path_params={"org_id": "org-123"})
        result = handler(req, org_id="org-123")
        assert isinstance(result, RedirectResponse)


# ---------------------------------------------------------------------------
# Integration: require_role via TestClient against actual app routes
# ---------------------------------------------------------------------------


class TestRoleViaApp:
    """Verify the admin-route role gate over the real ``app.main.app``."""

    @staticmethod
    def _stub_provider(role: str) -> MagicMock:
        provider = MagicMock()
        provider.get_current_user.return_value = {
            "id": "33333333-3333-3333-3333-333333333333",
            "email": "u@seadusloome.ee",
            "full_name": "Test User",
            "role": role,
            "org_id": "11111111-1111-1111-1111-111111111111",
        }
        return provider

    @staticmethod
    def _authed_client() -> TestClient:
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        client.cookies.set("access_token", "stub-token")
        return client

    def test_admin_orgs_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.get("/admin/organizations")
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"

    def test_admin_users_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.get("/admin/users")
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"

    @patch("app.auth.middleware._get_provider")
    def test_admin_orgs_role_denied_returns_403(self, mock_get_provider: MagicMock):
        """#739: an authenticated non-admin hitting an ``@require_role('admin')``
        route gets HTTP 403 — not a 200 OK page that could be cached or
        mistaken for success."""
        mock_get_provider.return_value = self._stub_provider("drafter")
        client = self._authed_client()
        response = client.get("/admin/organizations")
        assert response.status_code == 403
        assert response.headers["content-type"].startswith("text/html")
        assert "Ligipääs keelatud" in response.text

    @patch("app.auth.middleware._get_provider")
    def test_admin_users_role_denied_returns_403(self, mock_get_provider: MagicMock):
        """Same gate, second admin route — a non-admin gets 403 (#739)."""
        mock_get_provider.return_value = self._stub_provider("reviewer")
        client = self._authed_client()
        response = client.get("/admin/users")
        assert response.status_code == 403
        assert "Ligipääs keelatud" in response.text
