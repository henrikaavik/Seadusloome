"""Tests for the admin job monitor page (#543 + #188 polish).

Covers: status counts, type breakdown, recent failed jobs, retry action,
purge action, page rendering, route-level auth, the new filterable +
paginated jobs table (#188), per-handler 24h stats card (with and
without metrics rows), and the HTMX-loaded detail fragment.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import MagicMock, patch

from starlette.requests import Request
from starlette.testclient import TestClient


def _make_request(
    path: str = "/admin/jobs",
    method: str = "GET",
    query_string: str = "",
) -> Request:
    """Build a minimal Starlette Request for unit testing."""
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [],
        "query_string": query_string.encode(),
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
# Filter parsing + WHERE clause builder (#188)
# ---------------------------------------------------------------------------


class TestParseDateHelper:
    def test_valid_date(self):
        from app.admin.job_monitor import _parse_date

        assert _parse_date("2025-03-15") == date(2025, 3, 15)

    def test_invalid_returns_none(self):
        from app.admin.job_monitor import _parse_date

        assert _parse_date("not-a-date") is None

    def test_empty_returns_none(self):
        from app.admin.job_monitor import _parse_date

        assert _parse_date("") is None
        assert _parse_date(None) is None


class TestNormaliseStatus:
    def test_valid_status_passes_through(self):
        from app.admin.job_monitor import _normalise_status

        assert _normalise_status("pending") == "pending"
        assert _normalise_status("running") == "running"
        assert _normalise_status("failed") == "failed"
        assert _normalise_status("success") == "success"

    def test_completed_alias_maps_to_success(self):
        from app.admin.job_monitor import _normalise_status

        assert _normalise_status("completed") == "success"

    def test_unknown_returns_none(self):
        from app.admin.job_monitor import _normalise_status

        assert _normalise_status("garbage") is None
        assert _normalise_status("") is None
        assert _normalise_status(None) is None


class TestBuildJobsWhere:
    def test_no_filters_returns_true(self):
        from app.admin.job_monitor import _build_jobs_where

        where, params = _build_jobs_where()
        assert where == "TRUE"
        assert params == []

    def test_handler_uses_any(self):
        from app.admin.job_monitor import _build_jobs_where

        where, params = _build_jobs_where(handlers=["parse_draft", "extract_entities"])
        assert "job_type = ANY(%s)" in where
        assert params == [["parse_draft", "extract_entities"]]

    def test_status_filter(self):
        from app.admin.job_monitor import _build_jobs_where

        where, params = _build_jobs_where(status="failed")
        assert "status = %s" in where
        assert "failed" in params

    def test_date_range_filter(self):
        from app.admin.job_monitor import _build_jobs_where

        where, params = _build_jobs_where(date_from=date(2025, 1, 1), date_to=date(2025, 1, 31))
        assert "started_at >= %s" in where
        assert "started_at < %s" in where
        assert len(params) == 2

    def test_combined_filters(self):
        from app.admin.job_monitor import _build_jobs_where

        where, params = _build_jobs_where(
            handlers=["parse_draft"],
            status="failed",
            date_from=date(2025, 6, 1),
            date_to=date(2025, 6, 30),
        )
        assert "job_type = ANY(%s)" in where
        assert "status = %s" in where
        assert "started_at >= %s" in where
        assert "started_at < %s" in where
        assert len(params) == 4


class TestExtractFilters:
    def test_multi_select_handler(self):
        from app.admin.job_monitor import _extract_filters

        req = _make_request(query_string="handler=parse_draft&handler=extract_entities")
        filters = _extract_filters(req)
        assert filters["handlers"] == ["parse_draft", "extract_entities"]

    def test_status_and_dates(self):
        from app.admin.job_monitor import _extract_filters

        req = _make_request(query_string="status=failed&from=2025-01-01&to=2025-01-31")
        filters = _extract_filters(req)
        assert filters["status"] == "failed"
        assert filters["from"] == "2025-01-01"
        assert filters["to"] == "2025-01-31"

    def test_empty_query_string(self):
        from app.admin.job_monitor import _extract_filters

        req = _make_request()
        filters = _extract_filters(req)
        assert filters["handlers"] == []
        assert filters["status"] == ""


class TestFilterQueryString:
    def test_round_trip_handler_status(self):
        from app.admin.job_monitor import _filter_query_string

        qs = _filter_query_string(
            {
                "handlers": ["parse_draft", "extract_entities"],
                "status": "failed",
                "from": "",
                "to": "",
            }
        )
        assert "handler=parse_draft" in qs
        assert "handler=extract_entities" in qs
        assert "status=failed" in qs
        # No page key — Pagination owns that.
        assert "page=" not in qs

    def test_skips_empty_values(self):
        from app.admin.job_monitor import _filter_query_string

        qs = _filter_query_string({"handlers": [], "status": "", "from": "", "to": ""})
        assert qs == ""


# ---------------------------------------------------------------------------
# Filtered jobs page DB helper (#188)
# ---------------------------------------------------------------------------


class TestGetFilteredJobsPage:
    @patch("app.admin.job_monitor._connect")
    def test_returns_jobs_and_total(self, mock_connect: MagicMock):
        from app.admin.job_monitor import _get_filtered_jobs_page

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        now = datetime(2025, 6, 15, 14, 30, tzinfo=UTC)
        # Two execute calls: count + page rows.
        mock_conn.execute.return_value.fetchone.return_value = (42,)
        mock_conn.execute.return_value.fetchall.return_value = [
            (1, "parse_draft", "failed", "boom", 3, 3, now, now, now),
            (2, "extract_entities", "success", None, 1, 3, now, now, now),
        ]

        jobs, total = _get_filtered_jobs_page(
            page=1,
            per_page=20,
            handlers=["parse_draft", "extract_entities"],
            status="failed",
            date_from=date(2025, 6, 1),
            date_to=date(2025, 6, 30),
        )
        assert total == 42
        assert len(jobs) == 2
        assert jobs[0]["id"] == 1
        assert jobs[0]["status"] == "failed"
        # error_message defaults to '' for nulls.
        assert jobs[1]["error_message"] == ""

    @patch("app.admin.job_monitor._connect")
    def test_returns_empty_on_error(self, mock_connect: MagicMock):
        from app.admin.job_monitor import _get_filtered_jobs_page

        mock_connect.side_effect = Exception("DB down")
        jobs, total = _get_filtered_jobs_page()
        assert jobs == []
        assert total == 0


# ---------------------------------------------------------------------------
# Per-handler 24h aggregate stats (#188)
# ---------------------------------------------------------------------------


class TestGetHandlerStats24h:
    @patch("app.admin.job_monitor._connect")
    def test_aggregates_per_handler(self, mock_connect: MagicMock):
        from app.admin.job_monitor import _get_handler_stats_24h

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = [
            ("parse_draft", 10, 0.9, 1500.0),
            ("extract_entities", 5, 1.0, 800.0),
        ]

        stats = _get_handler_stats_24h()
        assert len(stats) == 2
        parse = next(s for s in stats if s["handler"] == "parse_draft")
        assert parse["count"] == 10
        assert parse["success_rate"] == 0.9
        assert parse["p95_ms"] == 1500.0

        # SQL must use the metrics table with the correct metric name.
        sql = mock_conn.execute.call_args[0][0]
        assert "FROM metrics" in sql
        assert "name = 'job_execution_ms'" in sql
        assert "percentile_cont(0.95)" in sql

    @patch("app.admin.job_monitor._connect")
    def test_returns_empty_when_no_rows(self, mock_connect: MagicMock):
        from app.admin.job_monitor import _get_handler_stats_24h

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = []

        stats = _get_handler_stats_24h()
        assert stats == []

    @patch("app.admin.job_monitor._connect")
    def test_returns_empty_on_error(self, mock_connect: MagicMock):
        from app.admin.job_monitor import _get_handler_stats_24h

        mock_connect.side_effect = Exception("DB down")
        stats = _get_handler_stats_24h()
        assert stats == []


# ---------------------------------------------------------------------------
# Page rendering
# ---------------------------------------------------------------------------


class TestJobMonitorPageRendering:
    @patch("app.admin.job_monitor._get_handler_stats_24h")
    @patch("app.admin.job_monitor._get_filtered_jobs_page")
    @patch("app.admin.job_monitor._get_type_breakdown")
    @patch("app.admin.job_monitor._get_status_counts")
    def test_page_renders_summary_badges(
        self,
        mock_counts: MagicMock,
        mock_breakdown: MagicMock,
        mock_jobs: MagicMock,
        mock_stats: MagicMock,
    ):
        from fasthtml.common import to_xml

        from app.admin.job_monitor import admin_jobs_page

        mock_counts.return_value = {"pending": 5, "running": 2, "failed": 3, "success": 100}
        mock_breakdown.return_value = []
        mock_jobs.return_value = ([], 0)
        mock_stats.return_value = []

        req = _make_request()
        result = admin_jobs_page(req)

        html = to_xml(result)
        assert "5 ootel" in html
        assert "2 töötab" in html
        assert "3 ebaõnnestunud" in html
        assert "100 õnnestunud" in html
        assert "Tööde monitor" in html

    @patch("app.admin.job_monitor._get_handler_stats_24h")
    @patch("app.admin.job_monitor._get_filtered_jobs_page")
    @patch("app.admin.job_monitor._get_type_breakdown")
    @patch("app.admin.job_monitor._get_status_counts")
    def test_page_renders_type_breakdown(
        self,
        mock_counts: MagicMock,
        mock_breakdown: MagicMock,
        mock_jobs: MagicMock,
        mock_stats: MagicMock,
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
        mock_jobs.return_value = ([], 0)
        mock_stats.return_value = []

        req = _make_request()
        result = admin_jobs_page(req)

        html = to_xml(result)
        assert "parse_draft" in html
        assert "Tööde tüübi kaupa" in html

    @patch("app.admin.job_monitor._get_handler_stats_24h")
    @patch("app.admin.job_monitor._get_filtered_jobs_page")
    @patch("app.admin.job_monitor._get_type_breakdown")
    @patch("app.admin.job_monitor._get_status_counts")
    def test_page_renders_failed_jobs_with_retry(
        self,
        mock_counts: MagicMock,
        mock_breakdown: MagicMock,
        mock_jobs: MagicMock,
        mock_stats: MagicMock,
    ):
        from fasthtml.common import to_xml

        from app.admin.job_monitor import admin_jobs_page

        mock_counts.return_value = {"pending": 0, "running": 0, "failed": 1, "success": 0}
        mock_breakdown.return_value = []
        mock_stats.return_value = []
        now = datetime(2025, 6, 15, 14, 30, tzinfo=UTC)
        mock_jobs.return_value = (
            [
                {
                    "id": 42,
                    "job_type": "parse_draft",
                    "status": "failed",
                    "error_message": "Connection timeout",
                    "attempts": 3,
                    "max_attempts": 3,
                    "finished_at": now,
                    "started_at": now,
                    "created_at": now,
                }
            ],
            1,
        )

        req = _make_request()
        result = admin_jobs_page(req)

        html = to_xml(result)
        assert "Connection timeout" in html
        assert "parse_draft" in html
        assert "Proovi uuesti" in html
        assert "/admin/jobs/42/retry" in html

    @patch("app.admin.job_monitor._get_handler_stats_24h")
    @patch("app.admin.job_monitor._get_filtered_jobs_page")
    @patch("app.admin.job_monitor._get_type_breakdown")
    @patch("app.admin.job_monitor._get_status_counts")
    def test_page_renders_purge_button(
        self,
        mock_counts: MagicMock,
        mock_breakdown: MagicMock,
        mock_jobs: MagicMock,
        mock_stats: MagicMock,
    ):
        from fasthtml.common import to_xml

        from app.admin.job_monitor import admin_jobs_page

        mock_counts.return_value = {"pending": 0, "running": 0, "failed": 0, "success": 0}
        mock_breakdown.return_value = []
        mock_jobs.return_value = ([], 0)
        mock_stats.return_value = []

        req = _make_request()
        result = admin_jobs_page(req)

        html = to_xml(result)
        assert "Puhasta vanad tööd" in html
        assert "/admin/jobs/purge" in html


# ---------------------------------------------------------------------------
# Filtering + pagination (#188)
# ---------------------------------------------------------------------------


class TestJobMonitorFiltering:
    @patch("app.admin.job_monitor._get_handler_stats_24h")
    @patch("app.admin.job_monitor._get_filtered_jobs_page")
    @patch("app.admin.job_monitor._get_type_breakdown")
    @patch("app.admin.job_monitor._get_status_counts")
    def test_filters_propagate_to_query(
        self,
        mock_counts: MagicMock,
        mock_breakdown: MagicMock,
        mock_jobs: MagicMock,
        mock_stats: MagicMock,
    ):
        """Multi-select handler + status + date range reach the DB helper."""
        from app.admin.job_monitor import admin_jobs_page

        mock_counts.return_value = {"pending": 0, "running": 0, "failed": 0, "success": 0}
        mock_breakdown.return_value = []
        mock_jobs.return_value = ([], 0)
        mock_stats.return_value = []

        qs = (
            "handler=parse_draft&handler=extract_entities"
            "&status=failed&from=2025-06-01&to=2025-06-30"
        )
        req = _make_request(query_string=qs)
        admin_jobs_page(req)

        call = mock_jobs.call_args
        assert call.kwargs["handlers"] == ["parse_draft", "extract_entities"]
        assert call.kwargs["status"] == "failed"
        assert call.kwargs["date_from"] == date(2025, 6, 1)
        assert call.kwargs["date_to"] == date(2025, 6, 30)

    @patch("app.admin.job_monitor._get_handler_stats_24h")
    @patch("app.admin.job_monitor._get_filtered_jobs_page")
    @patch("app.admin.job_monitor._get_type_breakdown")
    @patch("app.admin.job_monitor._get_status_counts")
    def test_completed_alias_normalises_to_success(
        self,
        mock_counts: MagicMock,
        mock_breakdown: MagicMock,
        mock_jobs: MagicMock,
        mock_stats: MagicMock,
    ):
        from app.admin.job_monitor import admin_jobs_page

        mock_counts.return_value = {"pending": 0, "running": 0, "failed": 0, "success": 0}
        mock_breakdown.return_value = []
        mock_jobs.return_value = ([], 0)
        mock_stats.return_value = []

        req = _make_request(query_string="status=completed")
        admin_jobs_page(req)
        assert mock_jobs.call_args.kwargs["status"] == "success"

    @patch("app.admin.job_monitor._get_handler_stats_24h")
    @patch("app.admin.job_monitor._get_filtered_jobs_page")
    @patch("app.admin.job_monitor._get_type_breakdown")
    @patch("app.admin.job_monitor._get_status_counts")
    def test_pagination_page_param_persists_filters(
        self,
        mock_counts: MagicMock,
        mock_breakdown: MagicMock,
        mock_jobs: MagicMock,
        mock_stats: MagicMock,
    ):
        """?page=3 reaches the DB helper AND filters survive in pagination links."""
        from fasthtml.common import to_xml

        from app.admin.job_monitor import admin_jobs_page

        mock_counts.return_value = {"pending": 0, "running": 0, "failed": 0, "success": 0}
        mock_breakdown.return_value = []
        # 65 total -> 4 pages at 20/page; we request page 3.
        mock_jobs.return_value = ([], 65)
        mock_stats.return_value = []

        req = _make_request(query_string="handler=parse_draft&status=failed&page=3")
        result = admin_jobs_page(req)

        assert mock_jobs.call_args.kwargs["page"] == 3
        html = to_xml(result)
        # The pagination links must carry the active filters forward so
        # paging back/forward keeps the filtered view.
        assert "handler=parse_draft" in html
        assert "status=failed" in html
        # And the pagination wrapper is present.
        assert "pagination" in html

    @patch("app.admin.job_monitor._get_handler_stats_24h")
    @patch("app.admin.job_monitor._get_filtered_jobs_page")
    @patch("app.admin.job_monitor._get_type_breakdown")
    @patch("app.admin.job_monitor._get_status_counts")
    def test_invalid_page_defaults_to_one(
        self,
        mock_counts: MagicMock,
        mock_breakdown: MagicMock,
        mock_jobs: MagicMock,
        mock_stats: MagicMock,
    ):
        from app.admin.job_monitor import admin_jobs_page

        mock_counts.return_value = {"pending": 0, "running": 0, "failed": 0, "success": 0}
        mock_breakdown.return_value = []
        mock_jobs.return_value = ([], 0)
        mock_stats.return_value = []

        req = _make_request(query_string="page=garbage")
        admin_jobs_page(req)
        assert mock_jobs.call_args.kwargs["page"] == 1


# ---------------------------------------------------------------------------
# Per-handler stats card rendering (#188)
# ---------------------------------------------------------------------------


class TestHandlerStatsCard:
    @patch("app.admin.job_monitor._get_handler_stats_24h")
    @patch("app.admin.job_monitor._get_filtered_jobs_page")
    @patch("app.admin.job_monitor._get_type_breakdown")
    @patch("app.admin.job_monitor._get_status_counts")
    def test_empty_state_when_no_metrics(
        self,
        mock_counts: MagicMock,
        mock_breakdown: MagicMock,
        mock_jobs: MagicMock,
        mock_stats: MagicMock,
    ):
        from fasthtml.common import to_xml

        from app.admin.job_monitor import admin_jobs_page

        mock_counts.return_value = {"pending": 0, "running": 0, "failed": 0, "success": 0}
        mock_breakdown.return_value = []
        mock_jobs.return_value = ([], 0)
        mock_stats.return_value = []

        req = _make_request()
        result = admin_jobs_page(req)

        html = to_xml(result)
        assert "Aktiivsuse statistikat pole veel kogutud" in html
        assert "Töötlejate statistika (viimased 24h)" in html

    @patch("app.admin.job_monitor._get_handler_stats_24h")
    @patch("app.admin.job_monitor._get_filtered_jobs_page")
    @patch("app.admin.job_monitor._get_type_breakdown")
    @patch("app.admin.job_monitor._get_status_counts")
    def test_renders_metrics_rows(
        self,
        mock_counts: MagicMock,
        mock_breakdown: MagicMock,
        mock_jobs: MagicMock,
        mock_stats: MagicMock,
    ):
        from fasthtml.common import to_xml

        from app.admin.job_monitor import admin_jobs_page

        mock_counts.return_value = {"pending": 0, "running": 0, "failed": 0, "success": 0}
        mock_breakdown.return_value = []
        mock_jobs.return_value = ([], 0)
        mock_stats.return_value = [
            {
                "handler": "parse_draft",
                "count": 42,
                "success_rate": 0.95,
                "p95_ms": 1234.0,
            }
        ]

        req = _make_request()
        result = admin_jobs_page(req)

        html = to_xml(result)
        assert "parse_draft" in html
        assert "42" in html
        assert "95.0%" in html
        assert "1234 ms" in html
        # Empty-state copy must NOT appear when we have rows.
        assert "Aktiivsuse statistikat pole veel kogutud" not in html


# ---------------------------------------------------------------------------
# Detail expand fragment (#188)
# ---------------------------------------------------------------------------


class TestAdminJobDetailFragment:
    @patch("app.admin.job_monitor._get_job_detail")
    def test_detail_renders_payload_and_history(self, mock_get: MagicMock):
        from fasthtml.common import to_xml

        from app.admin.job_monitor import admin_job_detail

        now = datetime(2025, 6, 15, 14, 30, tzinfo=UTC)
        mock_get.return_value = {
            "id": 7,
            "job_type": "parse_draft",
            "status": "failed",
            "payload": {"draft_id": "d-1", "owner": "u-2"},
            "error_message": "Traceback (most recent call last):\n  KeyError: 'doc'",
            "attempts": 3,
            "max_attempts": 3,
            "started_at": now,
            "finished_at": now,
            "created_at": now,
            "scheduled_for": now,
            "claimed_by": "worker-1",
            "claimed_at": now,
            "result": None,
        }

        req = _make_request("/admin/jobs/7/detail")
        result = admin_job_detail(req, id=7)
        html = to_xml(result)

        # Payload pretty-printed
        assert '"draft_id"' in html
        assert "d-1" in html
        # Estonian section headings
        assert "Päringu sisu" in html
        assert "Katsete ajalugu" in html
        assert "Veateade" in html
        # Traceback / error appears
        assert "KeyError" in html
        # Worker info shows up in the history
        assert "worker-1" in html

    @patch("app.admin.job_monitor._get_job_detail")
    def test_detail_missing_row(self, mock_get: MagicMock):
        from fasthtml.common import to_xml

        from app.admin.job_monitor import admin_job_detail

        mock_get.return_value = None
        req = _make_request("/admin/jobs/999/detail")
        result = admin_job_detail(req, id=999)
        html = to_xml(result)
        assert "Tööd ei leitud" in html

    @patch("app.admin.job_monitor._get_job_detail")
    def test_detail_handles_internal_error(self, mock_get: MagicMock):
        from fasthtml.common import to_xml

        from app.admin.job_monitor import admin_job_detail

        mock_get.side_effect = Exception("DB went away")
        req = _make_request("/admin/jobs/1/detail")
        result = admin_job_detail(req, id=1)
        html = to_xml(result)
        assert "Detailide laadimine ebaõnnestus" in html


# ---------------------------------------------------------------------------
# Retry handler
# ---------------------------------------------------------------------------


class TestAdminJobRetryHandler:
    @patch("app.admin.job_monitor._get_handler_stats_24h")
    @patch("app.admin.job_monitor._get_filtered_jobs_page")
    @patch("app.admin.job_monitor._get_type_breakdown")
    @patch("app.admin.job_monitor._get_status_counts")
    @patch("app.admin.job_monitor._retry_job")
    def test_retry_handler_success(
        self,
        mock_retry: MagicMock,
        mock_counts: MagicMock,
        mock_breakdown: MagicMock,
        mock_jobs: MagicMock,
        mock_stats: MagicMock,
    ):
        from fasthtml.common import to_xml

        from app.admin.job_monitor import admin_job_retry

        mock_retry.return_value = True
        mock_counts.return_value = {"pending": 0, "running": 0, "failed": 0, "success": 0}
        mock_breakdown.return_value = []
        mock_jobs.return_value = ([], 0)
        mock_stats.return_value = []

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
    @patch("app.admin.job_monitor._get_handler_stats_24h")
    @patch("app.admin.job_monitor._get_filtered_jobs_page")
    @patch("app.admin.job_monitor._get_type_breakdown")
    @patch("app.admin.job_monitor._get_status_counts")
    @patch("app.admin.job_monitor._purge_completed")
    def test_purge_handler_returns_refreshed_content(
        self,
        mock_purge: MagicMock,
        mock_counts: MagicMock,
        mock_breakdown: MagicMock,
        mock_jobs: MagicMock,
        mock_stats: MagicMock,
    ):
        from fasthtml.common import to_xml

        from app.admin.job_monitor import admin_jobs_purge

        mock_purge.return_value = 10
        mock_counts.return_value = {"pending": 0, "running": 0, "failed": 0, "success": 0}
        mock_breakdown.return_value = []
        mock_jobs.return_value = ([], 0)
        mock_stats.return_value = []

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

    def test_job_detail_redirects_unauthenticated(self):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.get("/admin/jobs/42/detail")
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"
