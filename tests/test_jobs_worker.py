"""Unit tests for ``app.jobs.worker``.

These tests never touch a real Postgres connection or spawn a real
worker loop in a thread. The pattern mirrors ``tests/test_jobs_queue.py``:
patch :class:`app.jobs.queue.JobQueue` via ``unittest.mock`` and drive
the worker manually with a ``threading.Event`` that is already set so
``run_forever`` exits after a single iteration.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from app.jobs import worker as worker_module
from app.jobs.queue import Job
from app.jobs.worker import (
    _HANDLERS,
    JobWorker,
    _default_worker_id,
    register_handler,
    start_worker_thread,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_job(job_type: str = "parse_draft", payload: dict | None = None) -> Job:
    """Build a realistic ``Job`` instance for dispatcher tests."""
    now = datetime.now(UTC)
    return Job(
        id=1,
        job_type=job_type,
        payload=payload or {"draft_id": "abc"},
        status="claimed",
        priority=0,
        attempts=0,
        max_attempts=3,
        claimed_by="worker-test",
        claimed_at=now,
        started_at=None,
        finished_at=None,
        error_message=None,
        result=None,
        scheduled_for=now,
        created_at=now,
    )


@pytest.fixture(autouse=True)
def _restore_handlers():
    """Snapshot and restore ``_HANDLERS`` around each test.

    Several tests re-register handlers for the same job_type (or under
    a throwaway name); rolling the registry back keeps the four stub
    handlers registered for the module as a whole.
    """
    snapshot = dict(_HANDLERS)
    try:
        yield
    finally:
        _HANDLERS.clear()
        _HANDLERS.update(snapshot)


# ---------------------------------------------------------------------------
# register_handler
# ---------------------------------------------------------------------------


class TestRegisterHandler:
    def test_register_handler_adds_to_registry(self):
        """@register_handler decorator must populate _HANDLERS."""

        @register_handler("_unit_test_job")
        def my_handler(payload: dict) -> dict:
            return {"ok": True}

        assert "_unit_test_job" in _HANDLERS
        assert _HANDLERS["_unit_test_job"] is my_handler

    def test_register_handler_returns_original_function(self):
        """Decorator must return the wrapped function unchanged."""

        def original(payload: dict) -> dict:
            return {"v": 1}

        wrapped = register_handler("_unit_test_return")(original)
        assert wrapped is original


# ---------------------------------------------------------------------------
# JobWorker dispatch
# ---------------------------------------------------------------------------


class TestWorkerDispatch:
    @patch("app.jobs.worker.JobQueue")
    def test_worker_dispatches_to_handler(self, mock_queue_cls: MagicMock):
        """Claimed job routes to its registered handler and marks success."""
        queue_instance = MagicMock()
        mock_queue_cls.return_value = queue_instance
        job = _make_job(job_type="_dispatch_test", payload={"foo": "bar"})
        # First call returns a job, then stop after the single tick.
        queue_instance.claim_next.return_value = job

        calls: list[dict] = []

        @register_handler("_dispatch_test")
        def handler(payload: dict) -> dict:
            calls.append(payload)
            return {"processed": True}

        stop = threading.Event()
        w = JobWorker(worker_id="test-worker", poll_interval=0.01)

        # Make claim_next return the job exactly once; on the second
        # call, set the stop flag so run_forever exits.
        def claim_side_effect(*_a, **_kw):
            if queue_instance.claim_next.call_count == 1:
                return job
            stop.set()
            return None

        queue_instance.claim_next.side_effect = claim_side_effect
        w.run_forever(stop)

        assert calls == [{"foo": "bar"}]
        queue_instance.mark_running.assert_called_once_with(1)
        queue_instance.mark_success.assert_called_once_with(1, {"processed": True})
        queue_instance.mark_failed.assert_not_called()

    @patch("app.jobs.worker.JobQueue")
    def test_worker_marks_failed_on_exception(self, mock_queue_cls: MagicMock):
        """Handler raising must flip the job to failed with the error string."""
        queue_instance = MagicMock()
        mock_queue_cls.return_value = queue_instance
        job = _make_job(job_type="_fail_test")

        @register_handler("_fail_test")
        def handler(payload: dict) -> dict:
            raise ValueError("kaboom")

        stop = threading.Event()

        def claim_side_effect(*_a, **_kw):
            if queue_instance.claim_next.call_count == 1:
                return job
            stop.set()
            return None

        queue_instance.claim_next.side_effect = claim_side_effect
        JobWorker(worker_id="test-worker", poll_interval=0.01).run_forever(stop)

        queue_instance.mark_running.assert_called_once_with(1)
        queue_instance.mark_failed.assert_called_once()
        args = queue_instance.mark_failed.call_args.args
        assert args[0] == 1
        assert "kaboom" in args[1]
        queue_instance.mark_success.assert_not_called()

    @patch("app.jobs.worker.JobQueue")
    def test_worker_marks_failed_on_unknown_type(self, mock_queue_cls: MagicMock):
        """Unknown job_type must fail cleanly with a helpful message."""
        queue_instance = MagicMock()
        mock_queue_cls.return_value = queue_instance
        job = _make_job(job_type="_not_registered_at_all")
        # Make sure the type really isn't registered.
        _HANDLERS.pop("_not_registered_at_all", None)

        stop = threading.Event()

        def claim_side_effect(*_a, **_kw):
            if queue_instance.claim_next.call_count == 1:
                return job
            stop.set()
            return None

        queue_instance.claim_next.side_effect = claim_side_effect
        JobWorker(worker_id="test-worker", poll_interval=0.01).run_forever(stop)

        queue_instance.mark_failed.assert_called_once()
        args = queue_instance.mark_failed.call_args.args
        assert args[0] == 1
        assert "No handler registered for job type" in args[1]
        assert "_not_registered_at_all" in args[1]
        # No mark_running because we bailed before touching the job.
        queue_instance.mark_running.assert_not_called()
        queue_instance.mark_success.assert_not_called()

    @patch("app.jobs.worker.JobQueue")
    def test_worker_truncates_long_error_messages(self, mock_queue_cls: MagicMock):
        """Handler errors longer than 500 chars must be truncated."""
        queue_instance = MagicMock()
        mock_queue_cls.return_value = queue_instance
        job = _make_job(job_type="_long_err")

        long = "x" * 1000

        @register_handler("_long_err")
        def handler(payload: dict) -> dict:
            raise RuntimeError(long)

        stop = threading.Event()

        def claim_side_effect(*_a, **_kw):
            if queue_instance.claim_next.call_count == 1:
                return job
            stop.set()
            return None

        queue_instance.claim_next.side_effect = claim_side_effect
        JobWorker(worker_id="test-worker", poll_interval=0.01).run_forever(stop)

        args = queue_instance.mark_failed.call_args.args
        assert len(args[1]) == 500


# ---------------------------------------------------------------------------
# Loop control
# ---------------------------------------------------------------------------


class TestRunForever:
    @patch("app.jobs.worker.JobQueue")
    def test_worker_skips_sleep_when_stop_event_set(self, mock_queue_cls: MagicMock):
        """Pre-set stop_event must short-circuit run_forever immediately."""
        queue_instance = MagicMock()
        mock_queue_cls.return_value = queue_instance

        stop = threading.Event()
        stop.set()

        JobWorker(worker_id="test-worker", poll_interval=999.0).run_forever(stop)

        # Loop body never ran because ``while not stop_event.is_set()``
        # bailed on the very first check.
        queue_instance.claim_next.assert_not_called()

    @patch("app.jobs.worker.JobQueue")
    def test_worker_handles_queue_errors_without_crashing(self, mock_queue_cls: MagicMock):
        """A claim_next exception must not propagate out of run_forever."""
        queue_instance = MagicMock()
        mock_queue_cls.return_value = queue_instance

        stop = threading.Event()

        def claim_side_effect(*_a, **_kw):
            if queue_instance.claim_next.call_count == 1:
                raise RuntimeError("db down")
            # Second call: signal shutdown so the loop exits.
            stop.set()
            return None

        queue_instance.claim_next.side_effect = claim_side_effect

        # Must not raise.
        JobWorker(worker_id="test-worker", poll_interval=0.01).run_forever(stop)

        # Two claim attempts: the first raised, the second returned None.
        assert queue_instance.claim_next.call_count == 2

    @patch("app.jobs.worker.JobQueue")
    def test_worker_sleeps_when_queue_empty(self, mock_queue_cls: MagicMock):
        """Empty queue must trigger stop_event.wait, then re-poll."""
        queue_instance = MagicMock()
        mock_queue_cls.return_value = queue_instance
        queue_instance.claim_next.return_value = None

        stop = threading.Event()

        # wait() is what the loop calls to sleep; hijack it to break the
        # loop on the first invocation.
        original_wait = stop.wait
        wait_calls: list[float | None] = []

        def wait_spy(timeout: float | None = None) -> bool:
            wait_calls.append(timeout)
            stop.set()
            return original_wait(0)

        stop.wait = wait_spy  # type: ignore[method-assign]

        JobWorker(worker_id="test-worker", poll_interval=2.0).run_forever(stop)

        assert wait_calls == [2.0]
        queue_instance.claim_next.assert_called_once()


# ---------------------------------------------------------------------------
# Worker id + thread spawn
# ---------------------------------------------------------------------------


class TestWorkerIdentity:
    def test_default_worker_id_has_host_and_suffix(self):
        worker_id = _default_worker_id()
        # ``<host>-<8 hex chars>``
        assert "-" in worker_id
        host, suffix = worker_id.rsplit("-", 1)
        assert len(host) > 0
        assert len(suffix) == 8
        int(suffix, 16)  # must parse as hex

    def test_two_default_ids_differ(self):
        assert _default_worker_id() != _default_worker_id()

    def test_explicit_worker_id_is_respected(self):
        w = JobWorker(worker_id="my-custom-id")
        assert w.worker_id == "my-custom-id"


class TestStartWorkerThread:
    @patch("app.jobs.worker.JobQueue")
    def test_start_worker_thread_returns_daemon_thread(self, mock_queue_cls: MagicMock):
        """Thread must be daemon and stop cleanly when the event fires."""
        queue_instance = MagicMock()
        mock_queue_cls.return_value = queue_instance
        # Make the loop exit immediately so the thread terminates within
        # the test timeout.
        queue_instance.claim_next.return_value = None

        stop = threading.Event()
        stop.set()  # already set so run_forever exits on first check

        thread = start_worker_thread(stop)
        try:
            assert thread.daemon is True
            assert thread.name.startswith("job-worker-")
            thread.join(timeout=2.0)
            assert not thread.is_alive()
        finally:
            stop.set()
            thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Phase 2 stub handlers
# ---------------------------------------------------------------------------


class TestStubHandlers:
    def test_parse_draft_handler_is_real_not_stub(self):
        """``parse_draft`` now points at the real Tika-backed handler.

        Batch 2A replaced the stub with
        :func:`app.docs.parse_handler.parse_draft`; this test guards
        the registry so a future refactor that accidentally drops the
        import fails loudly instead of silently falling back to the
        worker-module fallback.
        """
        # Importing app.docs triggers app.docs.parse_handler's
        # @register_handler side effect.
        import app.docs  # noqa: F401
        from app.docs.parse_handler import parse_draft as real_handler

        assert _HANDLERS["parse_draft"] is real_handler

    def test_extract_entities_handler_is_real_not_stub(self):
        """``extract_entities`` now points at the real LLM-backed handler.

        Batch 2B replaced the stub with
        :func:`app.docs.extract_handler.extract_entities`; this test
        guards the registry so a future refactor that accidentally
        drops the import fails loudly instead of silently falling
        back to the worker-module fallback.
        """
        # Importing app.docs triggers app.docs.extract_handler's
        # @register_handler side effect.
        import app.docs  # noqa: F401
        from app.docs.extract_handler import extract_entities as real_handler

        assert _HANDLERS["extract_entities"] is real_handler

    def test_analyze_impact_stub_returns_stub_dict(self):
        result = worker_module._analyze_impact_stub({"draft_id": "x"})
        assert result == {"status": "stub", "affected": 0, "conflicts": 0}

    def test_export_report_stub_returns_stub_dict(self):
        result = worker_module._export_report_stub({"draft_id": "x"})
        assert result == {"status": "stub"}

    def test_all_four_stubs_are_registered(self):
        """All four Phase 2 job types must be present in _HANDLERS."""
        for job_type in ("parse_draft", "extract_entities", "analyze_impact", "export_report"):
            assert job_type in _HANDLERS
