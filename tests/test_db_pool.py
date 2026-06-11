"""Tests for the connection pool behind app.db.get_connection (#861).

These tests stub the pool with a MagicMock so no real Postgres is needed; they
verify the wrapper contract that every existing call site depends on:

* ``with get_connection() as conn:`` commits on a clean exit, rolls back on an
  exception, and returns the connection to the pool exactly once;
* a bare ``conn = get_connection()`` handle returns to the pool on ``close()``;
* the pool is a lazily-initialised, thread-safe singleton.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pytest

import app.db as db


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Ensure each test starts and ends with no live pool singleton."""
    db._reset_pool()
    yield
    db._reset_pool()


def _fake_pool() -> tuple[MagicMock, MagicMock]:
    """Return (pool, conn) where pool.getconn() always hands out *conn*."""
    conn = MagicMock(name="connection")
    conn.closed = False
    pool = MagicMock(name="ConnectionPool")
    pool.getconn.return_value = conn
    return pool, conn


def test_get_connection_is_lazy_singleton():
    pool, _conn = _fake_pool()
    with patch.object(db, "_create_pool", return_value=pool) as create:
        assert db._pool is None  # not created at import / fixture time
        db.get_connection()
        db.get_connection()
        # Pool constructed exactly once despite two checkouts.
        create.assert_called_once()
    assert db._pool is pool


def test_context_manager_commits_and_returns_to_pool():
    pool, conn = _fake_pool()
    with patch.object(db, "_create_pool", return_value=pool):
        with db.get_connection():
            pass
        conn.commit.assert_called_once()
        conn.rollback.assert_not_called()
        # Returned to the pool exactly once.
        pool.putconn.assert_called_once_with(conn)


def test_context_manager_rolls_back_on_exception():
    pool, conn = _fake_pool()
    with patch.object(db, "_create_pool", return_value=pool):
        with pytest.raises(ValueError):
            with db.get_connection():
                raise ValueError("boom")
        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()
        pool.putconn.assert_called_once_with(conn)


def test_bare_handle_returns_to_pool_on_close():
    pool, conn = _fake_pool()
    with patch.object(db, "_create_pool", return_value=pool):
        handle = db.get_connection()
        # Attribute access forwards to the underlying connection.
        handle.execute("SELECT 1")
        conn.execute.assert_called_once_with("SELECT 1")
        handle.close()
        pool.putconn.assert_called_once_with(conn)


def test_return_is_idempotent():
    """A double close() must return the connection only once."""
    pool, _conn = _fake_pool()
    with patch.object(db, "_create_pool", return_value=pool):
        handle = db.get_connection()
        handle.close()
        handle.close()
        pool.putconn.assert_called_once()


def test_exit_returns_to_pool_even_if_commit_fails():
    pool, conn = _fake_pool()
    conn.commit.side_effect = RuntimeError("commit failed")
    with patch.object(db, "_create_pool", return_value=pool):
        with pytest.raises(RuntimeError):
            with db.get_connection():
                pass
        # Connection still handed back so the pool isn't leaked.
        pool.putconn.assert_called_once_with(conn)


def test_pool_sizing_env(monkeypatch: pytest.MonkeyPatch):
    """_create_pool honours DB_POOL_MIN / DB_POOL_MAX / DB_POOL_TIMEOUT."""
    captured: dict[str, Any] = {}

    class _FakePool:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def close(self) -> None:
            pass

    # _POOL_MIN/_POOL_MAX/_POOL_TIMEOUT are read at import time, so patch the
    # module globals the factory actually reads.
    monkeypatch.setattr(db, "_POOL_MIN", 2)
    monkeypatch.setattr(db, "_POOL_MAX", 7)
    monkeypatch.setattr(db, "_POOL_TIMEOUT", 5.0)
    monkeypatch.setattr(db, "_CONNECT_TIMEOUT", 4)
    with patch("psycopg_pool.ConnectionPool", _FakePool):
        pool = db._create_pool()

    assert isinstance(pool, _FakePool)
    assert captured["min_size"] == 2
    assert captured["max_size"] == 7
    assert captured["timeout"] == 5.0
    assert captured["kwargs"] == {"connect_timeout": 4}
    assert captured["conninfo"] == db.DATABASE_URL


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


def test_checkout_failure_trips_breaker_for_fast_subsequent_failures(
    monkeypatch: pytest.MonkeyPatch,
):
    """A getconn failure trips the breaker; the next call fails immediately."""
    monkeypatch.setattr(db, "_BREAKER_COOLDOWN", 30.0)
    pool = MagicMock(name="ConnectionPool")
    pool.getconn.side_effect = RuntimeError("pool timeout")

    with patch.object(db, "_create_pool", return_value=pool):
        # First call hits the pool (and pays its wait), then trips the breaker.
        with pytest.raises(RuntimeError):
            db.get_connection()
        assert pool.getconn.call_count == 1

        # While the breaker is open the pool is NOT consulted again — the call
        # fails fast with an OperationalError instead of waiting on getconn.
        with pytest.raises(psycopg.OperationalError):
            db.get_connection()
        assert pool.getconn.call_count == 1


def test_breaker_reopens_after_cooldown(monkeypatch: pytest.MonkeyPatch):
    """Once the cooldown elapses, get_connection consults the pool again."""
    monkeypatch.setattr(db, "_BREAKER_COOLDOWN", 30.0)
    pool, conn = _fake_pool()
    pool.getconn.side_effect = [RuntimeError("boom"), conn]

    fake_now = [1000.0]
    monkeypatch.setattr(db.time, "monotonic", lambda: fake_now[0])

    with patch.object(db, "_create_pool", return_value=pool):
        with pytest.raises(RuntimeError):
            db.get_connection()
        # Still in cooldown -> fast fail, pool untouched.
        with pytest.raises(psycopg.OperationalError):
            db.get_connection()
        # Advance past the cooldown; the pool is consulted again and succeeds.
        fake_now[0] += 31.0
        handle = db.get_connection()
        handle.close()
        assert pool.getconn.call_count == 2


def test_reset_pool_clears_breaker(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(db, "_BREAKER_COOLDOWN", 30.0)
    pool = MagicMock(name="ConnectionPool")
    pool.getconn.side_effect = RuntimeError("boom")
    with patch.object(db, "_create_pool", return_value=pool):
        with pytest.raises(RuntimeError):
            db.get_connection()
    # _reset_pool (autouse teardown also does this) must clear the open breaker.
    db._reset_pool()
    assert db._unavailable_until == 0.0
