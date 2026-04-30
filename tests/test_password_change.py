"""Tests for ``app.auth.password.change_password``.

The function is expected to:

1. Compute a fresh bcrypt hash (different from the old hash);
2. UPDATE ``users`` setting ``password_hash``,
   ``token_version = token_version + 1``,
   ``must_change_password = <flag>``,
   ``password_changed_at = now()``;
3. DELETE ``sessions`` rows for the user;
4. Commit on success, roll back on failure;
5. Run all of the above in one transaction so a partial failure
   cannot leave a half-rotated row.

We verify the SQL shapes against a mocked ``psycopg.Connection`` so
the tests do not need a live database. A second test exercises the
rollback branch by raising inside the second ``execute`` call.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import bcrypt
import pytest

from app.auth.password import change_password


def _make_conn() -> MagicMock:
    """Return a MagicMock that mimics ``psycopg.Connection`` enough.

    The function under test calls ``conn.execute(...)`` twice and then
    ``conn.commit()``; on exception it calls ``conn.rollback()``.
    """
    return MagicMock()


def test_change_password_writes_bcrypt_hash():
    conn = _make_conn()
    change_password("user-1", "NewPassword1", conn=conn, must_change=False)

    # First execute = UPDATE users.
    update_call = conn.execute.call_args_list[0]
    sql = update_call[0][0]
    args = update_call[0][1]

    assert "UPDATE users" in sql
    assert "password_hash" in sql
    # The hash must verify against the new password.
    new_hash = args[0]
    assert bcrypt.checkpw(b"NewPassword1", new_hash.encode())


def test_change_password_bumps_token_version():
    conn = _make_conn()
    change_password("user-1", "NewPassword1", conn=conn, must_change=False)

    update_sql = conn.execute.call_args_list[0][0][0]
    # The single UPDATE must touch token_version so existing access
    # tokens (which embed the old ``tv`` value) are immediately rejected.
    assert "token_version = token_version + 1" in update_sql


def test_change_password_sets_password_changed_at_now():
    conn = _make_conn()
    change_password("user-1", "NewPassword1", conn=conn, must_change=False)

    update_sql = conn.execute.call_args_list[0][0][0]
    assert "password_changed_at = now()" in update_sql


def test_change_password_clears_must_change_by_default():
    conn = _make_conn()
    change_password("user-1", "NewPassword1", conn=conn)

    update_call = conn.execute.call_args_list[0]
    sql = update_call[0][0]
    args = update_call[0][1]
    assert "must_change_password = %s" in sql
    # default ``must_change=False`` -> stored as False.
    assert args[1] is False


def test_change_password_sets_must_change_when_requested():
    conn = _make_conn()
    change_password("user-1", "NewPassword1", conn=conn, must_change=True)

    args = conn.execute.call_args_list[0][0][1]
    # must_change_password flag is the second bound argument.
    assert args[1] is True


def test_change_password_deletes_sessions_for_user():
    conn = _make_conn()
    change_password("user-42", "NewPassword1", conn=conn, must_change=False)

    delete_call = conn.execute.call_args_list[1]
    sql = delete_call[0][0]
    args = delete_call[0][1]
    assert "DELETE FROM sessions" in sql
    assert args == ("user-42",)


def test_change_password_runs_inside_transaction_block():
    """The UPDATE + DELETE must run inside a single ``conn.transaction()``
    block so a partial failure rolls back atomically.

    The donor (``app.auth.password``) uses psycopg3's
    ``with conn.transaction():`` context manager rather than explicit
    ``conn.commit()`` / ``conn.rollback()`` calls. The contract — atomic
    rotation — is the same; the implementation idiom is the more modern
    psycopg3 form.
    """
    conn = _make_conn()
    change_password("user-1", "NewPassword1", conn=conn, must_change=False)
    # Entered the transaction context manager exactly once.
    conn.transaction.assert_called_once()
    conn.transaction.return_value.__enter__.assert_called_once()
    conn.transaction.return_value.__exit__.assert_called_once()


def test_change_password_propagates_failure_for_rollback():
    """If the DELETE fails, the exception must propagate so the
    ``with conn.transaction():`` context manager rolls back the UPDATE.

    psycopg3's ``transaction()`` context manager issues BEGIN on enter
    and either COMMIT on clean exit or ROLLBACK when an exception
    propagates out of the ``with`` block.
    """
    conn = _make_conn()
    # First execute (UPDATE) succeeds; second (DELETE) raises.
    conn.execute.side_effect = [MagicMock(), RuntimeError("simulated failure")]

    with pytest.raises(RuntimeError, match="simulated failure"):
        change_password("user-1", "NewPassword1", conn=conn, must_change=False)

    # Transaction was entered, then __exit__ was called with the exception
    # tuple (rollback path) — first positional arg is the exc_type, not None.
    conn.transaction.return_value.__exit__.assert_called_once()
    exit_args = conn.transaction.return_value.__exit__.call_args[0]
    assert exit_args[0] is RuntimeError


def test_change_password_targets_correct_user_id():
    conn = _make_conn()
    change_password("uid-99", "NewPassword1", conn=conn, must_change=False)

    update_args = conn.execute.call_args_list[0][0][1]
    # Order: (pw_hash, must_change, user_id)
    assert update_args[2] == "uid-99"
