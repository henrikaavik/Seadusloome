"""Application-level performance metrics: recording and helpers.

Deliberately framework-free: this module imports no starlette/fasthtml so the
data layer (SPARQL client, LLM/RAG providers) and the standalone background
worker can call ``record_metric`` without dragging in the web framework (#895).
The starlette ASGI ``MetricsMiddleware`` that consumes ``record_metric`` lives
in ``app.metrics_middleware``.

Provides:
- ``record_metric(name, value, labels)`` — buffer a metric for periodic
  bulk INSERT into the ``metrics`` Postgres table (created by migration 011).
  Never blocks the caller; flushes every ``_FLUSH_INTERVAL`` seconds or when
  the buffer reaches ``_FLUSH_SIZE`` entries.
- ``track_duration(name, **labels)`` — context manager that measures a code
  block's wall-clock duration in milliseconds and records it.
"""

from __future__ import annotations

import atexit
import json
import logging
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Any

from app import config
from app.db import get_connection as _connect

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Buffered metric recording
# ---------------------------------------------------------------------------

# A bounded deque drops the oldest entries once full, so a DB outage can never
# let buffered telemetry grow without limit (metrics are best-effort, not
# durable — losing the oldest few under sustained failure is acceptable).
_BUFFER_MAXLEN = 10_000
_BUFFER: deque[tuple[str, float, str | None]] = deque(maxlen=_BUFFER_MAXLEN)
_FLUSH_INTERVAL = 10.0  # seconds
_FLUSH_SIZE = 100  # flush if buffer hits this many entries
_lock = threading.Lock()
_flush_timer: threading.Timer | None = None

# Single-flight guard: only one flush touches Postgres at a time. A flush that
# blocks (down DB / saturated pool waiting up to DB_POOL_TIMEOUT) would
# otherwise let every subsequent record_metric queue ANOTHER blocking flush,
# stacking N concurrent waits. With this flag the others no-op instead.
_flush_in_progress = False

# Failure backoff: after a flush can't reach Postgres we suppress further flush
# attempts for _FLUSH_BACKOFF seconds so we stop hammering an unavailable DB.
# Buffering still works (the deque just fills); the next flush after the
# cooldown drains it. Combined with the single-flight guard this caps the cost
# of a DB outage at one DB_POOL_TIMEOUT-bounded wait per cooldown window.
_FLUSH_BACKOFF = 30.0  # seconds
_flush_suppressed_until: float = 0.0

# Cap on any single label value so an unexpectedly long route template (mounts,
# catch-all paths) can never write an unbounded string into the labels JSONB.
_MAX_LABEL_LEN = 200

# ---------------------------------------------------------------------------
# Retention — self-contained, piggybacked on the flush path
# ---------------------------------------------------------------------------
#
# ``metrics`` is an append-only sink; without pruning it grows forever. We
# delete rows older than ``METRICS_RETENTION_DAYS`` (0 disables retention)
# opportunistically from inside ``_flush_buffer``, but at most once every
# ``_RETENTION_SWEEP_INTERVAL`` seconds so a busy flush path doesn't issue a
# DELETE on every batch. The (name, recorded_at) index from migration 011 plus
# the recorded_at index from migration 043 keep the sweep cheap.
_RETENTION_SWEEP_INTERVAL = 3600.0  # seconds — at most one sweep per hour
# ``None`` means "never swept yet" so the first flush always prunes. We must NOT
# use 0.0 as a "long ago" sentinel: ``time.monotonic()`` is relative to an
# arbitrary (often boot-relative) epoch, so on a freshly-booted host ``now``
# can be < _RETENTION_SWEEP_INTERVAL and ``now - 0.0`` would wrongly throttle
# the very first sweep.
_last_retention_sweep: float | None = None


def _retention_days() -> int:
    """Days of metrics history to keep; 0 (or invalid) disables retention."""
    raw = config.env_str("METRICS_RETENTION_DAYS", "30")
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _maybe_prune(conn: Any) -> None:
    """Delete metrics older than the retention window, throttled per interval.

    Runs on the existing flush connection (its own committed transaction — see
    ``_flush_buffer``) so retention never opens its own and a prune failure
    cannot roll back the metric rows that were just flushed.

    Concurrency + retry shape: the sweep slot is *claimed* eagerly under
    ``_lock`` (so two concurrent flushers never both issue the DELETE), but the
    timestamp is only allowed to *stick* once the DELETE commits. On failure the
    claim is rolled back to its previous value so the next flush retries instead
    of waiting a whole ``_RETENTION_SWEEP_INTERVAL``.

    Note the ``%s::interval`` cast: Postgres rejects the bare ``interval %s``
    placeholder form, so the parameter is bound as text and cast server-side.
    """
    global _last_retention_sweep

    days = _retention_days()
    if days <= 0:
        return

    now = time.monotonic()
    with _lock:
        # None => never swept => always due. Otherwise enforce the interval.
        if _last_retention_sweep is not None and (
            now - _last_retention_sweep < _RETENTION_SWEEP_INTERVAL
        ):
            return
        # Claim the slot so a concurrent flusher skips, remembering the prior
        # value so we can roll the claim back if the DELETE fails.
        previous_sweep = _last_retention_sweep
        _last_retention_sweep = now

    try:
        conn.execute(
            "DELETE FROM metrics WHERE recorded_at < now() - %s::interval",
            (f"{days} days",),
        )
        conn.commit()
    except Exception:
        # Clear the aborted transaction so the caller's connection is reusable
        # (a failed statement leaves it in InFailedSqlTransaction until rollback).
        try:
            conn.rollback()
        except Exception:
            logger.debug("metrics retention rollback failed", exc_info=True)
        # The prune didn't happen — release the claim (restore the prior value,
        # which is either None == "due now" or a valid earlier timestamp, both
        # correct on any monotonic epoch) so the next flush retries.
        with _lock:
            if _last_retention_sweep == now:
                _last_retention_sweep = previous_sweep
        raise


def _flush_suppressed() -> bool:
    """True while the post-failure flush cooldown is still in effect."""
    return time.monotonic() < _flush_suppressed_until


def _flush_buffer() -> None:
    """Bulk-INSERT all buffered metrics into Postgres."""
    global _flush_timer, _flush_in_progress, _flush_suppressed_until

    # Claim the single-flight slot and drain the buffer atomically. If another
    # flush is already running, or the failure cooldown is active, leave the
    # buffer intact and bail (the entries flush on a later attempt).
    with _lock:
        _flush_timer = None
        if _flush_in_progress or _flush_suppressed():
            return
        if not _BUFFER:
            return
        items: list[tuple[str, float, str | None]] = []
        while _BUFFER:
            items.append(_BUFFER.popleft())
        _flush_in_progress = True

    try:
        with _connect() as conn:
            cur = conn.cursor()
            cur.executemany(
                "INSERT INTO metrics (name, value, labels) VALUES (%s, %s, %s::jsonb)",
                items,
            )
            # Commit the INSERT *before* attempting retention so the metric rows
            # are durable regardless of what the prune does next.
            conn.commit()
            # Prune is best-effort and isolated: a retention failure must not
            # lose the just-committed rows nor trip the flush-failure backoff
            # below (the INSERT succeeded — the DB is clearly reachable).
            try:
                _maybe_prune(conn)
            except Exception:
                logger.debug("metrics retention prune failed", exc_info=True)
        # Clear any prior cooldown on a successful round-trip.
        with _lock:
            _flush_suppressed_until = 0.0
    except Exception:
        # The INSERT itself failed: re-buffer what we couldn't write (newest
        # entries win if the deque is full) and open the cooldown so we stop
        # hammering an unavailable DB.
        with _lock:
            _BUFFER.extendleft(reversed(items))
            _flush_suppressed_until = time.monotonic() + _FLUSH_BACKOFF
        logger.debug("Failed to flush %d metrics; backing off", len(items), exc_info=True)
    finally:
        with _lock:
            _flush_in_progress = False


def _schedule_flush() -> None:
    """Schedule a deferred flush if one isn't already pending.

    The check-and-set of ``_flush_timer`` runs entirely under ``_lock`` so two
    concurrent callers can't both observe ``None`` and start duplicate timers.
    Skipped while a flush is in flight or the failure cooldown is active.
    """
    global _flush_timer
    with _lock:
        if _flush_timer is not None or _flush_in_progress or _flush_suppressed():
            return
        timer = threading.Timer(_FLUSH_INTERVAL, _flush_buffer)
        timer.daemon = True
        _flush_timer = timer
    timer.start()


def record_metric(name: str, value: float, labels: dict[str, Any] | None = None) -> None:
    """Buffer a metric for bulk INSERT. Never blocks the caller."""
    try:
        labels_json = json.dumps(labels) if labels else None
        with _lock:
            _BUFFER.append((name, value, labels_json))
            buf_len = len(_BUFFER)
            # Don't open a fresh (potentially blocking) flush while one is
            # already running or during the post-failure cooldown.
            skip_flush = _flush_in_progress or _flush_suppressed()

        if skip_flush:
            return
        if buf_len >= _FLUSH_SIZE:
            # Flush immediately in a background thread
            threading.Thread(target=_flush_buffer, daemon=True).start()
        else:
            _schedule_flush()
    except Exception:
        logger.debug("Failed to buffer metric %s", name, exc_info=True)


# Final flush deadline at interpreter shutdown. A healthy DB drains the buffer
# in milliseconds; if Postgres is unreachable the daemon flusher thread is
# abandoned after this many seconds so a dead DB never wedges process exit.
_SHUTDOWN_FLUSH_TIMEOUT = 3.0


def _flush_on_shutdown() -> None:
    """Best-effort final flush, time-bounded so it can't block exit."""
    if not _BUFFER:
        return
    worker = threading.Thread(target=_flush_buffer, daemon=True)
    worker.start()
    worker.join(_SHUTDOWN_FLUSH_TIMEOUT)


# Flush remaining metrics on interpreter shutdown
atexit.register(_flush_on_shutdown)


# ---------------------------------------------------------------------------
# Context manager helper
# ---------------------------------------------------------------------------


@contextmanager
def track_duration(name: str, **labels: Any):  # type: ignore[no-untyped-def]
    """Measure a code block's wall-clock duration and record it as a metric.

    Usage::

        with track_duration("sparql_query_ms", query_type="category"):
            result = run_sparql(query)
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        record_metric(name, round(duration_ms, 2), labels if labels else None)
