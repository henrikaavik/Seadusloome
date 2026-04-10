"""Tests for the admin job monitor page (#543).

Covers: status counts, type breakdown, recent failed jobs, retry action,
purge action, page rendering, and route-level auth checks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from starlette.requests import Request
from starlette.testclient import TestClient


def _make_request(path: str = "/admin/jobs", method: str = "GET") -> Request:
    """Build a minimal Starlette Request for unit testing."""
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
        "auth": {"role": "admin", "id": "admin-1", "email": "a@b.ee", "full_name": "Admin User"},
        "path_params": {},
    }
    return Request(scope)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


class TestGetStatusCounts:
    @patch("app.admin.job_monitor._connect")
    def test_returns_counts(self, mock_connect: MagicMock):
        from app.admin.job_monitor import _get_status_counts

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = [
            ("pending", 5),
            ("running", 2),
            ("failed", 3),
            ("success", 100),
        ]

        counts = _get_status_counts()
        assert counts["pending"] == 5
        assert counts["running"] == 2
        assert counts["failed"] == 3
        assert counts["success"] == 100

    @patch("app.admin.job_monitor._connect")
    def test_returns_defaults_on_error(self, mock_connect: MagicMock):
        from app.admin.job_monitor import _get_status_counts

        mock_connect.side_effect = Exception("DB down")
        counts = _get_status_counts()
        assert counts == {"pending": 0, "running": 0, "failed": 0, "success": 0}


class TestGetTypeBreakdown:
    @patch("app.admin.job_monitor._connect")
    def test_returns_pivot(self, mock_connect: MagicMock):
        from app.admin.job_monitor import _get_type_breakdown

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = [
            ("parse_draft", "pending", 3),
            ("parse_draft", "success", 10),
            ("extract_entities", "failed", 2),
            ("extract_entities", "running", 1),
        ]

        breakdown = _get_type_breakdown()
        assert len(breakdown) == 2

        parse = next(b for b in breakdown if b["job_type"] == "parse_draft")
        assert parse["pending"] == 3
        assert parse["success"] == 10
        assert parse["failed"] == 0  # not in query results -> 0

        extract = next(b for b in breakdown if b["job_type"] == "extract_entities")
        assert extract["failed"] == 2
        assert extract["running"] == 1

    @patch("app.admin.job_monitor._connect")
    def test_returns_empty_on_error(self, mock_connect: MagicMock):
        from app.admin.job_monitor import _get_type_breakdown

        mock_connect.side_effect = Exception("DB down")
        breakdown = _get_type_breakdown()
        assert breakdown == []


class TestGetRecentFailed:
    @patch("app.admin.job_monitor._connect")
    def test_returns_failed_jobs(self, mock_connect: MagicMock):
        from app.admin.job_monitor import _get_recent_failed

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        now = datetime(2025, 3, 15, 10, 30, tzinfo=UTC)
        mock_conn.execute.return_value.fetchall.return_value = [
            (42, "parse_draft", "Connection timeout", 3, 3, now, now),
        ]

        jobs = _get_recent_failed()
        assert len(jobs) == 1
        assert jobs[0]["id"] == 42
        assert jobs[0]["job_type"] == "parse_draft"
        assert jobs[0]["error_message"] == "Connection timeout"

    @patch("app.admin.job_monitor._connect")
    def test_returns_empty_on_error(self, mock_connect: MagicMock):
        from app.admin.job_monitor import _get_recent_failed

        mock_connect.side_effect = Exception("DB down")
        jobs = _get_recent_failed()
        assert jobs == []


# ---------------------------------------------------------------------------
# Retry action
# ---------------------------------------------------------------------------


class TestRetryJob:
    @patch("app.admin.job_monitor._connect")
    def test_retry_resets_to_pending(self, mock_connect: MagicMock):
        from app.admin.job_monitor import _retry_job

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.rowcount = 1

        result = _retry_job(42)
        assert result is True

        sql = mock_conn.execute.call_args[0][0]
        assert "SET status = 'pending'" in sql
        assert "WHERE id = %s AND status = 'failed'" in sql
        mock_conn.commit.assert_called_once()

    @patch("app.admin.job_monitor._connect")
    def test_retry_returns_false_for_not_found(self, mock_connect: MagicMock):
        from app.admin.job_monitor import _retry_job

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.rowcount = 0

        result = _retry_job(999)
        assert result is False

    @patch("app.admin.job_monitor._connect")
    def test_retry_returns_false_on_error(self, mock_connect: MagicMock):
        from app.admin.job_monitor import _retry_job

        mock_connect.side_effect = Exception("DB down")
        result = _retry_job(42)
        assert result is False


# ---------------------------------------------------------------------------
# Purge action
# ---------------------------------------------------------------------------


class TestPurgeCompleted:
    @patch("app.admin.job_monitor._connect")
    def test_purge_deletes_old_success(self, mock_connect: MagicMock):
        from app.admin.job_monitor import _purge_completed

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.rowcount = 15

        deleted = _purge_completed(days=7)
        assert deleted == 15

        sql = mock_conn.execute.call_args[0][0]
        assert "DELETE FROM background_jobs" in sql
        assert "status = 'success'" in sql
        assert "finished_at < %s" in sql
        mock_conn.commit.assert_called_once()

    @patch("app.admin.job_monitor._connect")
    def test_purge_returns_zero_on_error(self, mock_connect: MagicMock):
        from app.admin.job_monitor import _purge_completed

        mock_connect.side_effect = Exception("DB down")
        deleted = _purge_completed()
        assert deleted == 0


# ---------------------------------------------------------------------------
# Page rendering
# ---------------------------------------------------------------------------


class TestJobMonitorPageRendering:
    @patch("app.admin.job_monitor._get_recent_failed")
    @patch("app.admin.job_monitor._get_type_breakdown")
    @patch("app.admin.job_monitor._get_status_counts")
    def test_page_renders_summary_badges(
        self,
        mock_counts: MagicMock,
        mock_breakdown: MagicMock,
        mock_failed: MagicMock,
    ):
        from fasthtml.common import to_xml

        from app.admin.job_monitor import admin_jobs_page

        mock_counts.return_value = {"pending": 5, "running": 2, "failed": 3, "success": 100}
        mock_breakdown.return_value = []
        mock_failed.return_value = []

        req = _make_request()
        result = admin_jobs_page(req)

        html = to_xml(result)
        assert "5 ootel" in html
        assert "2 t\u00f6\u00f6tab" in html
        assert "3 eba\u00f5nnestunud" in html
        assert "100 \u00f5nnestunud" in html
        # Page title
        assert "T\u00f6\u00f6de monitor" in html

    @patch("app.admin.job_monitor._get_recent_failed")
    @patch("app.admin.job_monitor._get_type_breakdown")
    @patch("app.admin.job_monitor._get_status_counts")
    def test_page_renders_type_breakdown(
        self,
        mock_counts: MagicMock,
        mock_breakdown: MagicMock,
        mock_failed: MagicMock,
    ):
        from fasthtml.common import to_xml

        from app.admin.job_monitor import admin_jobs_page

        mock_counts.return_value = {"pending": 0, "running": 0, "failed": 0, "success": 0}
        mock_breakdown.return_value = [
            {
                "job_type": "parse_draft",
                "pending": 1,
                "running": 0,
                "failed": 2,
                "success": 5,
            }
        ]
        mock_failed.return_value = []

        req = _make_request()
        result = admin_jobs_page(req)

        html = to_xml(result)
        assert "parse_draft" in html
        assert "T\u00f6\u00f6de t\u00fc\u00fcbi kaupa" in html

    @patch("app.admin.job_monitor._get_recent_failed")
    @patch("app.admin.job_monitor._get_type_breakdown")
    @patch("app.admin.job_monitor._get_status_counts")
    def test_page_renders_failed_jobs_with_retry(
        self,
        mock_counts: MagicMock,
        mock_breakdown: MagicMock,
        mock_failed: MagicMock,
    ):
        from fasthtml.common import to_xml

        from app.admin.job_monitor import admin_jobs_page

        mock_counts.return_value = {"pending": 0, "running": 0, "failed": 1, "success": 0}
        mock_breakdown.return_value = []
        now = datetime(2025, 6, 15, 14, 30)
        mock_failed.return_value = [
            {
                "id": 42,
                "job_type": "parse_draft",
                "error_message": "Connection timeout",
                "attempts": 3,
                "max_attempts": 3,
                "finished_at": now,
                "created_at": now,
            }
        ]

        req = _make_request()
        result = admin_jobs_page(req)

        html = to_xml(result)
        assert "Connection timeout" in html
        assert "parse_draft" in html
        assert "Proovi uuesti" in html
        assert "/admin/jobs/42/retry" in html

    @patch("app.admin.job_monitor._get_recent_failed")
    @patch("app.admin.job_monitor._get_type_breakdown")
    @patch("app.admin.job_monitor._get_status_counts")
    def test_page_renders_purge_button(
        self,
        mock_counts: MagicMock,
        mock_breakdown: MagicMock,
        mock_failed: MagicMock,
    ):
        from fasthtml.common import to_xml

        from app.admin.job_monitor import admin_jobs_page

        mock_counts.return_value = {"pending": 0, "running": 0, "failed": 0, "success": 0}
        mock_breakdown.return_value = []
        mock_failed.return_value = []

        req = _make_request()
        result = admin_jobs_page(req)

        html = to_xml(result)
        assert "Puhasta vanad t\u00f6\u00f6d" in html
        assert "/admin/jobs/purge" in html


# ---------------------------------------------------------------------------
# Retry handler
# ---------------------------------------------------------------------------


class TestAdminJobRetryHandler:
    @patch("app.admin.job_monitor._get_recent_failed")
    @patch("app.admin.job_monitor._get_type_breakdown")
    @patch("app.admin.job_monitor._get_status_counts")
    @patch("app.admin.job_monitor._retry_job")
    def test_retry_handler_success(
        self,
        mock_retry: MagicMock,
        mock_counts: MagicMock,
        mock_breakdown: MagicMock,
        mock_failed: MagicMock,
    ):
        from fasthtml.common import to_xml

        from app.admin.job_monitor import admin_job_retry

        mock_retry.return_value = True
        mock_counts.return_value = {"pending": 0, "running": 0, "failed": 0, "success": 0}
        mock_breakdown.return_value = []
        mock_failed.return_value = []

        req = _make_request("/admin/jobs/42/retry", method="POST")
        result = admin_job_retry(req, id=42)

        # Should return refreshed content (FT component, not JSON error)
        html = to_xml(result)
        assert "job-monitor-content" in html
        mock_retry.assert_called_once_with(42)

    @patch("app.admin.job_monitor._retry_job")
    def test_retry_handler_not_found(self, mock_retry: MagicMock):
        from app.admin.job_monitor import admin_job_retry

        mock_retry.return_value = False

        req = _make_request("/admin/jobs/999/retry", method="POST")
        result = admin_job_retry(req, id=999)

        assert result.status_code == 404  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Purge handler
# ---------------------------------------------------------------------------


class TestAdminJobsPurgeHandler:
    @patch("app.admin.job_monitor._get_recent_failed")
    @patch("app.admin.job_monitor._get_type_breakdown")
    @patch("app.admin.job_monitor._get_status_counts")
    @patch("app.admin.job_monitor._purge_completed")
    def test_purge_handler_returns_refreshed_content(
        self,
        mock_purge: MagicMock,
        mock_counts: MagicMock,
        mock_breakdown: MagicMock,
        mock_failed: MagicMock,
    ):
        from fasthtml.common import to_xml

        from app.admin.job_monitor import admin_jobs_purge

        mock_purge.return_value = 10
        mock_counts.return_value = {"pending": 0, "running": 0, "failed": 0, "success": 0}
        mock_breakdown.return_value = []
        mock_failed.return_value = []

        req = _make_request("/admin/jobs/purge", method="POST")
        result = admin_jobs_purge(req)

        html = to_xml(result)
        assert "job-monitor-content" in html
        mock_purge.assert_called_once_with(days=7)


# ---------------------------------------------------------------------------
# Route-level auth checks
# ---------------------------------------------------------------------------


class TestJobMonitorAuth:
    def test_jobs_page_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.get("/admin/jobs")
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"

    def test_job_retry_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.post("/admin/jobs/42/retry")
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"

    def test_jobs_purge_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.post("/admin/jobs/purge")
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"
