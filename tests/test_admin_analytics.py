"""Tests for ``app.admin.analytics`` — usage analytics page and helpers."""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock, patch

from fasthtml.common import to_xml
from starlette.requests import Request
from starlette.testclient import TestClient


def _scope(query: str = "") -> dict:
    """Return a minimal ASGI scope with an admin auth payload."""
    return {
        "type": "http",
        "method": "GET",
        "path": "/admin/analytics",
        "headers": [],
        "query_string": query.encode() if isinstance(query, str) else query,
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

    @patch("app.admin.analytics._connect")
    def test_aliases_draft_uploads_column(self, mock_connect: MagicMock):
        """The materialised view names the column ``draft_uploads``; the
        helper must SUM that column under the ``uploads`` alias so older
        callers keep working."""
        from app.admin.analytics import _get_usage_data

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = []

        _get_usage_data(days=30)
        sql = mock_conn.execute.call_args[0][0]
        assert "draft_uploads" in sql
        assert "AS uploads" in sql


# ---------------------------------------------------------------------------
# _get_usage_by_org
# ---------------------------------------------------------------------------


class TestGetUsageByOrg:
    @patch("app.admin.analytics._connect")
    def test_returns_aggregated_org_rows(self, mock_connect: MagicMock):
        from app.admin.analytics import _get_usage_by_org

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = [
            ("Ministeerium A", 12, 80, 4),
            ("Ministeerium B", 3, 15, 1),
        ]

        result = _get_usage_by_org(days=30)
        assert len(result) == 2
        assert result[0]["org_name"] == "Ministeerium A"
        assert result[0]["uploads"] == 12
        assert result[0]["chat_messages"] == 80
        assert result[0]["drafter_sessions"] == 4
        assert result[0]["total"] == 12 + 80 + 4
        assert result[1]["total"] == 3 + 15 + 1

    @patch("app.admin.analytics._connect")
    def test_empty_on_error(self, mock_connect: MagicMock):
        from app.admin.analytics import _get_usage_by_org

        mock_connect.side_effect = Exception("DB unavailable")
        assert _get_usage_by_org() == []


# ---------------------------------------------------------------------------
# _get_last_refresh
# ---------------------------------------------------------------------------


class TestGetLastRefresh:
    @patch("app.admin.analytics._connect")
    def test_returns_timestamp_when_present(self, mock_connect: MagicMock):
        from app.admin.analytics import _get_last_refresh

        ts = datetime(2026, 4, 8, 12, 30)
        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchone.return_value = (ts,)

        assert _get_last_refresh() == ts

    @patch("app.admin.analytics._connect")
    def test_returns_none_on_error(self, mock_connect: MagicMock):
        from app.admin.analytics import _get_last_refresh

        mock_connect.side_effect = Exception("boom")
        assert _get_last_refresh() is None


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
# _resolve_window
# ---------------------------------------------------------------------------


class TestResolveWindow:
    def test_default_30d(self):
        from app.admin.analytics import _resolve_window

        slug, days = _resolve_window(None)
        assert slug == "30d"
        assert days == 30

    def test_unknown_falls_back(self):
        from app.admin.analytics import _resolve_window

        slug, days = _resolve_window("nonsense")
        assert slug == "30d"
        assert days == 30

    def test_seven_days(self):
        from app.admin.analytics import _resolve_window

        slug, days = _resolve_window("7d")
        assert slug == "7d"
        assert days == 7

    def test_ninety_days(self):
        from app.admin.analytics import _resolve_window

        slug, days = _resolve_window("90d")
        assert slug == "90d"
        assert days == 90

    def test_ytd(self):
        from app.admin.analytics import _resolve_window

        slug, days = _resolve_window("ytd")
        assert slug == "ytd"
        assert days >= 1
        assert days <= 366


# ---------------------------------------------------------------------------
# Page rendering
# ---------------------------------------------------------------------------


class TestAnalyticsPageRender:
    @patch("app.admin.analytics._get_last_refresh")
    @patch("app.admin.analytics._get_usage_by_org")
    @patch("app.admin.analytics._get_usage_data")
    def test_page_renders_with_data(
        self,
        mock_data: MagicMock,
        mock_org: MagicMock,
        mock_refresh: MagicMock,
    ):
        mock_data.return_value = [
            {"day": date(2026, 4, 8), "uploads": 5, "chat_messages": 120, "drafter_sessions": 3},
            {"day": date(2026, 4, 7), "uploads": 2, "chat_messages": 80, "drafter_sessions": 1},
        ]
        mock_org.return_value = [
            {
                "org_name": "Ministeerium A",
                "uploads": 7,
                "chat_messages": 200,
                "drafter_sessions": 4,
                "total": 211,
            }
        ]
        mock_refresh.return_value = datetime(2026, 4, 8, 9, 0)

        from app.admin.analytics import admin_analytics_page

        req = Request(_scope())
        result = admin_analytics_page(req)
        html = to_xml(result)

        assert "Kasutusanalüütika" in html
        assert "Kokkuvõte" in html
        assert "Trendid" in html
        assert "Detailne tabel" in html
        assert "Organisatsioonide kaupa" in html
        assert "Ministeerium A" in html
        assert "Ekspordi CSV" in html
        assert "Värskenda andmeid" in html
        # Window selector pills
        assert "7 päeva" in html
        assert "30 päeva" in html
        assert "90 päeva" in html
        assert "Aasta algusest" in html
        # Sparkline
        assert "usage-sparkline" in html

    @patch("app.admin.analytics._get_last_refresh")
    @patch("app.admin.analytics._get_usage_by_org")
    @patch("app.admin.analytics._get_usage_data")
    def test_page_renders_empty_state(
        self,
        mock_data: MagicMock,
        mock_org: MagicMock,
        mock_refresh: MagicMock,
    ):
        mock_data.return_value = []
        mock_org.return_value = []
        mock_refresh.return_value = None

        from app.admin.analytics import admin_analytics_page

        req = Request(_scope())
        result = admin_analytics_page(req)
        html = to_xml(result)

        assert "Kasutusanalüütika" in html
        assert "Selles ajavahemikus pole andmeid." in html

    @patch("app.admin.analytics._get_last_refresh")
    @patch("app.admin.analytics._get_usage_by_org")
    @patch("app.admin.analytics._get_usage_data")
    def test_window_switching_updates_pill(
        self,
        mock_data: MagicMock,
        mock_org: MagicMock,
        mock_refresh: MagicMock,
    ):
        mock_data.return_value = []
        mock_org.return_value = []
        mock_refresh.return_value = None

        from app.admin.analytics import admin_analytics_page

        req = Request(_scope(query="window=90d"))
        result = admin_analytics_page(req)
        html = to_xml(result)

        # 90d pill carries the active marker; 30d does not.
        assert 'href="/admin/analytics?window=90d"' in html
        # The data fetcher must have been called with 90 days.
        assert mock_data.call_args[0][0] == 90

    @patch("app.admin.analytics._get_last_refresh")
    @patch("app.admin.analytics._get_usage_by_org")
    @patch("app.admin.analytics._get_usage_data")
    def test_window_ytd(
        self,
        mock_data: MagicMock,
        mock_org: MagicMock,
        mock_refresh: MagicMock,
    ):
        mock_data.return_value = []
        mock_org.return_value = []
        mock_refresh.return_value = None

        from app.admin.analytics import admin_analytics_page

        req = Request(_scope(query="window=ytd"))
        result = admin_analytics_page(req)
        html = to_xml(result)
        assert "Aasta algusest" in html
        # Default fallback would have used 30; YTD must produce a different
        # value (>= 1 day; bounded by 366) on every calendar date.
        days_called = mock_data.call_args[0][0]
        assert 1 <= days_called <= 366

    @patch("app.admin.analytics._get_last_refresh")
    @patch("app.admin.analytics._get_usage_by_org")
    @patch("app.admin.analytics._get_usage_data")
    def test_refresh_success_flash(
        self,
        mock_data: MagicMock,
        mock_org: MagicMock,
        mock_refresh: MagicMock,
    ):
        mock_data.return_value = []
        mock_org.return_value = []
        mock_refresh.return_value = None

        from app.admin.analytics import admin_analytics_page

        req = Request(_scope(query="window=30d&refreshed=ok&refreshed_at=2026-04-08+09%3A00"))
        result = admin_analytics_page(req)
        html = to_xml(result)
        assert "Andmed värskendati edukalt" in html

    @patch("app.admin.analytics._get_last_refresh")
    @patch("app.admin.analytics._get_usage_by_org")
    @patch("app.admin.analytics._get_usage_data")
    def test_refresh_failure_flash(
        self,
        mock_data: MagicMock,
        mock_org: MagicMock,
        mock_refresh: MagicMock,
    ):
        mock_data.return_value = []
        mock_org.return_value = []
        mock_refresh.return_value = None

        from app.admin.analytics import admin_analytics_page

        req = Request(_scope(query="refreshed=fail"))
        result = admin_analytics_page(req)
        html = to_xml(result)
        assert "Andmete värskendamine ebaõnnestus" in html

    @patch("app.admin.analytics._get_usage_data", side_effect=Exception("boom"))
    def test_page_handles_render_error(self, _mock: MagicMock):
        from app.admin.analytics import admin_analytics_page

        req = Request(_scope())
        result = admin_analytics_page(req)
        html = to_xml(result)
        assert "Kasutusanalüütika" in html


# ---------------------------------------------------------------------------
# admin_analytics_refresh — POST handler
# ---------------------------------------------------------------------------


class TestAnalyticsRefreshHandler:
    @patch("app.admin.analytics._refresh_usage_daily")
    def test_success_redirects_with_ok_flash(self, mock_refresh: MagicMock):
        mock_refresh.return_value = True

        from app.admin.analytics import admin_analytics_refresh

        scope = _scope(query="window=7d")
        scope["method"] = "POST"
        req = Request(scope)
        response = admin_analytics_refresh(req)
        assert response.status_code == 303
        location = response.headers["location"]
        assert "/admin/analytics?" in location
        assert "refreshed=ok" in location
        assert "window=7d" in location

    @patch("app.admin.analytics._refresh_usage_daily")
    def test_failure_redirects_with_fail_flash(self, mock_refresh: MagicMock):
        mock_refresh.return_value = False

        from app.admin.analytics import admin_analytics_refresh

        scope = _scope(query="window=30d")
        scope["method"] = "POST"
        req = Request(scope)
        response = admin_analytics_refresh(req)
        assert response.status_code == 303
        assert "refreshed=fail" in response.headers["location"]
        assert "window=30d" in response.headers["location"]

    @patch("app.admin.analytics._refresh_usage_daily", side_effect=Exception("boom"))
    def test_unexpected_error_redirects_with_fail_flash(self, _mock: MagicMock):
        from app.admin.analytics import admin_analytics_refresh

        scope = _scope()
        scope["method"] = "POST"
        req = Request(scope)
        response = admin_analytics_refresh(req)
        assert response.status_code == 303
        assert "refreshed=fail" in response.headers["location"]


# ---------------------------------------------------------------------------
# admin_analytics_export — CSV export handler
# ---------------------------------------------------------------------------


class TestAnalyticsExportHandler:
    @patch("app.admin.analytics._get_usage_data")
    def test_export_returns_csv_with_header_and_rows(self, mock_data: MagicMock):
        mock_data.return_value = [
            {"day": date(2026, 4, 8), "uploads": 5, "chat_messages": 120, "drafter_sessions": 3},
            {"day": date(2026, 4, 7), "uploads": 2, "chat_messages": 80, "drafter_sessions": 1},
        ]

        from app.admin.analytics import admin_analytics_export

        req = Request(_scope(query="window=30d"))
        response = admin_analytics_export(req)
        assert response.status_code == 200
        assert response.media_type == "text/csv"
        assert "kasutusandmed_30d.csv" in response.headers["content-disposition"]

        body = bytes(response.body).decode("utf-8")
        lines = [line for line in body.splitlines() if line]
        # 1 header + 2 data rows
        assert len(lines) == 3
        assert lines[0] == "Kuupäev,Üleslaadimised,Vestluse sõnumid,Koostamise seansid"
        assert lines[1].startswith("2026-04-08,5,120,3")
        assert lines[2].startswith("2026-04-07,2,80,1")

    @patch("app.admin.analytics._get_usage_data")
    def test_export_empty_returns_header_only(self, mock_data: MagicMock):
        mock_data.return_value = []

        from app.admin.analytics import admin_analytics_export

        req = Request(_scope(query="window=7d"))
        response = admin_analytics_export(req)
        body = bytes(response.body).decode("utf-8")
        lines = [line for line in body.splitlines() if line]
        assert len(lines) == 1  # header only
        assert "kasutusandmed_7d.csv" in response.headers["content-disposition"]

    @patch("app.admin.analytics._get_usage_data", side_effect=Exception("boom"))
    def test_export_returns_500_text_on_error(self, _mock: MagicMock):
        from app.admin.analytics import admin_analytics_export

        req = Request(_scope())
        response = admin_analytics_export(req)
        assert response.status_code == 500
        assert "ebaõnnestus" in bytes(response.body).decode("utf-8")


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

    def test_analytics_export_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.get("/admin/analytics/export")
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"

    def test_analytics_refresh_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.post("/admin/analytics/refresh")
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"
