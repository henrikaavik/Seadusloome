"""Unit tests for the JWT authentication module.

These tests exercise password hashing, JWT creation / decoding, and
credential rejection without requiring a running PostgreSQL instance.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.auth.jwt_provider import (
    JWT_ALGORITHM,
    SECRET_KEY,
    hash_password,
    verify_password,
)
from app.auth.provider import UserDict

# ---------------------------------------------------------------------------
# Password hashing / verification
# ---------------------------------------------------------------------------


class TestPasswordHashing:
    def test_hash_and_verify(self):
        pw = "s3cur3P@ssw0rd!"
        hashed = hash_password(pw)
        assert hashed != pw
        assert verify_password(pw, hashed)

    def test_wrong_password_rejected(self):
        hashed = hash_password("correct-horse-battery-staple")
        assert not verify_password("wrong-password", hashed)

    def test_different_hashes_for_same_password(self):
        pw = "same-password"
        h1 = hash_password(pw)
        h2 = hash_password(pw)
        # bcrypt salts should produce distinct hashes
        assert h1 != h2
        # But both must verify
        assert verify_password(pw, h1)
        assert verify_password(pw, h2)

    def test_empty_password_hashes(self):
        hashed = hash_password("")
        assert verify_password("", hashed)
        assert not verify_password("notempty", hashed)

    def test_unicode_password(self):
        pw = "\u00f5\u00e4\u00f6\u00fc\u0161\u017e"  # Estonian special chars
        hashed = hash_password(pw)
        assert verify_password(pw, hashed)


# ---------------------------------------------------------------------------
# JWT token creation and decoding
# ---------------------------------------------------------------------------


def _make_user(**overrides: str | None) -> UserDict:
    """Helper to build a UserDict with sensible defaults."""
    defaults: dict[str, str | None] = {
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "email": "test@seadusloome.ee",
        "full_name": "Test User",
        "role": "drafter",
        "org_id": None,
    }
    defaults.update(overrides)
    return UserDict(**defaults)  # type: ignore[arg-type]


class TestJWTTokens:
    def test_encode_and_decode(self):
        user = _make_user()
        now = datetime.now(UTC)
        payload = {
            "sub": user["id"],
            "email": user["email"],
            "role": user["role"],
            "exp": now + timedelta(hours=1),
            "iat": now,
        }
        token = jwt.encode(payload, SECRET_KEY, algorithm=JWT_ALGORITHM)
        decoded = jwt.decode(token, SECRET_KEY, algorithms=[JWT_ALGORITHM])

        assert decoded["sub"] == user["id"]
        assert decoded["email"] == user["email"]
        assert decoded["role"] == user["role"]

    def test_expired_token_raises(self):
        now = datetime.now(UTC)
        payload = {
            "sub": "some-user-id",
            "email": "expired@test.ee",
            "role": "drafter",
            "exp": now - timedelta(seconds=1),
            "iat": now - timedelta(hours=2),
        }
        token = jwt.encode(payload, SECRET_KEY, algorithm=JWT_ALGORITHM)
        with pytest.raises(jwt.ExpiredSignatureError):
            jwt.decode(token, SECRET_KEY, algorithms=[JWT_ALGORITHM])

    def test_tampered_token_raises(self):
        now = datetime.now(UTC)
        payload = {
            "sub": "user-id",
            "email": "user@test.ee",
            "role": "admin",
            "exp": now + timedelta(hours=1),
        }
        token = jwt.encode(payload, SECRET_KEY, algorithm=JWT_ALGORITHM)
        # Flip a character in the signature portion
        tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
        with pytest.raises(jwt.InvalidSignatureError):
            jwt.decode(tampered, SECRET_KEY, algorithms=[JWT_ALGORITHM])

    def test_wrong_secret_raises(self):
        now = datetime.now(UTC)
        payload = {
            "sub": "uid", "email": "u@t.ee", "role": "drafter",
            "exp": now + timedelta(hours=1),
        }
        token = jwt.encode(payload, "correct-secret", algorithm=JWT_ALGORITHM)
        with pytest.raises(jwt.InvalidSignatureError):
            jwt.decode(token, "wrong-secret", algorithms=[JWT_ALGORITHM])

    def test_payload_contains_required_fields(self):
        now = datetime.now(UTC)
        payload = {
            "sub": "uid-123",
            "email": "admin@seadusloome.ee",
            "role": "admin",
            "full_name": "Admin User",
            "org_id": "org-456",
            "exp": now + timedelta(hours=1),
            "iat": now,
        }
        token = jwt.encode(payload, SECRET_KEY, algorithm=JWT_ALGORITHM)
        decoded = jwt.decode(token, SECRET_KEY, algorithms=[JWT_ALGORITHM])

        assert decoded["sub"] == "uid-123"
        assert decoded["email"] == "admin@seadusloome.ee"
        assert decoded["role"] == "admin"
        assert decoded["full_name"] == "Admin User"
        assert decoded["org_id"] == "org-456"


# ---------------------------------------------------------------------------
# get_current_user (stateless — no DB needed)
# ---------------------------------------------------------------------------


class TestGetCurrentUser:
    """Test JWTAuthProvider.get_current_user without a database.

    We instantiate the provider but only call get_current_user which is
    purely stateless (JWT decode).  We pass a dummy DB URL since the
    constructor requires one, but no connection is made.
    """

    def _provider(self):  # noqa: ANN202
        from app.auth.jwt_provider import JWTAuthProvider

        return JWTAuthProvider(database_url="postgresql://fake:fake@localhost/fake")

    def test_valid_token_returns_user(self):
        provider = self._provider()
        now = datetime.now(UTC)
        payload = {
            "sub": "uid",
            "email": "a@b.ee",
            "role": "drafter",
            "full_name": "A B",
            "org_id": None,
            "exp": now + timedelta(hours=1),
            "iat": now,
        }
        token = jwt.encode(payload, SECRET_KEY, algorithm=JWT_ALGORITHM)
        user = provider.get_current_user(token)

        assert user is not None
        assert user["id"] == "uid"
        assert user["email"] == "a@b.ee"
        assert user["role"] == "drafter"

    def test_expired_token_returns_none(self):
        provider = self._provider()
        now = datetime.now(UTC)
        payload = {
            "sub": "uid",
            "email": "a@b.ee",
            "role": "drafter",
            "exp": now - timedelta(seconds=1),
        }
        token = jwt.encode(payload, SECRET_KEY, algorithm=JWT_ALGORITHM)
        assert provider.get_current_user(token) is None

    def test_garbage_token_returns_none(self):
        provider = self._provider()
        assert provider.get_current_user("not.a.jwt") is None

    def test_missing_sub_returns_none(self):
        provider = self._provider()
        now = datetime.now(UTC)
        payload = {"email": "a@b.ee", "role": "drafter", "exp": now + timedelta(hours=1)}
        token = jwt.encode(payload, SECRET_KEY, algorithm=JWT_ALGORITHM)
        assert provider.get_current_user(token) is None

    def test_missing_email_returns_none(self):
        provider = self._provider()
        now = datetime.now(UTC)
        payload = {"sub": "uid", "role": "drafter", "exp": now + timedelta(hours=1)}
        token = jwt.encode(payload, SECRET_KEY, algorithm=JWT_ALGORITHM)
        assert provider.get_current_user(token) is None

    def test_missing_role_returns_none(self):
        provider = self._provider()
        now = datetime.now(UTC)
        payload = {"sub": "uid", "email": "a@b.ee", "exp": now + timedelta(hours=1)}
        token = jwt.encode(payload, SECRET_KEY, algorithm=JWT_ALGORITHM)
        assert provider.get_current_user(token) is None
