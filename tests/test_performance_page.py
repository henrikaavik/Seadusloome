"""Tests for app.admin.performance — admin performance page (#545)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient


class TestPerformancePageAuth:
    def test_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.get("/admin/performance")
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"


class TestPerformanceDataHelpers:
    @patch("app.admin.performance._connect")
    def test_get_latency_percentiles_returns_dict(self, mock_connect: MagicMock):
        from app.admin.performance import _get_latency_percentiles

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchone.return_value = (15.3, 95.7, 210.1)

        result = _get_latency_percentiles()
        assert result["p50"] == 15.3
        assert result["p95"] == 95.7
        assert result["p99"] == 210.1

    @patch("app.admin.performance._connect")
    def test_get_latency_percentiles_returns_defaults_on_error(self, mock_connect: MagicMock):
        from app.admin.performance import _get_latency_percentiles

        mock_connect.side_effect = Exception("DB down")
        result = _get_latency_percentiles()
        assert result == {"p50": 0.0, "p95": 0.0, "p99": 0.0}

    @patch("app.admin.performance._connect")
    def test_get_slowest_routes_returns_list(self, mock_connect: MagicMock):
        from app.admin.performance import _get_slowest_routes

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = [
            ("/api/explorer/search", "GET", 50, 120.50, 450.00),
        ]

        result = _get_slowest_routes()
        assert len(result) == 1
        assert result[0]["path"] == "/api/explorer/search"
        assert result[0]["avg_ms"] == 120.50

    @patch("app.admin.performance._connect")
    def test_get_slowest_routes_returns_empty_on_error(self, mock_connect: MagicMock):
        from app.admin.performance import _get_slowest_routes

        mock_connect.side_effect = Exception("DB down")
        result = _get_slowest_routes()
        assert result == []
