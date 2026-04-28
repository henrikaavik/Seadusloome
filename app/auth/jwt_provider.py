"""JWT-based authentication provider backed by PostgreSQL sessions."""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt
import psycopg

from app.auth.provider import AuthProvider, UserDict
from app.db import get_connection

# Dev-only fallback. We refuse to start in any non-development environment
# without an explicitly set SECRET_KEY so a misconfigured deployment cannot
# silently sign tokens with a well-known value.
_DEV_SECRET_KEY = "dev-secret-do-not-use-in-production"


def _load_secret_key() -> str:
    """Return the JWT signing secret, enforcing an explicit value off-dev."""
    value = os.environ.get("SECRET_KEY")
    if value:
        return value
    if os.environ.get("APP_ENV", "development") == "development":
        return _DEV_SECRET_KEY
    raise RuntimeError("SECRET_KEY must be set in non-development environments")


SECRET_KEY = _load_secret_key()

ACCESS_TOKEN_EXPIRE_MINUTES = 60
REFRESH_TOKEN_EXPIRE_DAYS = 30
JWT_ALGORITHM = "HS256"


def _hash_token(token: str) -> str:
    """Return a SHA-256 hex digest of *token* for safe storage."""
    return hashlib.sha256(token.encode()).hexdigest()


def hash_password(password: str) -> str:
    """Hash *password* with bcrypt and return the encoded hash."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    """Return True if *password* matches the bcrypt *password_hash*."""
    return bcrypt.checkpw(password.encode(), password_hash.encode())


class JWTAuthProvider(AuthProvider):
    """Concrete auth provider using JWT access tokens and DB-backed refresh tokens."""

    def __init__(self, database_url: str | None = None):
        self._database_url = database_url

    # -- connection helper ---------------------------------------------------

    def _connect(self) -> psycopg.Connection:  # type: ignore[type-arg]
        if self._database_url:
            return psycopg.connect(self._database_url)
        return get_connection()

    # -- public interface ----------------------------------------------------

    def authenticate(self, email: str, password: str) -> UserDict | None:
        """Verify *email* / *password* against the users table.

        Returns a ``UserDict`` on success, ``None`` otherwise.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, email, password_hash, full_name, role, org_id, must_change_password "
                "FROM users WHERE email = %s AND is_active = TRUE",
                (email,),
            ).fetchone()

        if row is None:
            return None

        user_id, user_email, pw_hash, full_name, role, org_id, must_change = row
        if not verify_password(password, pw_hash):
            return None

        return UserDict(
            id=str(user_id),
            email=user_email,
            full_name=full_name,
            role=role,
            org_id=str(org_id) if org_id else None,
            must_change_password=must_change,
        )

    def get_current_user(self, token: str) -> UserDict | None:
        """Decode a JWT *token* and rehydrate the user from the database.

        Returns ``None`` when the token is expired, tampered, malformed, or
        when the server-side state invalidates it:

        - the user no longer exists;
        - ``is_active`` is ``FALSE``;
        - the token's ``tv`` claim does not match ``users.token_version``
          (i.e. the token was issued before the latest role change /
          deactivation / forced logout);
        - the token's ``role`` or ``org_id`` claims disagree with the DB
          (defence-in-depth: should not happen if ``tv`` is maintained,
          but cheap to verify).

        Cost: one indexed ``SELECT`` per authenticated request. At the
        5–50 concurrent user scale this is negligible. See #635.
        """
        try:
            payload: dict[str, Any] = jwt.decode(token, SECRET_KEY, algorithms=[JWT_ALGORITHM])
        except jwt.PyJWTError:
            return None

        sub = payload.get("sub")
        email = payload.get("email")
        role = payload.get("role")
        tv_claim = payload.get("tv")
        # Legacy tokens issued before the #635 fix have no ``tv`` claim.
        # Reject them so users re-login through the refresh path and
        # receive a versioned token.
        if not (sub and email and role) or tv_claim is None:
            return None

        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT token_version, is_active, role, org_id, must_change_password "
                    "FROM users WHERE id = %s",
                    (sub,),
                ).fetchone()
        except Exception:
            # Defensive: a DB outage must not silently grant access.
            return None

        if row is None:
            return None

        db_tv, db_active, db_role, db_org_id, db_must_change = row
        if not db_active:
            return None
        if db_tv != tv_claim:
            return None
        # Role / org_id drift beyond what ``tv`` should already catch —
        # defensive check in case someone updates those columns without
        # bumping ``token_version``.
        if db_role != role:
            return None
        db_org_id_str = str(db_org_id) if db_org_id is not None else None
        claim_org_id = payload.get("org_id")
        if db_org_id_str != claim_org_id:
            return None

        return UserDict(
            id=sub,
            email=email,
            full_name=payload.get("full_name", ""),
            role=db_role,
            org_id=db_org_id_str,
            must_change_password=db_must_change,
        )

    def logout(self, session_id: str) -> None:
        """Delete the session row identified by *session_id*."""
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE id = %s", (session_id,))
            conn.commit()

    # -- token helpers -------------------------------------------------------

    def create_tokens(self, user: UserDict) -> tuple[str, str]:
        """Create a JWT access token and a refresh token for *user*.

        The access token embeds the user's current ``token_version`` as
        the ``tv`` claim (#635) so that future role changes or
        deactivations can invalidate it in O(1) DB work.

        The refresh token is persisted in the ``sessions`` table.
        Returns ``(access_token, refresh_token)``.
        """
        now = datetime.now(UTC)

        # Fetch current token_version and create the session in one
        # short-lived connection. We do the SELECT before the INSERT so
        # a concurrent role change that bumped the counter does not
        # silently issue us a stale tv.
        refresh_token = uuid.uuid4().hex + uuid.uuid4().hex
        token_hash = _hash_token(refresh_token)
        expires_at = now + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)

        with self._connect() as conn:
            row = conn.execute(
                "SELECT token_version FROM users WHERE id = %s",
                (user["id"],),
            ).fetchone()
            token_version = row[0] if row is not None else 0
            conn.execute(
                "INSERT INTO sessions (user_id, token_hash, expires_at) VALUES (%s, %s, %s)",
                (user["id"], token_hash, expires_at),
            )
            conn.commit()

        # Access token (stateless JWT, with tv)
        access_payload: dict[str, Any] = {
            "sub": user["id"],
            "email": user["email"],
            "role": user["role"],
            "full_name": user["full_name"],
            "org_id": user.get("org_id"),
            "tv": token_version,
            "exp": now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
            "iat": now,
        }
        access_token = jwt.encode(access_payload, SECRET_KEY, algorithm=JWT_ALGORITHM)

        return access_token, refresh_token

    def verify_refresh_token(self, token: str) -> UserDict | None:
        """Check the ``sessions`` table for a valid refresh *token*.

        Returns the associated ``UserDict`` if valid and not expired, else ``None``.
        """
        token_hash = _hash_token(token)
        now = datetime.now(UTC)

        with self._connect() as conn:
            row = conn.execute(
                "SELECT s.id, u.id, u.email, u.full_name, u.role, u.org_id,"
                " u.must_change_password "
                "FROM sessions s "
                "JOIN users u ON u.id = s.user_id "
                "WHERE s.token_hash = %s AND s.expires_at > %s AND u.is_active = TRUE",
                (token_hash, now),
            ).fetchone()

        if row is None:
            return None

        _session_id, user_id, email, full_name, role, org_id, must_change = row
        return UserDict(
            id=str(user_id),
            email=email,
            full_name=full_name,
            role=role,
            org_id=str(org_id) if org_id else None,
            must_change_password=must_change,
        )

    def delete_refresh_token(self, token: str) -> None:
        """Remove the refresh token's session row from the database."""
        token_hash = _hash_token(token)
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = %s", (token_hash,))
            conn.commit()
