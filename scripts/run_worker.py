"""Standalone background worker entrypoint (#348).

Runs the same :class:`app.jobs.worker.JobWorker` that the in-process
mode uses, but without importing FastHTML / Starlette. Intended for
deployment as a second Coolify container that scales independently
from the web container.

Usage:
    WORKER_MODE=standalone python scripts/run_worker.py

Or via the console-script entrypoint declared in ``pyproject.toml``:

    WORKER_MODE=standalone seadusloome-worker

Lifecycle:
    - Set up logging (stdlib only — no structlog dependency).
    - Validate ``WORKER_MODE=standalone``; refuse to start in any other
      mode so an operator who forgot to flip the env var on the new
      container does not silently end up double-running with inproc.
    - Import every handler module via
      :func:`app.jobs.registry.register_all_handlers` so the dispatch
      registry is populated before the first job is claimed.
    - Install SIGTERM / SIGINT handlers that set the shared stop event
      so the worker drains its in-flight job and exits cleanly.
    - Block on :meth:`JobWorker.run_forever` until the stop event fires.

Why not also start the archive-warning scheduler here?
    A single daily scan must not run on multiple worker processes at
    once (each process would emit duplicate notifications). The
    scheduler stays co-located with the FastHTML web process, which is
    guaranteed to exist exactly once per deployment. If we ever want
    to scale web to N replicas, we will need a dedicated cron container
    — but that is out of scope for #348.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from types import FrameType
from typing import NoReturn

logger = logging.getLogger("seadusloome.worker")


def _configure_logging() -> None:
    """Configure stdlib logging matching the format used by ``app.main``.

    Kept deliberately simple: no structlog dependency (the worker
    container should be importable on a stripped-down image if
    needed). The format mirrors the one configured by uvicorn in the
    web container so log aggregation tooling sees consistent fields.
    """
    if logging.getLogger().handlers:
        # Already configured (e.g. when imported under pytest).
        return
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def _install_signal_handlers(stop_event: threading.Event) -> None:
    """Wire SIGTERM / SIGINT to set *stop_event* for graceful shutdown.

    Docker / Coolify deliver SIGTERM on container stop; an interactive
    Ctrl-C sends SIGINT. Both must drain the in-flight job (the worker
    loop checks ``stop_event`` between job claims) rather than
    abandoning a half-processed row in ``status='running'``.

    The handler is intentionally idempotent — repeated signals just
    re-set an already-set Event, which is a no-op.
    """

    def _handle(signum: int, _frame: FrameType | None) -> None:
        try:
            sig_name = signal.Signals(signum).name
        except ValueError:
            sig_name = str(signum)
        logger.info("Received %s — initiating graceful shutdown", sig_name)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


def main() -> NoReturn:
    """Standalone worker entrypoint. Blocks until SIGTERM / SIGINT.

    Exit codes:
        0 — clean shutdown via signal.
        1 — configuration error (e.g. ``WORKER_MODE != "standalone"``).
    """
    _configure_logging()

    # Import lazily so that even ``--help`` style invocations or import
    # checks (e.g. the test that asserts FastHTML is not loaded) avoid
    # any DB / Postgres pool side effects until main() is actually run.
    from app.config import get_worker_mode

    try:
        mode = get_worker_mode()
    except ValueError as exc:
        logger.error("Invalid WORKER_MODE: %s", exc)
        sys.exit(1)

    if mode != "standalone":
        logger.error(
            "scripts/run_worker.py refuses to start when WORKER_MODE=%r. "
            "Set WORKER_MODE=standalone on this container (the web "
            "container should keep WORKER_MODE=inproc or leave it unset). "
            "See docs/operations/worker-modes.md for the rationale.",
            mode,
        )
        sys.exit(1)

    from app.jobs.registry import register_all_handlers
    from app.jobs.worker import _HANDLERS, JobWorker

    register_all_handlers()
    logger.info(
        "Standalone worker: registered %d handler(s): %s",
        len(_HANDLERS),
        sorted(_HANDLERS.keys()),
    )

    stop_event = threading.Event()
    _install_signal_handlers(stop_event)

    worker = JobWorker()
    logger.info(
        "Starting standalone JobWorker worker_id=%s poll_interval=%.1fs",
        worker.worker_id,
        worker.poll_interval,
    )
    try:
        worker.run_forever(stop_event)
    except KeyboardInterrupt:
        # Unlikely (SIGINT handler already sets stop_event), but defensive
        # in case run_forever is interrupted before the handler installs.
        logger.info("KeyboardInterrupt — exiting")

    logger.info("Standalone worker exited cleanly")
    sys.exit(0)


if __name__ == "__main__":
    main()
