"""Regression: forgot/reset routes are reachable without auth, plus must_change_password redirect.

Tests that don't need DB (test_forgot_route_in_skip_paths) run everywhere.
DB-dependent tests require DATABASE_URL to be set.
"""

import os
import uuid

import bcrypt
import psycopg

from app.auth.jwt_provider import JWTAuthProvider


def _connect() -> psycopg.Connection:
    return psycopg.connect(os.environ.get("DATABASE_URL", ""))


def test_forgot_route_in_skip_paths():
    """SKIP_PATHS regex list contains the new public-route patterns."""
    from app.auth.middleware import SKIP_PATHS

    assert any(p == r"/auth/forgot" for p in SKIP_PATHS)
    assert any(p == r"/auth/reset/.*" for p in SKIP_PATHS)


def test_must_change_password_redirects_to_profile_password():
    """A user with must_change_password=True hitting / is 303'd to /profile/password."""
    user_id = uuid.uuid4()
    pw_hash = bcrypt.hashpw(b"Initial1A", bcrypt.gensalt()).decode()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO users (id, email, password_hash, full_name, role, must_change_password) "
            "VALUES (%s, %s, %s, %s, 'drafter', TRUE)",
            (user_id, f"mc-{user_id}@example.com", pw_hash, "MC"),
        )
        conn.commit()

    try:
        provider = JWTAuthProvider()
        user = provider.authenticate(f"mc-{user_id}@example.com", "Initial1A")
        assert user is not None
        access_token, _ = provider.create_tokens(user)

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [(b"cookie", f"access_token={access_token}".encode())],
            "scheme": "http",
            "server": ("test", 80),
            "client": ("127.0.0.1", 12345),
        }
        from starlette.requests import Request

        from app.auth.middleware import auth_before

        result = auth_before(Request(scope))
        assert result is not None
        assert result.status_code == 303
        assert result.headers["location"] == "/profile/password"
    finally:
        with _connect() as conn:
            conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
            conn.commit()


def test_must_change_password_does_not_redirect_from_profile_password():
    """Same user hitting /profile/password is allowed through (returns None)."""
    user_id = uuid.uuid4()
    pw_hash = bcrypt.hashpw(b"Initial1A", bcrypt.gensalt()).decode()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO users (id, email, password_hash, full_name, role, must_change_password) "
            "VALUES (%s, %s, %s, %s, 'drafter', TRUE)",
            (user_id, f"mc2-{user_id}@example.com", pw_hash, "MC2"),
        )
        conn.commit()

    try:
        provider = JWTAuthProvider()
        user = provider.authenticate(f"mc2-{user_id}@example.com", "Initial1A")
        assert user is not None
        access_token, _ = provider.create_tokens(user)

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/profile/password",
            "raw_path": b"/profile/password",
            "query_string": b"",
            "headers": [(b"cookie", f"access_token={access_token}".encode())],
            "scheme": "http",
            "server": ("test", 80),
            "client": ("127.0.0.1", 12345),
        }
        from starlette.requests import Request

        from app.auth.middleware import auth_before

        result = auth_before(Request(scope))
        assert result is None  # passed through
    finally:
        with _connect() as conn:
            conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
            conn.commit()
