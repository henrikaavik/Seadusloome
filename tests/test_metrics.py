"""Tests for app.metrics — record_metric, MetricsMiddleware, track_duration (#545)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

import app.metrics as _metrics

# Captured before any test patches the module attribute, so the dedupe test can
# exercise the genuine implementation despite the autouse no-op patch below.
_REAL_SCHEDULE_FLUSH = _metrics._schedule_flush


@pytest.fixture(autouse=True)
def _no_background_flush():
    """Neutralise the background flusher so buffer tests are hermetic (#877).

    ``record_metric`` schedules a ``threading.Timer`` (or, at ``_FLUSH_SIZE``,
    spawns a flusher thread) that can partially drain the module-global
    ``_BUFFER`` between a test's ``record_metric`` calls and its explicit
    ``_flush_buffer()``. Patching ``_schedule_flush`` to a no-op keeps the
    buffer untouched until the test flushes it deterministically.
    """
    import app.metrics as m

    with patch.object(m, "_schedule_flush", lambda: None):
        m._BUFFER.clear()
        # Reset cross-test flush/retention state so a failure-backoff, an
        # in-flight flag, or a retention sweep from one test can't affect another.
        m._flush_suppressed_until = 0.0
        m._flush_in_progress = False
        m._last_retention_sweep = 0.0
        yield
        m._BUFFER.clear()
        m._flush_suppressed_until = 0.0
        m._flush_in_progress = False
        m._last_retention_sweep = 0.0


def _authenticated_user() -> dict[str, Any]:
    return {
        "id": "uid-1",
        "email": "kasutaja@seadusloome.ee",
        "full_name": "Test Kasutaja",
        "role": "drafter",
        "org_id": None,
        "must_change_password": False,
    }


class TestRecordMetric:
    @patch("app.metrics._connect")
    def test_buffers_then_flushes(self, mock_connect: MagicMock):
        import app.metrics as m

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


class TestRetention:
    @patch.dict("os.environ", {"METRICS_RETENTION_DAYS": "7"})
    @patch("app.metrics._connect")
    def test_prune_runs_with_interval_cast(self, mock_connect: MagicMock):
        import app.metrics as m

        # Force the throttle to allow a sweep this flush.
        m._last_retention_sweep = 0.0

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        m.record_metric("ret_metric", 1.0)
        m._flush_buffer()

        # The DELETE must be issued on the flush connection, and must bind the
        # window as text cast with ``%s::interval`` (never ``interval %s``).
        delete_calls = [
            c for c in mock_conn.execute.call_args_list if "DELETE FROM metrics" in c[0][0]
        ]
        assert len(delete_calls) == 1
        sql, params = delete_calls[0][0]
        assert "%s::interval" in sql
        assert "interval %s" not in sql
        assert params == ("7 days",)

    @patch.dict("os.environ", {"METRICS_RETENTION_DAYS": "0"})
    @patch("app.metrics._connect")
    def test_retention_disabled(self, mock_connect: MagicMock):
        import app.metrics as m

        m._last_retention_sweep = 0.0

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        m.record_metric("ret_metric", 1.0)
        m._flush_buffer()

        delete_calls = [
            c for c in mock_conn.execute.call_args_list if "DELETE FROM metrics" in c[0][0]
        ]
        assert delete_calls == []

    @patch.dict("os.environ", {"METRICS_RETENTION_DAYS": "7"})
    @patch("app.metrics._connect")
    def test_retention_throttled_to_one_sweep_per_interval(self, mock_connect: MagicMock):
        import time

        import app.metrics as m

        # Pretend a sweep just happened: the next flush must NOT prune.
        m._last_retention_sweep = time.monotonic()

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        m.record_metric("ret_metric", 1.0)
        m._flush_buffer()

        delete_calls = [
            c for c in mock_conn.execute.call_args_list if "DELETE FROM metrics" in c[0][0]
        ]
        assert delete_calls == []


class TestScheduleFlush:
    def test_check_and_set_under_lock_starts_one_timer(self):
        """Two _schedule_flush calls must start exactly one timer (#861).

        The check-and-set of ``_flush_timer`` lives under ``_lock``; with a
        fake Timer we confirm the second call observes the pending timer and
        does not start a duplicate.
        """
        import app.metrics as m

        started: list[Any] = []

        class _FakeTimer:
            def __init__(self, *_a: Any, **_k: Any) -> None:
                self.daemon = False

            def start(self) -> None:
                started.append(self)

            def cancel(self) -> None:
                pass

        m._flush_timer = None
        try:
            with patch("app.metrics.threading.Timer", _FakeTimer):
                # Call the real implementation (the autouse fixture patches the
                # module attribute, but _REAL_SCHEDULE_FLUSH is the unpatched
                # function object captured at import time).
                _REAL_SCHEDULE_FLUSH()
                _REAL_SCHEDULE_FLUSH()
            assert len(started) == 1
        finally:
            m._flush_timer = None


def _middleware_app(*, authenticated: bool):
    """Minimal Starlette app wrapping the real MetricsMiddleware.

    Keeps the middleware logic (route-template extraction, auth gate, 404/static
    skips) under test without dragging in the production app's DB-backed
    handlers. A tiny beforeware-equivalent sets ``scope['auth']`` so the
    authenticated path can be exercised deterministically.
    """
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    from app.metrics import MetricsMiddleware

    async def item(request: Any) -> PlainTextResponse:
        return PlainTextResponse("ok")

    class _AuthScope:
        """Inner ASGI app (runs downstream of MetricsMiddleware) that stamps
        ``scope['auth']`` just like the real Beforeware does."""

        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            if scope.get("type") == "http" and authenticated:
                scope["auth"] = _authenticated_user()
            await self.app(scope, receive, send)

    app = Starlette(
        routes=[
            Route("/drafts/{draft_id}", item),
            Route("/static/x.css", item),
        ]
    )
    app.add_middleware(_AuthScope)
    app.add_middleware(MetricsMiddleware)
    return app


class TestMetricsMiddleware:
    @patch("app.metrics.record_metric")
    def test_records_route_template_for_authenticated_request(self, mock_record: MagicMock):
        app = _middleware_app(authenticated=True)
        client = TestClient(app, follow_redirects=False)
        response = client.get("/drafts/abc-123")
        assert response.status_code == 200

        calls = [c for c in mock_record.call_args_list if c[0][0] == "http_request_duration_ms"]
        assert len(calls) == 1
        metric_call = calls[0]
        assert isinstance(metric_call[0][1], float)
        labels = metric_call[0][2]
        assert labels["method"] == "GET"
        # The matched route TEMPLATE is recorded, not the raw attacker path.
        assert labels["route"] == "/drafts/{draft_id}"
        assert "path" not in labels
        assert labels["status"] == 200

    @patch("app.metrics.record_metric")
    def test_skips_unauthenticated_requests(self, mock_record: MagicMock):
        app = _middleware_app(authenticated=False)
        client = TestClient(app, follow_redirects=False)
        response = client.get("/drafts/abc-123")
        assert response.status_code == 200

        # A matched route but no auth in scope → no metric recorded.
        duration_calls = [
            c for c in mock_record.call_args_list if c[0][0] == "http_request_duration_ms"
        ]
        assert duration_calls == []

    @patch("app.metrics.record_metric")
    def test_skips_404_for_authenticated_user(self, mock_record: MagicMock):
        app = _middleware_app(authenticated=True)
        client = TestClient(app, follow_redirects=False)
        response = client.get("/this/path/does/not/exist/" + "x" * 50)
        assert response.status_code == 404

        # An unrouted (attacker-chosen) path must never write a metric row even
        # though the request is authenticated.
        duration_calls = [
            c for c in mock_record.call_args_list if c[0][0] == "http_request_duration_ms"
        ]
        assert duration_calls == []

    @patch("app.metrics.record_metric")
    def test_skips_static_requests(self, mock_record: MagicMock):
        app = _middleware_app(authenticated=True)
        client = TestClient(app, follow_redirects=False)
        client.get("/static/x.css")

        # No http_request_duration_ms should be recorded for static files.
        duration_calls = [
            c for c in mock_record.call_args_list if c[0][0] == "http_request_duration_ms"
        ]
        assert duration_calls == []

    @patch("app.metrics.record_metric")
    def test_real_app_skips_unauthenticated_ping(self, mock_record: MagicMock):
        """Integration: /api/ping (unauthenticated, DB-free) is not recorded."""
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        response = client.get("/api/ping")
        assert response.status_code == 200

        duration_calls = [
            c for c in mock_record.call_args_list if c[0][0] == "http_request_duration_ms"
        ]
        assert duration_calls == []


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
