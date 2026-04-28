"""Shared password helpers used by self-service, profile, and admin flows.

This module owns:

- :func:`validate_password` — rule check (length, case, digit, email substring).
- :func:`change_password` — atomic mutation: hash, bump token_version,
  delete sessions, set ``password_changed_at``, optionally set
  ``must_change_password``.
- Token issuance / claim helpers used by the forgot/reset flows.

See `docs/superpowers/specs/2026-04-28-password-management-design.md`.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

import bcrypt
import psycopg


def validate_password(password: str, *, email: str | None = None) -> str | None:
    """Return an Estonian error message if *password* fails the rules, else ``None``."""
    if len(password) < 8:
        return "Parool peab olema vähemalt 8 tähemärki pikk"
    if not any(c.isupper() for c in password):
        return "Parool peab sisaldama vähemalt ühte suurtähte"
    if not any(c.isdigit() for c in password):
        return "Parool peab sisaldama vähemalt ühte numbrit"
    if email:
        local_part = email.split("@", 1)[0].lower()
        if local_part and local_part in password.lower():
            return "Parool ei tohi sisaldada teie e-posti aadressi"
    return None


def hash_password(password: str) -> str:
    """Return a bcrypt-encoded hash for *password*."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def change_password(
    user_id: UUID | str,
    new_password: str,
    *,
    conn: psycopg.Connection,
    must_change: bool = False,
) -> None:
    """Atomically rotate password, bump token_version, delete sessions.

    - ``must_change=True`` is set ONLY by the admin temp-password flow
      (§5.4 of the spec); every other flow leaves it False so the user
      is not forced to change again.
    """
    pw_hash = hash_password(new_password)
    with conn.transaction():
        conn.execute(
            "UPDATE users SET "
            "  password_hash = %s, "
            "  token_version = token_version + 1, "
            "  must_change_password = %s, "
            "  password_changed_at = now() "
            "WHERE id = %s",
            (pw_hash, must_change, str(user_id)),
        )
        conn.execute("DELETE FROM sessions WHERE user_id = %s", (str(user_id),))


def hash_token(raw_token: str) -> str:
    """SHA-256 hex digest of *raw_token* — the form stored in DB."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


def hash_email(email: str) -> str:
    """SHA-256 hex digest of the lowercased *email* — keyed for rate-limit table."""
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def issue_reset_token(
    *,
    user_id: UUID | str,
    created_by: UUID | str | None,
    conn: psycopg.Connection,
    ttl: timedelta = timedelta(hours=1),
) -> str:
    """Generate, store, and return a fresh raw reset token for *user_id*.

    Invalidates any prior unused tokens for the same user (single-current-token
    policy, §4.3).

    Returns the raw token (caller emails it; only the SHA-256 hash is in DB).
    """
    raw = secrets.token_hex(32)
    digest = hash_token(raw)
    expires_at = datetime.now(UTC) + ttl
    with conn.transaction():
        conn.execute(
            "UPDATE password_reset_tokens "
            "SET used_at = now() "
            "WHERE user_id = %s AND used_at IS NULL",
            (str(user_id),),
        )
        conn.execute(
            "INSERT INTO password_reset_tokens "
            "(user_id, token_hash, expires_at, created_by) "
            "VALUES (%s, %s, %s, %s)",
            (str(user_id), digest, expires_at, str(created_by) if created_by else None),
        )
    return raw


def claim_reset_token(
    raw_token: str, *, conn: psycopg.Connection
) -> tuple[str, str | None] | None:
    """Atomically claim the token. Returns ``(user_id, created_by)`` or None.

    Race-safe: PostgreSQL serializes concurrent UPDATEs of the same row so
    only one writer claims the token; the others get zero rows back.
    """
    digest = hash_token(raw_token)
    row = conn.execute(
        "UPDATE password_reset_tokens "
        "SET used_at = now() "
        "WHERE token_hash = %s "
        "  AND used_at IS NULL "
        "  AND expires_at > now() "
        "RETURNING user_id, created_by",
        (digest,),
    ).fetchone()
    if row is None:
        return None
    user_id, created_by = row
    return str(user_id), str(created_by) if created_by else None
