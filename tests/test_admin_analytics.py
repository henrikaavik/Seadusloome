"""Tests for ``app.admin.analytics`` — usage analytics page and helpers."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

from fasthtml.common import to_xml
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# _refresh_usage_daily
# ---------------------------------------------------------------------------


class TestRefreshUsageDaily:
    @patch("app.admin.analytics._connect")
    def test_refresh_returns_true_on_success(self, mock_connect: MagicMock):
        from app.admin.analytics import _refresh_usage_daily

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        result = _refresh_usage_daily()
        assert result is True
        mock_conn.execute.assert_called_once()
        sql = mock_conn.execute.call_args[0][0]
        assert "REFRESH MATERIALIZED VIEW CONCURRENTLY usage_daily" in sql
        mock_conn.commit.assert_called_once()

    @patch("app.admin.analytics._connect")
    def test_refresh_returns_false_on_error(self, mock_connect: MagicMock):
        from app.admin.analytics import _refresh_usage_daily

        mock_connect.side_effect = Exception("DB unavailable")
        result = _refresh_usage_daily()
        assert result is False


# ---------------------------------------------------------------------------
# _get_usage_data
# ---------------------------------------------------------------------------


class TestGetUsageData:
    @patch("app.admin.analytics._connect")
    def test_returns_list_of_dicts(self, mock_connect: MagicMock):
        from app.admin.analytics import _get_usage_data

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = [
            (date(2026, 4, 8), 5, 120, 3),
            (date(2026, 4, 7), 2, 80, 1),
        ]

        result = _get_usage_data(days=7)
        assert len(result) == 2
        assert result[0]["day"] == date(2026, 4, 8)
        assert result[0]["uploads"] == 5
        assert result[0]["chat_messages"] == 120
        assert result[0]["drafter_sessions"] == 3
        assert result[1]["uploads"] == 2

    @patch("app.admin.analytics._connect")
    def test_returns_empty_on_error(self, mock_connect: MagicMock):
        from app.admin.analytics import _get_usage_data

        mock_connect.side_effect = Exception("DB unavailable")
        result = _get_usage_data()
        assert result == []


# ---------------------------------------------------------------------------
# _usage_summary
# ---------------------------------------------------------------------------


class TestUsageSummary:
    def test_computes_totals(self):
        from app.admin.analytics import _usage_summary

        data = [
            {"uploads": 5, "chat_messages": 100, "drafter_sessions": 3},
            {"uploads": 3, "chat_messages": 50, "drafter_sessions": 2},
        ]
        summary = _usage_summary(data)
        assert summary["total_uploads"] == 8
        assert summary["total_chat_messages"] == 150
        assert summary["total_drafter_sessions"] == 5
        assert summary["days"] == 2

    def test_empty_data(self):
        from app.admin.analytics import _usage_summary

        summary = _usage_summary([])
        assert summary["total_uploads"] == 0
        assert summary["total_chat_messages"] == 0
        assert summary["total_drafter_sessions"] == 0
        assert summary["days"] == 0


# ---------------------------------------------------------------------------
# Page rendering
# ---------------------------------------------------------------------------


class TestAnalyticsPageRender:
    @patch("app.admin.analytics._get_usage_data")
    def test_page_renders_with_data(self, mock_data: MagicMock):
        mock_data.return_value = [
            {"day": date(2026, 4, 8), "uploads": 5, "chat_messages": 120, "drafter_sessions": 3},
            {"day": date(2026, 4, 7), "uploads": 2, "chat_messages": 80, "drafter_sessions": 1},
        ]

        from starlette.requests import Request

        from app.admin.analytics import admin_analytics_page

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/admin/analytics",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
            "auth": {
                "role": "admin",
                "id": "admin-test",
                "email": "a@b.ee",
                "full_name": "Test Admin",
            },
        }
        req = Request(scope)
        result = admin_analytics_page(req)
        html = to_xml(result)

        assert "Kasutusanaluutika" in html
        assert "Kokkuvote" in html
        assert "Trendid" in html
        assert "Detailne tabel" in html

    @patch("app.admin.analytics._get_usage_data")
    def test_page_renders_empty_state(self, mock_data: MagicMock):
        mock_data.return_value = []

        from starlette.requests import Request

        from app.admin.analytics import admin_analytics_page

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/admin/analytics",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
            "auth": {
                "role": "admin",
                "id": "admin-test",
                "email": "a@b.ee",
                "full_name": "Test Admin",
            },
        }
        req = Request(scope)
        result = admin_analytics_page(req)
        html = to_xml(result)

        assert "Kasutusanaluutika" in html
        assert "puuduvad" in html.lower()


# ---------------------------------------------------------------------------
# Auth gate (route-level)
# ---------------------------------------------------------------------------


class TestAnalyticsAuth:
    def test_analytics_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.get("/admin/analytics")
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"
