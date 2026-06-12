"""Tests for app.metrics (record_metric, track_duration) and
app.metrics_middleware (MetricsMiddleware) (#545, #895)."""

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
        m._last_retention_sweep = None
        yield
        m._BUFFER.clear()
        m._flush_suppressed_until = 0.0
        m._flush_in_progress = False
        m._last_retention_sweep = None


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
    # Retention disabled in the INSERT-batching tests so the only commit is the
    # batch commit (retention runs a *second* committed transaction otherwise —
    # exercised separately in TestRetention).
    @patch.dict("os.environ", {"METRICS_RETENTION_DAYS": "0"})
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

    @patch.dict("os.environ", {"METRICS_RETENTION_DAYS": "0"})
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
        # Single commit for the whole batch (retention disabled here).
        mock_conn.commit.assert_called_once()


class TestRetention:
    @patch.dict("os.environ", {"METRICS_RETENTION_DAYS": "7"})
    @patch("app.metrics._connect")
    def test_prune_runs_with_interval_cast(self, mock_connect: MagicMock):
        import app.metrics as m

        # Force the throttle to allow a sweep this flush.
        m._last_retention_sweep = None

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
        # A successful prune engages the throttle (timestamp recorded).
        assert m._last_retention_sweep is not None

    @patch.dict("os.environ", {"METRICS_RETENTION_DAYS": "0"})
    @patch("app.metrics._connect")
    def test_retention_disabled(self, mock_connect: MagicMock):
        import app.metrics as m

        m._last_retention_sweep = None

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
        import app.metrics as m

        # Pin the clock so the test is fully deterministic (no wall-clock /
        # boot-relative dependence): a sweep happened 1s ago, well inside the
        # interval, so the next flush must NOT prune.
        with patch.object(m.time, "monotonic", return_value=10_000.0):
            m._last_retention_sweep = 9_999.0

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
    def test_prune_failure_keeps_rows_and_allows_retry(self, mock_connect: MagicMock):
        """A failed DELETE must not lose the flushed rows nor block the next sweep.

        The metric rows are committed *before* the prune, so even though the
        DELETE raises the INSERT survives; and the sweep claim is rolled back so
        retention is still "due" on the next flush (no hour-long throttle on a
        prune that never happened) — and the flush backoff is NOT engaged.
        """
        import app.metrics as m

        m._last_retention_sweep = None

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        # The INSERT (executemany) succeeds; the retention DELETE raises.
        def execute_side_effect(sql: str, *args: Any):
            if "DELETE FROM metrics" in sql:
                raise RuntimeError("prune boom")
            return MagicMock()

        mock_conn.execute.side_effect = execute_side_effect
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        m.record_metric("ret_metric", 1.0)
        m._flush_buffer()

        # Rows were flushed (executemany ran and the INSERT was committed).
        mock_cursor.executemany.assert_called_once()
        assert mock_conn.commit.call_count >= 1
        # The DELETE was attempted exactly once and failed.
        delete_calls = [
            c for c in mock_conn.execute.call_args_list if "DELETE FROM metrics" in c[0][0]
        ]
        assert len(delete_calls) == 1
        # Claim rolled back -> retention is still due on the next flush.
        assert m._last_retention_sweep is None
        # The aborted DELETE transaction was rolled back so the connection is
        # left reusable (no InFailedSqlTransaction leaking to the with-exit).
        mock_conn.rollback.assert_called_once()
        # A prune failure must NOT trip the flush-failure backoff (the DB and
        # the INSERT were fine), and must NOT re-buffer the flushed rows.
        assert not m._flush_suppressed()
        assert len(m._BUFFER) == 0

    @patch.dict("os.environ", {"METRICS_RETENTION_DAYS": "7"})
    @patch("app.metrics._connect")
    def test_prune_abort_does_not_break_flush_with_realistic_conn(self, mock_connect: MagicMock):
        """End-to-end: a DELETE that aborts the txn must not crash the flush.

        Mimics psycopg: a failed statement poisons the transaction so any later
        command (including the with-exit commit) raises until rollback. The
        flush must roll back the prune and exit cleanly with rows committed.
        """
        import app.db as db
        import app.metrics as m

        m._last_retention_sweep = None

        class _FakeConn:
            def __init__(self) -> None:
                self.aborted = False
                self.committed_batches = 0
                self._cursor = MagicMock()

            def cursor(self) -> MagicMock:
                return self._cursor

            def execute(self, sql: str, *args: Any):
                if "DELETE FROM metrics" in sql:
                    self.aborted = True
                    raise RuntimeError("prune boom")
                return MagicMock()

            def commit(self) -> None:
                if self.aborted:
                    raise RuntimeError("InFailedSqlTransaction")
                self.committed_batches += 1

            def rollback(self) -> None:
                self.aborted = False

            @property
            def closed(self) -> bool:
                return False

        conn = _FakeConn()
        # Wrap in the REAL _PooledConnection so the genuine __exit__ runs: it
        # commits on clean exit, which would raise InFailedSqlTransaction if the
        # aborted prune transaction had not been rolled back by _maybe_prune.
        fake_pool = MagicMock(name="pool")
        mock_connect.return_value = db._PooledConnection(fake_pool, conn)  # type: ignore[arg-type]

        m.record_metric("ret_metric", 1.0)
        m._flush_buffer()  # must not raise

        # INSERT committed once + the with-exit commit (both succeeded because
        # the aborted prune was rolled back); retention is still due; rows were
        # not re-buffered; backoff not engaged; connection returned to the pool.
        assert conn.committed_batches >= 1
        assert conn.aborted is False
        assert m._last_retention_sweep is None
        assert len(m._BUFFER) == 0
        assert not m._flush_suppressed()
        fake_pool.putconn.assert_called_once_with(conn)

    def test_concurrent_flush_attempts_issue_single_prune(self):
        """Two flushes overlapping the prune window issue exactly one DELETE."""
        import threading

        import app.metrics as m

        m._last_retention_sweep = None

        delete_attempts: list[str] = []
        # Gate so the second _maybe_prune runs while the first holds the claim.
        first_in_delete = threading.Event()
        release_first = threading.Event()

        def fake_conn_factory(blocking: bool) -> MagicMock:
            conn = MagicMock()

            def execute(sql: str, *args: Any):
                if "DELETE FROM metrics" in sql:
                    delete_attempts.append(sql)
                    if blocking:
                        first_in_delete.set()
                        release_first.wait(timeout=2)
                return MagicMock()

            conn.execute.side_effect = execute
            return conn

        with patch.dict("os.environ", {"METRICS_RETENTION_DAYS": "7"}):
            errors: list[BaseException] = []

            def run(blocking: bool):
                try:
                    m._maybe_prune(fake_conn_factory(blocking))
                except Exception as exc:  # pragma: no cover - defensive
                    errors.append(exc)

            t1 = threading.Thread(target=run, args=(True,))
            t1.start()
            assert first_in_delete.wait(timeout=2), "first prune never reached DELETE"

            # Second flush runs while the first still holds the claim: it must
            # observe the (just-claimed) sweep timestamp and skip the DELETE.
            run(False)

            release_first.set()
            t1.join(timeout=2)

        assert errors == []
        assert len(delete_attempts) == 1


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

    from app.metrics_middleware import MetricsMiddleware

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
    @patch("app.metrics_middleware.record_metric")
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

    @patch("app.metrics_middleware.record_metric")
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

    @patch("app.metrics_middleware.record_metric")
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

    @patch("app.metrics_middleware.record_metric")
    def test_skips_static_requests(self, mock_record: MagicMock):
        app = _middleware_app(authenticated=True)
        client = TestClient(app, follow_redirects=False)
        client.get("/static/x.css")

        # No http_request_duration_ms should be recorded for static files.
        duration_calls = [
            c for c in mock_record.call_args_list if c[0][0] == "http_request_duration_ms"
        ]
        assert duration_calls == []

    @patch("app.metrics_middleware.record_metric")
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
