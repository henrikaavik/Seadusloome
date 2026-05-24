"""Tests for the extended admin performance page (issue #198).

Covers the five timing series the page now surfaces:

    * ``http_request_duration_ms``
    * ``job_execution_ms``
    * ``llm_call_ms``
    * ``sparql_query_ms``
    * ``rag_retrieval_ms``

Each helper is mocked at ``app.admin.performance._connect`` (the module's
own bound name for the connection factory) following the same pattern as
``tests/test_dashboard.py`` and ``tests/test_performance_page.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fasthtml.common import to_xml
from starlette.requests import Request

# ---------------------------------------------------------------------------
# Window parsing
# ---------------------------------------------------------------------------


class TestParseWindow:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("1h", "1h"),
            ("24h", "24h"),
            ("7d", "7d"),
            ("1H", "1h"),
            (" 24h ", "24h"),
        ],
    )
    def test_valid_windows_pass_through(self, value: str, expected: str):
        from app.admin.performance import _parse_window

        assert _parse_window(value) == expected

    @pytest.mark.parametrize("value", [None, "", "30m", "garbage", "365d", "1"])
    def test_invalid_window_defaults_to_1h(self, value: str | None):
        from app.admin.performance import _parse_window

        assert _parse_window(value) == "1h"

    def test_interval_for_each_window(self):
        from app.admin.performance import _interval_for

        assert _interval_for("1h") == "1 hour"
        assert _interval_for("24h") == "24 hours"
        assert _interval_for("7d") == "7 days"
        # Unknown key falls back to 1h
        assert _interval_for("nonsense") == "1 hour"


# ---------------------------------------------------------------------------
# _get_series_summary — generic percentile aggregator
# ---------------------------------------------------------------------------


def _mock_connect_with_fetchone(mock_connect: MagicMock, row: tuple[Any, ...] | None) -> MagicMock:
    """Wire ``mock_connect()`` so ``conn.execute(...).fetchone()`` returns *row*."""
    mock_conn = MagicMock()
    mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
    mock_connect.return_value.__exit__ = MagicMock(return_value=False)
    mock_conn.execute.return_value.fetchone.return_value = row
    return mock_conn


def _mock_connect_with_fetchall(mock_connect: MagicMock, rows: list[tuple[Any, ...]]) -> MagicMock:
    mock_conn = MagicMock()
    mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
    mock_connect.return_value.__exit__ = MagicMock(return_value=False)
    mock_conn.execute.return_value.fetchall.return_value = rows
    return mock_conn


class TestGetSeriesSummary:
    @patch("app.admin.performance._connect")
    def test_returns_percentiles_and_count(self, mock_connect: MagicMock):
        from app.admin.performance import _get_series_summary

        _mock_connect_with_fetchone(mock_connect, (12.0, 87.5, 200.0, 1234))

        result = _get_series_summary("http_request_duration_ms", "1h")
        assert result == {"p50": 12.0, "p95": 87.5, "p99": 200.0, "count": 1234}

    @patch("app.admin.performance._connect")
    def test_p50_le_p95_le_p99_invariant(self, mock_connect: MagicMock):
        """Sanity check: even with synthetic data the helper must preserve
        ordering so the UI doesn't display a nonsensical p99 < p50."""
        from app.admin.performance import _get_series_summary

        _mock_connect_with_fetchone(mock_connect, (10.0, 50.0, 90.0, 42))

        s = _get_series_summary("llm_call_ms", "24h")
        assert s["p50"] <= s["p95"] <= s["p99"]

    @patch("app.admin.performance._connect")
    def test_empty_series_returns_zero_count(self, mock_connect: MagicMock):
        from app.admin.performance import _get_series_summary

        # Postgres returns (0, 0, 0, 0) thanks to COALESCE wrapping.
        _mock_connect_with_fetchone(mock_connect, (0, 0, 0, 0))

        result = _get_series_summary("rag_retrieval_ms", "7d")
        assert result["count"] == 0
        assert result["p50"] == 0.0

    @patch("app.admin.performance._connect")
    def test_db_error_returns_safe_defaults(self, mock_connect: MagicMock):
        from app.admin.performance import _get_series_summary

        mock_connect.side_effect = Exception("DB unavailable")

        result = _get_series_summary("sparql_query_ms", "1h")
        assert result == {"p50": 0.0, "p95": 0.0, "p99": 0.0, "count": 0}

    @patch("app.admin.performance._connect")
    def test_window_is_passed_to_sql(self, mock_connect: MagicMock):
        """The window param must reach the SQL execute() call as an interval."""
        from app.admin.performance import _get_series_summary

        mock_conn = _mock_connect_with_fetchone(mock_connect, (1.0, 2.0, 3.0, 4))

        _get_series_summary("http_request_duration_ms", "24h")
        call_args = mock_conn.execute.call_args
        # Second positional arg is the params tuple.
        params = call_args[0][1]
        assert params == ("http_request_duration_ms", "24 hours")

        _get_series_summary("http_request_duration_ms", "7d")
        params = mock_conn.execute.call_args[0][1]
        assert params[1] == "7 days"


# ---------------------------------------------------------------------------
# _get_series_breakdown — top-N labels by p95
# ---------------------------------------------------------------------------


class TestGetSeriesBreakdown:
    @patch("app.admin.performance._connect")
    def test_returns_breakdown_rows(self, mock_connect: MagicMock):
        from app.admin.performance import _get_series_breakdown

        _mock_connect_with_fetchall(
            mock_connect,
            [
                ("/api/explorer/search", 240.0, 50),
                ("/chat", 180.5, 30),
            ],
        )

        result = _get_series_breakdown("http_request_duration_ms", "path", "1h")
        assert len(result) == 2
        assert result[0] == {"bucket": "/api/explorer/search", "p95": 240.0, "count": 50}
        assert result[1] == {"bucket": "/chat", "p95": 180.5, "count": 30}

    @patch("app.admin.performance._connect")
    def test_empty_result_returns_empty_list(self, mock_connect: MagicMock):
        from app.admin.performance import _get_series_breakdown

        _mock_connect_with_fetchall(mock_connect, [])

        result = _get_series_breakdown("rag_retrieval_ms", "feature", "1h")
        assert result == []

    @patch("app.admin.performance._connect")
    def test_db_error_returns_empty_list(self, mock_connect: MagicMock):
        from app.admin.performance import _get_series_breakdown

        mock_connect.side_effect = Exception("DB unavailable")

        result = _get_series_breakdown("sparql_query_ms", "operation", "24h")
        assert result == []


# ---------------------------------------------------------------------------
# Backward-compat helpers still work (legacy callers + #545 tests)
# ---------------------------------------------------------------------------


class TestLegacyHelpers:
    @patch("app.admin.performance._connect")
    def test_get_latency_percentiles_default_window(self, mock_connect: MagicMock):
        from app.admin.performance import _get_latency_percentiles

        # Legacy 3-col SELECT preserved for backward-compat — the old
        # ``#545`` tests pass a 3-tuple.
        _mock_connect_with_fetchone(mock_connect, (15.0, 95.0, 210.0))

        result = _get_latency_percentiles()
        assert set(result.keys()) == {"p50", "p95", "p99"}
        assert result["p50"] == 15.0
        assert result["p95"] == 95.0
        assert result["p99"] == 210.0

    @patch("app.admin.performance._connect")
    def test_get_latency_percentiles_window_arg(self, mock_connect: MagicMock):
        from app.admin.performance import _get_latency_percentiles

        mock_conn = _mock_connect_with_fetchone(mock_connect, (1.0, 2.0, 3.0))

        _get_latency_percentiles("7d")
        params = mock_conn.execute.call_args[0][1]
        # Legacy helper passes a 1-tuple ``(interval,)`` — index 0.
        assert params[0] == "7 days"


# ---------------------------------------------------------------------------
# Page handler rendering
# ---------------------------------------------------------------------------


def _make_admin_request(window: str | None = None) -> Request:
    """Build a minimal authenticated admin request."""
    query = b""
    if window is not None:
        query = f"window={window}".encode()
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/admin/performance",
        "headers": [],
        "query_string": query,
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
        "auth": {
            "id": "admin-1",
            "email": "admin@seadusloome.ee",
            "full_name": "Admin",
            "role": "admin",
            "org_id": None,
        },
    }
    return Request(scope)


def _render_page(window: str | None, summaries: dict[str, dict[str, float | int]] | None = None):
    """Render ``admin_performance_page`` with all data helpers mocked.

    ``summaries`` maps series name → percentile dict; missing series default
    to the empty-state summary (count == 0). ``_get_series_breakdown`` is
    always mocked to a fixed two-row sample so populated series can be
    asserted against without exploding the parametrisation surface.
    """
    summaries = summaries or {}

    def fake_summary(name: str, win: str = "1h") -> dict[str, float | int]:
        return summaries.get(name, {"p50": 0.0, "p95": 0.0, "p99": 0.0, "count": 0})

    def fake_breakdown(
        name: str, label_key: str, win: str = "1h", limit: int = 10
    ) -> list[dict[str, object]]:
        return [
            {"bucket": f"sample-{name}-1", "p95": 100.0, "count": 7},
            {"bucket": f"sample-{name}-2", "p95": 50.0, "count": 3},
        ]

    with (
        patch("app.admin.performance._get_series_summary", side_effect=fake_summary),
        patch("app.admin.performance._get_series_breakdown", side_effect=fake_breakdown),
        patch("app.admin.performance._get_slowest_routes", return_value=[]),
    ):
        from app.admin.performance import admin_performance_page

        result = admin_performance_page(_make_admin_request(window))
        return to_xml(result)


_FULL_SUMMARIES = {
    "http_request_duration_ms": {"p50": 12.0, "p95": 87.5, "p99": 200.0, "count": 1234},
    "job_execution_ms": {"p50": 50.0, "p95": 800.0, "p99": 1500.0, "count": 42},
    "llm_call_ms": {"p50": 600.0, "p95": 2200.0, "p99": 4100.0, "count": 17},
    "sparql_query_ms": {"p50": 8.0, "p95": 35.0, "p99": 80.0, "count": 9012},
    "rag_retrieval_ms": {"p50": 110.0, "p95": 280.0, "p99": 410.0, "count": 88},
}


class TestPageRenderingHappyPath:
    def test_all_five_series_render(self):
        html = _render_page(window=None, summaries=_FULL_SUMMARIES)
        # Each series gets a card with a stable id.
        for stub in (
            "series-card-http-request-duration-ms",
            "series-card-job-execution-ms",
            "series-card-llm-call-ms",
            "series-card-sparql-query-ms",
            "series-card-rag-retrieval-ms",
        ):
            assert stub in html, stub

    def test_estonian_section_titles_render(self):
        html = _render_page(window=None, summaries=_FULL_SUMMARIES)
        for title in (
            "HTTP päringud",
            "Taustajobid",
            "LLM kutsed",
            "SPARQL päringud",
            "RAG retrieval",
        ):
            assert title in html, title
        # Page-level chrome.
        assert "Jõudlus" in html
        assert "Ajavahemik:" in html
        # Slow-routes section still rendered.
        assert "Aeglaseimad marsruudid" in html.lower() or "aeglaseimad" in html.lower()

    def test_p50_p95_p99_labels_render(self):
        html = _render_page(window=None, summaries=_FULL_SUMMARIES)
        # Each populated card should carry p50/p95/p99 metric chips.
        assert html.count("p50") >= 5
        assert html.count("p95") >= 5
        assert html.count("p99") >= 5

    def test_window_selector_marks_active_pill(self):
        html = _render_page(window="24h", summaries=_FULL_SUMMARIES)
        # Active pill has the modifier class.
        assert "window-pill window-pill--active" in html
        # The 24h link is present with that modifier.
        assert "Viimased 24 tundi" in html

    def test_window_selector_links_all_three_choices(self):
        html = _render_page(window=None, summaries=_FULL_SUMMARIES)
        assert "?window=1h" in html
        assert "?window=24h" in html
        assert "?window=7d" in html


class TestPageRenderingEmptyStates:
    def test_all_empty_shows_andmeid_pole_everywhere(self):
        """When every series has zero rows the page must still render
        cleanly and show ``Andmeid pole.`` per card — no crashes."""
        html = _render_page(window=None, summaries={})
        # One empty-state line per series card.
        assert html.count("Andmeid pole.") >= 5
        # Page still includes all five cards.
        assert "series-card-http-request-duration-ms" in html
        assert "series-card-rag-retrieval-ms" in html

    def test_partial_empty_one_populated(self):
        """Only one series populated — others render empty-state cleanly."""
        html = _render_page(
            window=None,
            summaries={"llm_call_ms": {"p50": 600.0, "p95": 2200.0, "p99": 4100.0, "count": 17}},
        )
        # 4 of 5 series cards show the empty-state copy.
        assert html.count("Andmeid pole.") >= 4
        # The populated one shows the breakdown bucket name.
        assert "sample-llm_call_ms-1" in html


class TestPageRenderingWindow:
    def test_default_window_is_1h(self):
        html = _render_page(window=None, summaries=_FULL_SUMMARIES)
        # The 1h pill must be the active one when no ?window= is provided.
        assert "window-pill window-pill--active" in html
        assert "Viimane tund" in html

    def test_window_switch_to_24h(self):
        html = _render_page(window="24h", summaries=_FULL_SUMMARIES)
        assert "Viimased 24 tundi" in html
        # 1h label is still rendered as an inactive pill option.
        assert "Viimane tund" in html

    def test_window_switch_to_7d(self):
        html = _render_page(window="7d", summaries=_FULL_SUMMARIES)
        assert "Viimased 7 päeva" in html

    def test_invalid_window_falls_back_to_1h(self):
        html = _render_page(window="garbage", summaries=_FULL_SUMMARIES)
        # The active pill remains 1h ("Viimane tund").
        assert "Viimane tund" in html
        # The 24h/7d pills must NOT be the active one.
        # We assert the active token appears once per page (the 1h pill).
        assert html.count("window-pill--active") == 1


class TestRouteAuth:
    def test_redirects_unauthenticated(self):
        """Route is admin-gated by the shim — no role, no access."""
        from starlette.testclient import TestClient

        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.get("/admin/performance")
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"
