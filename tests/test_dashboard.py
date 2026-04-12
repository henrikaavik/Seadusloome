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

    @patch("app.templates.admin_dashboard._insert_running_row", return_value=99)
    @patch("app.templates.admin_dashboard._get_sync_logs")
    @patch("threading.Thread")
    def test_admin_sync_triggers_thread(
        self,
        mock_thread_cls: MagicMock,
        mock_logs: MagicMock,
        mock_insert: MagicMock,
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
            (1, "2024-06-01 10:00", "2024-06-01 10:05", "success", 5000, None, None),
        ]

        result = _get_sync_logs()
        assert len(result) == 1
        assert result[0]["status"] == "success"
        assert result[0]["entity_count"] == 5000
        # Migration 013 adds current_step — helper must surface it.
        assert "current_step" in result[0]

    @patch("app.templates.admin_dashboard._connect")
    def test_get_sync_logs_returns_empty_on_error(self, mock_connect: MagicMock):
        from app.templates.admin_dashboard import _get_sync_logs

        mock_connect.side_effect = Exception("DB unavailable")
        result = _get_sync_logs()
        assert result == []


# ---------------------------------------------------------------------------
# Live-progress sync card (issue #567)
# ---------------------------------------------------------------------------


class TestSyncCardLiveProgress:
    """The admin sync card must auto-poll while a sync is running and stop
    polling when it reaches a terminal state."""

    def _running_log(self, step: str = "converting") -> dict:
        """Build a fake sync_log row representing a sync in flight."""
        from datetime import UTC, datetime

        return {
            "id": 1,
            "started_at": datetime.now(UTC),
            "finished_at": None,
            "status": "running",
            "entity_count": None,
            "error_message": None,
            "current_step": step,
        }

    def _success_log(self) -> dict:
        from datetime import UTC, datetime

        return {
            "id": 2,
            "started_at": datetime.now(UTC),
            "finished_at": datetime.now(UTC),
            "status": "success",
            "entity_count": 5_000_000,
            "error_message": None,
            "current_step": None,
        }

    def test_card_polls_while_running(self):
        """A running row triggers HTMX polling on the card element."""
        from fasthtml.common import to_xml

        from app.admin.sync import _sync_card

        html = to_xml(_sync_card([self._running_log()]))
        assert "/admin/sync/status" in html
        assert "every 3s" in html
        # Progress pills render with Estonian phase labels
        assert "Konverteerimine" in html

    def test_card_stops_polling_on_terminal_state(self):
        """A success/failed row must NOT emit polling attributes."""
        from fasthtml.common import to_xml

        from app.admin.sync import _sync_card

        html = to_xml(_sync_card([self._success_log()]))
        assert "/admin/sync/status" not in html
        assert "every 3s" not in html

    def test_progress_pills_mark_completed_phases_done(self):
        """Pills before the current step render with the 'done' state."""
        from fasthtml.common import to_xml

        from app.admin.sync import _sync_card

        html = to_xml(_sync_card([self._running_log(step="uploading")]))
        # Cloning + converting + validating completed before uploading,
        # so their pills should carry the -done modifier class.
        assert "sync-progress-pill-done" in html
        # Uploading is active
        assert "sync-progress-pill-active" in html
        # Reingesting is still pending
        assert "sync-progress-pill-pending" in html

    @patch("app.templates.admin_dashboard._get_sync_logs")
    def test_sync_status_endpoint_renders_card(self, mock_logs: MagicMock):
        """GET /admin/sync/status returns the same card fragment the
        polling loop expects to swap in-place."""
        from starlette.requests import Request

        from app.templates.admin_dashboard import sync_status_card

        mock_logs.return_value = [self._running_log()]

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/admin/sync/status",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
            "auth": {"role": "admin", "id": "admin-test", "email": "a@b.ee"},
        }
        req = Request(scope)

        result = sync_status_card(req)

        from fasthtml.common import to_xml

        html = to_xml(result)
        assert 'id="sync-card"' in html
        assert "every 3s" in html

    @patch("app.templates.admin_dashboard._insert_running_row", return_value=99)
    @patch("app.templates.admin_dashboard._get_sync_logs")
    def test_post_sync_inserts_running_row_before_thread(
        self,
        mock_logs: MagicMock,
        mock_insert: MagicMock,
    ):
        """The running row must be inserted synchronously before the
        background thread starts. Otherwise the rendered card misses the
        progress panel + polling trigger because the worker's own INSERT
        hasn't landed when the main thread queries sync_log."""
        from starlette.requests import Request

        from app.templates import admin_dashboard
        from app.templates.admin_dashboard import trigger_sync

        admin_dashboard._sync_in_progress = False
        # Simulate the worker landing the INSERT: the log helper returns
        # the row we pretend was just inserted.
        mock_logs.return_value = [
            {
                "id": 99,
                "started_at": __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ),
                "finished_at": None,
                "status": "running",
                "entity_count": None,
                "error_message": None,
                "current_step": "cloning",
            }
        ]

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

        with patch("threading.Thread") as mock_thread_cls:
            mock_thread_cls.return_value = MagicMock()
            result = trigger_sync(req)
            # INSERT must have run before the Thread constructor.
            mock_insert.assert_called_once()
            mock_thread_cls.assert_called_once()
            # Thread kwargs must carry the pre-allocated log_id so the
            # orchestrator reuses the row instead of inserting again.
            _, call_kwargs = mock_thread_cls.call_args
            assert call_kwargs["kwargs"]["log_id"] == 99

        from fasthtml.common import to_xml

        html = to_xml(result)
        # With a running row already in sync_log, the rendered card must
        # carry HTMX polling attributes so the UI actually updates.
        assert "/admin/sync/status" in html
        assert "every 3s" in html

        admin_dashboard._sync_in_progress = False

    @patch("app.templates.admin_dashboard._get_sync_logs")
    @patch("app.templates.admin_dashboard.has_recent_running_row", return_value=True)
    def test_post_sync_detects_running_via_db(
        self,
        mock_has_running: MagicMock,
        mock_logs: MagicMock,
    ):
        """If the DB already shows a running sync, trigger_sync must NOT
        spawn a second thread and must show the 'already running' banner."""
        from starlette.requests import Request

        from app.templates import admin_dashboard
        from app.templates.admin_dashboard import trigger_sync

        mock_logs.return_value = [self._running_log()]
        admin_dashboard._sync_in_progress = False

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

        with patch("threading.Thread") as mock_thread_cls:
            trigger_sync(req)
            mock_thread_cls.assert_not_called()

        # In-memory flag must be released again so a real subsequent sync
        # (after the other one finishes) can proceed.
        assert admin_dashboard._sync_in_progress is False


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
