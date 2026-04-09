"""PostgreSQL-backed background job queue.

Schema reference: ``migrations/005_phase2_document_upload.sql`` defines
the ``background_jobs`` table. Workers claim the next pending job with:

    SELECT ... FROM background_jobs
    WHERE status = 'pending' AND scheduled_for <= now()
    ORDER BY priority DESC, created_at ASC
    FOR UPDATE SKIP LOCKED
    LIMIT 1

``FOR UPDATE SKIP LOCKED`` is the key — every worker grabs a *different*
row and stalled claims don't block anyone else. Combined with the
partial index on ``status = 'pending'`` this scales to thousands of
jobs/second without contention.

State machine:
    pending → claimed → running → success
                     └→ retrying → (eventually pending again)
                     └→ failed
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from app.db import get_connection

logger = logging.getLogger(__name__)


@dataclass
class Job:
    """Snapshot of a ``background_jobs`` row.

    All timestamp columns are exposed as timezone-aware ``datetime``s.
    ``payload`` and ``result`` are already JSON-parsed into dicts.
    """

    id: int
    job_type: str
    payload: dict[str, Any]
    status: str
    priority: int
    attempts: int
    max_attempts: int
    claimed_by: str | None
    claimed_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None
    result: dict[str, Any] | None
    scheduled_for: datetime
    created_at: datetime


# Column order used by every SELECT in this module. Kept in sync with
# ``_row_to_job`` so the two never drift.
_JOB_COLUMNS = (
    "id, job_type, payload, status, priority, attempts, max_attempts, "
    "claimed_by, claimed_at, started_at, finished_at, error_message, "
    "result, scheduled_for, created_at"
)


def _parse_json(value: Any) -> dict[str, Any] | None:
    """Normalise a JSONB column value to ``dict | None``.

    psycopg 3 already decodes JSONB into Python objects, but older
    driver paths or test mocks may hand us back a raw ``str``; handle
    both so the caller never has to care.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray)):
        return json.loads(value.decode())
    if isinstance(value, str):
        return json.loads(value)
    # Fall through for unknown types — let the caller see the actual data.
    return value  # type: ignore[return-value]


def _row_to_job(row: tuple[Any, ...]) -> Job:
    """Build a ``Job`` dataclass from a raw cursor row."""
    (
        job_id,
        job_type,
        payload,
        status,
        priority,
        attempts,
        max_attempts,
        claimed_by,
        claimed_at,
        started_at,
        finished_at,
        error_message,
        result,
        scheduled_for,
        created_at,
    ) = row
    return Job(
        id=job_id,
        job_type=job_type,
        payload=_parse_json(payload) or {},
        status=status,
        priority=priority,
        attempts=attempts,
        max_attempts=max_attempts,
        claimed_by=claimed_by,
        claimed_at=claimed_at,
        started_at=started_at,
        finished_at=finished_at,
        error_message=error_message,
        result=_parse_json(result),
        scheduled_for=scheduled_for,
        created_at=created_at,
    )


class JobQueue:
    """High-level wrapper around the ``background_jobs`` table."""

    # -- producer side ------------------------------------------------------

    def enqueue(
        self,
        job_type: str,
        payload: dict[str, Any],
        *,
        priority: int = 0,
        scheduled_for: datetime | None = None,
    ) -> int:
        """Insert a new ``pending`` job and return its id.

        Args:
            job_type: e.g. ``"parse_draft"`` or ``"analyze_impact"``.
            payload: JSON-serialisable arguments for the handler.
            priority: Higher integers are dequeued first.
            scheduled_for: Optional future timestamp for delayed execution.
        """
        scheduled = scheduled_for or datetime.now(UTC)
        with get_connection() as conn:
            row = conn.execute(
                """
                INSERT INTO background_jobs
                    (job_type, payload, status, priority, scheduled_for)
                VALUES (%s, %s, 'pending', %s, %s)
                RETURNING id
                """,
                (job_type, Jsonb(payload), priority, scheduled),
            ).fetchone()
            conn.commit()

        if row is None:
            raise RuntimeError("INSERT ... RETURNING id produced no row")
        job_id = int(row[0])
        logger.info(
            "Enqueued job id=%d type=%s priority=%d scheduled_for=%s",
            job_id,
            job_type,
            priority,
            scheduled.isoformat(),
        )
        return job_id

    # -- consumer side ------------------------------------------------------

    def claim_next(self, worker_id: str) -> Job | None:
        """Atomically claim the highest-priority pending job.

        Uses ``FOR UPDATE SKIP LOCKED`` so concurrent workers each get a
        different job. Returns ``None`` if the queue is empty.
        """
        now = datetime.now(UTC)
        with get_connection() as conn:
            # Step 1: pick a candidate row and lock it for the duration of
            # the transaction. SKIP LOCKED prevents contention with peers.
            row = conn.execute(
                f"""
                SELECT {_JOB_COLUMNS}
                FROM background_jobs
                WHERE status = 'pending' AND scheduled_for <= %s
                ORDER BY priority DESC, created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
                (now,),
            ).fetchone()

            if row is None:
                conn.commit()
                return None

            job_id = row[0]
            # Step 2: flip the row to 'claimed' while we still hold the lock.
            conn.execute(
                """
                UPDATE background_jobs
                SET status = 'claimed', claimed_by = %s, claimed_at = %s
                WHERE id = %s
                """,
                (worker_id, now, job_id),
            )
            conn.commit()

        job = _row_to_job(row)
        # Reflect the in-memory status so the caller sees the fresh values
        # without having to re-fetch.
        job.status = "claimed"
        job.claimed_by = worker_id
        job.claimed_at = now
        logger.info("Claimed job id=%d type=%s worker=%s", job.id, job.job_type, worker_id)
        return job

    def mark_running(self, job_id: int) -> None:
        """Transition a claimed job into the ``running`` state."""
        now = datetime.now(UTC)
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE background_jobs
                SET status = 'running', started_at = %s
                WHERE id = %s
                """,
                (now, job_id),
            )
            conn.commit()

    def mark_success(self, job_id: int, result: dict[str, Any] | None = None) -> None:
        """Mark a job as ``success`` and persist its return value."""
        now = datetime.now(UTC)
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE background_jobs
                SET status = 'success', finished_at = %s, result = %s
                WHERE id = %s
                """,
                (now, Jsonb(result) if result is not None else None, job_id),
            )
            conn.commit()

    def mark_failed(self, job_id: int, error_message: str) -> None:
        """Record a failure and either schedule a retry or give up.

        Retries back off exponentially: the first retry runs ~2 minutes
        after failure, the second ~4 minutes, the third ~8, and so on.
        """
        now = datetime.now(UTC)
        with get_connection() as conn:
            row = conn.execute(
                "SELECT attempts, max_attempts FROM background_jobs WHERE id = %s",
                (job_id,),
            ).fetchone()
            if row is None:
                conn.commit()
                logger.warning("mark_failed: job id=%s not found", job_id)
                return

            attempts, max_attempts = int(row[0]), int(row[1])
            next_attempts = attempts + 1

            if next_attempts < max_attempts:
                # Schedule a retry. 2^attempts minute backoff keeps total
                # retry time bounded while still letting transient failures
                # ride through.
                backoff = timedelta(minutes=2**next_attempts)
                next_run = now + backoff
                conn.execute(
                    """
                    UPDATE background_jobs
                    SET status = 'retrying',
                        attempts = %s,
                        error_message = %s,
                        finished_at = %s,
                        scheduled_for = %s
                    WHERE id = %s
                    """,
                    (next_attempts, error_message, now, next_run, job_id),
                )
                logger.warning(
                    "Job id=%d failed (attempt %d/%d), retrying at %s",
                    job_id,
                    next_attempts,
                    max_attempts,
                    next_run.isoformat(),
                )
            else:
                conn.execute(
                    """
                    UPDATE background_jobs
                    SET status = 'failed',
                        attempts = %s,
                        error_message = %s,
                        finished_at = %s
                    WHERE id = %s
                    """,
                    (next_attempts, error_message, now, job_id),
                )
                logger.error(
                    "Job id=%d permanently failed after %d attempts", job_id, next_attempts
                )
            conn.commit()

    # -- query helpers ------------------------------------------------------

    def get(self, job_id: int) -> Job | None:
        """Fetch a single job by id, or ``None`` if it doesn't exist."""
        with get_connection() as conn:
            row = conn.execute(
                f"SELECT {_JOB_COLUMNS} FROM background_jobs WHERE id = %s",
                (job_id,),
            ).fetchone()
        return _row_to_job(row) if row else None

    def list_by_status(self, status: str, limit: int = 100) -> list[Job]:
        """Return jobs currently in *status*, newest first.

        Used by the admin dashboard to surface queue health.
        """
        with get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT {_JOB_COLUMNS}
                FROM background_jobs
                WHERE status = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (status, limit),
            ).fetchall()
        return [_row_to_job(row) for row in rows]
