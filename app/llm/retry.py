"""Retry helpers for transient LLM API failures (#354).

Wraps Anthropic SDK calls with bounded exponential backoff so transient
errors (rate limits, 5xx, network blips) don't bubble up to handlers as
hard failures. Permanent errors (400 / 401 / 403) fail fast because they
won't fix themselves on retry.

Counting convention
-------------------
The DoD wording "up to 3 attempts (1s, 5s, 30s)" is read as:

* ``_BACKOFF = [1.0, 5.0, 30.0]`` — three sleep durations.
* ``MAX_RETRIES = 3`` — retry up to three times, so the worst case is
  one initial attempt + three retries = four calls in total.
* On a permanent error the call raises immediately with zero retries.

Streaming caveat
----------------
We only retry the *stream-open* call, never the iteration. If a stream
has already begun emitting deltas and dies mid-flight, restarting it
would produce duplicate output that the chat orchestrator can't safely
reconcile. The handler sees the original error and decides what to do.

Cost-tracking caveat
--------------------
Retries here wrap the SDK call only. ``log_usage`` is invoked *after*
the wrapped call returns successfully (in the existing call sites in
``app.llm.claude``), so a retried-then-succeeded request still logs
exactly one usage row, and a fully failed request logs none.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# Status codes that warrant retry — transient server / rate-limit conditions.
_RETRYABLE_HTTP: frozenset[int] = frozenset({429, 500, 502, 503, 504})

# Status codes that must fail fast — broken request / auth / permissions.
_NON_RETRYABLE_HTTP: frozenset[int] = frozenset({400, 401, 403})

# Sleep durations (seconds) before retries 1, 2, 3.
_BACKOFF: tuple[float, ...] = (1.0, 5.0, 30.0)

# Total retries after the initial attempt. Worst case = 1 + MAX_RETRIES calls.
MAX_RETRIES: int = 3


def _http_status(exc: BaseException) -> int | None:
    """Best-effort extraction of an HTTP status code from an exception.

    Anthropic SDK ``APIStatusError`` subclasses expose ``.status_code``
    directly; we also check ``.response.status_code`` for httpx-shaped
    errors. Returns ``None`` when the exception isn't tied to an HTTP
    response (e.g. ``ConnectionError``, ``TimeoutError``).
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status
    return None


def _is_retryable(exc: BaseException) -> bool:
    """Decide whether *exc* represents a transient failure worth retrying.

    Policy:
    * HTTP status in ``_RETRYABLE_HTTP`` → retry.
    * HTTP status in ``_NON_RETRYABLE_HTTP`` → never retry.
    * No HTTP status (network/timeout) → retry.
    * Other HTTP statuses (e.g. 404, 409, 422) → don't retry; the caller's
      payload is wrong, not the server.
    """
    # Network / timeout errors carry no status code.
    if isinstance(exc, TimeoutError | ConnectionError):
        return True

    try:
        import anthropic
    except ImportError:  # pragma: no cover — anthropic is a hard dep
        anthropic = None  # type: ignore[assignment]

    if anthropic is not None:
        # APIConnectionError covers timeouts and connection drops in the
        # Anthropic SDK; they don't carry a status code.
        if isinstance(exc, anthropic.APIConnectionError):
            return True

    try:
        import httpx
    except ImportError:  # pragma: no cover — httpx is a hard dep
        httpx = None  # type: ignore[assignment]

    if httpx is not None:
        # Generic httpx transport errors (DNS, timeouts) — no status.
        if isinstance(exc, httpx.TransportError):
            return True

    status = _http_status(exc)
    if status is None:
        # Unknown shape — be conservative and DON'T retry. Random bugs
        # shouldn't trigger 3 extra calls.
        return False
    if status in _NON_RETRYABLE_HTTP:
        return False
    return status in _RETRYABLE_HTTP


def _wait_for_attempt(attempt: int) -> float:
    """Return the sleep duration before the *attempt*-th retry (1-indexed).

    ``attempt=1`` → wait before the first retry (after the initial call
    failed). Falls back to the last backoff slot for attempts that exceed
    the configured table — defensive, shouldn't trigger under MAX_RETRIES.
    """
    if attempt <= 0:
        return 0.0
    if attempt - 1 < len(_BACKOFF):
        return _BACKOFF[attempt - 1]
    return _BACKOFF[-1]


def retry_sync[T](
    fn: Callable[[], T],
    *,
    context: str = "anthropic",
) -> T:
    """Call *fn* with bounded retries on transient errors (sync).

    Args:
        fn: Zero-arg callable that performs the actual SDK call. Use a
            closure or ``functools.partial`` to bind the real arguments.
        context: Short label inserted into log messages (e.g. ``"complete"``,
            ``"acomplete"``, ``"astream-open"``).

    Returns:
        Whatever *fn* returns on its first successful attempt.

    Raises:
        The last underlying exception when retries are exhausted, or
        immediately when the error is non-retryable.
    """
    last_exc: BaseException | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if not _is_retryable(exc):
                logger.warning(
                    "LLM %s permanent error (no retry): %s: %s",
                    context,
                    type(exc).__name__,
                    exc,
                )
                raise
            if attempt >= MAX_RETRIES:
                logger.warning(
                    "LLM %s retries exhausted after %d attempts: %s: %s",
                    context,
                    attempt + 1,
                    type(exc).__name__,
                    exc,
                )
                raise
            wait = _wait_for_attempt(attempt + 1)
            logger.warning(
                "LLM %s retry attempt %d after %.1fs: %s: %s",
                context,
                attempt + 1,
                wait,
                type(exc).__name__,
                exc,
            )
            time.sleep(wait)
    # Unreachable — the loop either returns or raises — but mypy wants it.
    assert last_exc is not None  # pragma: no cover
    raise last_exc  # pragma: no cover


async def retry_async[T](
    fn: Callable[[], Awaitable[T]],
    *,
    context: str = "anthropic-async",
) -> T:
    """Async counterpart of :func:`retry_sync`.

    Uses ``asyncio.sleep`` between attempts so the event loop isn't
    blocked while we back off.
    """
    last_exc: BaseException | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            return await fn()
        except Exception as exc:
            last_exc = exc
            if not _is_retryable(exc):
                logger.warning(
                    "LLM %s permanent error (no retry): %s: %s",
                    context,
                    type(exc).__name__,
                    exc,
                )
                raise
            if attempt >= MAX_RETRIES:
                logger.warning(
                    "LLM %s retries exhausted after %d attempts: %s: %s",
                    context,
                    attempt + 1,
                    type(exc).__name__,
                    exc,
                )
                raise
            wait = _wait_for_attempt(attempt + 1)
            logger.warning(
                "LLM %s retry attempt %d after %.1fs: %s: %s",
                context,
                attempt + 1,
                wait,
                type(exc).__name__,
                exc,
            )
            await asyncio.sleep(wait)
    assert last_exc is not None  # pragma: no cover
    raise last_exc  # pragma: no cover
