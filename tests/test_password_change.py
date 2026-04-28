"""Integration tests for change_password() against a live Postgres."""

import os
import threading
import uuid
from datetime import UTC, datetime, timedelta

import bcrypt
import psycopg
import pytest

from app.auth.password import change_password, claim_reset_token, issue_reset_token


def _connect() -> psycopg.Connection:
    return psycopg.connect(os.environ.get("DATABASE_URL", ""))


@pytest.fixture
def temp_user():
    """Create a one-off user, yield its row dict, delete in teardown."""
    user_id = uuid.uuid4()
    pw_hash = bcrypt.hashpw(b"Initial1A", bcrypt.gensalt()).decode()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO users (id, email, password_hash, full_name, role) "
            "VALUES (%s, %s, %s, %s, 'drafter')",
            (user_id, f"pw-test-{user_id}@example.com", pw_hash, "PW Test"),
        )
        conn.execute(
            "INSERT INTO sessions (user_id, token_hash, expires_at) VALUES (%s, %s, %s)",
            (user_id, "fake-hash", datetime.now(UTC) + timedelta(days=1)),
        )
        conn.commit()
    yield {"id": str(user_id)}
    with _connect() as conn:
        conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.commit()


def test_change_password_writes_bcrypt_and_bumps_token_version(temp_user):
    with _connect() as conn:
        before = conn.execute(
            "SELECT token_version FROM users WHERE id = %s",
            (temp_user["id"],),
        ).fetchone()
        change_password(temp_user["id"], "NewPass1Z", conn=conn)
        conn.commit()
        after = conn.execute(
            "SELECT password_hash, token_version, must_change_password, password_changed_at "
            "FROM users WHERE id = %s",
            (temp_user["id"],),
        ).fetchone()
    pw_hash, tv, must_change, changed_at = after
    assert bcrypt.checkpw(b"NewPass1Z", pw_hash.encode())
    assert tv == before[0] + 1
    assert must_change is False
    assert changed_at is not None


def test_change_password_deletes_all_sessions(temp_user):
    with _connect() as conn:
        change_password(temp_user["id"], "NewPass1Z", conn=conn)
        conn.commit()
        rows = conn.execute(
            "SELECT 1 FROM sessions WHERE user_id = %s",
            (temp_user["id"],),
        ).fetchall()
    assert rows == []


def test_change_password_with_must_change_sets_flag(temp_user):
    with _connect() as conn:
        change_password(temp_user["id"], "TempPas1A", conn=conn, must_change=True)
        conn.commit()
        flag = conn.execute(
            "SELECT must_change_password FROM users WHERE id = %s",
            (temp_user["id"],),
        ).fetchone()[0]
    assert flag is True


def test_issue_reset_token_invalidates_prior_unused(temp_user):
    with _connect() as conn:
        first = issue_reset_token(user_id=temp_user["id"], created_by=None, conn=conn)
        second = issue_reset_token(user_id=temp_user["id"], created_by=None, conn=conn)
        conn.commit()
        # First should now be claim-rejected; second should still claim.
        assert claim_reset_token(first, conn=conn) is None
        claimed = claim_reset_token(second, conn=conn)
        conn.commit()
    assert claimed is not None
    assert claimed[0] == temp_user["id"]


def test_claim_reset_token_is_single_use(temp_user):
    with _connect() as conn:
        raw = issue_reset_token(user_id=temp_user["id"], created_by=None, conn=conn)
        conn.commit()
        first = claim_reset_token(raw, conn=conn)
        conn.commit()
        second = claim_reset_token(raw, conn=conn)
    assert first is not None
    assert second is None


def test_claim_reset_token_rejects_expired(temp_user):
    """Force the token to be expired and confirm claim returns None."""
    with _connect() as conn:
        raw = issue_reset_token(user_id=temp_user["id"], created_by=None, conn=conn)
        conn.execute(
            "UPDATE password_reset_tokens SET expires_at = now() - interval '1 minute' "
            "WHERE user_id = %s",
            (temp_user["id"],),
        )
        conn.commit()
        result = claim_reset_token(raw, conn=conn)
    assert result is None


def test_claim_reset_token_concurrent_only_one_wins(temp_user):
    """Two threads claim the same raw token; exactly one returns a row."""
    with _connect() as conn:
        raw = issue_reset_token(user_id=temp_user["id"], created_by=None, conn=conn)
        conn.commit()

    results: list = []

    def worker():
        with _connect() as c:
            results.append(claim_reset_token(raw, conn=c))
            c.commit()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = [r for r in results if r is not None]
    assert len(successes) == 1
