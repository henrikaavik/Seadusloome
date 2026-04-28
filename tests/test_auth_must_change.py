"""must_change_password threading through the auth provider."""

import os
import uuid

import bcrypt
import psycopg
import pytest

from app.auth.jwt_provider import JWTAuthProvider


def _connect() -> psycopg.Connection:
    return psycopg.connect(os.environ.get("DATABASE_URL", ""))


@pytest.fixture
def temp_user_must_change():
    user_id = uuid.uuid4()
    pw_hash = bcrypt.hashpw(b"Initial1A", bcrypt.gensalt()).decode()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO users (id, email, password_hash, full_name, role, must_change_password) "
            "VALUES (%s, %s, %s, %s, 'drafter', TRUE)",
            (user_id, f"mc-{user_id}@example.com", pw_hash, "MC User"),
        )
        conn.commit()
    yield str(user_id)
    with _connect() as conn:
        conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.commit()


def test_authenticate_returns_must_change_password(temp_user_must_change):
    provider = JWTAuthProvider()
    user = provider.authenticate(f"mc-{temp_user_must_change}@example.com", "Initial1A")
    assert user is not None
    assert user["must_change_password"] is True


def test_get_current_user_carries_must_change_password(temp_user_must_change):
    provider = JWTAuthProvider()
    user = provider.authenticate(f"mc-{temp_user_must_change}@example.com", "Initial1A")
    assert user is not None
    access_token, _ = provider.create_tokens(user)
    rehydrated = provider.get_current_user(access_token)
    assert rehydrated is not None
    assert rehydrated["must_change_password"] is True
