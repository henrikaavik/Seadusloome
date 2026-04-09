"""Unit tests for ``app.jobs.queue``.

These tests patch ``app.jobs.queue.get_connection`` to hand back a
``MagicMock`` cursor — they never open a real Postgres connection. The
mocking pattern mirrors ``tests/test_dashboard.py::TestBookmarkAdd``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from app.jobs.queue import Job, JobQueue


def _mock_conn(mock_get_connection: MagicMock) -> MagicMock:
    """Wire ``get_connection()`` so it behaves as a context manager."""
    conn = MagicMock()
    mock_get_connection.return_value.__enter__ = MagicMock(return_value=conn)
    mock_get_connection.return_value.__exit__ = MagicMock(return_value=False)
    return conn


# ---------------------------------------------------------------------------
# enqueue
# ---------------------------------------------------------------------------


class TestEnqueue:
    @patch("app.jobs.queue.get_connection")
    def test_enqueue_returns_id(self, mock_get_connection: MagicMock):
        conn = _mock_conn(mock_get_connection)
        conn.execute.return_value.fetchone.return_value = (42,)

        queue = JobQueue()
        job_id = queue.enqueue("parse_draft", {"draft_id": "abc"}, priority=5)

        assert job_id == 42
        conn.execute.assert_called_once()
        sql = conn.execute.call_args.args[0]
        assert "INSERT INTO background_jobs" in sql
        assert "RETURNING id" in sql
        assert "'pending'" in sql
        conn.commit.assert_called_once()

    @patch("app.jobs.queue.get_connection")
    def test_enqueue_wraps_payload_in_jsonb(self, mock_get_connection: MagicMock):
        """Payload must be passed through psycopg's Jsonb adapter."""
        from psycopg.types.json import Jsonb

        conn = _mock_conn(mock_get_connection)
        conn.execute.return_value.fetchone.return_value = (7,)

        queue = JobQueue()
        queue.enqueue("extract_entities", {"draft_id": "xyz"})

        params = conn.execute.call_args.args[1]
        # Order from the SQL: (job_type, payload, priority, scheduled_for)
        assert params[0] == "extract_entities"
        assert isinstance(params[1], Jsonb)


# ---------------------------------------------------------------------------
# claim_next
# ---------------------------------------------------------------------------


class TestClaimNext:
    @patch("app.jobs.queue.get_connection")
    def test_claim_next_returns_none_when_empty(self, mock_get_connection: MagicMock):
        conn = _mock_conn(mock_get_connection)
        conn.execute.return_value.fetchone.return_value = None

        queue = JobQueue()
        result = queue.claim_next(worker_id="worker-1")

        assert result is None
        # The SELECT ran but no UPDATE should have followed.
        assert conn.execute.call_count == 1
        sql = conn.execute.call_args.args[0]
        assert "FOR UPDATE SKIP LOCKED" in sql

    @patch("app.jobs.queue.get_connection")
    def test_claim_next_returns_job_when_pending(self, mock_get_connection: MagicMock):
        conn = _mock_conn(mock_get_connection)
        now = datetime.now(UTC)
        row = (
            101,  # id
            "parse_draft",  # job_type
            {"draft_id": "abc"},  # payload (psycopg returns dict)
            "pending",  # status
            0,  # priority
            0,  # attempts
            3,  # max_attempts
            None,  # claimed_by
            None,  # claimed_at
            None,  # started_at
            None,  # finished_at
            None,  # error_message
            None,  # result
            now,  # scheduled_for
            now,  # created_at
        )
        conn.execute.return_value.fetchone.return_value = row

        queue = JobQueue()
        job = queue.claim_next(worker_id="worker-7")

        assert job is not None
        assert isinstance(job, Job)
        assert job.id == 101
        assert job.job_type == "parse_draft"
        assert job.payload == {"draft_id": "abc"}
        assert job.status == "claimed"
        assert job.claimed_by == "worker-7"
        assert job.claimed_at is not None

        # Both the SELECT and the UPDATE should have run.
        assert conn.execute.call_count == 2
        update_sql = conn.execute.call_args_list[1].args[0]
        assert "UPDATE background_jobs" in update_sql
        assert "status = 'claimed'" in update_sql


# ---------------------------------------------------------------------------
# mark_success / mark_running
# ---------------------------------------------------------------------------


class TestMarkSuccess:
    @patch("app.jobs.queue.get_connection")
    def test_mark_success_sets_result(self, mock_get_connection: MagicMock):
        from psycopg.types.json import Jsonb

        conn = _mock_conn(mock_get_connection)
        queue = JobQueue()
        queue.mark_success(job_id=9, result={"entity_count": 17})

        conn.execute.assert_called_once()
        sql = conn.execute.call_args.args[0]
        params = conn.execute.call_args.args[1]
        assert "status = 'success'" in sql
        assert "result = %s" in sql
        # params = (now, Jsonb(result), job_id)
        assert isinstance(params[1], Jsonb)
        assert params[2] == 9
        conn.commit.assert_called_once()

    @patch("app.jobs.queue.get_connection")
    def test_mark_success_with_none_result(self, mock_get_connection: MagicMock):
        conn = _mock_conn(mock_get_connection)
        queue = JobQueue()
        queue.mark_success(job_id=9, result=None)

        params = conn.execute.call_args.args[1]
        # result param is None, not Jsonb(None).
        assert params[1] is None


# ---------------------------------------------------------------------------
# mark_failed retry logic
# ---------------------------------------------------------------------------


class TestMarkFailed:
    @patch("app.jobs.queue.get_connection")
    def test_mark_failed_retries_under_limit(self, mock_get_connection: MagicMock):
        """attempts=0, max=3 → next_attempts=1 → status='pending' (#441)."""
        conn = _mock_conn(mock_get_connection)
        conn.execute.return_value.fetchone.return_value = (0, 3)

        queue = JobQueue()
        queue.mark_failed(job_id=5, error_message="boom")

        # SELECT then UPDATE.
        assert conn.execute.call_count == 2
        update_sql = conn.execute.call_args_list[1].args[0]
        # #441 fix: re-queued jobs go back to 'pending' (not 'retrying')
        # so the standard claim_next() SELECT picks them up.
        assert "status = 'pending'" in update_sql
        assert "status = 'retrying'" not in update_sql
        assert "scheduled_for" in update_sql
        # Worker bookkeeping must also be reset so the next claim
        # starts from a clean state.
        assert "claimed_by = NULL" in update_sql
        assert "claimed_at = NULL" in update_sql
        assert "started_at = NULL" in update_sql
        conn.commit.assert_called_once()

    @patch("app.jobs.queue.get_connection")
    def test_mark_failed_gives_up_at_limit(self, mock_get_connection: MagicMock):
        """attempts=2, max=3 → next_attempts=3 → status='failed'."""
        conn = _mock_conn(mock_get_connection)
        conn.execute.return_value.fetchone.return_value = (2, 3)

        queue = JobQueue()
        queue.mark_failed(job_id=5, error_message="final boom")

        assert conn.execute.call_count == 2
        update_sql = conn.execute.call_args_list[1].args[0]
        assert "status = 'failed'" in update_sql
        assert "scheduled_for" not in update_sql
        conn.commit.assert_called_once()

    @patch("app.jobs.queue.get_connection")
    def test_mark_failed_missing_job_is_noop(self, mock_get_connection: MagicMock):
        """A missing job id must not raise — just log and return."""
        conn = _mock_conn(mock_get_connection)
        conn.execute.return_value.fetchone.return_value = None

        queue = JobQueue()
        queue.mark_failed(job_id=999, error_message="ghost")

        # Only the SELECT ran; no UPDATE.
        assert conn.execute.call_count == 1


class TestRetryRoundTrip:
    """Regression for #441: claim → mark_failed (under limit) → claim again.

    Uses a stateful in-memory ``_FakeRow`` that mimics the columns of the
    ``background_jobs`` table so we can drive the queue through one full
    retry cycle without a real Postgres connection. This is the test
    that would have caught the original bug — under the old code path
    the second ``claim_next`` returned ``None`` because the row was in
    ``status='retrying'``.
    """

    def test_failed_job_is_reclaimable_after_backoff(self):
        from datetime import timedelta

        # Simulated row state. Only the columns the queue cares about
        # are tracked.
        row_state: dict = {
            "id": 101,
            "job_type": "parse_draft",
            "payload": {"draft_id": "abc"},
            "status": "pending",
            "priority": 0,
            "attempts": 0,
            "max_attempts": 3,
            "claimed_by": None,
            "claimed_at": None,
            "started_at": None,
            "finished_at": None,
            "error_message": None,
            "result": None,
            "scheduled_for": datetime.now(UTC) - timedelta(seconds=1),
            "created_at": datetime.now(UTC),
        }

        def _row_tuple() -> tuple:
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

        # Build a fake conn that interprets each SQL by the keywords in
        # it. This is just enough to drive the JobQueue methods that the
        # round-trip exercises (claim_next, mark_failed) without
        # reproducing real psycopg semantics.
        def _execute(sql: str, params: tuple | None = None):
            sql_norm = " ".join(sql.split())  # collapse whitespace
            cursor = MagicMock()

            if "FROM background_jobs WHERE status = 'pending'" in sql_norm:
                # claim_next SELECT — only returns the row when status is
                # pending AND scheduled_for has elapsed.
                if (
                    row_state["status"] == "pending" and row_state["scheduled_for"] <= params[0]  # type: ignore[index]
                ):
                    cursor.fetchone.return_value = _row_tuple()
                else:
                    cursor.fetchone.return_value = None
                return cursor

            if "SET status = 'claimed'" in sql_norm:
                row_state["status"] = "claimed"
                row_state["claimed_by"] = params[0]  # type: ignore[index]
                row_state["claimed_at"] = params[1]  # type: ignore[index]
                cursor.rowcount = 1
                return cursor

            if "SELECT attempts, max_attempts FROM background_jobs" in sql_norm:
                cursor.fetchone.return_value = (
                    row_state["attempts"],
                    row_state["max_attempts"],
                )
                return cursor

            if "SET status = 'pending'" in sql_norm:
                # mark_failed retry path — copy params back into row_state.
                (
                    next_attempts,
                    error_message,
                    finished_at,
                    next_scheduled,
                    _job_id,
                ) = params  # type: ignore[misc]
                row_state["status"] = "pending"
                row_state["attempts"] = next_attempts
                row_state["error_message"] = error_message
                row_state["finished_at"] = finished_at
                row_state["scheduled_for"] = next_scheduled
                row_state["claimed_by"] = None
                row_state["claimed_at"] = None
                row_state["started_at"] = None
                cursor.rowcount = 1
                return cursor

            cursor.fetchone.return_value = None
            return cursor

        conn = MagicMock()
        conn.execute.side_effect = _execute

        with patch("app.jobs.queue.get_connection") as mock_get_connection:
            mock_get_connection.return_value.__enter__ = MagicMock(return_value=conn)
            mock_get_connection.return_value.__exit__ = MagicMock(return_value=False)

            queue = JobQueue()

            # 1) Initial claim works.
            first = queue.claim_next(worker_id="worker-A")
            assert first is not None
            assert first.id == 101
            assert first.attempts == 0

            # 2) Mark it failed (still under retry limit).
            queue.mark_failed(job_id=101, error_message="transient")

            # The row must NOT be in 'retrying' (#441 — that would
            # strand it).
            assert row_state["status"] == "pending"
            assert row_state["attempts"] == 1

            # 3) Advance the simulated clock by rewinding ``scheduled_for``
            #    so the next claim sees the row as eligible. We DON'T
            #    actually wait — we just simulate time having passed.
            row_state["scheduled_for"] = datetime.now(UTC) - timedelta(seconds=1)

            # 4) The same row must be reclaimable.
            second = queue.claim_next(worker_id="worker-B")
            assert second is not None, (
                "Failed job stuck in 'pending' was not reclaimed — #441 regression"
            )
            assert second.id == 101
            assert second.attempts == 1
            assert second.claimed_by == "worker-B"


# ---------------------------------------------------------------------------
# list_by_status
# ---------------------------------------------------------------------------


class TestListByStatus:
    @patch("app.jobs.queue.get_connection")
    def test_list_by_status_returns_jobs(self, mock_get_connection: MagicMock):
        conn = _mock_conn(mock_get_connection)
        now = datetime.now(UTC)
        rows = [
            (
                1,
                "parse_draft",
                {"draft_id": "a"},
                "pending",
                0,
                0,
                3,
                None,
                None,
                None,
                None,
                None,
                None,
                now,
                now,
            ),
            (
                2,
                "analyze_impact",
                {"draft_id": "b"},
                "pending",
                5,
                0,
                3,
                None,
                None,
                None,
                None,
                None,
                None,
                now,
                now,
            ),
        ]
        conn.execute.return_value.fetchall.return_value = rows

        queue = JobQueue()
        jobs = queue.list_by_status("pending", limit=10)

        assert len(jobs) == 2
        assert jobs[0].job_type == "parse_draft"
        assert jobs[1].priority == 5
        sql = conn.execute.call_args.args[0]
        assert "WHERE status = %s" in sql
