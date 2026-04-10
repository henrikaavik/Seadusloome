"""Tests for app.metrics — record_metric, MetricsMiddleware, track_duration (#545)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient


class TestRecordMetric:
    @patch("app.metrics._connect")
    def test_buffers_then_flushes(self, mock_connect: MagicMock):
        import app.metrics as m

        # Clear any entries buffered by prior tests
        m._BUFFER.clear()

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        m.record_metric("test_metric", 42.5, {"route": "/api/test"})

        # Not yet flushed — still in buffer
        mock_cursor.executemany.assert_not_called()

        # Force flush
        m._flush_buffer()

        mock_cursor.executemany.assert_called_once()
        args = mock_cursor.executemany.call_args[0]
        assert "INSERT INTO metrics" in args[0]
        rows = args[1]
        assert len(rows) == 1
        assert rows[0][0] == "test_metric"
        assert rows[0][1] == 42.5
        assert '"route"' in rows[0][2]
        mock_conn.commit.assert_called_once()

    @patch("app.metrics._connect")
    def test_buffers_null_labels(self, mock_connect: MagicMock):
        import app.metrics as m

        m._BUFFER.clear()

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        m.record_metric("simple_metric", 1.0)
        m._flush_buffer()

        rows = mock_cursor.executemany.call_args[0][1]
        assert rows[0][2] is None

    @patch("app.metrics._connect")
    def test_swallows_db_errors(self, mock_connect: MagicMock):
        import app.metrics as m

        mock_connect.side_effect = Exception("DB down")
        m.record_metric("broken", 0)
        # Flush should not raise even though the DB is down
        m._flush_buffer()

    @patch("app.metrics._connect")
    def test_bulk_flushes_multiple_rows(self, mock_connect: MagicMock):
        import app.metrics as m

        m._BUFFER.clear()

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        m.record_metric("m1", 1.0)
        m.record_metric("m2", 2.0, {"k": "v"})
        m.record_metric("m3", 3.0)
        m._flush_buffer()

        rows = mock_cursor.executemany.call_args[0][1]
        assert len(rows) == 3
        assert rows[0][0] == "m1"
        assert rows[1][0] == "m2"
        assert rows[2][0] == "m3"
        # Single commit for the whole batch
        mock_conn.commit.assert_called_once()


class TestMetricsMiddleware:
    @patch("app.metrics.record_metric")
    def test_records_request_duration(self, mock_record: MagicMock):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        # /api/ping is unauthenticated
        response = client.get("/api/ping")
        assert response.status_code == 200

        # The middleware should have recorded at least one metric
        calls = [c for c in mock_record.call_args_list if c[0][0] == "http_request_duration_ms"]
        assert len(calls) >= 1
        metric_call = calls[0]
        assert metric_call[0][0] == "http_request_duration_ms"
        assert isinstance(metric_call[0][1], float)
        labels = metric_call[0][2]
        assert labels["method"] == "GET"
        assert labels["path"] == "/api/ping"
        assert labels["status"] == 200

    @patch("app.metrics.record_metric")
    def test_skips_static_requests(self, mock_record: MagicMock):
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        client.get("/static/css/tokens.css")

        # No http_request_duration_ms should be recorded for static files
        duration_calls = [
            c for c in mock_record.call_args_list if c[0][0] == "http_request_duration_ms"
        ]
        static_calls = [
            c
            for c in duration_calls
            if isinstance(c[0][2], dict) and c[0][2].get("path", "").startswith("/static/")
        ]
        assert len(static_calls) == 0


class TestTrackDuration:
    @patch("app.metrics.record_metric")
    def test_measures_block(self, mock_record: MagicMock):
        from app.metrics import track_duration

        with track_duration("test_block_ms", query_type="sparql"):
            total = sum(range(1000))  # noqa: F841

        mock_record.assert_called_once()
        args = mock_record.call_args[0]
        assert args[0] == "test_block_ms"
        assert isinstance(args[1], float)
        assert args[1] >= 0
        assert args[2] == {"query_type": "sparql"}

    @patch("app.metrics.record_metric")
    def test_records_even_on_exception(self, mock_record: MagicMock):
        from app.metrics import track_duration

        try:
            with track_duration("failing_block_ms"):
                raise ValueError("boom")
        except ValueError:
            pass

        mock_record.assert_called_once()
        assert mock_record.call_args[0][0] == "failing_block_ms"

    @patch("app.metrics.record_metric")
    def test_no_labels(self, mock_record: MagicMock):
        from app.metrics import track_duration

        with track_duration("bare_metric"):
            pass

        args = mock_record.call_args[0]
        assert args[0] == "bare_metric"
        assert args[2] is None
