"""Unit tests for the role-based access control decorators."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import RedirectResponse
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
        # Should return a Titled FT object (not a string), containing "Ligipääs keelatud"
        assert result is not None
        assert result != "ok"

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
        assert result != "ok"

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
    """Verify that admin routes in the app redirect unauthenticated users."""

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
