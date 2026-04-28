# Password Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship three new password-management flows — self-service forgot password, in-account password change, and admin-initiated reset (email link + temp password fallback) — sharing one `change_password()` core, one Postmark-backed email module, and one new DB schema.

**Architecture:** Provider-abstracted email module (`app/email/`) mirroring `app/llm/`. New `password_reset_tokens` and `password_reset_attempts` tables plus two new `users` columns (`must_change_password`, `password_changed_at`). Atomic token claim via `UPDATE … RETURNING`. Auth middleware gains SKIP_PATHS for public reset routes and a `must_change_password` redirect.

**Tech Stack:** Python 3.13, FastHTML, psycopg 3, bcrypt, postmarker (HTTP API client), pgvector PostgreSQL 18, pytest. UI in Estonian.

**Spec:** `docs/superpowers/specs/2026-04-28-password-management-design.md` is the source of truth — read it before starting.

**Conventions reused throughout the plan:**
- All commands run from repo root.
- DB tests run against the local Postgres at `postgresql://seadusloome:localdev@localhost:5432/seadusloome` (override with `DATABASE_URL`).
- After every task, run `uv run ruff format . && uv run ruff check . && uv run pyright app/`.
- Commit at the end of each task with the exact message shown.

---

## Task 0: Add `postmarker` dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add the dependency**

```bash
uv add postmarker
```

This appends `postmarker>=...` to the `[project] dependencies` array and updates `uv.lock`.

- [ ] **Step 2: Verify import works at the package level (no network)**

```bash
uv run python -c "from postmarker.core import PostmarkClient; print('ok')"
```

Expected: prints `ok`.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore(deps): add postmarker for transactional email"
```

---

## Task 1: Migration 024 — schema additions

**Files:**
- Create: `migrations/024_password_management.sql`

- [ ] **Step 1: Write the migration**

```sql
-- 024_password_management.sql
-- Schema for password reset (self-service + admin-initiated) and forced
-- post-temp-password change. Spec: 2026-04-28-password-management-design.md.

CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash    TEXT NOT NULL UNIQUE,
    expires_at    TIMESTAMPTZ NOT NULL,
    used_at       TIMESTAMPTZ,
    created_by    UUID REFERENCES users(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pwreset_user_id ON password_reset_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_pwreset_expires_at ON password_reset_tokens(expires_at);
CREATE INDEX IF NOT EXISTS idx_pwreset_created_by ON password_reset_tokens(created_by) WHERE created_by IS NOT NULL;

CREATE TABLE IF NOT EXISTS password_reset_attempts (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email_hash    TEXT NOT NULL,
    ip            TEXT NOT NULL,
    attempted_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pwreset_attempts_email_hash_time ON password_reset_attempts(email_hash, attempted_at);
CREATE INDEX IF NOT EXISTS idx_pwreset_attempts_ip_time ON password_reset_attempts(ip, attempted_at);

ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_changed_at TIMESTAMPTZ;
```

- [ ] **Step 2: Apply the migration**

```bash
uv run python scripts/migrate.py
```

Expected: `Applying 024_password_management...` line, no errors.

- [ ] **Step 3: Verify schema landed**

```bash
psql "$DATABASE_URL" -c "\d password_reset_tokens" -c "\d password_reset_attempts" -c "\d users"
```

Expected: both new tables and the two new `users` columns are visible. (If `psql` isn't installed, use `uv run python -c "import psycopg, os; print(psycopg.connect(os.environ['DATABASE_URL']).execute(\"SELECT column_name FROM information_schema.columns WHERE table_name='users' AND column_name IN ('must_change_password','password_changed_at')\").fetchall())"`.)

- [ ] **Step 4: Commit**

```bash
git add migrations/024_password_management.sql
git commit -m "feat(db): migration 024 — password reset tokens, attempts, must_change_password"
```

---

## Task 2: `EmailProvider` ABC + `StubProvider`

**Files:**
- Create: `app/email/__init__.py`
- Create: `app/email/provider.py`
- Create: `app/email/stub_provider.py`
- Test: `tests/test_email_provider.py`

- [ ] **Step 1: Write the failing test**

`tests/test_email_provider.py`:

```python
"""Email provider tests."""

import logging

from app.email.stub_provider import StubProvider


def test_stub_provider_logs_subject_and_body(caplog):
    provider = StubProvider()
    with caplog.at_level(logging.INFO, logger="app.email.stub_provider"):
        provider.send(
            to="alice@example.com",
            subject="Hello",
            html="<p>Hi</p>",
            text="Hi",
        )
    assert any("alice@example.com" in r.message for r in caplog.records)
    assert any("Hello" in r.message for r in caplog.records)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_email_provider.py::test_stub_provider_logs_subject_and_body -v
```

Expected: FAIL — `app.email.stub_provider` does not exist yet.

- [ ] **Step 3: Write the package init**

`app/email/__init__.py`:

```python
"""Transactional email module — provider-abstracted, stub-by-default."""
```

- [ ] **Step 4: Write the ABC**

`app/email/provider.py`:

```python
"""Abstract email provider — concrete impls in stub_provider.py / postmark_provider.py."""

from __future__ import annotations

from abc import ABC, abstractmethod


class EmailProvider(ABC):
    """Send transactional email. One method, sync."""

    @abstractmethod
    def send(
        self,
        *,
        to: str,
        subject: str,
        html: str,
        text: str,
        message_stream: str | None = None,
    ) -> None:
        """Deliver one transactional message. Raises on failure."""
        ...
```

- [ ] **Step 5: Write the stub**

`app/email/stub_provider.py`:

```python
"""StubProvider — logs the email instead of sending. Used in dev/test/CI."""

from __future__ import annotations

import logging

from app.email.provider import EmailProvider

logger = logging.getLogger(__name__)


class StubProvider(EmailProvider):
    def send(
        self,
        *,
        to: str,
        subject: str,
        html: str,
        text: str,
        message_stream: str | None = None,
    ) -> None:
        logger.info(
            "[StubEmail] to=%s subject=%r stream=%s text=%r",
            to,
            subject,
            message_stream or "outbound",
            text,
        )
```

- [ ] **Step 6: Run test to verify it passes**

```bash
uv run pytest tests/test_email_provider.py::test_stub_provider_logs_subject_and_body -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/email/ tests/test_email_provider.py
git commit -m "feat(email): EmailProvider ABC + StubProvider"
```

---

## Task 3: `PostmarkProvider`

**Files:**
- Create: `app/email/postmark_provider.py`
- Modify: `tests/test_email_provider.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_email_provider.py`:

```python
from unittest.mock import MagicMock, patch

from app.email.postmark_provider import PostmarkProvider


def test_postmark_provider_calls_emails_send():
    fake_client = MagicMock()
    with patch("app.email.postmark_provider.PostmarkClient", return_value=fake_client):
        provider = PostmarkProvider(api_token="test-token", default_from="x@y.z")
        provider.send(
            to="alice@example.com",
            subject="Hello",
            html="<p>Hi</p>",
            text="Hi",
        )
    fake_client.emails.send.assert_called_once()
    kwargs = fake_client.emails.send.call_args.kwargs
    assert kwargs["To"] == "alice@example.com"
    assert kwargs["From"] == "x@y.z"
    assert kwargs["Subject"] == "Hello"
    assert kwargs["HtmlBody"] == "<p>Hi</p>"
    assert kwargs["TextBody"] == "Hi"
    assert kwargs["MessageStream"] == "outbound"


def test_postmark_provider_lazy_init():
    """Client construction is deferred until first send."""
    with patch("app.email.postmark_provider.PostmarkClient") as cls:
        provider = PostmarkProvider(api_token="t", default_from="x@y.z")
        cls.assert_not_called()
        provider.send(to="a@b.c", subject="s", html="h", text="t")
        cls.assert_called_once_with(server_token="t")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_email_provider.py -v
```

Expected: the two new tests FAIL with `ModuleNotFoundError: No module named 'app.email.postmark_provider'`.

- [ ] **Step 3: Write the implementation**

`app/email/postmark_provider.py`:

```python
"""PostmarkProvider — Postmark HTTP API via postmarker.

Lazy-initialises the SDK client on first send so dev/test environments
that have not installed postmarker still work as long as the stub path
is taken.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from postmarker.core import PostmarkClient

from app.email.provider import EmailProvider

logger = logging.getLogger(__name__)


class PostmarkProvider(EmailProvider):
    def __init__(self, *, api_token: str, default_from: str) -> None:
        self._token = api_token
        self._default_from = default_from
        self._client: Any = None
        self._lock = threading.Lock()

    def _get_client(self) -> Any:
        if self._client is None:
            with self._lock:
                if self._client is None:
                    self._client = PostmarkClient(server_token=self._token)
        return self._client

    def send(
        self,
        *,
        to: str,
        subject: str,
        html: str,
        text: str,
        message_stream: str | None = None,
    ) -> None:
        client = self._get_client()
        client.emails.send(
            From=self._default_from,
            To=to,
            Subject=subject,
            HtmlBody=html,
            TextBody=text,
            MessageStream=message_stream or "outbound",
        )
        logger.info("[PostmarkEmail] sent to=%s subject=%r", to, subject)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_email_provider.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/email/postmark_provider.py tests/test_email_provider.py
git commit -m "feat(email): PostmarkProvider via postmarker SDK with lazy init"
```

---

## Task 4: `get_email_provider()` gate

**Files:**
- Create: `app/email/service.py`
- Modify: `tests/test_email_provider.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_email_provider.py`:

```python
import pytest

from app.email.service import _reset_provider_for_tests, get_email_provider
from app.email.stub_provider import StubProvider


@pytest.fixture(autouse=True)
def _reset_email_singleton():
    _reset_provider_for_tests()
    yield
    _reset_provider_for_tests()


def test_provider_is_stub_when_dev_and_no_token(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("POSTMARK_API_TOKEN", raising=False)
    assert isinstance(get_email_provider(), StubProvider)


def test_provider_is_postmark_when_dev_and_token_present(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("POSTMARK_API_TOKEN", "tok")
    monkeypatch.setenv("EMAIL_FROM", "x@y.z")
    from app.email.postmark_provider import PostmarkProvider
    assert isinstance(get_email_provider(), PostmarkProvider)


def test_provider_raises_in_production_without_token(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("POSTMARK_API_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="POSTMARK_API_TOKEN"):
        get_email_provider()
```

- [ ] **Step 2: Run tests — they should fail**

```bash
uv run pytest tests/test_email_provider.py -v
```

Expected: the three new tests FAIL.

- [ ] **Step 3: Write the service**

`app/email/service.py`:

```python
"""Lazy singleton selecting the active EmailProvider per env config."""

from __future__ import annotations

import os
import threading

from app.config import is_stub_allowed
from app.email.provider import EmailProvider
from app.email.stub_provider import StubProvider

_provider: EmailProvider | None = None
_lock = threading.Lock()

_DEFAULT_FROM = "Seadusloome <noreply@sixtyfour.ee>"


def get_email_provider() -> EmailProvider:
    """Return the active provider per env config.

    Selection rule (mirrors ``app/llm/claude.py``):

    - dev/test/staging (``APP_ENV != production``) without ``POSTMARK_API_TOKEN`` → StubProvider
    - dev/test/staging with ``POSTMARK_API_TOKEN`` → real PostmarkProvider (lets staging exercise the wire)
    - production without ``POSTMARK_API_TOKEN`` → ``RuntimeError`` so deployment fails loudly
    - production with ``POSTMARK_API_TOKEN`` → real PostmarkProvider
    """
    global _provider
    if _provider is not None:
        return _provider

    with _lock:
        if _provider is not None:
            return _provider

        token = os.environ.get("POSTMARK_API_TOKEN", "").strip()
        from_addr = os.environ.get("EMAIL_FROM", "").strip() or _DEFAULT_FROM

        if not token:
            if is_stub_allowed():
                _provider = StubProvider()
                return _provider
            raise RuntimeError(
                "POSTMARK_API_TOKEN must be set in production "
                "(APP_ENV=production). Refusing to silently fall back to stub."
            )

        # Imported lazily so envs without postmarker installed can still use the stub.
        from app.email.postmark_provider import PostmarkProvider

        _provider = PostmarkProvider(api_token=token, default_from=from_addr)
        return _provider


def _reset_provider_for_tests() -> None:
    global _provider
    _provider = None
```

- [ ] **Step 4: Run tests — they should pass**

```bash
uv run pytest tests/test_email_provider.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/email/service.py tests/test_email_provider.py
git commit -m "feat(email): get_email_provider() gate — stub in dev, raise in prod without token"
```

---

## Task 5: Estonian email templates

**Files:**
- Create: `app/email/templates.py`
- Modify: `tests/test_email_provider.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_email_provider.py`:

```python
from app.email.templates import password_reset, password_reset_admin


def test_password_reset_template_estonian():
    subject, html, text = password_reset(
        full_name="Mari Maasikas",
        reset_url="https://example.com/auth/reset/abc",
    )
    assert "Seadusloome" in subject
    assert "Mari Maasikas" in html
    assert "https://example.com/auth/reset/abc" in html
    assert "https://example.com/auth/reset/abc" in text
    assert "1 tunni" in text  # 1-hour TTL mentioned


def test_password_reset_admin_template_estonian():
    subject, html, text = password_reset_admin(
        full_name="Mari",
        reset_url="https://example.com/auth/reset/xyz",
        admin_name="Henrik Aavik",
    )
    assert "Administraator" in subject
    assert "Henrik Aavik" in html
    assert "https://example.com/auth/reset/xyz" in text
```

- [ ] **Step 2: Run — should fail**

```bash
uv run pytest tests/test_email_provider.py -k template -v
```

Expected: FAIL — `app.email.templates` does not exist.

- [ ] **Step 3: Write the templates**

`app/email/templates.py`:

```python
"""Estonian transactional email templates returning ``(subject, html, text)``."""

from __future__ import annotations


def password_reset(*, full_name: str, reset_url: str) -> tuple[str, str, str]:
    subject = "Parooli lähtestamine — Seadusloome"
    html = f"""\
<p>Tere {full_name},</p>

<p>Saime taotluse teie parooli lähtestamiseks Seadusloome platvormil.
Uue parooli määramiseks klõpsake allpool oleval lingil:</p>

<p><a href="{reset_url}">{reset_url}</a></p>

<p>Link kehtib 1 tunni. Kui te ei taotlenud lähtestamist, võite selle e-kirja eirata —
teie parool jääb muutmata.</p>

<p>Lugupidamisega,<br>Seadusloome</p>
"""
    text = f"""\
Tere {full_name},

Saime taotluse teie parooli lähtestamiseks Seadusloome platvormil.
Uue parooli määramiseks avage:

{reset_url}

Link kehtib 1 tunni. Kui te ei taotlenud lähtestamist, võite selle e-kirja eirata —
teie parool jääb muutmata.

Lugupidamisega,
Seadusloome
"""
    return subject, html, text


def password_reset_admin(
    *, full_name: str, reset_url: str, admin_name: str
) -> tuple[str, str, str]:
    subject = "Administraator on lähtestanud teie parooli — Seadusloome"
    html = f"""\
<p>Tere {full_name},</p>

<p>Administraator <strong>{admin_name}</strong> on algatanud teie parooli lähtestamise
Seadusloome platvormil. Uue parooli määramiseks klõpsake allpool oleval lingil:</p>

<p><a href="{reset_url}">{reset_url}</a></p>

<p>Link kehtib 1 tunni.</p>

<p>Lugupidamisega,<br>Seadusloome</p>
"""
    text = f"""\
Tere {full_name},

Administraator {admin_name} on algatanud teie parooli lähtestamise
Seadusloome platvormil. Uue parooli määramiseks avage:

{reset_url}

Link kehtib 1 tunni.

Lugupidamisega,
Seadusloome
"""
    return subject, html, text
```

- [ ] **Step 4: Run — should pass**

```bash
uv run pytest tests/test_email_provider.py -k template -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/email/templates.py tests/test_email_provider.py
git commit -m "feat(email): Estonian password_reset and password_reset_admin templates"
```

---

## Task 6: Move + extend `validate_password()`

The function currently lives in `app/auth/users.py` (lines 43-51). Move it to a new `app/auth/password.py` module and extend it with email-substring rejection. Re-export from `users.py` so existing callers keep working.

**Files:**
- Create: `app/auth/password.py`
- Modify: `app/auth/users.py:43-51` — remove definition, re-export.
- Test: `tests/test_password_validation.py`

- [ ] **Step 1: Write the failing test**

`tests/test_password_validation.py`:

```python
"""validate_password tests — existing rules + email-substring extension."""

from app.auth.password import validate_password


def test_existing_rules_still_pass():
    assert validate_password("Abcdef12") is None


def test_short_password_rejected():
    assert "8 tähemärki" in (validate_password("Ab1") or "")


def test_no_uppercase_rejected():
    assert "suurtähte" in (validate_password("abcdef12") or "")


def test_no_digit_rejected():
    assert "numbrit" in (validate_password("Abcdefgh") or "")


def test_email_substring_rejected():
    assert "e-posti" in (validate_password("Henrik123", email="henrik@example.com") or "")


def test_email_substring_check_is_case_insensitive():
    assert "e-posti" in (validate_password("HENRIK123", email="henrik@example.com") or "")


def test_email_substring_optional():
    # No email arg → no substring check.
    assert validate_password("Henrik123") is None


def test_email_with_no_at_treated_as_full_localpart():
    # Defensive: callers should always pass a real email, but if they don't,
    # use the full string as the local-part.
    assert "e-posti" in (validate_password("Henrik123", email="henrik") or "")
```

- [ ] **Step 2: Run — should fail**

```bash
uv run pytest tests/test_password_validation.py -v
```

Expected: FAIL on import (`app.auth.password` doesn't exist).

- [ ] **Step 3: Create `app/auth/password.py`**

`app/auth/password.py`:

```python
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
```

- [ ] **Step 4: Replace `validate_password` in `users.py` with a re-export**

In `app/auth/users.py`, replace lines 43-51 (the `validate_password` function definition) with:

```python
from app.auth.password import validate_password  # noqa: F401 — re-export for callers
```

(Place this with the other top-of-file imports; remove the original function body.)

- [ ] **Step 5: Run — should pass**

```bash
uv run pytest tests/test_password_validation.py -v
uv run pytest tests/test_auth.py tests/test_auth_routes.py -v
```

Expected: all pass — existing call sites still work via re-export.

- [ ] **Step 6: Commit**

```bash
git add app/auth/password.py app/auth/users.py tests/test_password_validation.py
git commit -m "feat(auth): split validate_password into app/auth/password + extend with email-substring check"
```

---

## Task 7: `change_password()` integration test

**Files:**
- Test: `tests/test_password_change.py`

(No new production code — exercises Task 6's `change_password` against the live DB.)

- [ ] **Step 1: Write the failing test**

`tests/test_password_change.py`:

```python
"""Integration tests for change_password() against a live Postgres."""

import os
import uuid
from datetime import UTC, datetime, timedelta

import bcrypt
import psycopg
import pytest

from app.auth.password import change_password


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
            "INSERT INTO sessions (user_id, token_hash, expires_at) "
            "VALUES (%s, %s, %s)",
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
```

- [ ] **Step 2: Run — should pass (Task 6 implementation already satisfies it)**

```bash
uv run pytest tests/test_password_change.py -v
```

Expected: all pass. If not, adjust `app/auth/password.py::change_password` until they do.

- [ ] **Step 3: Commit**

```bash
git add tests/test_password_change.py
git commit -m "test(auth): integration tests for change_password against live DB"
```

---

## Task 8: Token-helper integration tests (atomic claim, prior-token invalidation)

**Files:**
- Modify: `tests/test_password_change.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_password_change.py`:

```python
import threading

from app.auth.password import claim_reset_token, issue_reset_token


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
```

- [ ] **Step 2: Run — should pass**

```bash
uv run pytest tests/test_password_change.py -v
```

Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_password_change.py
git commit -m "test(auth): atomic claim and prior-token invalidation for reset tokens"
```

---

## Task 9: Extend `UserDict` and `JWTAuthProvider` with `must_change_password`

**Files:**
- Modify: `app/auth/provider.py`
- Modify: `app/auth/jwt_provider.py`
- Test: `tests/test_auth_must_change.py`

- [ ] **Step 1: Write the failing test**

`tests/test_auth_must_change.py`:

```python
"""must_change_password threading through the auth provider."""

import os
import uuid
from datetime import UTC, datetime, timedelta

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
```

- [ ] **Step 2: Run — should fail**

```bash
uv run pytest tests/test_auth_must_change.py -v
```

Expected: FAIL — `must_change_password` is not yet on `UserDict`.

- [ ] **Step 3: Add the field to `UserDict`**

In `app/auth/provider.py`, modify the TypedDict:

```python
class UserDict(TypedDict):
    """User data returned by authentication operations."""

    id: str
    email: str
    full_name: str
    role: str
    org_id: str | None
    must_change_password: bool
```

- [ ] **Step 4: Update `JWTAuthProvider.authenticate`**

In `app/auth/jwt_provider.py`, locate the `authenticate` method (around line 71) and replace with:

```python
    def authenticate(self, email: str, password: str) -> UserDict | None:
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
```

- [ ] **Step 5: Update `JWTAuthProvider.get_current_user`**

In the same file, locate `get_current_user` and update the SELECT + return:

```python
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT token_version, is_active, role, org_id, must_change_password "
                    "FROM users WHERE id = %s",
                    (sub,),
                ).fetchone()
```

and update the unpack + return:

```python
        db_tv, db_active, db_role, db_org_id, db_must_change = row
        # ... existing checks unchanged ...
        return UserDict(
            id=sub,
            email=email,
            full_name=payload.get("full_name", ""),
            role=db_role,
            org_id=db_org_id_str,
            must_change_password=db_must_change,
        )
```

- [ ] **Step 6: Update `verify_refresh_token` similarly**

In the same file, replace the SELECT and return block:

```python
        with self._connect() as conn:
            row = conn.execute(
                "SELECT s.id, u.id, u.email, u.full_name, u.role, u.org_id, u.must_change_password "
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
```

- [ ] **Step 7: Run — should pass**

```bash
uv run pytest tests/test_auth_must_change.py tests/test_auth.py tests/test_auth_token_version.py -v
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add app/auth/provider.py app/auth/jwt_provider.py tests/test_auth_must_change.py
git commit -m "feat(auth): thread must_change_password through UserDict and JWTAuthProvider"
```

---

## Task 10: Middleware — `SKIP_PATHS` + `must_change_password` enforcement

**Files:**
- Modify: `app/auth/middleware.py:44-53` (SKIP_PATHS) and `auth_before` (around line 129)
- Test: `tests/test_auth_middleware_skip.py`
- Modify: `tests/test_auth_middleware.py` (regression: existing flows still work)

- [ ] **Step 1: Write the failing skip-path test**

`tests/test_auth_middleware_skip.py`:

```python
"""Regression: forgot/reset routes are reachable without auth."""

from starlette.requests import Request

from app.auth.middleware import auth_before


def _make_req(path: str) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "scheme": "http",
        "server": ("test", 80),
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope)


def test_forgot_route_in_skip_paths():
    """Beforeware does not run on skip paths — but auth_before is called only
    on non-skip paths by FastHTML, so we test by importing the SKIP_PATHS list."""
    from app.auth.middleware import SKIP_PATHS

    assert any(p == r"/auth/forgot" for p in SKIP_PATHS)
    assert any(p == r"/auth/reset/.*" for p in SKIP_PATHS)
```

- [ ] **Step 2: Write the failing must-change-redirect test**

Append to `tests/test_auth_middleware_skip.py`:

```python
import os
import uuid
from datetime import UTC, datetime, timedelta

import bcrypt
import psycopg

from app.auth.cookies import COOKIE_SECURE  # noqa: F401
from app.auth.jwt_provider import JWTAuthProvider


def _connect() -> psycopg.Connection:
    return psycopg.connect(os.environ.get("DATABASE_URL", ""))


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
```

- [ ] **Step 3: Run — should fail**

```bash
uv run pytest tests/test_auth_middleware_skip.py -v
```

Expected: FAIL on `SKIP_PATHS` not containing the new entries; the redirect tests will also fail.

- [ ] **Step 4: Update `SKIP_PATHS`**

In `app/auth/middleware.py`, update the `SKIP_PATHS` list (around line 44):

```python
SKIP_PATHS: list[str] = [
    r"/auth/login",
    r"/auth/forgot",
    r"/auth/reset/.*",
    r"/static/.*",
    r"/favicon\.ico",
    r"/api/health",
    r"/api/ping",
    r"/ws/explorer",
    r"/webhooks/github",
    r"/api/validate/.*",
]
```

- [ ] **Step 5: Add the must-change redirect**

In `app/auth/middleware.py`, modify `auth_before`. Locate the early-return block (around the line `req.scope["auth"] = user; return None` inside the `if access_token:` branch) and replace with:

```python
    if access_token:
        user = provider.get_current_user(access_token)
        if user is not None:
            req.scope["auth"] = user
            return _redirect_if_must_change(req, user)
```

Then add a helper at module-bottom (or above `auth_before`):

```python
_MUST_CHANGE_ALLOWED_PATHS = (
    "/profile/password",
    "/auth/logout",
)


def _redirect_if_must_change(req: Request, user: dict[str, Any]) -> Response | None:
    """Force users with must_change_password=True onto /profile/password."""
    if not user.get("must_change_password"):
        return None
    path = req.url.path
    if path in _MUST_CHANGE_ALLOWED_PATHS:
        return None
    if path.startswith("/static/") or path.startswith("/api/health"):
        return None
    return RedirectResponse(url="/profile/password", status_code=303)
```

Also update the silent-refresh branch to apply the same check after `try_refresh_access_token` succeeds:

```python
        rotated = try_refresh_access_token(refresh_token, provider=provider)
        if rotated is not None:
            new_access, new_refresh, _user = rotated

            redirect = RedirectResponse(url=str(req.url), status_code=307)
            set_auth_cookie(redirect, "access_token", new_access, max_age=3600)
            set_auth_cookie(redirect, "refresh_token", new_refresh, max_age=30 * 86400)
            return redirect
```

(No change to this branch — it already redirects to self with new cookies, and the next request will re-enter `auth_before` and hit the must-change check.)

- [ ] **Step 6: Run — should pass**

```bash
uv run pytest tests/test_auth_middleware_skip.py tests/test_auth_middleware.py -v
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add app/auth/middleware.py tests/test_auth_middleware_skip.py
git commit -m "feat(auth): SKIP_PATHS for forgot/reset + must_change_password redirect"
```

---

## Task 11: Self-service `/auth/forgot` route

**Files:**
- Modify: `app/auth/routes.py`
- Test: `tests/test_auth_forgot_routes.py`

- [ ] **Step 1: Write the failing test**

`tests/test_auth_forgot_routes.py`:

```python
"""Forgot-password route tests."""

import os
import uuid
from datetime import UTC, datetime, timedelta

import bcrypt
import psycopg
import pytest
from starlette.testclient import TestClient

from app.email.service import _reset_provider_for_tests, get_email_provider
from app.email.stub_provider import StubProvider


def _connect() -> psycopg.Connection:
    return psycopg.connect(os.environ.get("DATABASE_URL", ""))


@pytest.fixture(autouse=True)
def _stub_email(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("POSTMARK_API_TOKEN", raising=False)
    _reset_provider_for_tests()
    yield
    _reset_provider_for_tests()


@pytest.fixture
def real_user():
    user_id = uuid.uuid4()
    email = f"forgot-{user_id}@example.com"
    pw_hash = bcrypt.hashpw(b"Initial1A", bcrypt.gensalt()).decode()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO users (id, email, password_hash, full_name, role) "
            "VALUES (%s, %s, %s, %s, 'drafter')",
            (user_id, email, pw_hash, "Forgot User"),
        )
        conn.commit()
    yield {"id": str(user_id), "email": email}
    with _connect() as conn:
        conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.execute(
            "DELETE FROM password_reset_attempts WHERE email_hash IN ("
            "  SELECT encode(digest(lower(%s), 'sha256'), 'hex')"
            ")",
            (email,),
        )
        conn.commit()


@pytest.fixture
def client():
    from app.main import app  # builds the FastHTML app
    return TestClient(app)


def test_get_forgot_page_renders(client):
    resp = client.get("/auth/forgot")
    assert resp.status_code == 200
    assert "Parooli lähtestamine" in resp.text


def test_post_forgot_unknown_email_renders_generic_success(client):
    resp = client.post("/auth/forgot", data={"email": "nobody@example.com"})
    assert resp.status_code == 200
    assert "Kui see e-post on registreeritud" in resp.text


def test_post_forgot_known_email_creates_token_and_logs_email(client, real_user, caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="app.email.stub_provider"):
        resp = client.post("/auth/forgot", data={"email": real_user["email"]})
    assert resp.status_code == 200
    # Stub provider logged the email body containing the reset URL.
    assert any("/auth/reset/" in r.message for r in caplog.records)
    # Token row was created.
    with _connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM password_reset_tokens WHERE user_id = %s",
            (real_user["id"],),
        ).fetchone()[0]
    assert n == 1


def test_post_forgot_records_attempt_for_unknown_email(client):
    """Unknown emails still record an attempt row — used by rate limiter."""
    resp = client.post("/auth/forgot", data={"email": "rate-limit@example.com"})
    assert resp.status_code == 200
    with _connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM password_reset_attempts WHERE email_hash = "
            "encode(digest(lower(%s), 'sha256'), 'hex')",
            ("rate-limit@example.com",),
        ).fetchone()[0]
    assert n == 1


def test_post_forgot_email_rate_limit_blocks_after_3(client, real_user):
    for _ in range(3):
        client.post("/auth/forgot", data={"email": real_user["email"]})
    # Fourth in same hour should silently render generic page WITHOUT issuing a new token.
    with _connect() as conn:
        before = conn.execute(
            "SELECT COUNT(*) FROM password_reset_tokens WHERE user_id = %s",
            (real_user["id"],),
        ).fetchone()[0]
    resp = client.post("/auth/forgot", data={"email": real_user["email"]})
    assert resp.status_code == 200
    assert "Kui see e-post on registreeritud" in resp.text
    with _connect() as conn:
        after = conn.execute(
            "SELECT COUNT(*) FROM password_reset_tokens WHERE user_id = %s",
            (real_user["id"],),
        ).fetchone()[0]
    # Single-current-token policy: each issuance keeps token count = 1
    # but bumps used_at on prior. The 4th attempt must NOT have caused a new
    # token row (would still be 1 either way), so we assert via attempts table.
    n_attempts = conn.execute(
        "SELECT COUNT(*) FROM password_reset_attempts WHERE email_hash = "
        "encode(digest(lower(%s), 'sha256'), 'hex')",
        (real_user["email"],),
    ).fetchone()[0]
    assert n_attempts == 4  # all four attempts recorded
```

- [ ] **Step 2: Run — tests should fail**

```bash
uv run pytest tests/test_auth_forgot_routes.py -v
```

Expected: FAIL — `/auth/forgot` route not yet registered.

- [ ] **Step 3: Add forgot routes to `app/auth/routes.py`**

Add at the top of `app/auth/routes.py` (with other imports):

```python
from datetime import UTC, datetime, timedelta
import logging
import os

from app.auth.password import (
    hash_email,
    issue_reset_token,
)
from app.db import get_connection
from app.email.service import get_email_provider
from app.email.templates import password_reset

logger = logging.getLogger(__name__)

EMAIL_RATE_LIMIT_PER_HOUR = 3
IP_RATE_LIMIT_PER_HOUR = 10
```

Then add the new handlers below the existing ones:

```python
def _forgot_form(error: str | None = None):
    return Card(
        CardHeader(H2("Parooli lähtestamine", cls="card-title")),
        CardBody(
            Alert(error, variant="danger") if error else None,
            P(
                "Sisestage oma e-posti aadress ja saadame teile parooli "
                "lähtestamise lingi.",
                cls="muted-text",
            ),
            AppForm(
                FormField(
                    name="email",
                    label="E-post",
                    type="email",
                    required=True,
                    validator="email",
                ),
                Button("Saada lähtestamise link", type="submit", variant="primary"),
                method="post",
                action="/auth/forgot",
                cls="auth-form",
            ),
            P(
                A("← Tagasi sisselogimisele", href="/auth/login"),
                cls="back-link",
            ),
        ),
        cls="auth-card",
    )


def _forgot_sent_page(req: Request):
    """Generic post-submit page — same response for known and unknown emails."""
    from app.ui.theme import get_theme_from_request
    return PageShell(
        Card(
            CardHeader(H2("Kontrollige e-posti", cls="card-title")),
            CardBody(
                P(
                    "Kui see e-post on registreeritud, saatsime parooli "
                    "lähtestamise lingi. Vaata e-postist."
                ),
                P(
                    A("← Tagasi sisselogimisele", href="/auth/login"),
                    cls="back-link",
                ),
            ),
            cls="auth-card",
        ),
        title="Parooli lähtestamine",
        user=None,
        theme=get_theme_from_request(req),
        container_size="sm",
    )


def forgot_page(req: Request):
    from app.ui.theme import get_theme_from_request
    return PageShell(
        _forgot_form(),
        title="Parooli lähtestamine",
        user=None,
        theme=get_theme_from_request(req),
        container_size="sm",
    )


def forgot_post(req: Request, email: str):
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return forgot_page(req)

    email_h = hash_email(email)
    ip = (req.client.host if req.client else "unknown") or "unknown"

    with get_connection() as conn:
        conn.execute(
            "INSERT INTO password_reset_attempts (email_hash, ip) VALUES (%s, %s)",
            (email_h, ip),
        )
        conn.commit()

        # Rate-limit checks BEFORE user lookup → unknown emails throttled identically.
        n_email = conn.execute(
            "SELECT COUNT(*) FROM password_reset_attempts "
            "WHERE email_hash = %s AND attempted_at > now() - interval '1 hour'",
            (email_h,),
        ).fetchone()[0]
        n_ip = conn.execute(
            "SELECT COUNT(*) FROM password_reset_attempts "
            "WHERE ip = %s AND attempted_at > now() - interval '1 hour'",
            (ip,),
        ).fetchone()[0]
        if n_email > EMAIL_RATE_LIMIT_PER_HOUR or n_ip > IP_RATE_LIMIT_PER_HOUR:
            logger.info("forgot rate-limited email_hash=%s ip=%s", email_h, ip)
            return _forgot_sent_page(req)

        row = conn.execute(
            "SELECT id, full_name FROM users WHERE email = %s AND is_active = TRUE",
            (email,),
        ).fetchone()

        if row is not None:
            user_id, full_name = row
            raw = issue_reset_token(user_id=user_id, created_by=None, conn=conn)
            conn.commit()
            base = os.environ.get("APP_BASE_URL", "http://localhost:8000").rstrip("/")
            reset_url = f"{base}/auth/reset/{raw}"
            subject, html, text = password_reset(full_name=full_name, reset_url=reset_url)
            try:
                get_email_provider().send(
                    to=email, subject=subject, html=html, text=text
                )
            except Exception:
                logger.exception("password reset email failed to send")

    return _forgot_sent_page(req)
```

In the same file, update `register_auth_routes`:

```python
def register_auth_routes(rt):  # type: ignore[no-untyped-def]
    rt("/auth/login", methods=["GET"])(login_page)
    rt("/auth/login", methods=["POST"])(login_post)
    rt("/auth/logout", methods=["POST"])(logout_post)
    rt("/auth/forgot", methods=["GET"])(forgot_page)
    rt("/auth/forgot", methods=["POST"])(forgot_post)
```

- [ ] **Step 4: Run — should pass**

```bash
uv run pytest tests/test_auth_forgot_routes.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/auth/routes.py tests/test_auth_forgot_routes.py
git commit -m "feat(auth): /auth/forgot — issues reset token, rate-limited, enumeration-safe"
```

---

## Task 12: Self-service `/auth/reset/<token>` route

**Files:**
- Modify: `app/auth/routes.py`
- Test: `tests/test_auth_reset_routes.py`

- [ ] **Step 1: Write the failing test**

`tests/test_auth_reset_routes.py`:

```python
"""Reset-password route tests."""

import os
import threading
import uuid

import bcrypt
import psycopg
import pytest
from starlette.testclient import TestClient

from app.auth.password import claim_reset_token, issue_reset_token


def _connect() -> psycopg.Connection:
    return psycopg.connect(os.environ.get("DATABASE_URL", ""))


@pytest.fixture
def real_user_with_token():
    user_id = uuid.uuid4()
    email = f"reset-{user_id}@example.com"
    pw_hash = bcrypt.hashpw(b"Initial1A", bcrypt.gensalt()).decode()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO users (id, email, password_hash, full_name, role) "
            "VALUES (%s, %s, %s, %s, 'drafter')",
            (user_id, email, pw_hash, "Reset User"),
        )
        conn.commit()
        token = issue_reset_token(user_id=user_id, created_by=None, conn=conn)
        conn.commit()
    yield {"id": str(user_id), "email": email, "token": token}
    with _connect() as conn:
        conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.commit()


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app)


def test_get_reset_page_renders_for_valid_token(client, real_user_with_token):
    resp = client.get(f"/auth/reset/{real_user_with_token['token']}")
    assert resp.status_code == 200
    assert "Määra uus parool" in resp.text


def test_get_reset_page_invalid_token(client):
    resp = client.get("/auth/reset/notarealtoken")
    assert resp.status_code == 200
    assert "aegunud või vigane" in resp.text


def test_post_reset_success_clears_cookies_and_changes_password(client, real_user_with_token):
    resp = client.post(
        f"/auth/reset/{real_user_with_token['token']}",
        data={"new_password": "Brandnew1Z", "new_password_confirm": "Brandnew1Z"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"
    # Cookies cleared by Set-Cookie max-age=0 (Starlette delete_cookie):
    set_cookies = resp.headers.get_list("set-cookie")
    assert any("access_token" in sc and "Max-Age=0" in sc for sc in set_cookies)
    assert any("refresh_token" in sc and "Max-Age=0" in sc for sc in set_cookies)
    # New password works for authentication.
    with _connect() as conn:
        new_hash = conn.execute(
            "SELECT password_hash FROM users WHERE id = %s",
            (real_user_with_token["id"],),
        ).fetchone()[0]
    assert bcrypt.checkpw(b"Brandnew1Z", new_hash.encode())


def test_post_reset_password_mismatch(client, real_user_with_token):
    resp = client.post(
        f"/auth/reset/{real_user_with_token['token']}",
        data={"new_password": "Brandnew1Z", "new_password_confirm": "Different1Z"},
    )
    assert resp.status_code == 200
    assert "Paroolid ei kattu" in resp.text


def test_post_reset_password_validation_error(client, real_user_with_token):
    resp = client.post(
        f"/auth/reset/{real_user_with_token['token']}",
        data={"new_password": "short", "new_password_confirm": "short"},
    )
    assert resp.status_code == 200
    assert "8 tähemärki" in resp.text


def test_post_reset_used_token_rejected(client, real_user_with_token):
    # First use succeeds.
    client.post(
        f"/auth/reset/{real_user_with_token['token']}",
        data={"new_password": "Brandnew1Z", "new_password_confirm": "Brandnew1Z"},
    )
    # Second use of same token is rejected at the atomic UPDATE.
    resp = client.post(
        f"/auth/reset/{real_user_with_token['token']}",
        data={"new_password": "Anothernw1Z", "new_password_confirm": "Anothernw1Z"},
    )
    assert resp.status_code == 200
    assert "aegunud või vigane" in resp.text
```

- [ ] **Step 2: Run — should fail**

```bash
uv run pytest tests/test_auth_reset_routes.py -v
```

Expected: FAIL.

- [ ] **Step 3: Add reset routes to `app/auth/routes.py`**

Add imports near the top (extend the password import):

```python
from app.auth.password import (
    change_password,
    claim_reset_token,
    hash_email,
    issue_reset_token,
    validate_password,
)
from app.auth.cookies import clear_auth_cookie, set_auth_cookie
```

Add the handlers:

```python
def _reset_form(token: str, error: str | None = None):
    from app.ui.forms.form_field import FormField
    return Card(
        CardHeader(H2("Määra uus parool", cls="card-title")),
        CardBody(
            Alert(error, variant="danger") if error else None,
            AppForm(
                FormField(name="new_password", label="Uus parool", type="password", required=True),
                FormField(
                    name="new_password_confirm",
                    label="Korda uut parooli",
                    type="password",
                    required=True,
                ),
                Button("Salvesta uus parool", type="submit", variant="primary"),
                method="post",
                action=f"/auth/reset/{token}",
                cls="auth-form",
            ),
        ),
        cls="auth-card",
    )


def _reset_invalid_page(req: Request):
    from app.ui.theme import get_theme_from_request
    return PageShell(
        Card(
            CardHeader(H2("Lähtestamise link on aegunud või vigane", cls="card-title")),
            CardBody(
                P("Palun taotlege uus parooli lähtestamise link."),
                P(A("Taotle uus link", href="/auth/forgot"), cls="back-link"),
            ),
            cls="auth-card",
        ),
        title="Lähtestamise link",
        user=None,
        theme=get_theme_from_request(req),
        container_size="sm",
    )


def reset_page(req: Request, token: str):
    from app.auth.password import hash_token
    from app.db import get_connection
    from app.ui.theme import get_theme_from_request

    digest = hash_token(token)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM password_reset_tokens "
            "WHERE token_hash = %s AND used_at IS NULL AND expires_at > now()",
            (digest,),
        ).fetchone()
    if row is None:
        return _reset_invalid_page(req)

    return PageShell(
        _reset_form(token),
        title="Määra uus parool",
        user=None,
        theme=get_theme_from_request(req),
        container_size="sm",
    )


def reset_post(req: Request, token: str, new_password: str, new_password_confirm: str):
    from app.db import get_connection
    from app.ui.theme import get_theme_from_request

    if new_password != new_password_confirm:
        return PageShell(
            _reset_form(token, error="Paroolid ei kattu."),
            title="Määra uus parool",
            user=None,
            theme=get_theme_from_request(req),
            container_size="sm",
        )

    pw_error = validate_password(new_password)
    if pw_error:
        return PageShell(
            _reset_form(token, error=pw_error),
            title="Määra uus parool",
            user=None,
            theme=get_theme_from_request(req),
            container_size="sm",
        )

    with get_connection() as conn:
        with conn.transaction():
            claimed = claim_reset_token(token, conn=conn)
            if claimed is None:
                # Token already used / expired / never existed.
                return _reset_invalid_page(req)
            user_id, _created_by = claimed
            change_password(user_id, new_password, conn=conn)
        conn.commit()

        # Audit log inside its own commit (it's fire-and-forget elsewhere).
        from app.auth.audit import log_action
        log_action(user_id, "user.password_reset", {"self_service": True})

    response = RedirectResponse(url="/auth/login", status_code=303)
    clear_auth_cookie(response, "access_token")
    clear_auth_cookie(response, "refresh_token")
    return response
```

Update `register_auth_routes`:

```python
    rt("/auth/reset/{token}", methods=["GET"])(reset_page)
    rt("/auth/reset/{token}", methods=["POST"])(reset_post)
```

- [ ] **Step 4: Run — should pass**

```bash
uv run pytest tests/test_auth_reset_routes.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/auth/routes.py tests/test_auth_reset_routes.py
git commit -m "feat(auth): /auth/reset/<token> — atomic claim, password change, cookie clearing"
```

---

## Task 13: `/profile` and `/profile/password`

**Files:**
- Create: `app/auth/profile.py`
- Test: `tests/test_profile_password.py`
- Modify: `app/main.py` — register profile routes

- [ ] **Step 1: Write the failing test**

`tests/test_profile_password.py`:

```python
"""Profile + change-password integration tests."""

import os
import uuid

import bcrypt
import psycopg
import pytest
from starlette.testclient import TestClient


def _connect() -> psycopg.Connection:
    return psycopg.connect(os.environ.get("DATABASE_URL", ""))


@pytest.fixture
def logged_in_user_client():
    user_id = uuid.uuid4()
    email = f"prof-{user_id}@example.com"
    pw_hash = bcrypt.hashpw(b"Initial1A", bcrypt.gensalt()).decode()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO users (id, email, password_hash, full_name, role) "
            "VALUES (%s, %s, %s, %s, 'drafter')",
            (user_id, email, pw_hash, "Prof User"),
        )
        conn.commit()

    from app.auth.jwt_provider import JWTAuthProvider
    from app.main import app
    provider = JWTAuthProvider()
    user = provider.authenticate(email, "Initial1A")
    assert user is not None
    access, refresh = provider.create_tokens(user)

    client = TestClient(app)
    client.cookies.set("access_token", access)
    client.cookies.set("refresh_token", refresh)

    yield {"client": client, "id": str(user_id), "email": email}

    with _connect() as conn:
        conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.commit()


def test_get_profile_lists_change_password(logged_in_user_client):
    resp = logged_in_user_client["client"].get("/profile")
    assert resp.status_code == 200
    assert "Profiil" in resp.text
    assert "Vaheta parool" in resp.text


def test_get_profile_password_form(logged_in_user_client):
    resp = logged_in_user_client["client"].get("/profile/password")
    assert resp.status_code == 200
    assert "Praegune parool" in resp.text


def test_post_profile_password_wrong_current(logged_in_user_client):
    resp = logged_in_user_client["client"].post(
        "/profile/password",
        data={
            "current_password": "wrong",
            "new_password": "Brandnew1Z",
            "new_password_confirm": "Brandnew1Z",
        },
    )
    assert resp.status_code == 200
    assert "Praegune parool on vale" in resp.text


def test_post_profile_password_success(logged_in_user_client):
    resp = logged_in_user_client["client"].post(
        "/profile/password",
        data={
            "current_password": "Initial1A",
            "new_password": "Brandnew1Z",
            "new_password_confirm": "Brandnew1Z",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"
    set_cookies = resp.headers.get_list("set-cookie")
    assert any("access_token" in sc and "Max-Age=0" in sc for sc in set_cookies)
    assert any("refresh_token" in sc and "Max-Age=0" in sc for sc in set_cookies)


def test_post_profile_password_mismatch(logged_in_user_client):
    resp = logged_in_user_client["client"].post(
        "/profile/password",
        data={
            "current_password": "Initial1A",
            "new_password": "Brandnew1Z",
            "new_password_confirm": "Different1Z",
        },
    )
    assert resp.status_code == 200
    assert "Paroolid ei kattu" in resp.text
```

- [ ] **Step 2: Run — should fail**

```bash
uv run pytest tests/test_profile_password.py -v
```

Expected: FAIL — no /profile routes.

- [ ] **Step 3: Write `app/auth/profile.py`**

```python
"""User profile routes — /profile (hub) and /profile/password (change pw form)."""

from __future__ import annotations

from fasthtml.common import *  # noqa: F403
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.auth.audit import log_action
from app.auth.cookies import clear_auth_cookie
from app.auth.jwt_provider import verify_password
from app.auth.password import change_password, validate_password
from app.db import get_connection
from app.ui.forms.app_form import AppForm
from app.ui.forms.form_field import FormField
from app.ui.layout import PageShell
from app.ui.primitives.button import Button
from app.ui.surfaces.alert import Alert
from app.ui.surfaces.card import Card, CardBody, CardHeader


def _password_form(error: str | None = None, *, force: bool = False):
    return Card(
        CardHeader(H2("Vaheta parool", cls="card-title")),
        CardBody(
            (
                Alert(
                    "Administraator on lähtestanud teie parooli. "
                    "Palun määrake uus parool jätkamiseks.",
                    variant="warning",
                )
                if force
                else None
            ),
            Alert(error, variant="danger") if error else None,
            AppForm(
                FormField(
                    name="current_password",
                    label="Praegune parool",
                    type="password",
                    required=True,
                ),
                FormField(
                    name="new_password",
                    label="Uus parool",
                    type="password",
                    required=True,
                ),
                FormField(
                    name="new_password_confirm",
                    label="Korda uut parooli",
                    type="password",
                    required=True,
                ),
                Div(
                    Button("Salvesta", type="submit", variant="primary"),
                    A("Tühista", href="/profile", cls="btn btn-ghost btn-md"),
                    cls="form-actions",
                ),
                method="post",
                action="/profile/password",
            ),
        ),
    )


def profile_page(req: Request):
    auth = req.scope.get("auth")
    return PageShell(
        H1("Profiil", cls="page-title"),
        Card(
            CardHeader(H3("Konto", cls="card-title")),
            CardBody(
                P(f"E-post: {auth['email']}", cls="muted-text") if auth else None,
                P(f"Nimi: {auth['full_name']}", cls="muted-text") if auth else None,
                Hr(),
                P(A("Vaheta parool", href="/profile/password", cls="btn btn-secondary")),
            ),
        ),
        title="Profiil",
        user=auth,
        active_nav="/profile",
    )


def profile_password_page(req: Request):
    auth = req.scope.get("auth")
    return PageShell(
        H1("Vaheta parool", cls="page-title"),
        _password_form(force=bool(auth and auth.get("must_change_password"))),
        title="Vaheta parool",
        user=auth,
        active_nav="/profile",
    )


def profile_password_post(
    req: Request,
    current_password: str,
    new_password: str,
    new_password_confirm: str,
):
    auth = req.scope.get("auth")
    if auth is None:
        return RedirectResponse(url="/auth/login", status_code=303)

    user_id = auth["id"]
    email = auth["email"]
    forced = bool(auth.get("must_change_password"))

    if new_password != new_password_confirm:
        return PageShell(
            H1("Vaheta parool", cls="page-title"),
            _password_form(error="Paroolid ei kattu.", force=forced),
            title="Vaheta parool",
            user=auth,
            active_nav="/profile",
        )

    pw_error = validate_password(new_password, email=email)
    if pw_error:
        return PageShell(
            H1("Vaheta parool", cls="page-title"),
            _password_form(error=pw_error, force=forced),
            title="Vaheta parool",
            user=auth,
            active_nav="/profile",
        )

    with get_connection() as conn:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE id = %s",
            (user_id,),
        ).fetchone()
        if row is None or not verify_password(current_password, row[0]):
            return PageShell(
                H1("Vaheta parool", cls="page-title"),
                _password_form(error="Praegune parool on vale.", force=forced),
                title="Vaheta parool",
                user=auth,
                active_nav="/profile",
            )

        change_password(user_id, new_password, conn=conn)
        conn.commit()

    log_action(user_id, "user.password_change", {"forced": forced})

    response = RedirectResponse(url="/auth/login", status_code=303)
    clear_auth_cookie(response, "access_token")
    clear_auth_cookie(response, "refresh_token")
    return response


def register_profile_routes(rt):  # type: ignore[no-untyped-def]
    rt("/profile", methods=["GET"])(profile_page)
    rt("/profile/password", methods=["GET"])(profile_password_page)
    rt("/profile/password", methods=["POST"])(profile_password_post)
```

- [ ] **Step 4: Register the routes in `app/main.py`**

Find the imports block (around line 16) and add:

```python
from app.auth.profile import register_profile_routes
```

Then in the route-registration block (after `register_user_routes(rt)` around line 205) add:

```python
register_profile_routes(rt)
```

- [ ] **Step 5: Run — should pass**

```bash
uv run pytest tests/test_profile_password.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add app/auth/profile.py app/main.py tests/test_profile_password.py
git commit -m "feat(profile): /profile + /profile/password change-password flow"
```

---

## Task 14: Admin reset routes — `reset_email` (system + org)

**Files:**
- Modify: `app/auth/users.py` — new `admin_user_reset_page`, `admin_user_reset_email`, plus org variants.
- Test: `tests/test_admin_password_reset.py`

- [ ] **Step 1: Write the failing test**

`tests/test_admin_password_reset.py`:

```python
"""Admin reset password tests — system and org variants."""

import os
import uuid

import bcrypt
import psycopg
import pytest
from starlette.testclient import TestClient


def _connect() -> psycopg.Connection:
    return psycopg.connect(os.environ.get("DATABASE_URL", ""))


@pytest.fixture
def org_with_users():
    org_id = uuid.uuid4()
    sysadmin_id = uuid.uuid4()
    orgadmin_id = uuid.uuid4()
    drafter_id = uuid.uuid4()
    other_org_admin_id = uuid.uuid4()

    pw = bcrypt.hashpw(b"Initial1A", bcrypt.gensalt()).decode()

    with _connect() as conn:
        conn.execute(
            "INSERT INTO organizations (id, name, slug) VALUES (%s, %s, %s)",
            (org_id, f"OrgX-{org_id}", f"orgx-{org_id}"),
        )
        conn.execute(
            "INSERT INTO users (id, email, password_hash, full_name, role, org_id) VALUES "
            "(%s, %s, %s, 'SysAdmin', 'admin', NULL)",
            (sysadmin_id, f"sa-{sysadmin_id}@example.com", pw),
        )
        conn.execute(
            "INSERT INTO users (id, email, password_hash, full_name, role, org_id) VALUES "
            "(%s, %s, %s, 'OrgAdmin', 'org_admin', %s)",
            (orgadmin_id, f"oa-{orgadmin_id}@example.com", pw, org_id),
        )
        conn.execute(
            "INSERT INTO users (id, email, password_hash, full_name, role, org_id) VALUES "
            "(%s, %s, %s, 'Drafter', 'drafter', %s)",
            (drafter_id, f"dr-{drafter_id}@example.com", pw, org_id),
        )
        conn.execute(
            "INSERT INTO users (id, email, password_hash, full_name, role, org_id) VALUES "
            "(%s, %s, %s, 'OtherOrgAdmin', 'org_admin', %s)",
            (other_org_admin_id, f"ooa-{other_org_admin_id}@example.com", pw, org_id),
        )
        conn.commit()

    yield {
        "org_id": str(org_id),
        "sysadmin_id": str(sysadmin_id),
        "orgadmin_id": str(orgadmin_id),
        "drafter_id": str(drafter_id),
        "other_org_admin_id": str(other_org_admin_id),
    }

    with _connect() as conn:
        for uid in (sysadmin_id, orgadmin_id, drafter_id, other_org_admin_id):
            conn.execute("DELETE FROM users WHERE id = %s", (uid,))
        conn.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
        conn.commit()


def _client_as(user_id: str, email: str) -> TestClient:
    from app.auth.jwt_provider import JWTAuthProvider
    from app.main import app
    p = JWTAuthProvider()
    user = p.authenticate(email, "Initial1A")
    assert user is not None, f"Failed to auth as {email}"
    access, refresh = p.create_tokens(user)
    c = TestClient(app)
    c.cookies.set("access_token", access)
    c.cookies.set("refresh_token", refresh)
    return c


def test_system_admin_can_reset_drafter(org_with_users, caplog):
    import logging
    sa = org_with_users["sysadmin_id"]
    sa_email = f"sa-{sa}@example.com"
    target = org_with_users["drafter_id"]
    c = _client_as(sa, sa_email)
    with caplog.at_level(logging.INFO):
        resp = c.post(f"/admin/users/{target}/reset_email", follow_redirects=False)
    assert resp.status_code == 303
    with _connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM password_reset_tokens "
            "WHERE user_id = %s AND created_by = %s",
            (target, sa),
        ).fetchone()[0]
    assert n == 1


def test_org_admin_can_reset_own_org_drafter(org_with_users):
    oa = org_with_users["orgadmin_id"]
    oa_email = f"oa-{oa}@example.com"
    target = org_with_users["drafter_id"]
    c = _client_as(oa, oa_email)
    resp = c.post(f"/org/users/{target}/reset_email", follow_redirects=False)
    assert resp.status_code == 303
    with _connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM password_reset_tokens "
            "WHERE user_id = %s AND created_by = %s",
            (target, oa),
        ).fetchone()[0]
    assert n == 1


def test_org_admin_cannot_reset_another_org_admin(org_with_users):
    oa = org_with_users["orgadmin_id"]
    oa_email = f"oa-{oa}@example.com"
    target = org_with_users["other_org_admin_id"]
    c = _client_as(oa, oa_email)
    resp = c.post(f"/org/users/{target}/reset_email", follow_redirects=False)
    # Returns the error page (200 with Estonian error), no token created.
    assert resp.status_code in (200, 303, 403)
    with _connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM password_reset_tokens WHERE user_id = %s",
            (target,),
        ).fetchone()[0]
    assert n == 0
```

- [ ] **Step 2: Run — should fail**

```bash
uv run pytest tests/test_admin_password_reset.py -v
```

Expected: FAIL — routes don't exist.

- [ ] **Step 3: Add admin reset routes to `app/auth/users.py`**

Add imports near the top:

```python
import os

from app.auth.password import (
    change_password,
    issue_reset_token,
    validate_password,
)
from app.email.service import get_email_provider
from app.email.templates import password_reset_admin
```

Add the route handlers (place them after the existing `org_user_deactivate` and before the route registration block):

```python
def _admin_reset_page(req: Request, user_id: str, *, base_path: str, active_nav: str):
    """GET /admin/users/{id}/reset (or /org/users/{id}/reset)."""
    auth = req.scope.get("auth", {}) or {}
    user = get_user(user_id)
    if user is None:
        return _error_page(req, "Kasutajat ei leitud.", base_path, active_nav)

    # For org route, additional scope check
    if base_path == "/org/users":
        org_id = auth.get("org_id")
        if user.get("org_id") != org_id or user["role"] not in ORG_ASSIGNABLE_ROLES:
            return _error_page(
                req,
                "Selle kasutaja parooli ei saa lähtestada.",
                base_path,
                active_nav,
            )

    flash = req.query_params.get("temp")  # temp password reveal block

    return PageShell(
        H1("Lähtesta parool", cls="page-title"),
        Card(
            CardHeader(P(f"Kasutaja: {user['full_name']} ({user['email']})", cls="card-subtitle")),
            CardBody(
                Alert(
                    f"Ajutine parool on määratud. Edasta see kasutajale turvaliselt: {flash}",
                    variant="success",
                )
                if flash
                else None,
                # Email-link option (primary)
                AppForm(
                    Button(
                        "Saada e-postiga",
                        type="submit",
                        variant="primary",
                    ),
                    method="post",
                    action=f"{base_path}/{user_id}/reset_email",
                ),
                Hr(),
                # Temp-password option (fallback)
                AppForm(
                    FormField(
                        name="new_password",
                        label="Ajutine parool",
                        type="password",
                        required=True,
                    ),
                    Button("Määra ajutine parool", type="submit", variant="secondary"),
                    method="post",
                    action=f"{base_path}/{user_id}/reset_temp",
                ),
                Div(
                    A("Tühista", href=base_path, cls="btn btn-ghost btn-md"),
                    cls="form-actions",
                ),
            ),
        ),
        title="Lähtesta parool",
        user=auth,
        active_nav=active_nav,
    )


def _admin_reset_email(req: Request, user_id: str, *, base_path: str, active_nav: str):
    """POST /admin/users/{id}/reset_email — issue token + send Postmark email."""
    auth = req.scope.get("auth", {})
    user = get_user(user_id)
    if user is None:
        return _error_page(req, "Kasutajat ei leitud.", base_path, active_nav)

    if base_path == "/org/users":
        if user.get("org_id") != auth.get("org_id") or user["role"] not in ORG_ASSIGNABLE_ROLES:
            return _error_page(
                req,
                "Selle kasutaja parooli ei saa lähtestada.",
                base_path,
                active_nav,
            )

    with _connect() as conn:
        raw = issue_reset_token(
            user_id=user_id, created_by=auth.get("id"), conn=conn
        )
        conn.commit()

    base = os.environ.get("APP_BASE_URL", "http://localhost:8000").rstrip("/")
    reset_url = f"{base}/auth/reset/{raw}"
    subject, html, text = password_reset_admin(
        full_name=user["full_name"],
        reset_url=reset_url,
        admin_name=auth.get("full_name", "Administraator"),
    )
    try:
        get_email_provider().send(to=user["email"], subject=subject, html=html, text=text)
    except Exception:
        logger.exception("admin password reset email failed to send")

    log_action(
        auth.get("id"),
        "user.password_reset_initiated",
        {"target_user_id": user_id, "mode": "email"},
    )
    return RedirectResponse(url=base_path, status_code=303)


def admin_user_reset_page(req: Request, user_id: str):
    return _admin_reset_page(req, user_id, base_path="/admin/users", active_nav="/admin")


def admin_user_reset_email(req: Request, user_id: str):
    return _admin_reset_email(req, user_id, base_path="/admin/users", active_nav="/admin")


def org_user_reset_page(req: Request, user_id: str):
    return _admin_reset_page(req, user_id, base_path="/org/users", active_nav="/org/users")


def org_user_reset_email(req: Request, user_id: str):
    return _admin_reset_email(req, user_id, base_path="/org/users", active_nav="/org/users")
```

Update the registration helpers near the bottom of `users.py`:

```python
_admin_user_reset_page = require_role("admin")(admin_user_reset_page)
_admin_user_reset_email = require_role("admin")(admin_user_reset_email)

_org_user_reset_page = require_role("org_admin", "admin")(org_user_reset_page)
_org_user_reset_email = require_role("org_admin", "admin")(org_user_reset_email)
```

And register them in `register_user_routes`:

```python
    rt("/admin/users/{user_id}/reset", methods=["GET"])(_admin_user_reset_page)
    rt("/admin/users/{user_id}/reset_email", methods=["POST"])(_admin_user_reset_email)

    rt("/org/users/{user_id}/reset", methods=["GET"])(_org_user_reset_page)
    rt("/org/users/{user_id}/reset_email", methods=["POST"])(_org_user_reset_email)
```

- [ ] **Step 4: Run — should pass**

```bash
uv run pytest tests/test_admin_password_reset.py::test_system_admin_can_reset_drafter -v
uv run pytest tests/test_admin_password_reset.py::test_org_admin_can_reset_own_org_drafter -v
uv run pytest tests/test_admin_password_reset.py::test_org_admin_cannot_reset_another_org_admin -v
```

Expected: all three pass.

- [ ] **Step 5: Commit**

```bash
git add app/auth/users.py tests/test_admin_password_reset.py
git commit -m "feat(admin): admin reset password — email link path (system + org variants)"
```

---

## Task 15: Admin reset — `reset_temp` (system + org)

**Files:**
- Modify: `app/auth/users.py`
- Modify: `tests/test_admin_password_reset.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_admin_password_reset.py`:

```python
def test_system_admin_temp_password_sets_must_change(org_with_users):
    sa = org_with_users["sysadmin_id"]
    sa_email = f"sa-{sa}@example.com"
    target = org_with_users["drafter_id"]
    c = _client_as(sa, sa_email)
    resp = c.post(
        f"/admin/users/{target}/reset_temp",
        data={"new_password": "Tempnew1Z"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    # Redirects to the reset page with the temp password as ?temp= query for one-time reveal.
    assert "/reset" in resp.headers["location"]
    assert "temp=Tempnew1Z" in resp.headers["location"]

    with _connect() as conn:
        row = conn.execute(
            "SELECT must_change_password, password_hash FROM users WHERE id = %s",
            (target,),
        ).fetchone()
    must_change, pw_hash = row
    assert must_change is True
    assert bcrypt.checkpw(b"Tempnew1Z", pw_hash.encode())


def test_org_admin_cannot_temp_password_org_admin(org_with_users):
    oa = org_with_users["orgadmin_id"]
    oa_email = f"oa-{oa}@example.com"
    target = org_with_users["other_org_admin_id"]
    c = _client_as(oa, oa_email)
    resp = c.post(
        f"/org/users/{target}/reset_temp",
        data={"new_password": "Tempnew1Z"},
        follow_redirects=False,
    )
    assert resp.status_code in (200, 303, 403)
    with _connect() as conn:
        must_change = conn.execute(
            "SELECT must_change_password FROM users WHERE id = %s",
            (target,),
        ).fetchone()[0]
    assert must_change is False
```

- [ ] **Step 2: Run — should fail**

```bash
uv run pytest tests/test_admin_password_reset.py -k temp -v
```

Expected: FAIL.

- [ ] **Step 3: Add the temp routes**

In `app/auth/users.py`, add the shared handler:

```python
def _admin_reset_temp(
    req: Request, user_id: str, new_password: str, *, base_path: str, active_nav: str
):
    """POST /…/reset_temp — set a temp password and force change on next login."""
    auth = req.scope.get("auth", {})
    user = get_user(user_id)
    if user is None:
        return _error_page(req, "Kasutajat ei leitud.", base_path, active_nav)

    if base_path == "/org/users":
        if user.get("org_id") != auth.get("org_id") or user["role"] not in ORG_ASSIGNABLE_ROLES:
            return _error_page(
                req,
                "Selle kasutaja parooli ei saa lähtestada.",
                base_path,
                active_nav,
            )

    pw_error = validate_password(new_password, email=user["email"])
    if pw_error:
        return _error_page(req, pw_error, f"{base_path}/{user_id}/reset", active_nav)

    with _connect() as conn:
        change_password(user_id, new_password, conn=conn, must_change=True)
        conn.commit()

    log_action(
        auth.get("id"),
        "user.password_reset_initiated",
        {"target_user_id": user_id, "mode": "temp"},
    )

    # One-time reveal of the temp password on the reset page via query param.
    # Refresh / navigation removes it.
    return RedirectResponse(
        url=f"{base_path}/{user_id}/reset?temp={new_password}",
        status_code=303,
    )


def admin_user_reset_temp(req: Request, user_id: str, new_password: str):
    return _admin_reset_temp(
        req,
        user_id,
        new_password,
        base_path="/admin/users",
        active_nav="/admin",
    )


def org_user_reset_temp(req: Request, user_id: str, new_password: str):
    return _admin_reset_temp(
        req,
        user_id,
        new_password,
        base_path="/org/users",
        active_nav="/org/users",
    )
```

Update the role-decorator wrappers and route registration:

```python
_admin_user_reset_temp = require_role("admin")(admin_user_reset_temp)
_org_user_reset_temp = require_role("org_admin", "admin")(org_user_reset_temp)
```

```python
    rt("/admin/users/{user_id}/reset_temp", methods=["POST"])(_admin_user_reset_temp)
    rt("/org/users/{user_id}/reset_temp", methods=["POST"])(_org_user_reset_temp)
```

- [ ] **Step 4: Run — should pass**

```bash
uv run pytest tests/test_admin_password_reset.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/auth/users.py tests/test_admin_password_reset.py
git commit -m "feat(admin): admin reset — temp password fallback with must_change_password"
```

---

## Task 16: UI — login link, admin user-row reset link

**Files:**
- Modify: `app/auth/routes.py` — add "Unustasid parooli?" link below the password field on the login form.
- Modify: `app/auth/users.py` — add `Lähtesta parool` link to the user-row actions in `_user_table`.

- [ ] **Step 1: Update `_login_form`**

In `app/auth/routes.py`, locate `_login_form` (around line 27) and modify the body so the form contains a `Unustasid parooli?` link below the password field:

```python
def _login_form(email: str = "", error: str | None = None):
    return Card(
        CardHeader(H2("Sisselogimine", cls="card-title")),
        CardBody(
            Alert("Vale e-post või parool.", variant="danger") if error else None,
            AppForm(
                FormField(
                    name="email",
                    label="E-post",
                    type="email",
                    value=email,
                    required=True,
                    validator="email",
                ),
                FormField(
                    name="password",
                    label="Parool",
                    type="password",
                    required=True,
                ),
                P(A("Unustasid parooli?", href="/auth/forgot"), cls="forgot-link"),
                Button("Logi sisse", type="submit", variant="primary"),
                method="post",
                action="/auth/login",
                cls="auth-form",
            ),
        ),
        cls="auth-card",
    )
```

- [ ] **Step 2: Update `_user_table` actions**

In `app/auth/users.py`, locate `_actions_cell` inside `_user_table` (around line 258) and modify so the `Lähtesta parool` link is added (after `Muuda rolli`):

```python
    def _actions_cell(row: dict) -> object:  # type: ignore[type-arg]
        actions: list = [
            A(
                "Muuda rolli",
                href=f"{base_path}/{row['id']}/role",
                cls="btn btn-secondary btn-sm",
            ),
        ]
        # For org admin pages, only show reset for users in ORG_ASSIGNABLE_ROLES;
        # for system admin pages, show for everyone.
        show_reset = (
            base_path == "/admin/users"
            or (base_path == "/org/users" and row["role"] in ORG_ASSIGNABLE_ROLES)
        )
        if row["_is_active"] and show_reset:
            actions.append(
                A(
                    "Lähtesta parool",
                    href=f"{base_path}/{row['id']}/reset",
                    cls="btn btn-secondary btn-sm",
                )
            )
        if row["_is_active"]:
            actions.append(
                AppForm(
                    Button(
                        "Deaktiveeri",
                        type="submit",
                        variant="danger",
                        size="sm",
                    ),
                    method="post",
                    action=f"{base_path}/{row['id']}/deactivate",
                    cls="inline-form",
                )
            )
        return Div(*actions, cls="table-actions")
```

Note: the `_user_table` rows builder must include `role` in its row dict so `_actions_cell` can read it. Locate the rows = [...] block (around line 301) and ensure `role` is included:

```python
    rows = [
        {
            "id": u["id"],
            "full_name": u["full_name"],
            "email": u["email"],
            "org_name": u.get("org_name", "—"),
            "role": u["role"],
            "_is_active": u.get("is_active", True),
        }
        for u in users
    ]
```

(The existing code already maps `role` — verify and leave as-is.)

- [ ] **Step 3: Smoke-test the UI changes locally**

```bash
uv run python -c "from app.main import app; from starlette.testclient import TestClient; c = TestClient(app); r = c.get('/auth/login'); assert 'Unustasid parooli?' in r.text; print('login OK')"
```

Expected: `login OK`.

- [ ] **Step 4: Run the full auth/admin/profile test sweep**

```bash
uv run pytest tests/test_auth_routes.py tests/test_admin_password_reset.py tests/test_profile_password.py tests/test_auth_forgot_routes.py tests/test_auth_reset_routes.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/auth/routes.py app/auth/users.py
git commit -m "feat(ui): login forgot link + admin user-row Lähtesta parool action"
```

---

## Task 17: Final integration — full test sweep, type check, lint

**Files:** none new.

- [ ] **Step 1: Run the full test suite**

```bash
uv run pytest -v
```

Expected: ALL existing + new tests pass. If any old test fails, investigate (likely a forgotten `must_change_password` field in a test fixture or mock).

- [ ] **Step 2: Run lint and type-check**

```bash
uv run ruff format .
uv run ruff check .
uv run pyright app/
```

Expected: zero errors. Fix any reported issues.

- [ ] **Step 3: Manual smoke test (browser)**

Bring up the local app:

```bash
uv run uvicorn app.main:app --reload --port 8000
```

In a browser:

1. Open http://localhost:8000/auth/login → click `Unustasid parooli?` → submit a known email → confirm the generic "Kontrollige e-posti" page.
2. Watch the server log for the `[StubEmail]` line containing the reset URL.
3. Visit the reset URL → set a new password → confirm redirect to login.
4. Log in with the new password → visit `/profile` → click `Vaheta parool` → change password again → confirm redirect to login.
5. As a system admin, visit `/admin/users` → `Lähtesta parool` on a user → `Saada e-postiga` → check log for the admin email.
6. Repeat with `Määra ajutine parool` → confirm the temp-password reveal banner appears once.
7. Log in as that user with the temp password → confirm forced redirect to `/profile/password` regardless of where you navigate.

- [ ] **Step 4: Commit (if any cleanup landed)**

```bash
git status
# If anything is dirty:
git add -A
git commit -m "chore: cleanup after final smoke test"
```

---

## Task 18: Production deploy notes (no code change)

**Files:**
- Modify: `README.md` deployment section

- [ ] **Step 1: Add env var documentation**

Append to the deployment section of `README.md` (or `.env.example` if that's where deployment env vars are documented in the project):

```markdown
### Email — Postmark (Phase 4)

The forgot-password / admin-reset flows send transactional email through
Postmark. In `APP_ENV=production`:

- `POSTMARK_API_TOKEN` (required) — Server API token for the "Seadusloome"
  Postmark server.
- `EMAIL_FROM` (optional, default `Seadusloome <noreply@sixtyfour.ee>`) —
  From-address for transactional email.
- `APP_BASE_URL` (required) — Base URL used to build reset links in email
  bodies (e.g. `https://seadusloome.sixtyfour.ee`).

In dev/test/CI (`APP_ENV != production`), all three vars are optional —
the email module falls back to a stub provider that logs the email body
to stdout.
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: env vars for Postmark password-reset email"
```

---

## Spec coverage check

| Spec section | Implementing task |
|---|---|
| §3 Constraints — postmarker dep | Task 0 |
| §4.1 File structure | Tasks 2-6, 13 |
| §4.2 EmailProvider gate | Tasks 2-4 |
| §4.3 DB schema | Task 1 |
| §4.4 change_password core | Tasks 6-7 |
| §4.5 validate_password extension | Task 6 |
| §4.6 Atomic token claim | Tasks 6, 8 |
| §4.7 SKIP_PATHS + cookie clearing | Tasks 10, 12, 13 |
| §4.8 must_change_password redirect | Tasks 9, 10 |
| §4.9 CSRF posture | Documented in spec; nothing to implement |
| §5.1 Forgot flow | Task 11 |
| §5.2 Profile change flow | Task 13 |
| §5.3 Admin reset email | Task 14 |
| §5.4 Admin reset temp | Task 15 |
| §5.5 Reset landing page | Task 12 |
| §6 Security controls | Verified by Task 17 sweep |
| §7 UI/UX | Tasks 11-16 |
| §8 Estonian copy | Tasks 5, 11-15 |
| §9 Env vars | Tasks 4, 18 |
| §10 Tests | Tasks 2-15 |
| §11 Rollout | Task 1 (migration) |
