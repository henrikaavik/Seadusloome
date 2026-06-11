"""Tests for the orphaned-job reaper (#852 E1).

Covers :meth:`app.jobs.queue.JobQueue.reap_stale_jobs` (retry-policy
accounting, batch/locking SQL shape, domain-row consequence for draft
jobs), the crash-simulation round-trip (claimed row + dead worker →
recovered and re-claimable), and the worker-loop wiring (startup pass,
interval gating, metric emission, failure isolation).

Mocking mirrors ``tests/test_jobs_queue.py`` / ``tests/test_jobs_worker.py``
— no real Postgres, no real threads beyond a driven ``run_forever``.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from app.jobs.queue import (
    JobQueue,
    ReapStats,
    _finalize_draft_for_lost_job,
)
from app.jobs.worker import JobWorker


def _mock_conn(mock_get_connection: MagicMock) -> MagicMock:
    conn = MagicMock()
    mock_get_connection.return_value.__enter__ = MagicMock(return_value=conn)
    mock_get_connection.return_value.__exit__ = MagicMock(return_value=False)
    return conn


def _stale_row(
    *,
    job_id: int = 7,
    job_type: str = "extract_entities",
    payload: dict | None = None,
    status: str = "running",
    attempts: int = 0,
    max_attempts: int = 3,
    claimed_by: str = "dead-worker",
) -> tuple:
    """Row shape of the reaper's candidate SELECT."""
    return (
        job_id,
        job_type,
        payload if payload is not None else {"draft_id": "abc"},
        status,
        attempts,
        max_attempts,
        claimed_by,
    )


# ---------------------------------------------------------------------------
# reap_stale_jobs — retry policy
# ---------------------------------------------------------------------------


class TestReapStaleJobs:
    @patch("app.jobs.queue._finalize_draft_for_lost_job")
    @patch("app.jobs.queue.get_connection")
    def test_stale_claimed_job_is_repended(
        self, mock_get_connection: MagicMock, mock_finalize: MagicMock
    ):
        """claimed + budget remaining → back to pending, attempt consumed."""
        conn = _mock_conn(mock_get_connection)
        conn.execute.return_value.fetchall.return_value = [
            _stale_row(status="claimed", attempts=0, max_attempts=3)
        ]

        stats = JobQueue().reap_stale_jobs()

        assert stats.recovered == 1
        assert stats.exhausted == 0
        # SELECT + one UPDATE.
        update_sql = conn.execute.call_args_list[1].args[0]
        update_params = conn.execute.call_args_list[1].args[1]
        assert "status = 'pending'" in update_sql
        assert "claimed_by = NULL" in update_sql
        assert "claimed_at = NULL" in update_sql
        assert "started_at = NULL" in update_sql
        # One lost attempt is consumed: attempts 0 → 1.
        assert update_params[0] == 1
        # Re-pend is immediate (scheduled_for = now, no backoff).
        assert isinstance(update_params[2], datetime)
        assert update_params[2] <= datetime.now(UTC)
        conn.commit.assert_called_once()
        # Budget not exhausted → no domain finalisation.
        mock_finalize.assert_not_called()

    @patch("app.jobs.queue._finalize_draft_for_lost_job")
    @patch("app.jobs.queue.get_connection")
    def test_stale_running_job_is_repended(
        self, mock_get_connection: MagicMock, mock_finalize: MagicMock
    ):
        conn = _mock_conn(mock_get_connection)
        conn.execute.return_value.fetchall.return_value = [
            _stale_row(status="running", attempts=1, max_attempts=3)
        ]

        stats = JobQueue().reap_stale_jobs()

        assert stats.recovered == 1
        assert stats.exhausted == 0
        update_sql = conn.execute.call_args_list[1].args[0]
        update_params = conn.execute.call_args_list[1].args[1]
        assert "status = 'pending'" in update_sql
        assert update_params[0] == 2  # attempts 1 → 2
        mock_finalize.assert_not_called()

    @patch("app.jobs.queue._finalize_draft_for_lost_job")
    @patch("app.jobs.queue.get_connection")
    def test_exhausted_job_is_failed_and_finalized(
        self, mock_get_connection: MagicMock, mock_finalize: MagicMock
    ):
        """No budget left → failed + draft-pipeline domain consequence."""
        conn = _mock_conn(mock_get_connection)
        payload = {"draft_id": "11111111-1111-1111-1111-111111111111"}
        conn.execute.return_value.fetchall.return_value = [
            _stale_row(
                job_id=42,
                job_type="analyze_impact",
                payload=payload,
                status="running",
                attempts=2,
                max_attempts=3,
            )
        ]

        stats = JobQueue().reap_stale_jobs()

        assert stats.recovered == 0
        assert stats.exhausted == 1
        update_sql = conn.execute.call_args_list[1].args[0]
        update_params = conn.execute.call_args_list[1].args[1]
        assert "status = 'failed'" in update_sql
        assert "finished_at" in update_sql
        assert update_params[0] == 3  # attempts 2 → 3 == max
        # Domain finalisation ran AFTER commit, with the job's payload.
        mock_finalize.assert_called_once_with(42, "analyze_impact", payload)

    @patch("app.jobs.queue._finalize_draft_for_lost_job")
    @patch("app.jobs.queue.get_connection")
    def test_empty_pass_is_noop(self, mock_get_connection: MagicMock, mock_finalize: MagicMock):
        conn = _mock_conn(mock_get_connection)
        conn.execute.return_value.fetchall.return_value = []

        stats = JobQueue().reap_stale_jobs()

        assert stats == ReapStats(recovered=0, exhausted=0)
        assert conn.execute.call_count == 1  # only the candidate SELECT
        mock_finalize.assert_not_called()

    @patch("app.jobs.queue._finalize_draft_for_lost_job")
    @patch("app.jobs.queue.get_connection")
    def test_candidate_select_is_bounded_and_lock_safe(
        self, mock_get_connection: MagicMock, _mock_finalize: MagicMock
    ):
        """SELECT must use SKIP LOCKED + LIMIT and both status cutoffs."""
        conn = _mock_conn(mock_get_connection)
        conn.execute.return_value.fetchall.return_value = []

        JobQueue().reap_stale_jobs(claimed_timeout_s=600, running_timeout_s=1800)

        select_sql = conn.execute.call_args_list[0].args[0]
        params = conn.execute.call_args_list[0].args[1]
        assert "FOR UPDATE SKIP LOCKED" in select_sql
        assert "LIMIT" in select_sql
        assert "status = 'claimed'" in select_sql
        assert "status = 'running'" in select_sql
        # Two cutoffs ~now-600s and ~now-1800s, claimed first.
        claimed_cutoff, running_cutoff = params[0], params[1]
        assert claimed_cutoff > running_cutoff
        delta = claimed_cutoff - running_cutoff
        assert abs(delta.total_seconds() - 1200) < 5


# ---------------------------------------------------------------------------
# Crash simulation: claimed row, worker gone → recovered and re-claimable
# ---------------------------------------------------------------------------


class TestCrashRecoveryRoundTrip:
    """A deploy-killed worker leaves a ``claimed`` row behind; the reaper
    must re-pend it so a fresh ``claim_next`` picks it up — i.e. the job
    is never permanently stuck. Stateful fake-row pattern mirrors
    ``tests/test_jobs_queue.py::TestRetryRoundTrip``."""

    def test_orphaned_claimed_row_is_recovered_and_reclaimed(self):
        stale_claimed_at = datetime.now(UTC) - timedelta(hours=2)
        row_state: dict = {
            "id": 55,
            "job_type": "parse_draft",
            "payload": {"draft_id": "abc"},
            "status": "claimed",  # crash happened between claim and run
            "priority": 0,
            "attempts": 0,
            "max_attempts": 3,
            "claimed_by": "worker-died",
            "claimed_at": stale_claimed_at,
            "started_at": None,
            "finished_at": None,
            "error_message": None,
            "result": None,
            "scheduled_for": stale_claimed_at,
            "created_at": stale_claimed_at,
        }

        def _full_row() -> tuple:
            return (
                row_state["id"],
                row_state["job_type"],
                row_state["payload"],
                row_state["status"],
                row_state["priority"],
                row_state["attempts"],
                row_state["max_attempts"],
                row_state["claimed_by"],
                row_state["claimed_at"],
                row_state["started_at"],
                row_state["finished_at"],
                row_state["error_message"],
                row_state["result"],
                row_state["scheduled_for"],
                row_state["created_at"],
            )

        def _execute(sql: str, params: tuple | None = None):
            sql_norm = " ".join(sql.split())
            cursor = MagicMock()

            if "WHERE (status = 'claimed'" in sql_norm:
                # Reaper candidate SELECT — apply the age predicate.
                claimed_cutoff = params[0]  # type: ignore[index]
                stale = (
                    row_state["status"] == "claimed" and row_state["claimed_at"] < claimed_cutoff
                )
                cursor.fetchall.return_value = (
                    [
                        (
                            row_state["id"],
                            row_state["job_type"],
                            row_state["payload"],
                            row_state["status"],
                            row_state["attempts"],
                            row_state["max_attempts"],
                            row_state["claimed_by"],
                        )
                    ]
                    if stale
                    else []
                )
                return cursor

            if "SET status = 'pending'" in sql_norm and "scheduled_for = %s" in sql_norm:
                # Reaper re-pend UPDATE.
                next_attempts, error_message, scheduled_for, _job_id = params  # type: ignore[misc]
                row_state.update(
                    status="pending",
                    attempts=next_attempts,
                    error_message=error_message,
                    scheduled_for=scheduled_for,
                    claimed_by=None,
                    claimed_at=None,
                    started_at=None,
                )
                cursor.rowcount = 1
                return cursor

            if "FROM background_jobs WHERE status = 'pending'" in sql_norm:
                # claim_next SELECT.
                if (
                    row_state["status"] == "pending" and row_state["scheduled_for"] <= params[0]  # type: ignore[index]
                ):
                    cursor.fetchone.return_value = _full_row()
                else:
                    cursor.fetchone.return_value = None
                return cursor

            if "SET status = 'claimed'" in sql_norm:
                row_state["status"] = "claimed"
                row_state["claimed_by"] = params[0]  # type: ignore[index]
                row_state["claimed_at"] = params[1]  # type: ignore[index]
                cursor.rowcount = 1
                return cursor

            cursor.fetchone.return_value = None
            cursor.fetchall.return_value = []
            return cursor

        conn = MagicMock()
        conn.execute.side_effect = _execute

        with patch("app.jobs.queue.get_connection") as mock_get_connection:
            mock_get_connection.return_value.__enter__ = MagicMock(return_value=conn)
            mock_get_connection.return_value.__exit__ = MagicMock(return_value=False)

            queue = JobQueue()

            # Sanity: before the reaper runs, the orphan is unclaimable.
            assert queue.claim_next(worker_id="worker-new") is None

            stats = queue.reap_stale_jobs()
            assert stats.recovered == 1
            assert stats.exhausted == 0
            assert row_state["status"] == "pending"
            assert row_state["attempts"] == 1  # the lost attempt was consumed
            assert row_state["claimed_by"] is None

            # The recovered job must be immediately claimable.
            job = queue.claim_next(worker_id="worker-new")
            assert job is not None, "orphaned job was not re-claimable after reap"
            assert job.id == 55
            assert job.claimed_by == "worker-new"

    def test_fresh_claimed_row_is_left_alone(self):
        """A row claimed seconds ago must NOT be reaped (no double-run)."""
        fresh_claimed_at = datetime.now(UTC) - timedelta(seconds=5)
        select_calls: list[tuple] = []

        def _execute(sql: str, params: tuple | None = None):
            sql_norm = " ".join(sql.split())
            cursor = MagicMock()
            if "WHERE (status = 'claimed'" in sql_norm:
                select_calls.append(params or ())
                claimed_cutoff = params[0]  # type: ignore[index]
                stale = fresh_claimed_at < claimed_cutoff
                cursor.fetchall.return_value = [] if not stale else [("should-not-happen",)]
                return cursor
            cursor.fetchall.return_value = []
            cursor.fetchone.return_value = None
            return cursor

        conn = MagicMock()
        conn.execute.side_effect = _execute

        with patch("app.jobs.queue.get_connection") as mock_get_connection:
            mock_get_connection.return_value.__enter__ = MagicMock(return_value=conn)
            mock_get_connection.return_value.__exit__ = MagicMock(return_value=False)

            stats = JobQueue().reap_stale_jobs(claimed_timeout_s=600)

        assert select_calls, "candidate SELECT did not run"
        assert stats.recovered == 0
        assert stats.exhausted == 0


# ---------------------------------------------------------------------------
# Domain finalisation for draft-pipeline jobs
# ---------------------------------------------------------------------------


class TestFinalizeDraftForLostJob:
    @patch("app.docs.status.update_draft_status")
    @patch("app.jobs.queue.get_connection")
    def test_processing_draft_is_marked_failed(
        self, mock_get_connection: MagicMock, mock_update: MagicMock
    ):
        conn = _mock_conn(mock_get_connection)
        conn.execute.return_value.fetchone.return_value = ("extracting",)

        _finalize_draft_for_lost_job(
            9, "extract_entities", {"draft_id": "11111111-1111-1111-1111-111111111111"}
        )

        mock_update.assert_called_once()
        args = mock_update.call_args
        assert args.args[2] == "failed"
        # The user-facing message is Estonian and actionable.
        assert "Palun proovige uuesti" in args.args[3]
        # Optimistic-concurrency guard against a parallel transition.
        assert args.kwargs.get("expected_status") == "extracting"
        conn.commit.assert_called_once()

    @patch("app.docs.status.update_draft_status")
    @patch("app.jobs.queue.get_connection")
    def test_terminal_draft_is_left_alone(
        self, mock_get_connection: MagicMock, mock_update: MagicMock
    ):
        """An orphaned export job must not fail an already-ready draft."""
        conn = _mock_conn(mock_get_connection)
        conn.execute.return_value.fetchone.return_value = ("ready",)

        _finalize_draft_for_lost_job(
            9, "export_report", {"draft_id": "11111111-1111-1111-1111-111111111111"}
        )

        mock_update.assert_not_called()

    @patch("app.jobs.queue.get_connection")
    def test_session_payload_is_skipped(self, mock_get_connection: MagicMock):
        """Drafter jobs surface via the job row; the session is not touched."""
        _finalize_draft_for_lost_job(
            9, "drafter_draft", {"session_id": "22222222-2222-2222-2222-222222222222"}
        )
        mock_get_connection.assert_not_called()

    @patch("app.jobs.queue.get_connection")
    def test_missing_draft_row_is_noop(self, mock_get_connection: MagicMock):
        conn = _mock_conn(mock_get_connection)
        conn.execute.return_value.fetchone.return_value = None
        # Must not raise.
        _finalize_draft_for_lost_job(9, "parse_draft", {"draft_id": "gone"})

    @patch("app.jobs.queue.get_connection")
    def test_db_error_is_swallowed(self, mock_get_connection: MagicMock):
        mock_get_connection.side_effect = RuntimeError("db down")
        # Best-effort: must not raise out of the reaper.
        _finalize_draft_for_lost_job(9, "parse_draft", {"draft_id": "abc"})


# ---------------------------------------------------------------------------
# Worker-loop wiring
# ---------------------------------------------------------------------------


class TestWorkerReaperWiring:
    @patch("app.jobs.worker.JobQueue")
    def test_startup_pass_runs_before_first_claim(self, mock_queue_cls: MagicMock):
        queue_instance = MagicMock()
        mock_queue_cls.return_value = queue_instance
        queue_instance.reap_stale_jobs.return_value = ReapStats()
        order: list[str] = []
        stop = threading.Event()

        queue_instance.reap_stale_jobs.side_effect = lambda **_kw: (
            order.append("reap"),
            ReapStats(),
        )[1]

        def claim_side_effect(*_a, **_kw):
            order.append("claim")
            stop.set()
            return None

        queue_instance.claim_next.side_effect = claim_side_effect

        JobWorker(worker_id="t", poll_interval=0.01).run_forever(stop)

        assert order[:2] == ["reap", "claim"], (
            "startup reaper pass must run before the first claim"
        )

    @patch("app.jobs.worker.JobQueue")
    def test_interval_gating_skips_back_to_back_passes(self, mock_queue_cls: MagicMock):
        queue_instance = MagicMock()
        mock_queue_cls.return_value = queue_instance
        queue_instance.reap_stale_jobs.return_value = ReapStats()
        stop = threading.Event()

        ticks = {"n": 0}

        def claim_side_effect(*_a, **_kw):
            ticks["n"] += 1
            if ticks["n"] >= 3:
                stop.set()
            return None

        queue_instance.claim_next.side_effect = claim_side_effect
        # Avoid real sleeping between iterations.
        stop.wait = lambda timeout=None: stop.is_set()  # type: ignore[method-assign]

        JobWorker(worker_id="t", poll_interval=0.01, reap_interval=3600.0).run_forever(stop)

        assert queue_instance.claim_next.call_count == 3
        # Only the startup pass within a 1h interval.
        assert queue_instance.reap_stale_jobs.call_count == 1

    @patch("app.jobs.worker.JobQueue")
    def test_elapsed_interval_triggers_second_pass(self, mock_queue_cls: MagicMock):
        queue_instance = MagicMock()
        mock_queue_cls.return_value = queue_instance
        queue_instance.reap_stale_jobs.return_value = ReapStats()
        stop = threading.Event()

        ticks = {"n": 0}

        def claim_side_effect(*_a, **_kw):
            ticks["n"] += 1
            if ticks["n"] >= 2:
                stop.set()
            return None

        queue_instance.claim_next.side_effect = claim_side_effect
        stop.wait = lambda timeout=None: stop.is_set()  # type: ignore[method-assign]

        worker = JobWorker(worker_id="t", poll_interval=0.01, reap_interval=0.0)
        # reap_interval=0 → every loop iteration is past the deadline.
        worker.run_forever(stop)

        assert queue_instance.reap_stale_jobs.call_count == 2

    @patch("app.jobs.worker.JobQueue")
    def test_reaper_failure_does_not_block_dispatch(self, mock_queue_cls: MagicMock):
        queue_instance = MagicMock()
        mock_queue_cls.return_value = queue_instance
        queue_instance.reap_stale_jobs.side_effect = RuntimeError("reaper db down")
        stop = threading.Event()

        def claim_side_effect(*_a, **_kw):
            stop.set()
            return None

        queue_instance.claim_next.side_effect = claim_side_effect

        # Must not raise, and the claim must still happen.
        JobWorker(worker_id="t", poll_interval=0.01).run_forever(stop)
        queue_instance.claim_next.assert_called_once()

    @patch("app.jobs.worker.record_metric")
    @patch("app.jobs.worker.JobQueue")
    def test_pass_outcomes_are_metered(self, mock_queue_cls: MagicMock, mock_metric: MagicMock):
        queue_instance = MagicMock()
        mock_queue_cls.return_value = queue_instance
        queue_instance.reap_stale_jobs.return_value = ReapStats(recovered=2, exhausted=1)

        worker = JobWorker(worker_id="t", poll_interval=0.01)
        worker._maybe_reap()

        labels = {
            (c.args[0], c.args[2].get("outcome"), c.args[1]) for c in mock_metric.call_args_list
        }
        assert ("jobs_reaped", "recovered", 2.0) in labels
        assert ("jobs_reaped", "exhausted", 1.0) in labels

    @patch("app.jobs.worker.JobQueue")
    def test_timeouts_are_passed_through(self, mock_queue_cls: MagicMock):
        queue_instance = MagicMock()
        mock_queue_cls.return_value = queue_instance
        queue_instance.reap_stale_jobs.return_value = ReapStats()

        worker = JobWorker(worker_id="t")
        worker._maybe_reap()

        kwargs = queue_instance.reap_stale_jobs.call_args.kwargs
        assert kwargs["claimed_timeout_s"] == worker.claimed_timeout_s
        assert kwargs["running_timeout_s"] == worker.running_timeout_s

    def test_env_overrides_are_read(self, monkeypatch):
        monkeypatch.setenv("JOB_REAPER_INTERVAL_S", "120")
        monkeypatch.setenv("JOB_REAPER_CLAIMED_TIMEOUT_S", "300")
        monkeypatch.setenv("JOB_REAPER_RUNNING_TIMEOUT_S", "900")

        worker = JobWorker(worker_id="t")

        assert worker.reap_interval == 120.0
        assert worker.claimed_timeout_s == 300
        assert worker.running_timeout_s == 900

    def test_invalid_env_falls_back_to_defaults(self, monkeypatch):
        monkeypatch.setenv("JOB_REAPER_INTERVAL_S", "not-a-number")
        monkeypatch.setenv("JOB_REAPER_RUNNING_TIMEOUT_S", "-5")

        worker = JobWorker(worker_id="t")

        assert worker.reap_interval == 60.0
        assert worker.running_timeout_s == 1800

    def test_failed_pass_backs_off_to_next_interval(self):
        """The deadline advances even when the pass raises (no hot loop)."""
        worker = JobWorker(worker_id="t", reap_interval=3600.0)
        with patch("app.jobs.worker.JobQueue") as mock_queue_cls:
            mock_queue_cls.return_value.reap_stale_jobs.side_effect = RuntimeError("boom")
            worker._maybe_reap()
            assert worker._next_reap_at > time.monotonic()
            # Second call inside the interval: no new attempt.
            worker._maybe_reap()
            assert mock_queue_cls.return_value.reap_stale_jobs.call_count == 1
