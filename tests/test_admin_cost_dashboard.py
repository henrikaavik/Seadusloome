"""Tests for ``app.admin.cost_dashboard`` — LLM cost dashboard page and helpers."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

from fasthtml.common import to_xml
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# _get_cost_by_org
# ---------------------------------------------------------------------------


class TestGetCostByOrg:
    @patch("app.admin.cost_dashboard._connect")
    def test_returns_org_costs(self, mock_connect: MagicMock):
        from app.admin.cost_dashboard import _get_cost_by_org

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = [
            ("Ministeerium A", 25.50),
            ("Ministeerium B", 10.00),
        ]

        result = _get_cost_by_org()
        assert len(result) == 2
        assert result[0]["org_name"] == "Ministeerium A"
        assert result[0]["cost_usd"] == 25.50
        assert "budget_usd" in result[0]

    @patch("app.admin.cost_dashboard._connect")
    def test_returns_empty_on_error(self, mock_connect: MagicMock):
        from app.admin.cost_dashboard import _get_cost_by_org

        mock_connect.side_effect = Exception("DB unavailable")
        result = _get_cost_by_org()
        assert result == []


# ---------------------------------------------------------------------------
# _get_cost_by_feature
# ---------------------------------------------------------------------------


class TestGetCostByFeature:
    @patch("app.admin.cost_dashboard._connect")
    def test_returns_feature_costs(self, mock_connect: MagicMock):
        from app.admin.cost_dashboard import _get_cost_by_feature

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = [
            ("chat", 50000, 20000, 1.50),
            ("drafter_draft", 30000, 15000, 0.80),
        ]

        result = _get_cost_by_feature()
        assert len(result) == 2
        assert result[0]["feature"] == "chat"
        assert result[0]["cost_usd"] == 1.50
        assert result[0]["tokens_input"] == 50000
        assert result[0]["tokens_output"] == 20000

    @patch("app.admin.cost_dashboard._connect")
    def test_returns_empty_on_error(self, mock_connect: MagicMock):
        from app.admin.cost_dashboard import _get_cost_by_feature

        mock_connect.side_effect = Exception("DB unavailable")
        result = _get_cost_by_feature()
        assert result == []


# ---------------------------------------------------------------------------
# _get_cost_by_model
# ---------------------------------------------------------------------------


class TestGetCostByModel:
    @patch("app.admin.cost_dashboard._connect")
    def test_returns_model_costs(self, mock_connect: MagicMock):
        from app.admin.cost_dashboard import _get_cost_by_model

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = [
            ("claude-sonnet-4-6", 80000, 40000, 2.00),
            ("claude-opus-4-6", 20000, 10000, 1.50),
        ]

        result = _get_cost_by_model()
        assert len(result) == 2
        assert result[0]["model"] == "claude-sonnet-4-6"
        assert result[1]["model"] == "claude-opus-4-6"

    @patch("app.admin.cost_dashboard._connect")
    def test_returns_empty_on_error(self, mock_connect: MagicMock):
        from app.admin.cost_dashboard import _get_cost_by_model

        mock_connect.side_effect = Exception("DB unavailable")
        result = _get_cost_by_model()
        assert result == []


# ---------------------------------------------------------------------------
# _get_monthly_trend
# ---------------------------------------------------------------------------


class TestGetMonthlyTrend:
    @patch("app.admin.cost_dashboard._connect")
    def test_returns_monthly_data(self, mock_connect: MagicMock):
        from app.admin.cost_dashboard import _get_monthly_trend

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = [
            (datetime(2026, 4, 1), 100000, 50000, 5.00),
            (datetime(2026, 3, 1), 80000, 40000, 3.50),
        ]

        result = _get_monthly_trend(months=6)
        assert len(result) == 2
        assert result[0]["cost_usd"] == 5.00
        assert result[1]["tokens_input"] == 80000

    @patch("app.admin.cost_dashboard._connect")
    def test_returns_empty_on_error(self, mock_connect: MagicMock):
        from app.admin.cost_dashboard import _get_monthly_trend

        mock_connect.side_effect = Exception("DB unavailable")
        result = _get_monthly_trend()
        assert result == []


# ---------------------------------------------------------------------------
# Page rendering
# ---------------------------------------------------------------------------


class TestCostPageRender:
    @patch("app.admin.cost_dashboard._get_monthly_trend")
    @patch("app.admin.cost_dashboard._get_cost_by_model")
    @patch("app.admin.cost_dashboard._get_cost_by_feature")
    @patch("app.admin.cost_dashboard._get_cost_by_org")
    def test_page_renders_with_data(
        self,
        mock_org: MagicMock,
        mock_feat: MagicMock,
        mock_model: MagicMock,
        mock_trend: MagicMock,
    ):
        mock_org.return_value = [
            {"org_name": "Test Org", "cost_usd": 15.0, "budget_usd": 50.0},
        ]
        mock_feat.return_value = [
            {"feature": "chat", "tokens_input": 1000, "tokens_output": 500, "cost_usd": 0.50},
        ]
        mock_model.return_value = [
            {
                "model": "claude-sonnet-4-6",
                "tokens_input": 1000,
                "tokens_output": 500,
                "cost_usd": 0.50,
            },
        ]
        mock_trend.return_value = [
            {
                "month": datetime(2026, 4, 1),
                "tokens_input": 5000,
                "tokens_output": 2000,
                "cost_usd": 2.00,
            },
        ]

        from starlette.requests import Request

        from app.admin.cost_dashboard import admin_cost_page

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/admin/costs",
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
        result = admin_cost_page(req)
        html = to_xml(result)

        assert "LLM kulud" in html
        assert "organisatsioonide kaupa" in html.lower()
        assert "funktsioonide kaupa" in html.lower()
        assert "mudelite kaupa" in html.lower()
        assert "trend" in html.lower()

    @patch("app.admin.cost_dashboard._get_monthly_trend")
    @patch("app.admin.cost_dashboard._get_cost_by_model")
    @patch("app.admin.cost_dashboard._get_cost_by_feature")
    @patch("app.admin.cost_dashboard._get_cost_by_org")
    def test_page_renders_empty_state(
        self,
        mock_org: MagicMock,
        mock_feat: MagicMock,
        mock_model: MagicMock,
        mock_trend: MagicMock,
    ):
        mock_org.return_value = []
        mock_feat.return_value = []
        mock_model.return_value = []
        mock_trend.return_value = []

        from starlette.requests import Request

        from app.admin.cost_dashboard import admin_cost_page

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/admin/costs",
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
        result = admin_cost_page(req)
        html = to_xml(result)

        assert "LLM kulud" in html
        assert "puuduvad" in html.lower()


# ---------------------------------------------------------------------------
# Auth gate (route-level)
# ---------------------------------------------------------------------------


class TestCostDashboardAuth:
    def test_costs_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.get("/admin/costs")
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"
