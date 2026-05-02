"""In-memory pub/sub for draft pipeline status events (#608).

The chat module's WebSocket layer is one-shot per message; the draft
status WS is the opposite — a long-lived subscription that fires
whenever the worker thread transitions a draft through one of its
pipeline stages (``uploaded`` → ``parsing`` → ``extracting`` →
``analyzing`` → ``ready`` / ``failed``).

The worker thread runs in the same Python process as the web server
(see CLAUDE.md: "FOR UPDATE SKIP LOCKED job queue with worker thread"),
so an in-memory broadcast is sufficient for current single-process
deployment. If/when uvicorn ever runs multiple workers, this module is
where Postgres ``LISTEN`` / ``NOTIFY`` would slot in — the public
``subscribe`` / ``emit`` API stays the same; only the internal
fan-out becomes cross-process.

Cross-thread safety
-------------------

Subscribers are async send callables registered from the WS layer
(running on the asyncio event loop). Emits originate from two places:

1. The same event loop — when a handler running in async context
   needs to publish (rare: only the boot-time replay if any).
2. The sync worker thread — every pipeline status transition.

For (1) the public ``emit`` coroutine is awaited directly. For (2)
the helper ``emit_threadsafe`` schedules the coroutine onto the web
event loop via ``asyncio.run_coroutine_threadsafe``. The web process
captures the loop reference at startup via ``register_event_loop``.

The dispatch itself uses ``asyncio.gather(..., return_exceptions=True)``
so a single broken subscriber doesn't drop events for the rest. Dead
subscribers are silently removed when their send raises.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)


# Type alias for the per-connection send callable. Mirrors the shape
# the FastHTML WS handler hands us: an async callable that accepts a
# string payload and returns nothing.
SendCallable = Callable[[str], Awaitable[None]]


# Per-draft subscriber registry. Keyed by ``str(draft_id)`` so callers
# don't have to remember whether they hold a UUID or a str.
_subscribers: dict[str, set[SendCallable]] = {}

# Lock around _subscribers mutation. asyncio.Lock is used (not a
# threading.Lock) because all reads + writes happen on the event loop.
# The cross-thread emit path schedules a coroutine onto the loop so it
# enters this critical section the same way as in-loop callers.
_subscribers_lock = asyncio.Lock()


# The web process's event loop, captured at startup. ``emit_threadsafe``
# schedules coroutines onto this loop from worker threads. ``None``
# means we're running in a context that hasn't called
# :func:`register_event_loop` yet (test mode, or a stub run); in that
# case ``emit_threadsafe`` becomes a logged no-op.
_event_loop: asyncio.AbstractEventLoop | None = None


def register_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Capture the web process's event loop.

    Called once at startup from the FastHTML lifespan/init hook. The
    worker thread uses this reference to schedule
    :func:`emit_threadsafe` coroutines onto the right loop.
    """
    global _event_loop
    _event_loop = loop


async def subscribe(draft_id: Any, send: SendCallable) -> None:
    """Register *send* as a subscriber for status events on *draft_id*.

    Idempotent: registering the same callable twice is a no-op (the
    underlying ``set`` deduplicates).
    """
    key = str(draft_id)
    async with _subscribers_lock:
        _subscribers.setdefault(key, set()).add(send)


async def unsubscribe(draft_id: Any, send: SendCallable) -> None:
    """Remove *send* from the subscriber set for *draft_id*.

    Idempotent: unsubscribing a callable that isn't registered is a
    no-op. The slot is dropped entirely once it's empty so the registry
    doesn't accumulate dead keys.
    """
    key = str(draft_id)
    async with _subscribers_lock:
        bucket = _subscribers.get(key)
        if bucket is None:
            return
        bucket.discard(send)
        if not bucket:
            _subscribers.pop(key, None)


async def emit(draft_id: Any, **payload: Any) -> None:
    """Broadcast a status event to every subscriber of *draft_id*.

    The ``payload`` keyword args are merged with the draft id and JSON-
    encoded; typical shape::

        await emit(
            draft_id,
            type="status",
            status="ready",
            error_message=None,
        )

    Dead subscribers (send raised) are silently removed so transient
    socket failures don't accumulate noise. Non-existent draft ids are
    a no-op (no subscribers = nothing to do).
    """
    key = str(draft_id)
    async with _subscribers_lock:
        # Snapshot the bucket so we can iterate without holding the
        # lock while awaiting per-subscriber sends.
        bucket = _subscribers.get(key)
        targets = list(bucket) if bucket else []

    if not targets:
        return

    event_payload = {"draft_id": key, **payload}
    encoded = json.dumps(event_payload, default=str)

    # Fan out concurrently; collect exceptions per-subscriber so one
    # broken socket doesn't drop the event for the rest.
    results = await asyncio.gather(
        *(send(encoded) for send in targets),
        return_exceptions=True,
    )

    # Reap any subscribers whose send raised. Holding the lock again
    # is fine — small contention window vs. leaking dead callables.
    dead = [
        send
        for send, result in zip(targets, results, strict=False)
        if isinstance(result, Exception)
    ]
    if dead:
        async with _subscribers_lock:
            bucket = _subscribers.get(key)
            if bucket is not None:
                for send in dead:
                    bucket.discard(send)
                if not bucket:
                    _subscribers.pop(key, None)
        logger.debug("draft-status: removed %d dead subscribers for draft=%s", len(dead), key)


def emit_threadsafe(draft_id: Any, **payload: Any) -> None:
    """Schedule :func:`emit` onto the web event loop from a worker thread.

    The pipeline handlers (``parse_handler``, ``extract_handler``,
    ``analyze_handler``) run in a worker thread separate from the
    asyncio event loop. They call this helper on every status
    transition; it dispatches the actual fan-out onto the loop via
    ``asyncio.run_coroutine_threadsafe``.

    Failures are logged at DEBUG and swallowed — a stale subscriber or
    a missing event loop must never break the pipeline. The pipeline
    is the source of truth; the WS push is best-effort UX glue.
    """
    if _event_loop is None:
        # Web process hasn't started its event loop yet (or this is a
        # stub run / test). Drop silently.
        logger.debug(
            "draft-status: emit_threadsafe called before register_event_loop "
            "(draft=%s payload=%s) — dropping",
            draft_id,
            payload,
        )
        return

    try:
        asyncio.run_coroutine_threadsafe(emit(draft_id, **payload), _event_loop)
    except RuntimeError:
        # Loop closed mid-flight (shutdown). Pipeline keeps running.
        logger.debug("draft-status: event loop closed while emitting draft=%s", draft_id)
    except Exception:
        # Defence in depth — never raise into the pipeline.
        logger.debug(
            "draft-status: emit_threadsafe failed for draft=%s",
            draft_id,
            exc_info=True,
        )


async def subscriber_count(draft_id: Any) -> int:
    """Return the number of active subscribers for *draft_id*.

    Test-only helper. Kept on the public API so tests don't have to
    poke at private state.
    """
    key = str(draft_id)
    async with _subscribers_lock:
        bucket = _subscribers.get(key)
        return len(bucket) if bucket else 0
