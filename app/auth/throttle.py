"""Login brute-force throttling backed by the ``login_attempts`` table.

Issue #851 (review finding D1): ``POST /auth/login`` had no rate
limiting while the forgot-password flow was already throttled via
``password_reset_attempts``. This module mirrors that pattern for the
login flow:

- failures are counted per normalized email (``hash_email`` — SHA-256
  of the lowercased address, so unknown emails throttle identically to
  known ones and the limiter cannot be used for account enumeration);
- failures are also counted per validated client IP (validated because
  ``ProxyHeadersMiddleware`` only honours ``X-Forwarded-For`` from the
  ``TRUSTED_PROXY_HOSTS`` ranges after #851 D3);
- a successful login clears the email-keyed failures so a legitimate
  user who finally remembers their password is not locked out for the
  rest of the window. IP-keyed failures age out with the window.

Failure posture: every function here is **fail-open** — DB errors are
logged loudly and treated as "not throttled" / no-op. This cannot be
abused to brute-force during a DB outage because
``JWTAuthProvider.authenticate`` needs the very same database to check
the password: when Postgres is down, logins fail anyway. The fail-open
branch only matters when the ``login_attempts`` table is missing
(migration 040 not applied), which the error log makes obvious. This
matches the fire-and-forget posture of :mod:`app.auth.audit`.
"""

from __future__ import annotations

import logging

from app.db import get_connection

logger = logging.getLogger(__name__)

# Limits chosen against the password-reset precedent (3/hour per email,
# 10/hour per IP) but tuned for login UX: typos are common, so the email
# budget is a little larger over a much shorter window.
LOGIN_EMAIL_FAIL_LIMIT = 5
LOGIN_IP_FAIL_LIMIT = 20
LOGIN_THROTTLE_WINDOW_MINUTES = 15


def is_login_throttled(email_hash: str, ip: str) -> bool:
    """Return True when *email_hash* or *ip* has exhausted its failure budget.

    Checked BEFORE ``authenticate`` so a locked identifier is refused
    even with the correct password (standard lockout semantics), and so
    the check costs the same whether or not the email exists.
    """
    try:
        with get_connection() as conn:
            # NB: ``%s::interval`` / ``make_interval`` — never ``interval %s``
            # (psycopg substitution rule, see Sprint-2 postmortem).
            row = conn.execute(
                "SELECT "
                "  (SELECT COUNT(*) FROM login_attempts "
                "    WHERE email_hash = %s "
                "    AND attempted_at > now() - make_interval(mins => %s)), "
                "  (SELECT COUNT(*) FROM login_attempts "
                "    WHERE ip = %s "
                "    AND attempted_at > now() - make_interval(mins => %s))",
                (
                    email_hash,
                    LOGIN_THROTTLE_WINDOW_MINUTES,
                    ip,
                    LOGIN_THROTTLE_WINDOW_MINUTES,
                ),
            ).fetchone()
    except Exception:
        logger.exception(
            "login throttle check failed (fail-open) email_hash=%s ip=%s", email_hash, ip
        )
        return False

    if row is None:
        return False
    n_email, n_ip = int(row[0]), int(row[1])
    return n_email >= LOGIN_EMAIL_FAIL_LIMIT or n_ip >= LOGIN_IP_FAIL_LIMIT


def record_login_failure(email_hash: str, ip: str) -> None:
    """Persist one failed login attempt for *email_hash* / *ip*."""
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO login_attempts (email_hash, ip) VALUES (%s, %s)",
                (email_hash, ip),
            )
            conn.commit()
    except Exception:
        logger.exception("failed to record login failure email_hash=%s ip=%s", email_hash, ip)


def clear_login_failures(email_hash: str) -> None:
    """Delete failure rows for *email_hash* after a successful login."""
    try:
        with get_connection() as conn:
            conn.execute(
                "DELETE FROM login_attempts WHERE email_hash = %s",
                (email_hash,),
            )
            conn.commit()
    except Exception:
        logger.exception("failed to clear login failures email_hash=%s", email_hash)
