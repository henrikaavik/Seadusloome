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
                     └→ pending (re-scheduled with backoff, attempts<max)
                     └→ failed  (attempts==max)

Note on the ``retrying`` status: the schema CHECK constraint still
allows it (see ``migrations/005_phase2_document_upload.sql``), but
the worker no longer parks failed jobs there. Re-queued jobs go
straight back to ``pending`` with a future ``scheduled_for``, so the
``claim_next`` SELECT (which only looks at ``status='pending'``)
picks them up automatically once the backoff window passes (#441).

Orphan recovery (#852 E1): ``claim_next`` only ever looks at
``status='pending'``, so a worker that dies (crash, OOM, deploy
SIGKILL) between claim and completion used to strand its row in
``claimed``/``running`` forever. :meth:`JobQueue.reap_stale_jobs`
implements a visibility timeout: rows stuck in ``claimed`` or
``running`` past a threshold are treated as one lost attempt and fed
through the SAME retry policy as :meth:`JobQueue.mark_failed` —
re-pended while budget remains, flipped to ``failed`` once the budget
is exhausted (including the domain-row consequence for draft-pipeline
jobs). The worker loop runs a pass at startup and periodically; see
``app/jobs/worker.py``.
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


# ---------------------------------------------------------------------------
# Reaper policy (#852 E1)
# ---------------------------------------------------------------------------
#
# Threshold rationale:
#   * ``claimed`` → ``running`` is a single UPDATE issued immediately after
#     the claim (see ``JobWorker._tick``), so a row sitting in ``claimed``
#     for 10 minutes can only mean the worker died in between. 10 min is
#     pure safety margin over the realistic worst case of milliseconds.
#   * ``running`` jobs hold no DB lock while the handler executes, so age
#     is the only stall signal. The threshold must exceed the LONGEST
#     legitimate handler runtime or a slow-but-alive job gets double-run.
#     Worst case today is ``drafter_draft`` (one LLM call per section;
#     a VTK/full-law structure can mean tens of sequential calls), which
#     can legitimately run for many minutes — 30 min clears it with room
#     while still bounding the stuck-pipeline window to one reaper pass.
# Both are overridable via env (``JOB_REAPER_CLAIMED_TIMEOUT_S`` /
# ``JOB_REAPER_RUNNING_TIMEOUT_S``, read in ``app/jobs/worker.py``) so an
# operator can tighten or relax them without a release.
DEFAULT_REAPER_CLAIMED_TIMEOUT_S = 600
DEFAULT_REAPER_RUNNING_TIMEOUT_S = 1800

# Upper bound per reaper pass — keeps a pathological backlog from turning
# one pass into a long transaction. The next pass picks up the remainder.
_REAP_BATCH_LIMIT = 100

# Draft statuses the reaper may flip to ``failed`` when a draft-pipeline
# job loses its retry budget. Terminal states (``ready``/``failed``) are
# never touched — e.g. an orphaned ``export_report`` job must not fail a
# draft that already finished analysis.
_REAPABLE_DRAFT_STATUSES = frozenset({"uploaded", "parsing", "extracting", "analyzing"})

# ``background_jobs.error_message`` surfaces in the admin job monitor and
# in the drafter step-status alert, so the strings are Estonian.
_REAPED_RETRY_MSG_ET = (
    "Töötlus katkes ootamatult (tööprotsess seiskus); "
    "töö pandi automaatselt uuesti järjekorda (katse {attempts}/{max_attempts})."
)
_REAPED_EXHAUSTED_MSG_ET = (
    "Töötlus katkes ootamatult ja katsete limiit on ammendatud ({attempts}/{max_attempts})."
)
_REAPED_DRAFT_MSG_ET = (
    "Töötlemine katkes ootamatult (näiteks süsteemi taaskäivituse tõttu). Palun proovige uuesti."
)


@dataclass
class ReapStats:
    """Outcome counts of one :meth:`JobQueue.reap_stale_jobs` pass."""

    recovered: int = 0
    """Orphaned jobs re-pended for another attempt."""

    exhausted: int = 0
    """Orphaned jobs whose retry budget ran out — flipped to ``failed``."""


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

        IMPORTANT (#441): jobs with attempts < max_attempts go back to
        ``status='pending'`` (NOT ``'retrying'``). The dequeue query in
        :meth:`claim_next` only looks at ``WHERE status='pending'``, so
        parking the row in ``retrying`` would strand it forever — the
        ``scheduled_for`` gate is the actual retry trigger. Keeping the
        column on ``pending`` and letting the future ``scheduled_for``
        timestamp gate the next claim is the simplest, most reliable
        path. The schema's ``retrying`` value is now unused but kept
        in the CHECK constraint for backwards compatibility.
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
                # ride through. The row goes back to ``pending`` so the
                # standard claim_next() SELECT will re-pick it up once the
                # backoff window has elapsed.
                backoff = timedelta(minutes=2**next_attempts)
                next_run = now + backoff
                conn.execute(
                    """
                    UPDATE background_jobs
                    SET status = 'pending',
                        attempts = %s,
                        error_message = %s,
                        finished_at = %s,
                        scheduled_for = %s,
                        claimed_by = NULL,
                        claimed_at = NULL,
                        started_at = NULL
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

    # -- orphan recovery (#852 E1) -------------------------------------------

    def reap_stale_jobs(
        self,
        *,
        claimed_timeout_s: int = DEFAULT_REAPER_CLAIMED_TIMEOUT_S,
        running_timeout_s: int = DEFAULT_REAPER_RUNNING_TIMEOUT_S,
    ) -> ReapStats:
        """Recover ``claimed``/``running`` rows orphaned by a dead worker.

        A worker killed mid-job (crash, OOM, deploy SIGKILL) leaves its row
        in ``claimed`` or ``running``; ``claim_next`` only selects
        ``pending`` so the row would otherwise be stuck forever. Each stale
        row counts as ONE lost attempt and follows the same retry policy as
        :meth:`mark_failed`:

        * budget remaining → back to ``pending`` with ``scheduled_for=now``
          (no backoff — the failure was not the job's fault, so the retry
          runs as soon as a worker is free),
        * budget exhausted → ``failed``, plus the domain-row consequence
          for draft-pipeline jobs (the draft is flipped to ``failed`` so
          the user sees the error and the retry button instead of a
          pipeline stuck in ``extracting``/``analyzing``).

        Bounded (``LIMIT {batch}``) and concurrency-safe: the candidate
        SELECT uses ``FOR UPDATE SKIP LOCKED`` so multiple reapers (web
        replicas + standalone worker) never double-process a row.

        Returns:
            :class:`ReapStats` with recovered/exhausted counts so callers
            can log and meter the pass.
        """
        now = datetime.now(UTC)
        claimed_cutoff = now - timedelta(seconds=claimed_timeout_s)
        running_cutoff = now - timedelta(seconds=running_timeout_s)
        stats = ReapStats()
        # (job_id, job_type, payload) of permanently-failed jobs; domain
        # finalisation runs AFTER the queue transaction commits so a
        # domain-side error cannot roll back the queue bookkeeping.
        exhausted_jobs: list[tuple[int, str, dict[str, Any]]] = []

        with get_connection() as conn:
            # COALESCE keeps the predicate total even if an operator
            # hand-edited a row and nulled the timestamps — such rows age
            # out via created_at instead of being stranded forever.
            rows = conn.execute(
                """
                SELECT id, job_type, payload, status, attempts, max_attempts, claimed_by
                FROM background_jobs
                WHERE (status = 'claimed' AND COALESCE(claimed_at, created_at) < %s)
                   OR (status = 'running'
                       AND COALESCE(started_at, claimed_at, created_at) < %s)
                ORDER BY id
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (claimed_cutoff, running_cutoff, _REAP_BATCH_LIMIT),
            ).fetchall()

            for row in rows:
                job_id, job_type, payload, status, attempts, max_attempts, claimed_by = row
                next_attempts = int(attempts or 0) + 1
                budget = int(max_attempts or 1)

                if next_attempts < budget:
                    conn.execute(
                        """
                        UPDATE background_jobs
                        SET status = 'pending',
                            attempts = %s,
                            error_message = %s,
                            scheduled_for = %s,
                            claimed_by = NULL,
                            claimed_at = NULL,
                            started_at = NULL
                        WHERE id = %s
                        """,
                        (
                            next_attempts,
                            _REAPED_RETRY_MSG_ET.format(
                                attempts=next_attempts, max_attempts=budget
                            ),
                            now,
                            job_id,
                        ),
                    )
                    stats.recovered += 1
                    logger.warning(
                        "Reaper: recovered orphaned job id=%s type=%s status=%s "
                        "worker=%s — re-pended (attempt %d/%d)",
                        job_id,
                        job_type,
                        status,
                        claimed_by,
                        next_attempts,
                        budget,
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
                        (
                            next_attempts,
                            _REAPED_EXHAUSTED_MSG_ET.format(
                                attempts=next_attempts, max_attempts=budget
                            ),
                            now,
                            job_id,
                        ),
                    )
                    stats.exhausted += 1
                    logger.error(
                        "Reaper: orphaned job id=%s type=%s status=%s worker=%s "
                        "exhausted its retry budget (%d/%d) — marked failed",
                        job_id,
                        job_type,
                        status,
                        claimed_by,
                        next_attempts,
                        budget,
                    )
                    exhausted_jobs.append((int(job_id), str(job_type), _parse_json(payload) or {}))
            conn.commit()

        for job_id, job_type, payload in exhausted_jobs:
            _finalize_draft_for_lost_job(job_id, job_type, payload)

        if stats.recovered or stats.exhausted:
            logger.info(
                "Reaper pass complete: recovered=%d exhausted=%d",
                stats.recovered,
                stats.exhausted,
            )
        return stats

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


# ---------------------------------------------------------------------------
# Reaper domain finalisation (#852 E1)
# ---------------------------------------------------------------------------


def _finalize_draft_for_lost_job(job_id: int, job_type: str, payload: dict[str, Any]) -> None:
    """Apply the domain-row consequence when the reaper permanently fails a job.

    Handlers normally flip their domain row to ``failed`` on the final
    attempt (#448), but a reaped job never got to run its final attempt —
    without this hook the draft would sit in ``parsing``/``extracting``/
    ``analyzing`` forever with no retry button.

    Only draft-pipeline jobs (payloads carrying ``draft_id``) need this:
    the drafter wizard reads the JOB row's status/error directly (see
    ``step_status_fragment``), so ``session_id`` payloads already surface
    the failure without touching the session row (abandoning a session for
    an infrastructure failure would destroy user work).

    Best-effort by design — every error is logged and swallowed so a
    domain-side hiccup never breaks the reaper pass. The imports are local
    to avoid an ``app.jobs`` → ``app.docs`` import cycle at module load
    (``app.docs`` handlers import ``app.jobs.worker`` for registration).
    """
    draft_id = (payload or {}).get("draft_id")
    if not draft_id:
        return

    try:
        from app.docs.status import update_draft_status

        with get_connection() as conn:
            row = conn.execute(
                "SELECT status FROM drafts WHERE id = %s",
                (str(draft_id),),
            ).fetchone()
            if row is None:
                return
            current = str(row[0])
            if current not in _REAPABLE_DRAFT_STATUSES:
                return
            # ``expected_status`` guards against a concurrent transition
            # between our SELECT and the UPDATE.
            update_draft_status(
                conn,
                draft_id,
                "failed",
                _REAPED_DRAFT_MSG_ET,
                error_debug=(
                    f"Reaper: background job id={job_id} type={job_type} was orphaned "
                    f"(worker lost) and its retry budget is exhausted"
                ),
                expected_status=current,
            )
            conn.commit()
        logger.warning(
            "Reaper: marked draft %s failed after orphaned job id=%s type=%s "
            "exhausted its retry budget",
            draft_id,
            job_id,
            job_type,
        )
    except Exception:  # noqa: BLE001 — domain flip must never break the reaper
        logger.exception(
            "Reaper: failed to mark draft %s failed for lost job id=%s",
            draft_id,
            job_id,
        )
        return

    # Push the failure to WS subscribers so an open draft page updates
    # without a manual refresh (#608 pattern). Best-effort.
    try:
        from uuid import UUID

        from app.docs.status_events import emit_threadsafe

        emit_threadsafe(
            UUID(str(draft_id)),
            type="status",
            status="failed",
            error_message=_REAPED_DRAFT_MSG_ET,
        )
    except Exception:  # noqa: BLE001
        logger.debug("Reaper: WS status emit failed for draft %s", draft_id, exc_info=True)
