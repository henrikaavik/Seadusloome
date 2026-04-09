"""Background job worker loop.

The worker pulls rows off the ``background_jobs`` table via
:class:`app.jobs.queue.JobQueue` and dispatches them to Python
handlers registered via :func:`register_handler`. A single FastHTML
process spawns one worker thread at startup (see ``app/main.py``'s
lifespan hook); running multiple processes on the same database is
safe because ``JobQueue.claim_next`` uses ``FOR UPDATE SKIP LOCKED``.

Handler contract:
    - Handlers are plain synchronous functions ``(payload: dict) -> dict | None``.
    - The returned dict is persisted in ``background_jobs.result``.
    - Handlers MUST raise to signal failure; raising triggers the
      queue's exponential backoff retry logic automatically.

Phase 2 currently ships stub handlers for the four job types the
spec names (``parse_draft``, ``extract_entities``, ``analyze_impact``,
``export_report``); later batches swap these out for real
implementations.
"""

from __future__ import annotations

import logging
import socket
import threading
import traceback
import uuid
from collections.abc import Callable
from typing import Any

from app.jobs.queue import JobQueue

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------


HandlerFn = Callable[[dict[str, Any]], dict[str, Any] | None]

_HANDLERS: dict[str, HandlerFn] = {}


def register_handler(job_type: str) -> Callable[[HandlerFn], HandlerFn]:
    """Decorator: register *func* as the handler for *job_type*.

    Registering twice overwrites the previous handler; this keeps test
    fixtures simple (tests can re-register without worrying about
    leaking state from a previous run).
    """

    def decorator(func: HandlerFn) -> HandlerFn:
        _HANDLERS[job_type] = func
        logger.debug("Registered handler for job_type=%s -> %s", job_type, func.__name__)
        return func

    return decorator


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def _default_worker_id() -> str:
    """Generate a unique worker id combining hostname and a random suffix.

    Two processes on the same host must not clash (they'd appear as a
    single worker in the audit view), so we append a short uuid.
    """
    host = socket.gethostname() or "unknown"
    return f"{host}-{uuid.uuid4().hex[:8]}"


class JobWorker:
    """Single-threaded worker that claims and executes jobs in a loop.

    Args:
        worker_id: Stable identifier written to ``background_jobs.claimed_by``.
            Defaults to ``<hostname>-<uuid8>`` so multiple workers on the
            same box do not collide.
        poll_interval: Seconds to sleep when the queue is empty. Kept low
            (2s) because ``stop_event.wait`` wakes immediately on shutdown.
    """

    def __init__(self, worker_id: str | None = None, poll_interval: float = 2.0) -> None:
        self.worker_id = worker_id or _default_worker_id()
        self.poll_interval = poll_interval

    def run_forever(self, stop_event: threading.Event) -> None:
        """Main loop. Runs until *stop_event* is set.

        Any exception raised by the queue itself (e.g. a transient
        Postgres disconnect) is logged and swallowed so the worker does
        not crash the entire thread. Handler exceptions are also
        contained — they flip the specific job to ``failed`` but never
        propagate out of the loop.
        """
        logger.info(
            "JobWorker %s starting (poll_interval=%ss)", self.worker_id, self.poll_interval
        )
        while not stop_event.is_set():
            try:
                self._tick(stop_event)
            except Exception:  # noqa: BLE001 — top-level guard, must never die
                logger.exception(
                    "JobWorker %s caught unexpected error in main loop", self.worker_id
                )
                # Back off so a hard-looping failure (e.g. DB down) does
                # not saturate the logs.
                stop_event.wait(self.poll_interval)
        logger.info("JobWorker %s stopped", self.worker_id)

    # -- internal -----------------------------------------------------------

    def _tick(self, stop_event: threading.Event) -> None:
        """Claim at most one job and dispatch it. May sleep if idle."""
        queue = JobQueue()
        job = queue.claim_next(self.worker_id)
        if job is None:
            # Idle — wait up to poll_interval, but wake immediately on shutdown.
            stop_event.wait(self.poll_interval)
            return

        handler = _HANDLERS.get(job.job_type)
        if handler is None:
            error = f"No handler registered for job type: {job.job_type}"
            logger.error("Job id=%d %s", job.id, error)
            queue.mark_failed(job.id, error)
            return

        queue.mark_running(job.id)
        try:
            result = handler(job.payload)
        except Exception as exc:  # noqa: BLE001 — handler errors flip job to failed
            tb = traceback.format_exc()
            logger.error(
                "Handler for job id=%d type=%s raised: %s\n%s",
                job.id,
                job.job_type,
                exc,
                tb,
            )
            # error_message column is VARCHAR(500); truncate defensively.
            queue.mark_failed(job.id, str(exc)[:500])
            return

        queue.mark_success(job.id, result)
        logger.info("Job id=%d type=%s completed successfully", job.id, job.job_type)


def start_worker_thread(stop_event: threading.Event) -> threading.Thread:
    """Spawn a :class:`JobWorker` in a daemon thread and return the thread.

    Daemon threads die automatically when the main interpreter exits,
    which is the right behaviour under ``uvicorn --reload`` and during
    test teardown; for graceful shutdown, callers should still set the
    ``stop_event`` and ``join`` the returned thread.
    """
    worker = JobWorker()
    thread = threading.Thread(
        target=worker.run_forever,
        args=(stop_event,),
        name=f"job-worker-{worker.worker_id}",
        daemon=True,
    )
    thread.start()
    return thread


# ---------------------------------------------------------------------------
# Phase 2 stub handlers
# ---------------------------------------------------------------------------
#
# These keep the dispatcher wired up for the four job types the Phase 2
# spec names. Later batches will remove each stub as the real
# implementation (Tika parsing, LLM entity extraction, SPARQL impact
# engine, .docx export) lands in its own module.


@register_handler("parse_draft")
def _parse_draft_stub(payload: dict[str, Any]) -> dict[str, Any]:
    logger.warning("parse_draft stub — real Tika integration lands in a later Phase 2 batch")
    return {"status": "stub", "note": "Tika not wired yet"}


@register_handler("extract_entities")
def _extract_entities_stub(payload: dict[str, Any]) -> dict[str, Any]:
    logger.warning("extract_entities stub — LLM extraction lands in a later Phase 2 batch")
    return {"status": "stub", "entities": []}


@register_handler("analyze_impact")
def _analyze_impact_stub(payload: dict[str, Any]) -> dict[str, Any]:
    logger.warning("analyze_impact stub — SPARQL impact engine lands in a later Phase 2 batch")
    return {"status": "stub", "affected": 0, "conflicts": 0}


@register_handler("export_report")
def _export_report_stub(payload: dict[str, Any]) -> dict[str, Any]:
    logger.warning("export_report stub — .docx export lands in a later Phase 2 batch")
    return {"status": "stub"}
