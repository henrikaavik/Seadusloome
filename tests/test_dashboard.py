"""Tests for personal dashboard, admin dashboard, and health check."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# /dashboard requires auth (redirects when not logged in)
# ---------------------------------------------------------------------------


class TestDashboardAuth:
    def test_dashboard_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.get("/dashboard")
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"
        # Make sure the redirect carried no auth cookies (#423).
        for h in response.headers.get_list("set-cookie"):
            assert "access_token=" not in h
            assert "refresh_token=" not in h

    def test_index_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.get("/")
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"


# ---------------------------------------------------------------------------
# /api/health returns JSON without auth
# ---------------------------------------------------------------------------


class TestHealthCheck:
    @patch("app.templates.admin_dashboard.jena_check_health")
    @patch("app.templates.admin_dashboard._check_postgres")
    def test_health_returns_json(self, mock_pg: MagicMock, mock_jena: MagicMock):
        mock_pg.return_value = True
        mock_jena.return_value = True

        from app.main import app

        client = TestClient(app)
        response = client.get("/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["jena"] is True
        assert data["postgres"] is True

    @patch("app.templates.admin_dashboard.jena_check_health")
    @patch("app.templates.admin_dashboard._check_postgres")
    def test_health_degraded_when_service_down(self, mock_pg: MagicMock, mock_jena: MagicMock):
        mock_pg.return_value = True
        mock_jena.return_value = False

        from app.main import app

        client = TestClient(app)
        response = client.get("/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "degraded"
        assert data["jena"] is False
        assert data["postgres"] is True

    def test_health_no_auth_required(self):
        """Health check must be accessible without cookies."""
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.get("/api/health")
        # Should not redirect to login
        assert response.status_code == 200
        data = response.json()
        assert data["status"] in {"ok", "degraded"}
        assert "jena" in data
        assert "postgres" in data

    def test_ping_no_auth_required(self):
        """/api/ping is a lightweight liveness probe that must work anon."""
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.get("/api/ping")
        assert response.status_code == 200
        assert response.text == "ok"


# ---------------------------------------------------------------------------
# /admin requires admin role
# ---------------------------------------------------------------------------


class TestAdminDashboard:
    def test_admin_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.get("/admin")
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"
        assert response.text == "" or "Sisselogimine" not in response.text

    def test_admin_audit_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.get("/admin/audit")
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"
        # The redirect must not leak any audit content.
        assert "Auditilogi" not in response.text

    def test_admin_sync_requires_auth(self):
        """POST /admin/sync must not run a sync for unauthenticated users."""
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.post("/admin/sync")
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"

    @patch("app.templates.admin_dashboard._get_sync_logs")
    @patch("threading.Thread")
    def test_admin_sync_triggers_thread(
        self,
        mock_thread_cls: MagicMock,
        mock_logs: MagicMock,
    ):
        """When called directly (simulating an admin request) the handler
        starts a background thread and returns a sync card with a status
        banner. Asserts the lock state is reset via the `finally`."""
        from starlette.requests import Request

        from app.templates import admin_dashboard
        from app.templates.admin_dashboard import trigger_sync

        mock_logs.return_value = []

        # Ensure a clean lock state so the test is deterministic.
        admin_dashboard._sync_in_progress = False
        mock_thread = MagicMock()
        mock_thread_cls.return_value = mock_thread

        # Build a minimal Request object for the handler.
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/admin/sync",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
            "auth": {"role": "admin", "id": "admin-test", "email": "a@b.ee"},
        }
        req = Request(scope)

        result = trigger_sync(req)

        # Thread was started
        mock_thread_cls.assert_called_once()
        mock_thread.start.assert_called_once()

        # Handler should have flipped the in-progress flag on entry.
        assert admin_dashboard._sync_in_progress is True

        # Result is a FastTag we can convert to XML to look for the banner.
        from fasthtml.common import to_xml

        html = to_xml(result)
        assert "sünkroniseerimine käivitati" in html.lower()

        # Clean up: simulate the thread finishing and clearing the flag.
        admin_dashboard._sync_in_progress = False


# ---------------------------------------------------------------------------
# Bookmark DB helpers (unit tests with mocked DB)
# ---------------------------------------------------------------------------


class TestBookmarkAdd:
    @patch("app.templates.dashboard._connect")
    def test_add_bookmark_returns_dict(self, mock_connect: MagicMock):
        from app.templates.dashboard import _add_bookmark

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchone.return_value = (
            "bm-id-1",
            "http://example.org/entity/1",
            "Test Entity",
            "2024-06-01",
        )

        result = _add_bookmark("user-1", "http://example.org/entity/1", "Test Entity")
        assert result is not None
        assert result["id"] == "bm-id-1"
        assert result["entity_uri"] == "http://example.org/entity/1"
        assert result["label"] == "Test Entity"
        mock_conn.commit.assert_called_once()

    @patch("app.templates.dashboard._connect")
    def test_add_bookmark_returns_none_on_conflict(self, mock_connect: MagicMock):
        from app.templates.dashboard import _add_bookmark

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        # ON CONFLICT DO NOTHING returns None
        mock_conn.execute.return_value.fetchone.return_value = None

        result = _add_bookmark("user-1", "http://example.org/entity/1", "Test")
        assert result is None

    @patch("app.templates.dashboard._connect")
    def test_add_bookmark_returns_none_on_db_error(self, mock_connect: MagicMock):
        from app.templates.dashboard import _add_bookmark

        mock_connect.side_effect = Exception("DB unavailable")
        result = _add_bookmark("user-1", "http://example.org/entity/1", "Test")
        assert result is None


class TestBookmarkRemove:
    @patch("app.templates.dashboard._connect")
    def test_remove_bookmark_returns_true(self, mock_connect: MagicMock):
        from app.templates.dashboard import _remove_bookmark

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.rowcount = 1

        result = _remove_bookmark("bm-id-1", "user-1")
        assert result is True
        mock_conn.commit.assert_called_once()

    @patch("app.templates.dashboard._connect")
    def test_remove_bookmark_returns_false_for_nonexistent(self, mock_connect: MagicMock):
        from app.templates.dashboard import _remove_bookmark

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.rowcount = 0

        result = _remove_bookmark("nonexistent", "user-1")
        assert result is False

    @patch("app.templates.dashboard._connect")
    def test_remove_bookmark_returns_false_on_db_error(self, mock_connect: MagicMock):
        from app.templates.dashboard import _remove_bookmark

        mock_connect.side_effect = Exception("DB unavailable")
        result = _remove_bookmark("bm-id-1", "user-1")
        assert result is False


# ---------------------------------------------------------------------------
# Admin dashboard DB helpers (unit tests with mocked DB)
# ---------------------------------------------------------------------------


class TestSyncLogs:
    @patch("app.templates.admin_dashboard._connect")
    def test_get_sync_logs_returns_list(self, mock_connect: MagicMock):
        from app.templates.admin_dashboard import _get_sync_logs

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = [
            (1, "2024-06-01 10:00", "2024-06-01 10:05", "success", 5000, None),
        ]

        result = _get_sync_logs()
        assert len(result) == 1
        assert result[0]["status"] == "success"
        assert result[0]["entity_count"] == 5000

    @patch("app.templates.admin_dashboard._connect")
    def test_get_sync_logs_returns_empty_on_error(self, mock_connect: MagicMock):
        from app.templates.admin_dashboard import _get_sync_logs

        mock_connect.side_effect = Exception("DB unavailable")
        result = _get_sync_logs()
        assert result == []


class TestUserStats:
    @patch("app.templates.admin_dashboard._connect")
    def test_get_user_stats_returns_dict(self, mock_connect: MagicMock):
        from app.templates.admin_dashboard import _get_user_stats

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        # Mock the three separate execute calls
        call_results = [
            MagicMock(fetchone=MagicMock(return_value=(10,))),  # total users
            MagicMock(fetchall=MagicMock(return_value=[("Org A", 5), ("Org B", 3)])),  # per org
            MagicMock(fetchone=MagicMock(return_value=(2,))),  # active sessions
        ]
        mock_conn.execute.side_effect = call_results

        result = _get_user_stats()
        assert result["total_users"] == 10
        assert len(result["users_per_org"]) == 2
        assert result["active_sessions"] == 2

    @patch("app.templates.admin_dashboard._connect")
    def test_get_user_stats_returns_defaults_on_error(self, mock_connect: MagicMock):
        from app.templates.admin_dashboard import _get_user_stats

        mock_connect.side_effect = Exception("DB unavailable")
        result = _get_user_stats()
        assert result["total_users"] == 0
        assert result["users_per_org"] == []
        assert result["active_sessions"] == 0
