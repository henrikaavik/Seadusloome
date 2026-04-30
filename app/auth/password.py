"""Shared password validation and password-change helpers.

The single ``change_password`` core is used by every flow that mutates
``users.password_hash`` so the same invariants apply everywhere:

* hash with bcrypt;
* bump ``token_version`` so every previously-issued access token is
  rejected on its next use (#635);
* set ``must_change_password`` per the caller's flag (only the
  admin temp-password flow passes ``True``);
* set ``password_changed_at = now()`` for audit;
* delete ``sessions`` rows for the user so refresh tokens cannot be
  replayed after the password rotation.

All five operations run inside one transaction so a partial failure
cannot leave a half-rotated row behind.

Validation rules (`validate_password`) are kept here, not in
``app/auth/users.py``, because the same checks are applied across the
self-service forgot, /profile/password, and admin temp-password flows.
The Estonian error strings live in `docs/superpowers/specs/
2026-04-28-password-management-design.md` §8 (single glossary).
"""

from __future__ import annotations

import bcrypt
import psycopg


def validate_password(password: str, *, email: str | None = None) -> str | None:
    """Return an Estonian error message when *password* fails policy.

    Returns ``None`` when the password is acceptable. Rules:

    1. ``len(password) >= 8``;
    2. at least one uppercase letter;
    3. at least one digit;
    4. when ``email`` is given, the local-part (before ``@``)
       lowercased must NOT appear as a substring in the lowercased
       password.

    Rule 4 is the new, password-management-spec rule (§4.5). It blocks
    obvious passwords like ``Henrik2024`` for ``henrik@…``. Existing
    callers that don't pass ``email`` get the same behaviour as
    ``app.auth.users.validate_password`` for back-compat.
    """
    if len(password) < 8:
        return "Parool peab olema vähemalt 8 tähemärki pikk"
    if not any(c.isupper() for c in password):
        return "Parool peab sisaldama vähemalt ühte suurtähte"
    if not any(c.isdigit() for c in password):
        return "Parool peab sisaldama vähemalt ühte numbrit"
    if email:
        local_part = email.split("@", 1)[0].strip().lower()
        if local_part and local_part in password.lower():
            return "Parool ei tohi sisaldada teie e-posti aadressi"
    return None


def change_password(
    user_id: str,
    new_password: str,
    *,
    conn: psycopg.Connection,  # type: ignore[type-arg]
    must_change: bool = False,
) -> None:
    """Rotate the user's password atomically.

    All five steps run inside a single transaction:

    * compute a fresh bcrypt hash;
    * UPDATE ``users`` setting ``password_hash``,
      ``token_version = token_version + 1``,
      ``must_change_password = must_change``,
      ``password_changed_at = now()``;
    * DELETE ``sessions`` for the user.

    The caller is responsible for:

    * password validation (see :func:`validate_password`);
    * audit logging (e.g. ``user.password_change`` /
      ``user.password_reset``);
    * consuming the reset token (when applicable, §4.6 of the spec);
    * clearing the browser's ``access_token`` and ``refresh_token``
      cookies on the redirect response (§4.7 of the spec).

    Pass ``must_change=True`` ONLY for the admin temp-password flow
    (§5.4 of the spec). All other paths set ``must_change=False`` so
    the user is not forced to change again immediately after picking a
    real password.

    The ``conn`` parameter is required so the caller can compose this
    call with other DB work in the same transaction (notably the
    atomic reset-token UPDATE in §4.6). The function ``commit()``s on
    success and ``rollback()``s on failure.
    """
    pw_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    try:
        conn.execute(
            "UPDATE users "
            "SET password_hash = %s, "
            "    token_version = token_version + 1, "
            "    must_change_password = %s, "
            "    password_changed_at = now() "
            "WHERE id = %s",
            (pw_hash, must_change, user_id),
        )
        conn.execute(
            "DELETE FROM sessions WHERE user_id = %s",
            (user_id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
