"""Tests for ``app.admin.cost_dashboard`` — LLM cost dashboard page,
helpers, top-users/daily trend additions, CSV export, window selector
and Estonian feature labels.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from unittest.mock import MagicMock, patch

from fasthtml.common import to_xml
from starlette.requests import Request
from starlette.testclient import TestClient


def _make_request(
    path: str = "/admin/costs",
    query_string: str = "",
    *,
    role: str = "admin",
    org_id: str | None = None,
) -> Request:
    """Build a minimal Starlette Request for unit testing."""
    auth: dict[str, object] = {
        "role": role,
        "id": "admin-test",
        "email": "a@b.ee",
        "full_name": "Test Admin",
    }
    if org_id is not None:
        auth["org_id"] = org_id
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "query_string": query_string.encode(),
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
        "auth": auth,
    }
    return Request(scope)


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

    @patch("app.admin.cost_dashboard._connect")
    def test_filters_by_org_id_when_provided(self, mock_connect: MagicMock):
        from app.admin.cost_dashboard import _get_cost_by_org

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = []

        _get_cost_by_org("7d", org_id="abc-123")
        called_sql, called_params = mock_conn.execute.call_args[0]
        assert "WHERE o.id = %s" in called_sql
        assert "abc-123" in called_params


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

    @patch("app.admin.cost_dashboard._connect")
    def test_window_param_adjusts_where_clause(self, mock_connect: MagicMock):
        from app.admin.cost_dashboard import _get_cost_by_feature

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = []

        _get_cost_by_feature("ytd", org_id="org-1")
        called_sql, called_params = mock_conn.execute.call_args[0]
        assert "created_at >= %s" in called_sql
        assert "org_id = %s" in called_sql
        # First param is a window-start datetime; second is the org id.
        assert isinstance(called_params[0], datetime)
        assert called_params[1] == "org-1"


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
        # Input/output token counts surface even though we cannot split cost.
        assert result[0]["tokens_input"] == 80000
        assert result[0]["tokens_output"] == 40000

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
# _get_daily_trend (new)
# ---------------------------------------------------------------------------


class TestGetDailyTrend:
    @patch("app.admin.cost_dashboard._connect")
    def test_returns_daily_points(self, mock_connect: MagicMock):
        from app.admin.cost_dashboard import _get_daily_trend

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = [
            (date(2026, 5, 20), 0.50),
            (date(2026, 5, 21), 1.20),
            (date(2026, 5, 22), 0.10),
        ]

        result = _get_daily_trend("7d")
        assert len(result) == 3
        assert result[0]["day"] == date(2026, 5, 20)
        assert result[1]["cost_usd"] == 1.20

    @patch("app.admin.cost_dashboard._connect")
    def test_returns_empty_on_error(self, mock_connect: MagicMock):
        from app.admin.cost_dashboard import _get_daily_trend

        mock_connect.side_effect = Exception("DB unavailable")
        assert _get_daily_trend("30d") == []


# ---------------------------------------------------------------------------
# _get_top_users (new)
# ---------------------------------------------------------------------------


class TestGetTopUsers:
    @patch("app.admin.cost_dashboard._connect")
    def test_returns_top_spenders(self, mock_connect: MagicMock):
        from app.admin.cost_dashboard import _get_top_users

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = [
            ("u-1", "Anna Tamm", "anna@ministry.ee", 5000, 3.25),
            ("u-2", "Mart Maasikas", "mart@ministry.ee", 1200, 0.40),
        ]

        result = _get_top_users("30d", limit=10)
        assert len(result) == 2
        assert result[0]["user_id"] == "u-1"
        assert result[0]["full_name"] == "Anna Tamm"
        assert result[0]["email"] == "anna@ministry.ee"
        assert result[0]["tokens"] == 5000
        assert result[0]["cost_usd"] == 3.25

    @patch("app.admin.cost_dashboard._connect")
    def test_returns_empty_on_error(self, mock_connect: MagicMock):
        from app.admin.cost_dashboard import _get_top_users

        mock_connect.side_effect = Exception("DB unavailable")
        assert _get_top_users("30d") == []

    @patch("app.admin.cost_dashboard._connect")
    def test_org_filter_appended(self, mock_connect: MagicMock):
        from app.admin.cost_dashboard import _get_top_users

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = []

        _get_top_users("30d", org_id="org-7", limit=5)
        called_sql, called_params = mock_conn.execute.call_args[0]
        assert "u.org_id = %s" in called_sql
        assert "org-7" in called_params
        # Limit goes through as the last param.
        assert called_params[-1] == 5


# ---------------------------------------------------------------------------
# Window helpers
# ---------------------------------------------------------------------------


class TestWindowHelpers:
    def test_normalise_window_defaults_to_30d(self):
        from app.admin.cost_dashboard import _DEFAULT_WINDOW, _normalise_window

        assert _normalise_window(None) == _DEFAULT_WINDOW
        assert _normalise_window("") == _DEFAULT_WINDOW
        assert _normalise_window("bogus") == _DEFAULT_WINDOW

    def test_normalise_window_accepts_known_values(self):
        from app.admin.cost_dashboard import _normalise_window

        for w in ("7d", "30d", "90d", "ytd"):
            assert _normalise_window(w) == w

    def test_window_start_returns_earlier_for_longer_windows(self):
        from app.admin.cost_dashboard import _window_start

        s7 = _window_start("7d")
        s90 = _window_start("90d")
        assert s90 < s7

    def test_format_window_label_estonian(self):
        from app.admin.cost_dashboard import _format_window_label

        assert _format_window_label("7d") == "Viimased 7 päeva"
        assert _format_window_label("ytd") == "Aasta algusest"


# ---------------------------------------------------------------------------
# Feature-label fallback
# ---------------------------------------------------------------------------


class TestFeatureLabelFallback:
    def test_known_label_returns_estonian(self):
        from app.admin.cost_dashboard import _feature_label

        assert _feature_label("chat") == "Nõustaja"
        assert _feature_label("drafter_clarify") == "Koostaja täpsustamine"

    def test_unknown_label_falls_back_to_key_and_warns_once(self, caplog):
        from app.admin import cost_dashboard

        # Reset the warned-set so this test is hermetic.
        cost_dashboard._warned_missing_labels.discard("new_feature_xyz")

        caplog.set_level(logging.WARNING, logger="app.admin.cost_dashboard")
        first = cost_dashboard._feature_label("new_feature_xyz")
        second = cost_dashboard._feature_label("new_feature_xyz")

        assert first == "new_feature_xyz"
        assert second == "new_feature_xyz"
        # Exactly one warning, mentioning the missing key.
        warn_records = [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING and "new_feature_xyz" in r.getMessage()
        ]
        assert len(warn_records) == 1


# ---------------------------------------------------------------------------
# Multi-org access helper
# ---------------------------------------------------------------------------


class TestMultiOrgGate:
    def test_admin_can_see_all_orgs(self):
        from app.admin.cost_dashboard import _user_can_see_all_orgs

        assert _user_can_see_all_orgs({"role": "admin"}) is True

    def test_org_admin_cannot(self):
        from app.admin.cost_dashboard import _user_can_see_all_orgs

        assert _user_can_see_all_orgs({"role": "org_admin"}) is False

    def test_none_returns_false(self):
        from app.admin.cost_dashboard import _user_can_see_all_orgs

        assert _user_can_see_all_orgs(None) is False


# ---------------------------------------------------------------------------
# Sparkline rendering
# ---------------------------------------------------------------------------


class TestSparkline:
    def test_empty_returns_estonian_message(self):
        from app.admin.cost_dashboard import _sparkline

        result = _sparkline([])
        html = to_xml(result)
        assert "Päevatrendi" in html

    def test_single_point_renders_circle(self):
        from app.admin.cost_dashboard import _sparkline

        result = _sparkline([{"day": date(2026, 5, 22), "cost_usd": 1.0}])
        html = to_xml(result)
        assert "<svg" in html
        assert "circle" in html

    def test_multi_point_renders_polyline(self):
        from app.admin.cost_dashboard import _sparkline

        points = [
            {"day": date(2026, 5, 20), "cost_usd": 0.5},
            {"day": date(2026, 5, 21), "cost_usd": 1.2},
            {"day": date(2026, 5, 22), "cost_usd": 0.1},
        ]
        html = to_xml(_sparkline(points))
        assert "<svg" in html
        assert "polyline" in html
        # Estonian aria-label describes the visualisation.
        assert "Päevakulude" in html


# ---------------------------------------------------------------------------
# Page rendering
# ---------------------------------------------------------------------------


class TestCostPageRender:
    @patch("app.admin.cost_dashboard._get_orgs_for_filter")
    @patch("app.admin.cost_dashboard._get_monthly_trend")
    @patch("app.admin.cost_dashboard._get_daily_trend")
    @patch("app.admin.cost_dashboard._get_top_users")
    @patch("app.admin.cost_dashboard._get_cost_by_model")
    @patch("app.admin.cost_dashboard._get_cost_by_feature")
    @patch("app.admin.cost_dashboard._get_cost_by_org")
    def test_page_renders_with_data(
        self,
        mock_org: MagicMock,
        mock_feat: MagicMock,
        mock_model: MagicMock,
        mock_top: MagicMock,
        mock_daily: MagicMock,
        mock_trend: MagicMock,
        mock_orgs_filter: MagicMock,
    ):
        mock_org.return_value = [
            {"org_name": "Test Org", "cost_usd": 15.0, "budget_usd": 50.0},
        ]
        mock_feat.return_value = [
            {"feature": "chat", "tokens_input": 1000, "tokens_output": 500, "cost_usd": 0.50},
            {
                "feature": "drafter_clarify",
                "tokens_input": 200,
                "tokens_output": 100,
                "cost_usd": 0.10,
            },
        ]
        mock_model.return_value = [
            {
                "model": "claude-sonnet-4-6",
                "tokens_input": 1000,
                "tokens_output": 500,
                "cost_usd": 0.50,
            },
        ]
        mock_top.return_value = [
            {
                "user_id": "u-1",
                "full_name": "Anna Tamm",
                "email": "anna@m.ee",
                "tokens": 1500,
                "cost_usd": 0.50,
            },
        ]
        mock_daily.return_value = [
            {"day": date(2026, 5, 20), "cost_usd": 0.30},
            {"day": date(2026, 5, 21), "cost_usd": 0.20},
        ]
        mock_trend.return_value = [
            {
                "month": datetime(2026, 4, 1),
                "tokens_input": 5000,
                "tokens_output": 2000,
                "cost_usd": 2.00,
            },
        ]
        mock_orgs_filter.return_value = [
            {"id": "11111111-1111-1111-1111-111111111111", "name": "M1"}
        ]

        from app.admin.cost_dashboard import admin_cost_page

        req = _make_request("/admin/costs", "window=30d")
        result = admin_cost_page(req)
        html = to_xml(result)

        assert "LLM kulud" in html
        # All six cards rendered.
        assert "organisatsioonide kaupa" in html.lower()
        assert "funktsioonide kaupa" in html.lower()
        assert "mudelite kaupa" in html.lower()
        assert "top 10 kasutajat" in html.lower()
        assert "päevatrend" in html.lower()
        assert "igakuine" in html.lower()
        # Estonian feature label rendered.
        assert "Nõustaja" in html
        assert "Koostaja täpsustamine" in html
        # CSV export link present.
        assert "/admin/costs/export?" in html
        # Top user surfaced.
        assert "Anna Tamm" in html
        # Sparkline rendered.
        assert "<svg" in html
        # Window tabs present.
        assert "Viimased 7" in html
        assert "Aasta algusest" in html

    @patch("app.admin.cost_dashboard._get_orgs_for_filter")
    @patch("app.admin.cost_dashboard._get_monthly_trend")
    @patch("app.admin.cost_dashboard._get_daily_trend")
    @patch("app.admin.cost_dashboard._get_top_users")
    @patch("app.admin.cost_dashboard._get_cost_by_model")
    @patch("app.admin.cost_dashboard._get_cost_by_feature")
    @patch("app.admin.cost_dashboard._get_cost_by_org")
    def test_page_renders_empty_state(
        self,
        mock_org: MagicMock,
        mock_feat: MagicMock,
        mock_model: MagicMock,
        mock_top: MagicMock,
        mock_daily: MagicMock,
        mock_trend: MagicMock,
        mock_orgs_filter: MagicMock,
    ):
        mock_org.return_value = []
        mock_feat.return_value = []
        mock_model.return_value = []
        mock_top.return_value = []
        mock_daily.return_value = []
        mock_trend.return_value = []
        mock_orgs_filter.return_value = []

        from app.admin.cost_dashboard import admin_cost_page

        req = _make_request("/admin/costs")
        result = admin_cost_page(req)
        html = to_xml(result)

        assert "LLM kulud" in html
        # New unified empty state copy.
        assert "Kuludest pole andmeid valitud ajavahemikus." in html

    @patch("app.admin.cost_dashboard._get_orgs_for_filter")
    @patch("app.admin.cost_dashboard._get_monthly_trend")
    @patch("app.admin.cost_dashboard._get_daily_trend")
    @patch("app.admin.cost_dashboard._get_top_users")
    @patch("app.admin.cost_dashboard._get_cost_by_model")
    @patch("app.admin.cost_dashboard._get_cost_by_feature")
    @patch("app.admin.cost_dashboard._get_cost_by_org")
    def test_window_switching_changes_label(
        self,
        mock_org: MagicMock,
        mock_feat: MagicMock,
        mock_model: MagicMock,
        mock_top: MagicMock,
        mock_daily: MagicMock,
        mock_trend: MagicMock,
        mock_orgs_filter: MagicMock,
    ):
        for m in (
            mock_org,
            mock_feat,
            mock_model,
            mock_top,
            mock_daily,
            mock_trend,
            mock_orgs_filter,
        ):
            m.return_value = []

        from app.admin.cost_dashboard import admin_cost_page

        for window, label in [
            ("7d", "Viimased 7 päeva"),
            ("90d", "Viimased 90 päeva"),
            ("ytd", "Aasta algusest"),
        ]:
            req = _make_request("/admin/costs", f"window={window}")
            html = to_xml(admin_cost_page(req))
            assert label in html, f"window={window} should render label {label!r}"

    @patch("app.admin.cost_dashboard._get_orgs_for_filter")
    @patch("app.admin.cost_dashboard._get_monthly_trend")
    @patch("app.admin.cost_dashboard._get_daily_trend")
    @patch("app.admin.cost_dashboard._get_top_users")
    @patch("app.admin.cost_dashboard._get_cost_by_model")
    @patch("app.admin.cost_dashboard._get_cost_by_feature")
    @patch("app.admin.cost_dashboard._get_cost_by_org")
    def test_unknown_feature_falls_back_to_raw_key(
        self,
        mock_org: MagicMock,
        mock_feat: MagicMock,
        mock_model: MagicMock,
        mock_top: MagicMock,
        mock_daily: MagicMock,
        mock_trend: MagicMock,
        mock_orgs_filter: MagicMock,
    ):
        mock_org.return_value = []
        mock_feat.return_value = [
            {
                "feature": "shiny_new_feature",
                "tokens_input": 10,
                "tokens_output": 5,
                "cost_usd": 0.001,
            }
        ]
        mock_model.return_value = []
        mock_top.return_value = []
        mock_daily.return_value = []
        mock_trend.return_value = []
        mock_orgs_filter.return_value = []

        from app.admin import cost_dashboard
        from app.admin.cost_dashboard import admin_cost_page

        cost_dashboard._warned_missing_labels.discard("shiny_new_feature")

        req = _make_request("/admin/costs")
        html = to_xml(admin_cost_page(req))
        # Raw key surfaces in the rendered table.
        assert "shiny_new_feature" in html
        # Single warning emitted for this key.
        assert "shiny_new_feature" in cost_dashboard._warned_missing_labels


# ---------------------------------------------------------------------------
# Org scoping
# ---------------------------------------------------------------------------


class TestOrgScoping:
    @patch("app.admin.cost_dashboard._get_orgs_for_filter")
    @patch("app.admin.cost_dashboard._get_monthly_trend")
    @patch("app.admin.cost_dashboard._get_daily_trend")
    @patch("app.admin.cost_dashboard._get_top_users")
    @patch("app.admin.cost_dashboard._get_cost_by_model")
    @patch("app.admin.cost_dashboard._get_cost_by_feature")
    @patch("app.admin.cost_dashboard._get_cost_by_org")
    def test_org_admin_pinned_to_own_org(
        self,
        mock_org: MagicMock,
        mock_feat: MagicMock,
        mock_model: MagicMock,
        mock_top: MagicMock,
        mock_daily: MagicMock,
        mock_trend: MagicMock,
        mock_orgs_filter: MagicMock,
    ):
        for m in (
            mock_org,
            mock_feat,
            mock_model,
            mock_top,
            mock_daily,
            mock_trend,
            mock_orgs_filter,
        ):
            m.return_value = []

        from app.admin.cost_dashboard import admin_cost_page

        # Non-admin role with org_id; query string asks for a different org.
        req = _make_request(
            "/admin/costs",
            "org=other-org",
            role="org_admin",
            org_id="own-org",
        )
        admin_cost_page(req)
        # _get_cost_by_feature should be called with the user's own org_id,
        # not the query-string-provided value.
        assert mock_feat.call_args[0][1] == "own-org"

    @patch("app.admin.cost_dashboard._get_orgs_for_filter")
    @patch("app.admin.cost_dashboard._get_monthly_trend")
    @patch("app.admin.cost_dashboard._get_daily_trend")
    @patch("app.admin.cost_dashboard._get_top_users")
    @patch("app.admin.cost_dashboard._get_cost_by_model")
    @patch("app.admin.cost_dashboard._get_cost_by_feature")
    @patch("app.admin.cost_dashboard._get_cost_by_org")
    def test_system_admin_honours_query_string_org(
        self,
        mock_org: MagicMock,
        mock_feat: MagicMock,
        mock_model: MagicMock,
        mock_top: MagicMock,
        mock_daily: MagicMock,
        mock_trend: MagicMock,
        mock_orgs_filter: MagicMock,
    ):
        for m in (
            mock_org,
            mock_feat,
            mock_model,
            mock_top,
            mock_daily,
            mock_trend,
            mock_orgs_filter,
        ):
            m.return_value = []

        from app.admin.cost_dashboard import admin_cost_page

        req = _make_request("/admin/costs", "org=picked-org", role="admin")
        admin_cost_page(req)
        assert mock_feat.call_args[0][1] == "picked-org"


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------


class TestCostExport:
    @patch("app.admin.cost_dashboard._get_cost_by_feature")
    def test_csv_export_returns_csv_response(self, mock_feat: MagicMock):
        from app.admin.cost_dashboard import admin_cost_export

        mock_feat.return_value = [
            {"feature": "chat", "tokens_input": 100, "tokens_output": 50, "cost_usd": 0.10},
            {
                "feature": "drafter_draft",
                "tokens_input": 200,
                "tokens_output": 80,
                "cost_usd": 0.30,
            },
        ]

        req = _make_request("/admin/costs/export", "format=csv&window=7d")
        response = admin_cost_export(req)

        assert response.status_code == 200  # type: ignore[union-attr]
        assert response.media_type == "text/csv"  # type: ignore[union-attr]
        cd = response.headers["content-disposition"]  # type: ignore[union-attr]
        assert "attachment" in cd
        assert "llm-kulud-7d.csv" in cd

        body = response.body.decode()  # type: ignore[union-attr]
        lines = body.strip().split("\n")
        # 1 header + 2 data rows.
        assert len(lines) == 3
        # Header has Estonian columns.
        header = lines[0]
        for col in (
            "Ajavahemik",
            "Funktsioon",
            "Funktsiooni võti",
            "Sisend-tokenid",
            "Väljund-tokenid",
            "Tokeneid kokku",
            "Kulu (USD)",
        ):
            assert col in header
        # Estonian feature labels rendered, raw key preserved in its own column.
        assert "Nõustaja" in body
        assert "chat" in body
        assert "Koostaja mustand" in body
        assert "drafter_draft" in body

    @patch("app.admin.cost_dashboard._get_cost_by_feature")
    def test_csv_export_empty(self, mock_feat: MagicMock):
        from app.admin.cost_dashboard import admin_cost_export

        mock_feat.return_value = []
        req = _make_request("/admin/costs/export", "format=csv")
        response = admin_cost_export(req)
        body = response.body.decode()  # type: ignore[union-attr]
        lines = body.strip().split("\n")
        # Just the header row.
        assert len(lines) == 1
        assert "Funktsioon" in lines[0]

    @patch("app.admin.cost_dashboard._get_cost_by_feature")
    def test_csv_export_rejects_non_csv_format(self, mock_feat: MagicMock):
        from app.admin.cost_dashboard import admin_cost_export

        req = _make_request("/admin/costs/export", "format=json")
        response = admin_cost_export(req)
        assert response.status_code == 400  # type: ignore[union-attr]
        mock_feat.assert_not_called()

    @patch("app.admin.cost_dashboard._get_cost_by_feature")
    def test_csv_export_org_scoping(self, mock_feat: MagicMock):
        from app.admin.cost_dashboard import admin_cost_export

        mock_feat.return_value = []
        # org_admin: own org wins regardless of ?org= query param.
        req = _make_request(
            "/admin/costs/export",
            "format=csv&window=30d&org=other-org",
            role="org_admin",
            org_id="own-org",
        )
        admin_cost_export(req)
        assert mock_feat.call_args[0][1] == "own-org"


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

    def test_costs_export_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.get("/admin/costs/export?format=csv&window=30d")
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"
