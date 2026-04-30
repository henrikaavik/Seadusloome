"""Integration tests for issue_reset_token / claim_reset_token.

Lives in its own file (separate from tests/test_password_change.py
which is mock-based) because these tests need a live Postgres for the
atomic-claim and prior-token-invalidation guarantees.
"""

from __future__ import annotations

import os
import threading
import uuid

import bcrypt
import psycopg
import pytest


def _connect() -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"])


@pytest.fixture
def temp_user():
    if not os.getenv("DATABASE_URL"):
        pytest.skip("integration test — DATABASE_URL not set")
    user_id = uuid.uuid4()
    pw = bcrypt.hashpw(b"Initial1A", bcrypt.gensalt()).decode()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO users (id, email, password_hash, full_name, role) "
            "VALUES (%s, %s, %s, 'TempUser', 'drafter')",
            (user_id, f"reset-{user_id}@example.com", pw),
        )
        conn.commit()
    yield {"id": str(user_id)}
    with _connect() as conn:
        conn.execute("DELETE FROM password_reset_tokens WHERE user_id = %s", (user_id,))
        conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.commit()


def test_issue_reset_token_invalidates_prior_unused(temp_user):
    from app.auth.password import claim_reset_token, issue_reset_token

    with _connect() as conn:
        first = issue_reset_token(user_id=temp_user["id"], created_by=None, conn=conn)
        second = issue_reset_token(user_id=temp_user["id"], created_by=None, conn=conn)
        conn.commit()
        assert claim_reset_token(first, conn=conn) is None
        claimed = claim_reset_token(second, conn=conn)
        conn.commit()
    assert claimed is not None
    assert claimed[0] == temp_user["id"]


def test_claim_reset_token_is_single_use(temp_user):
    from app.auth.password import claim_reset_token, issue_reset_token

    with _connect() as conn:
        raw = issue_reset_token(user_id=temp_user["id"], created_by=None, conn=conn)
        conn.commit()
        first = claim_reset_token(raw, conn=conn)
        conn.commit()
        second = claim_reset_token(raw, conn=conn)
    assert first is not None
    assert second is None


def test_claim_reset_token_rejects_expired(temp_user):
    from app.auth.password import claim_reset_token, issue_reset_token

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
    from app.auth.password import claim_reset_token, issue_reset_token

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
