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
    def test_health_returns_json(
        self, mock_pg: MagicMock, mock_jena: MagicMock
    ):
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
    def test_health_degraded_when_service_down(
        self, mock_pg: MagicMock, mock_jena: MagicMock
    ):
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

    def test_admin_audit_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.get("/admin/audit")
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"


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
