"""Centralised database connection helper.

Every module that needs a PostgreSQL connection should import from here
rather than duplicating the ``DATABASE_URL`` lookup and ``psycopg.connect`` call.

A process-wide :class:`psycopg_pool.ConnectionPool` backs :func:`get_connection`
so the dashboard's ~10 serial widget queries, a document-detail render's 3
queries, every metric flush and every job-poll reuse pooled connections
instead of paying a fresh TCP+TLS+auth handshake each time.

The pool is created lazily on first use behind a thread-safe lock (the same
double-checked-locking singleton pattern as ``ClaudeProvider`` /
``SparqlClient``) so importing this module never opens a socket — stub-mode
callers and unit tests that monkeypatch :func:`get_connection` never touch a
real database.

``get_connection()`` keeps its original public contract: it returns a
``psycopg`` connection usable BOTH as a context manager
(``with get_connection() as conn: ...`` — the connection is committed/rolled
back and returned to the pool on exit) AND as a bare handle
(``conn = get_connection(); ...; conn.close()`` — ``close()`` returns it to the
pool). The advisory-lock holder in ``app/sync/orchestrator.py`` relies on the
bare form.
"""

from __future__ import annotations

import atexit
import os
import threading
import time
from typing import TYPE_CHECKING, Any

import psycopg

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

# Dev-only fallback. In any non-development environment a missing
# DATABASE_URL is a hard failure so we don't silently talk to a local DB.
_DEV_DATABASE_URL = "postgresql://seadusloome:localdev@localhost:5432/seadusloome"


def _load_database_url() -> str:
    """Return the DATABASE_URL, enforcing an explicit value off-dev."""
    value = os.environ.get("DATABASE_URL")
    if value:
        return value
    if os.environ.get("APP_ENV", "development") == "development":
        return _DEV_DATABASE_URL
    raise RuntimeError("DATABASE_URL must be set in non-development environments")


DATABASE_URL = _load_database_url()


# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------
#
# Sizing: defaults of 1/10 are deliberately conservative. The background job
# worker thread holds a connection only for the duration of a single
# ``FOR UPDATE SKIP LOCKED`` claim (claim -> commit -> release in one short
# ``with`` block), and the WebSocket DB watcher loops likewise check out a
# connection per poll iteration (each offloaded via ``asyncio.to_thread``) and
# return it immediately -- neither holds a pooled connection across iterations.
# The one genuinely long-lived holder is the sync advisory-lock connection,
# which uses the *bare* form and is closed (returned) the moment the sync
# finishes. With 5-50 concurrent officials a max of 10 leaves ample headroom
# under Postgres' default ``max_connections`` while collapsing the
# connection-per-query storm that motivated this change. Override with
# ``DB_POOL_MIN`` / ``DB_POOL_MAX`` per deployment.
_POOL_MIN = max(0, int(os.environ.get("DB_POOL_MIN", "1")))
_POOL_MAX = max(1, int(os.environ.get("DB_POOL_MAX", "10")))
# Seconds getconn() blocks waiting for the pool to hand back a connection
# before raising PoolTimeout. psycopg_pool always routes getconn through its
# background maintenance worker, so this also bounds how long a *single* call
# waits when Postgres is unreachable. Kept short (2s): with max_size=10 and
# short-lived checkouts a longer wait only signals pathological saturation or a
# dead DB (a healthy pool hands back a connection instantly). The circuit
# breaker below keeps a dead DB from making *every* subsequent call pay even
# this wait. Raise ``DB_POOL_TIMEOUT`` in production if a connection-storm
# warm-up is expected.
_POOL_TIMEOUT = float(os.environ.get("DB_POOL_TIMEOUT", "2"))
# Per-attempt TCP/auth connect timeout, applied via the libpq ``connect_timeout``
# keyword so a dead host fails in seconds rather than the OS default (~75s).
_CONNECT_TIMEOUT = int(os.environ.get("DB_CONNECT_TIMEOUT", "5"))
# Circuit breaker: when a checkout fails (PoolTimeout / OperationalError) the
# DB is treated as unavailable for this cooldown, during which get_connection()
# fails *immediately* instead of making each caller wait DB_POOL_TIMEOUT again.
# This restores the near-instant failure of the old bare ``psycopg.connect`` on
# an unreachable DB (so a DB-less environment — e.g. the unit-test CI job that
# runs without a Postgres service — does not serialise into one timeout per
# call) while still pooling normally when the DB is up. Kept short so a
# transient blip self-heals within a couple of seconds of DB recovery.
_BREAKER_COOLDOWN = float(os.environ.get("DB_BREAKER_COOLDOWN", "2"))

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()
_unavailable_until = 0.0


def _create_pool() -> ConnectionPool:
    """Construct the process-wide connection pool.

    Imported lazily so the ``psycopg_pool`` dependency is only loaded when a
    real connection is first opened -- never at import time, so stub-mode
    callers and tests that patch :func:`get_connection` stay decoupled from it.
    """
    from psycopg_pool import ConnectionPool

    return ConnectionPool(
        conninfo=DATABASE_URL,
        min_size=min(_POOL_MIN, _POOL_MAX),
        max_size=_POOL_MAX,
        # Block (rather than raise) for up to DB_POOL_TIMEOUT seconds if every
        # pooled connection is checked out before giving up.
        timeout=_POOL_TIMEOUT,
        # Bound each underlying connection attempt so an unreachable host can't
        # wedge a caller (or interpreter shutdown) for the OS default.
        kwargs={"connect_timeout": _CONNECT_TIMEOUT},
        open=True,
    )


def _get_pool() -> ConnectionPool:
    """Return the lazily-initialised singleton pool (double-checked lock)."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = _create_pool()
    return _pool


def _reset_pool() -> None:
    """Close and drop the singleton pool (for tests / clean shutdown)."""
    global _pool, _unavailable_until
    with _pool_lock:
        _unavailable_until = 0.0
        if _pool is not None:
            try:
                _pool.close()
            finally:
                _pool = None


# Close the pool on interpreter shutdown so its background maintenance thread
# stops and psycopg_pool doesn't emit an "unclosed pool" warning. No-op when
# the pool was never opened (the common case for stub-mode / test processes).
atexit.register(_reset_pool)


class _PooledConnection:
    """Context-manager + bare-handle wrapper around a pooled connection.

    Forwards every attribute (``execute``, ``cursor``, ``commit``, ...) to the
    underlying ``psycopg`` connection so existing call sites are untouched.
    The connection is returned to the pool exactly once, whether the caller
    leaves a ``with`` block (``__exit__``) or calls ``close()`` on a bare
    handle -- never both, never neither.

    On ``with``-block exit psycopg's own transaction semantics are preserved:
    a clean exit commits, an exception rolls back, before the connection goes
    back to the pool.
    """

    __slots__ = ("_conn", "_pool", "_returned")

    def __init__(self, pool: ConnectionPool, conn: psycopg.Connection) -> None:  # type: ignore[type-arg]
        self._pool = pool
        self._conn = conn
        self._returned = False

    # -- bare-handle / attribute forwarding ---------------------------------

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes not defined on the wrapper itself
        # (``_conn`` / ``_pool`` / ``_returned`` live in ``__slots__``).
        return getattr(self._conn, name)

    def close(self) -> None:
        """Return the connection to the pool (idempotent)."""
        self._return()

    def _return(self) -> None:
        if self._returned:
            return
        self._returned = True
        self._pool.putconn(self._conn)

    # -- context-manager protocol -------------------------------------------

    def __enter__(self) -> psycopg.Connection:  # type: ignore[type-arg]
        return self._conn

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        # Mirror psycopg.Connection.__exit__: commit on success, roll back on
        # error, then hand the connection back to the pool.
        try:
            if not self._conn.closed:
                if exc_type is None:
                    self._conn.commit()
                else:
                    self._conn.rollback()
        finally:
            self._return()
        return False


def get_connection() -> psycopg.Connection:  # type: ignore[type-arg]
    """Return a pooled ``psycopg`` connection using the shared ``DATABASE_URL``.

    The return value behaves like the bare ``psycopg.connect(...)`` result it
    replaced: use it as a context manager (``with get_connection() as conn:`` --
    committed/rolled-back and returned to the pool on exit) or as a plain handle
    (call ``conn.close()`` to return it to the pool).

    A short circuit breaker (``_BREAKER_COOLDOWN``) trips on a checkout failure
    so that, while the DB is unreachable, callers fail fast instead of each
    waiting ``DB_POOL_TIMEOUT`` — matching the near-instant failure of the old
    bare ``psycopg.connect``.
    """
    global _unavailable_until

    if time.monotonic() < _unavailable_until:
        raise psycopg.OperationalError(
            "database unavailable (connection pool circuit breaker open)"
        )

    pool = _get_pool()
    try:
        conn = pool.getconn()
    except Exception:
        # Trip the breaker so the next callers within the cooldown fail fast
        # rather than each blocking for the full pool timeout.
        _unavailable_until = time.monotonic() + _BREAKER_COOLDOWN
        raise
    return _PooledConnection(pool, conn)  # type: ignore[return-value]
