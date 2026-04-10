"""Tests for the enhanced audit log viewer (#542).

Covers: filter form rendering, query param extraction, filtered DB queries,
CSV export, date parsing, and the page handler with mocked DB.
"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock, patch

from starlette.requests import Request
from starlette.testclient import TestClient


def _make_request(path: str = "/admin/audit", query_string: str = "") -> Request:
    """Build a minimal Starlette Request for unit testing."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "query_string": query_string.encode(),
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
        "auth": {"role": "admin", "id": "admin-1", "email": "a@b.ee", "full_name": "Admin User"},
    }
    return Request(scope)


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------


class TestParseDateHelper:
    def test_valid_date(self):
        from app.admin.audit import _parse_date

        result = _parse_date("2025-03-15")
        assert result == date(2025, 3, 15)

    def test_invalid_date_returns_none(self):
        from app.admin.audit import _parse_date

        assert _parse_date("not-a-date") is None

    def test_empty_string_returns_none(self):
        from app.admin.audit import _parse_date

        assert _parse_date("") is None

    def test_none_returns_none(self):
        from app.admin.audit import _parse_date

        assert _parse_date(None) is None


# ---------------------------------------------------------------------------
# WHERE clause builder
# ---------------------------------------------------------------------------


class TestBuildAuditWhere:
    def test_no_filters_returns_true(self):
        from app.admin.audit import _build_audit_where

        where, params = _build_audit_where()
        assert where == "TRUE"
        assert params == []

    def test_action_filter(self):
        from app.admin.audit import _build_audit_where

        where, params = _build_audit_where(action="user.login")
        assert "a.action = %s" in where
        assert "user.login" in params

    def test_user_filter(self):
        from app.admin.audit import _build_audit_where

        where, params = _build_audit_where(user_id="user-42")
        assert "a.user_id = %s" in where
        assert "user-42" in params

    def test_date_range_filter(self):
        from app.admin.audit import _build_audit_where

        where, params = _build_audit_where(date_from=date(2025, 1, 1), date_to=date(2025, 1, 31))
        assert "a.created_at >= %s" in where
        assert "a.created_at < %s" in where
        assert len(params) == 2

    def test_query_filter(self):
        from app.admin.audit import _build_audit_where

        where, params = _build_audit_where(query="draft")
        assert "a.detail::text ILIKE %s" in where
        assert "%draft%" in params

    def test_combined_filters(self):
        from app.admin.audit import _build_audit_where

        where, params = _build_audit_where(action="doc.upload", user_id="u-1", query="test")
        assert "a.action = %s" in where
        assert "a.user_id = %s" in where
        assert "a.detail::text ILIKE %s" in where
        assert len(params) == 3


# ---------------------------------------------------------------------------
# Filtered DB query
# ---------------------------------------------------------------------------


class TestGetAuditLogPageFiltered:
    @patch("app.admin.audit._connect")
    def test_filtered_query_includes_where(self, mock_connect: MagicMock):
        from app.admin.audit import _get_audit_log_page

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        mock_conn.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=(0,))),
            MagicMock(fetchall=MagicMock(return_value=[])),
        ]

        entries, total = _get_audit_log_page(1, 25, action="user.login", query="test")

        # First call is COUNT
        count_sql = mock_conn.execute.call_args_list[0][0][0]
        assert "a.action = %s" in count_sql
        assert "a.detail::text ILIKE %s" in count_sql

    @patch("app.admin.audit._connect")
    def test_unfiltered_returns_entries(self, mock_connect: MagicMock):
        from app.admin.audit import _get_audit_log_page

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        now = datetime(2025, 3, 15, 10, 30)
        mock_conn.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=(1,))),
            MagicMock(
                fetchall=MagicMock(
                    return_value=[
                        (1, "user-1", "Test User", "user.login", None, now),
                    ]
                )
            ),
        ]

        entries, total = _get_audit_log_page(1, 25)
        assert total == 1
        assert len(entries) == 1
        assert entries[0]["action"] == "user.login"
        assert entries[0]["user_name"] == "Test User"


# ---------------------------------------------------------------------------
# Distinct actions and users
# ---------------------------------------------------------------------------


class TestFilterOptions:
    @patch("app.admin.audit._connect")
    def test_get_distinct_actions(self, mock_connect: MagicMock):
        from app.admin.audit import _get_distinct_actions

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = [
            ("doc.upload",),
            ("user.login",),
        ]

        actions = _get_distinct_actions()
        assert actions == ["doc.upload", "user.login"]

    @patch("app.admin.audit._connect")
    def test_get_distinct_actions_on_error(self, mock_connect: MagicMock):
        from app.admin.audit import _get_distinct_actions

        mock_connect.side_effect = Exception("DB down")
        actions = _get_distinct_actions()
        assert actions == []

    @patch("app.admin.audit._connect")
    def test_get_audit_users(self, mock_connect: MagicMock):
        from app.admin.audit import _get_audit_users

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = [
            ("u-1", "Alice"),
            ("u-2", "Bob"),
        ]

        users = _get_audit_users()
        assert len(users) == 2
        assert users[0] == {"id": "u-1", "name": "Alice"}

    @patch("app.admin.audit._connect")
    def test_get_audit_users_on_error(self, mock_connect: MagicMock):
        from app.admin.audit import _get_audit_users

        mock_connect.side_effect = Exception("DB down")
        users = _get_audit_users()
        assert users == []


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------


class TestAuditExport:
    @patch("app.admin.audit._get_all_filtered_entries")
    def test_csv_export_returns_csv_response(self, mock_entries: MagicMock):
        from app.admin.audit import admin_audit_export

        now = datetime(2025, 3, 15, 10, 30)
        mock_entries.return_value = [
            {
                "id": 1,
                "user_id": "u-1",
                "user_name": "Alice",
                "action": "user.login",
                "detail": "some detail",
                "created_at": now,
            }
        ]

        req = _make_request("/admin/audit/export", "action=user.login")
        response = admin_audit_export(req)

        assert response.status_code == 200  # type: ignore[union-attr]
        assert response.media_type == "text/csv"  # type: ignore[union-attr]
        assert "attachment" in response.headers["content-disposition"]  # type: ignore[union-attr]

        body = response.body.decode()  # type: ignore[union-attr]
        assert "Kasutaja" in body  # header row
        assert "Alice" in body
        assert "user.login" in body

    @patch("app.admin.audit._get_all_filtered_entries")
    def test_csv_export_empty(self, mock_entries: MagicMock):
        from app.admin.audit import admin_audit_export

        mock_entries.return_value = []
        req = _make_request("/admin/audit/export")
        response = admin_audit_export(req)

        assert response.status_code == 200  # type: ignore[union-attr]
        body = response.body.decode()  # type: ignore[union-attr]
        lines = body.strip().split("\n")
        assert len(lines) == 1  # header only

    @patch("app.admin.audit._get_all_filtered_entries")
    def test_csv_export_passes_filters(self, mock_entries: MagicMock):
        from app.admin.audit import admin_audit_export

        mock_entries.return_value = []
        req = _make_request(
            "/admin/audit/export",
            "action=doc.upload&user=u-2&query=test&from=2025-01-01&to=2025-12-31",
        )
        admin_audit_export(req)

        call_kwargs = mock_entries.call_args[1]
        assert call_kwargs["action"] == "doc.upload"
        assert call_kwargs["user_id"] == "u-2"
        assert call_kwargs["query"] == "test"
        assert call_kwargs["date_from"] == date(2025, 1, 1)
        assert call_kwargs["date_to"] == date(2025, 12, 31)


# ---------------------------------------------------------------------------
# Page rendering
# ---------------------------------------------------------------------------


class TestAdminAuditPageRendering:
    @patch("app.admin.audit._get_audit_users")
    @patch("app.admin.audit._get_distinct_actions")
    @patch("app.admin.audit._get_audit_log_page")
    def test_page_renders_filter_form(
        self,
        mock_page: MagicMock,
        mock_actions: MagicMock,
        mock_users: MagicMock,
    ):
        from fasthtml.common import to_xml

        from app.admin.audit import admin_audit_page

        mock_page.return_value = ([], 0)
        mock_actions.return_value = ["user.login", "doc.upload"]
        mock_users.return_value = [{"id": "u-1", "name": "Alice"}]

        req = _make_request()
        result = admin_audit_page(req)

        html = to_xml(result)
        # Filter form elements
        assert "filter-action" in html
        assert "filter-user" in html
        assert "filter-from" in html
        assert "filter-to" in html
        assert "filter-query" in html
        # Estonian labels
        assert "Tegevus" in html
        assert "Kasutaja" in html
        # Export link
        assert "Ekspordi CSV" in html

    @patch("app.admin.audit._get_audit_users")
    @patch("app.admin.audit._get_distinct_actions")
    @patch("app.admin.audit._get_audit_log_page")
    def test_page_renders_entries(
        self,
        mock_page: MagicMock,
        mock_actions: MagicMock,
        mock_users: MagicMock,
    ):
        from fasthtml.common import to_xml

        from app.admin.audit import admin_audit_page

        now = datetime(2025, 6, 15, 14, 30)
        mock_page.return_value = (
            [
                {
                    "id": 1,
                    "user_id": "u-1",
                    "user_name": "Alice",
                    "action": "user.login",
                    "detail": "logged in",
                    "created_at": now,
                }
            ],
            1,
        )
        mock_actions.return_value = []
        mock_users.return_value = []

        req = _make_request()
        result = admin_audit_page(req)

        html = to_xml(result)
        assert "Alice" in html
        assert "user.login" in html
        assert "15.06.2025" in html


# ---------------------------------------------------------------------------
# Route-level auth check
# ---------------------------------------------------------------------------


class TestAuditRouteAuth:
    def test_audit_export_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.get("/admin/audit/export")
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"

    def test_jobs_page_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.get("/admin/jobs")
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"
