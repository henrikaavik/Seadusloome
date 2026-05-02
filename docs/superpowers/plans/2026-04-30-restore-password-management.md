# Restore Password Management onto `main` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surgically restore the missing pieces of password-management (admin reset, self-service forgot, email provider module) onto `main` by porting from the never-merged `feature/password-management` branch, then add regression tests so the feature cannot be silently dropped again.

**Architecture:** `main` already has the *core* of password management (validate_password split, change_password, /profile/password, must_change_password middleware redirect, migration 024 with `must_change_password` + `password_changed_at` columns). What is missing is (a) the `app/email/` provider module, (b) token issuance/claim helpers, (c) the `password_reset_tokens` and `password_reset_attempts` tables, (d) `/auth/forgot` and `/auth/reset/<token>` routes, (e) the admin reset UI + handlers, (f) entry-point UI elements (login "Unustasid parooli?" link, admin user-row "Lähtesta parool" button). We do **not** do `git merge feature/password-management` because the branches diverged at `5b4f483` and the feature branch is missing the 89-file follow-ups commit `7c786ce` plus `bdc3d6e` and `07d9af9`. Instead we cherry-pick the missing chunks onto `main` on a new branch `restore/password-management`, add a fresh `migration 025` (so `024` keeps its prod-applied shape), and add UI smoke tests that assert the entry points are wired.

**Tech Stack:** FastHTML, Python 3.13, Starlette, PostgreSQL 18, postmarker SDK, uv, pytest, ruff, pyright. Donor branch already in worktree at `.worktrees/password-mgmt/`.

---

## Background — What is actually wrong

`git log --oneline main..feature/password-management` shows 15 commits not on main:
- `d08e82a` `feat(email): EmailProvider ABC + StubProvider`
- `611a750` `feat(email): PostmarkProvider via postmarker SDK with lazy init`
- `233ea1f` `feat(email): get_email_provider() gate — stub in dev, raise in prod without token`
- `f10d7d6` `feat(email): Estonian password_reset and password_reset_admin templates`
- `7c9a684` `feat(db): migration 024 — password reset tokens, attempts, must_change_password`
- `eb00072` `feat(auth): split validate_password into app/auth/password + extend with email-substring check`
- `25fa11f` `test(auth): integration tests for change_password + atomic token claim`
- `15f1dd1` `feat(auth): thread must_change_password through UserDict and JWTAuthProvider`
- `70f01d3` `feat(auth): SKIP_PATHS for forgot/reset + must_change_password redirect`
- `76b98f2` `feat(auth): /auth/forgot and /auth/reset/<token> with rate limit, atomic claim, cookie clearing`
- `56e5675` `feat(profile): /profile + /profile/password change-password flow`
- `8586997` `feat(admin): admin reset password — email link + temp password fallback (system + org)`
- `19f71f2` `feat(ui): login forgot link + admin user-row Lähtesta parool action`
- `f3c8148` `docs: env vars for Postmark password-reset email`
- `515f6c7` `chore(deps): add postmarker for transactional email`

`main` independently grew commit `7c786ce` ("UI review 2026-04-29 follow-ups") which re-implemented a *subset* of the same spec: it added `app/auth/password.py` (with `validate_password` + `change_password` only), `app/auth/profile.py`, `app/auth/middleware.py` redirect, and `migration 024_must_change_password.sql`. That overlap is fine — main's versions are functional but minimal; the feature branch has a superset.

**Strategy:** treat main's existing files as the foundation. Add what's missing without disturbing what works.

---

## File Structure

| File | Action | Reason |
|---|---|---|
| `migrations/025_password_reset_tables.sql` | Create | Add `password_reset_tokens` + `password_reset_attempts` (024 already has the column changes) |
| `app/email/__init__.py` | Create | Module init |
| `app/email/provider.py` | Create | `EmailProvider` ABC |
| `app/email/stub_provider.py` | Create | Logs only, dev default |
| `app/email/postmark_provider.py` | Create | `PostmarkProvider` with lazy SDK init |
| `app/email/service.py` | Create | `get_email_provider()` env-gated singleton |
| `app/email/templates.py` | Create | Estonian `password_reset` + `password_reset_admin` |
| `app/auth/password.py` | Replace | Donor file is a strict superset of main's (identical `validate_password` and `change_password` signatures, plus `hash_password`, `hash_token`, `hash_email`, `issue_reset_token`, `claim_reset_token`). Wholesale replace is safer than hand-merging |
| `app/auth/middleware.py` | Modify | Add `/auth/forgot` and `/auth/reset/.*` to `SKIP_PATHS` |
| `app/auth/routes.py` | Modify | Add forgot/reset GET+POST handlers; login form gets "Unustasid parooli?" link |
| `app/auth/users.py` | Modify | Drop local `_hash_password` + `validate_password`; import from `app.auth.password`; add `_admin_reset_page` + 6 handlers + 6 route registrations + table button |
| `pyproject.toml` | Modify | Add `postmarker>=1.0` |
| `tests/test_email_provider.py` | Create | Port from donor `tests/test_email_provider.py` |
| `tests/test_admin_password_reset.py` | Create | Port from donor `tests/test_admin_password_reset.py` (donor uses inline `_client_as` helper, no `admin_session` fixture) |
| `tests/test_auth_forgot_routes.py` | Create | Port from donor `tests/test_auth_forgot_routes.py` |
| `tests/test_auth_reset_routes.py` | Create | Port from donor `tests/test_auth_reset_routes.py` (separate from forgot routes) |
| `tests/test_reset_tokens.py` | Create | Extract token-helper integration tests from donor `tests/test_password_change.py` (main's existing `test_password_change.py` is mock-based and stays) |
| `tests/test_password_change.py` | Keep | Main's mock-based `change_password` tests already cover that core; do not overwrite |
| `tests/test_password_ui_smoke.py` | Create | NEW regression: render `_user_table()` and `_login_form()` and assert the entry-point HTML strings are present (pure unit test, no DB, no fixture machinery) |
| `README.md` | Modify | Add Postmark env-var section (port from donor `README.md` — `POSTMARK_API_TOKEN`, `EMAIL_FROM`) |
| `docs/2026-04-30-password-management-prevention.md` | Create | Document the future-development guardrails (answers user's second question) |

The donor branch is checked out at `.worktrees/password-mgmt/`. Read source files directly from that path with `Read` or `cat .worktrees/password-mgmt/<path>`.

---

## Task 1: Branch + pre-flight verification

**Files:** none (env setup only)

- [ ] **Step 1: Create the working branch off `main`**

```bash
cd /Users/henrikaavik/progemoge/Seadusloome
git switch -c restore/password-management main
```

- [ ] **Step 2: Confirm baseline state**

```bash
test -f migrations/024_must_change_password.sql && echo "OK: 024 present"
test -f app/auth/password.py && grep -q "def change_password" app/auth/password.py && echo "OK: change_password core present"
test -f app/auth/profile.py && grep -q "/profile/password" app/auth/profile.py && echo "OK: /profile/password route present"
test ! -d app/email && echo "OK: app/email missing as expected"
grep -q "postmarker" pyproject.toml || echo "OK: postmarker missing as expected"
```

Expected: all five lines print `OK:`.

- [ ] **Step 3: Confirm donor worktree is on the right commit**

```bash
git -C .worktrees/password-mgmt log -1 --oneline
```

Expected: `f3c8148 docs: env vars for Postmark password-reset email`. If not, abort and tell the user.

- [ ] **Step 4: Run baseline test suite to capture starting state**

```bash
uv run pytest -x --tb=short 2>&1 | tail -20
```

Expected: PASS. Record the count (e.g. `1783 passed`). This is the floor we must not regress below.

- [ ] **Step 5: Commit checkpoint** (no changes yet, but record the branch creation in reflog only)

No commit; proceed.

---

## Task 2: Add migration 025 for the reset-token tables

**Files:**
- Create: `migrations/025_password_reset_tables.sql`
- Test: `tests/test_migration_025.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_migration_025.py
"""Migration 025 creates password_reset_tokens + password_reset_attempts tables."""
from __future__ import annotations

import os

import pytest

from app.db import get_connection


@pytest.mark.integration
def test_password_reset_tokens_table_exists():
    if not os.getenv("DATABASE_URL"):
        pytest.skip("integration test — DATABASE_URL not set")
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'password_reset_tokens' ORDER BY column_name"
        )
        cols = {r[0] for r in cur.fetchall()}
    assert {"id", "user_id", "token_hash", "expires_at", "used_at", "created_by", "created_at"} <= cols


@pytest.mark.integration
def test_password_reset_attempts_table_exists():
    if not os.getenv("DATABASE_URL"):
        pytest.skip("integration test — DATABASE_URL not set")
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'password_reset_attempts' ORDER BY column_name"
        )
        cols = {r[0] for r in cur.fetchall()}
    assert {"id", "email_hash", "ip", "attempted_at"} <= cols
```

- [ ] **Step 2: Run test, verify it fails**

```bash
uv run pytest tests/test_migration_025.py -v
```

Expected: FAIL or SKIP (skip if no `DATABASE_URL` — that is fine; the assertion is what matters once the migration is applied).

- [ ] **Step 3: Create migration file**

```sql
-- migrations/025_password_reset_tables.sql
--
-- Adds the two tables required by the self-service forgot-password flow
-- and admin-initiated reset (email link path). Migration 024 already
-- added the user-level columns (`must_change_password`,
-- `password_changed_at`); this migration covers what was deferred to a
-- second step on `feature/password-management` (see plan
-- 2026-04-28-password-management.md, originally migration 024 on that
-- branch).

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
```

- [ ] **Step 4: Apply the migration locally and re-run the test**

```bash
uv run python scripts/migrate.py
uv run pytest tests/test_migration_025.py -v
```

Expected: PASS (or SKIP if no integration DB; if SKIP, mark this task done — CI will exercise it).

- [ ] **Step 5: Commit**

```bash
git add migrations/025_password_reset_tables.sql tests/test_migration_025.py
git commit -m "feat(db): migration 025 — password_reset_tokens and password_reset_attempts

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Replace `app/auth/password.py` with donor superset; add reset-token tests

**Files:**
- Replace: `app/auth/password.py`
- Create: `tests/test_reset_tokens.py` (token-helper integration tests; main's `tests/test_password_change.py` stays unchanged)

The donor file is a strict superset of main's:
- identical `validate_password(password, *, email=None)` signature (donor drops a `.strip()` we add back below);
- identical `change_password(user_id, new_password, *, conn, must_change=False)` signature — main's existing callers (`app/auth/profile.py:240` and `tests/test_password_change.py`) already pass `conn=conn`;
- adds `hash_password`, `hash_token`, `hash_email`, `issue_reset_token(*, user_id, created_by, conn, ttl=...)`, `claim_reset_token(raw_token, *, conn) -> tuple[user_id, created_by] | None`.

**Important signature notes (these were wrong in v1 of this plan):**
- `issue_reset_token` is keyword-only and takes `conn=conn`. It does NOT open its own connection.
- `claim_reset_token` takes `conn=conn` and returns a `(user_id, created_by)` tuple, not just `user_id`.
- `hash_email` is unsalted (`sha256(email.strip().lower())`); there is no `PASSWORD_RESET_EMAIL_SALT` env var.

Main's existing `tests/test_password_change.py` is mock-based and only tests `change_password`. We keep it. The donor's `tests/test_password_change.py` adds 5 integration tests for `issue_reset_token` + `claim_reset_token` (lines ~83–131 of the donor file). We extract those into a new `tests/test_reset_tokens.py` so the mock-based tests stay fast.

- [ ] **Step 1: Read the donor file (sanity)**

```bash
git -C .worktrees/password-mgmt show feature/password-management:app/auth/password.py | head -25
git -C .worktrees/password-mgmt show feature/password-management:app/auth/password.py | grep -n "^def "
```

Expected output: `validate_password`, `hash_password`, `change_password`, `hash_token`, `hash_email`, `issue_reset_token`, `claim_reset_token`.

- [ ] **Step 2: Replace main's `app/auth/password.py` with the donor file**

```bash
git -C .worktrees/password-mgmt show feature/password-management:app/auth/password.py > app/auth/password.py
```

- [ ] **Step 3: Re-add the `.strip()` defensive cleanup main had**

In the new `app/auth/password.py`, find `validate_password`. Donor has:

```python
        local_part = email.split("@", 1)[0].lower()
```

Replace with main's slightly more defensive form:

```python
        local_part = email.split("@", 1)[0].strip().lower()
```

This is a minor delta but the only behavioural change between main and donor for `validate_password`; preserve it.

- [ ] **Step 4: Confirm pre-existing main tests still pass against donor file**

```bash
uv run pytest tests/test_password_change.py tests/test_password_validation.py -v
```

Expected: all PASS. The mock-based change_password tests in main expect the same SQL shape donor produces.

- [ ] **Step 5: Create the new reset-token tests file**

Extract the 5 integration tests from the donor's `tests/test_password_change.py` into a new file `tests/test_reset_tokens.py`:

```bash
git -C .worktrees/password-mgmt show feature/password-management:tests/test_password_change.py > /tmp/donor_test_password_change.py
```

Then create `tests/test_reset_tokens.py` with:

```python
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
        conn.execute(
            "DELETE FROM password_reset_tokens WHERE user_id = %s", (user_id,)
        )
        conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.commit()


@pytest.mark.integration
def test_issue_reset_token_invalidates_prior_unused(temp_user):
    from app.auth.password import claim_reset_token, issue_reset_token

    if not os.getenv("DATABASE_URL"):
        pytest.skip("integration test")
    with _connect() as conn:
        first = issue_reset_token(user_id=temp_user["id"], created_by=None, conn=conn)
        second = issue_reset_token(user_id=temp_user["id"], created_by=None, conn=conn)
        conn.commit()
        assert claim_reset_token(first, conn=conn) is None
        claimed = claim_reset_token(second, conn=conn)
        conn.commit()
    assert claimed is not None
    assert claimed[0] == temp_user["id"]


@pytest.mark.integration
def test_claim_reset_token_is_single_use(temp_user):
    from app.auth.password import claim_reset_token, issue_reset_token

    if not os.getenv("DATABASE_URL"):
        pytest.skip("integration test")
    with _connect() as conn:
        raw = issue_reset_token(user_id=temp_user["id"], created_by=None, conn=conn)
        conn.commit()
        first = claim_reset_token(raw, conn=conn)
        conn.commit()
        second = claim_reset_token(raw, conn=conn)
    assert first is not None
    assert second is None


@pytest.mark.integration
def test_claim_reset_token_rejects_expired(temp_user):
    from app.auth.password import claim_reset_token, issue_reset_token

    if not os.getenv("DATABASE_URL"):
        pytest.skip("integration test")
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


@pytest.mark.integration
def test_claim_reset_token_concurrent_only_one_wins(temp_user):
    from app.auth.password import claim_reset_token, issue_reset_token

    if not os.getenv("DATABASE_URL"):
        pytest.skip("integration test")
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

- [ ] **Step 6: Run the new tests**

```bash
uv run pytest tests/test_reset_tokens.py -v
```

Expected: PASS (or SKIP if no `DATABASE_URL`; that is fine for unit-only runs — CI hits a real DB).

- [ ] **Step 7: Commit**

```bash
git add app/auth/password.py tests/test_reset_tokens.py
git commit -m "feat(auth): add token-helper superset to password.py + integration tests

Donor app/auth/password.py is a strict superset (same validate_password
and change_password signatures plus hash_password, hash_token,
hash_email, issue_reset_token, claim_reset_token). Wholesale replace
keeps the diff small and avoids hand-merge errors.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Port the `app/email/` module

**Files:**
- Create: `app/email/__init__.py`, `provider.py`, `stub_provider.py`, `postmark_provider.py`, `service.py`, `templates.py`
- Test: `tests/test_email_provider.py`

The donor module is already correct and has its own tests; copy as-is.

- [ ] **Step 1: Copy the entire module from donor**

```bash
mkdir -p app/email
cp .worktrees/password-mgmt/app/email/__init__.py        app/email/__init__.py
cp .worktrees/password-mgmt/app/email/provider.py        app/email/provider.py
cp .worktrees/password-mgmt/app/email/stub_provider.py   app/email/stub_provider.py
cp .worktrees/password-mgmt/app/email/postmark_provider.py app/email/postmark_provider.py
cp .worktrees/password-mgmt/app/email/service.py         app/email/service.py
cp .worktrees/password-mgmt/app/email/templates.py       app/email/templates.py
```

- [ ] **Step 2: Copy the donor tests**

```bash
cp .worktrees/password-mgmt/tests/test_email_provider.py tests/test_email_provider.py
```

- [ ] **Step 3: Add `postmarker` to `pyproject.toml`**

Open `pyproject.toml`, find the `[project] dependencies = [...]` array, and add inside the list (alphabetically sorted if the file is sorted; otherwise next to `bleach`):

```toml
    "postmarker>=1.0",
```

Then refresh the lock:

```bash
uv lock
uv sync
```

- [ ] **Step 4: Run the email tests**

```bash
uv run pytest tests/test_email_provider.py -v
```

Expected: PASS. If failing because the test imports something the donor renamed, read the donor test file and adjust imports.

- [ ] **Step 5: Run ruff + pyright on new module**

```bash
uv run ruff check app/email tests/test_email_provider.py
uv run pyright app/email
```

Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add app/email tests/test_email_provider.py pyproject.toml uv.lock
git commit -m "feat(email): port EmailProvider ABC, Stub, Postmark, service gate, Estonian templates

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Allow forgot/reset paths in auth middleware `SKIP_PATHS`

**Files:**
- Modify: `app/auth/middleware.py`
- Test: `tests/test_auth_skip_paths.py` (extend if exists, else create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_auth_skip_paths.py
"""SKIP_PATHS lets unauthenticated users reach forgot + reset pages."""
from __future__ import annotations

import re

from app.auth.middleware import SKIP_PATHS


def test_forgot_in_skip_paths():
    assert any(re.fullmatch(p, "/auth/forgot") for p in SKIP_PATHS)


def test_reset_with_token_in_skip_paths():
    assert any(re.fullmatch(p, "/auth/reset/abc123") for p in SKIP_PATHS)
```

- [ ] **Step 2: Run, verify failure**

```bash
uv run pytest tests/test_auth_skip_paths.py -v
```

Expected: FAIL.

- [ ] **Step 3: Update `app/auth/middleware.py`**

Find the `SKIP_PATHS: list[str] = [` block. Add two entries (alongside `/auth/login`):

```python
    r"/auth/forgot",
    r"/auth/reset/.*",
```

- [ ] **Step 4: Run, verify pass**

```bash
uv run pytest tests/test_auth_skip_paths.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/auth/middleware.py tests/test_auth_skip_paths.py
git commit -m "feat(auth): allow /auth/forgot and /auth/reset/<token> through SKIP_PATHS

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: `/auth/forgot` and `/auth/reset/<token>` handlers + login forgot link

**Files:**
- Modify: `app/auth/routes.py`
- Test: `tests/test_auth_forgot_routes.py`, `tests/test_auth_reset_routes.py`

- [ ] **Step 1: Port both donor test files**

```bash
git -C .worktrees/password-mgmt show feature/password-management:tests/test_auth_forgot_routes.py > tests/test_auth_forgot_routes.py
git -C .worktrees/password-mgmt show feature/password-management:tests/test_auth_reset_routes.py > tests/test_auth_reset_routes.py
```

- [ ] **Step 2: Run, verify both fail**

```bash
uv run pytest tests/test_auth_forgot_routes.py tests/test_auth_reset_routes.py -v
```

Expected: FAIL with 404 on `/auth/forgot` and `/auth/reset/<token>`, or import error.

- [ ] **Step 3: Read donor `app/auth/routes.py` to see the exact handlers**

```bash
git -C .worktrees/password-mgmt show feature/password-management:app/auth/routes.py | sed -n '1,400p'
```

- [ ] **Step 4: Add the full top-of-file import block to main's `app/auth/routes.py`**

Locate the existing imports at the top of `app/auth/routes.py`. Replace the existing `from app.auth.password import validate_password` (or whatever main currently has) with the full donor import block. The complete set the new handlers need:

```python
import logging
import os

from app.auth.audit import log_action
from app.auth.password import (
    change_password,
    claim_reset_token,
    hash_email,
    hash_token,
    issue_reset_token,
    validate_password,
)
from app.db import get_connection
from app.email.service import get_email_provider
from app.email.templates import password_reset
```

Plus, after the existing imports, add (donor file does this near line 34):

```python
logger = logging.getLogger(__name__)

EMAIL_RATE_LIMIT_PER_HOUR = 3
IP_RATE_LIMIT_PER_HOUR = 10
```

Without `logging` / `os` / `log_action` / `hash_token` / `get_connection`, the donor handler bodies (which reference all of these) will raise `NameError` at import time.

- [ ] **Step 5: Port the forgot+reset handlers into main's `app/auth/routes.py`**

Locate main's `register_auth_routes(rt)` function. *Before* it, copy verbatim from the donor:
- `_forgot_form()` builder
- `_forgot_sent_page()` builder
- `_reset_form()` builder
- `forgot_page()` GET handler
- `forgot_post()` POST handler (rate limit + atomic create + Postmark send)
- `reset_page()` GET handler
- `reset_post()` POST handler (atomic claim + change_password + cookie clear)

Then inside `register_auth_routes`, add:

```python
    rt("/auth/forgot", methods=["GET"])(forgot_page)
    rt("/auth/forgot", methods=["POST"])(forgot_post)
    rt("/auth/reset/{token}", methods=["GET"])(reset_page)
    rt("/auth/reset/{token}", methods=["POST"])(reset_post)
```

Copy the *exact* handler bodies from the donor — do not paraphrase. The atomic-claim, rate-limit, and cookie-clearing logic is load-bearing.

- [ ] **Step 6: Add the "Unustasid parooli?" link to `_login_form`**

In `app/auth/routes.py`, locate `_login_form(...)`. After the password `FormField(...)` and **before** `Button("Logi sisse", ...)`, insert:

```python
                P(A("Unustasid parooli?", href="/auth/forgot"), cls="forgot-link"),
```

- [ ] **Step 7: Run both test files**

```bash
uv run pytest tests/test_auth_forgot_routes.py tests/test_auth_reset_routes.py -v
```

Expected: PASS. If a test fails because the donor's user-fixture differs from main's, the donor tests use inline DB-only fixtures (no `conftest.py` machinery) — open the test file and confirm the fixture creates rows directly with `psycopg.connect(DATABASE_URL)`. If `DATABASE_URL` is unset locally, the tests should `skip` cleanly; CI hits a real DB.

- [ ] **Step 8: Commit**

```bash
git add app/auth/routes.py tests/test_auth_forgot_routes.py tests/test_auth_reset_routes.py
git commit -m "feat(auth): /auth/forgot and /auth/reset/<token> routes + login forgot link

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Admin reset handlers + UI button + route registrations

**Files:**
- Modify: `app/auth/users.py`
- Test: `tests/test_admin_password_reset.py`

- [ ] **Step 1: Port the donor test file**

```bash
git -C .worktrees/password-mgmt show feature/password-management:tests/test_admin_password_reset.py > tests/test_admin_password_reset.py
```

- [ ] **Step 2: Run, verify failure**

```bash
uv run pytest tests/test_admin_password_reset.py -v
```

Expected: FAIL — handlers and routes do not exist.

- [ ] **Step 3: Read the donor handlers**

```bash
git -C .worktrees/password-mgmt show feature/password-management:app/auth/users.py | sed -n '750,1000p'
```

- [ ] **Step 4: Reorganise main's `app/auth/users.py` imports + drop local helpers**

Current main `app/auth/users.py` defines its own `_hash_password()` and `validate_password()` *locally* (lines 38–51). It does NOT have `from app.auth.password import validate_password`. We:

(a) Add `import os` to the stdlib block at the top.

(b) Add the new third-party-ish imports to the existing import block (alongside `from app.auth.audit import log_action` etc.):

```python
from app.auth.password import (
    change_password,
    hash_password,
    issue_reset_token,
    validate_password,  # noqa: F401 — re-export for callers that import it from here
)
from app.email.service import get_email_provider
from app.email.templates import password_reset_admin
```

(c) Delete the now-redundant local definitions of `_hash_password` (line 38) and `validate_password` (line 43) from `app/auth/users.py`. They are strictly subsumed by `app.auth.password` (which has the same `validate_password` plus an email-substring rule, and an equivalent `hash_password`).

(d) Find every internal caller of `_hash_password(...)` inside `app/auth/users.py` and rename it to `hash_password(...)` to match the new import:

```bash
grep -n "_hash_password" app/auth/users.py
```

For each hit, replace the leading underscore call. Expected callers: `create_user`, `org_user_create`, `admin_user_create`. Verify with:

```bash
grep -n "_hash_password\|hash_password" app/auth/users.py
```

After the edit, only `hash_password` (no leading underscore) should remain.

- [ ] **Step 5: Add the "Lähtesta parool" button to `_user_table._actions_cell`**

In main's `app/auth/users.py`, find `_actions_cell` (around line 258). Right after the `actions: list = [A("Muuda rolli", ...)]` block, **before** the deactivate `if row["_is_active"]` block, insert:

```python
        # Reset password link — gated by active status and (for org admin) role.
        show_reset = base_path == "/admin/users" or (
            base_path == "/org/users" and row["role"] in ORG_ASSIGNABLE_ROLES
        )
        if row["_is_active"] and show_reset:
            actions.append(
                A(
                    "Lähtesta parool",
                    href=f"{base_path}/{row['id']}/reset",
                    cls="btn btn-secondary btn-sm",
                )
            )
```

- [ ] **Step 6: Append the donor handlers at end of `app/auth/users.py`** (before `register_user_routes`)

Copy these six public handlers verbatim from the donor (between donor lines ~754–950):
- `_admin_reset_page(req, user_id, *, base_path, active_nav)` — shared GET page
- `_admin_reset_email(req, user_id, *, base_path, active_nav)` — POST: issue token + send email
- `_admin_reset_temp(req, user_id, *, new_password, base_path, active_nav)` — POST: set temp pw + flag must_change
- `admin_user_reset_page` / `admin_user_reset_email` / `admin_user_reset_temp` — system-admin wrappers
- `org_user_reset_page` / `org_user_reset_email` / `org_user_reset_temp` — org-admin wrappers

Each public wrapper is `require_role(...)`-decorated; copy the decorators too.

- [ ] **Step 7: Update `register_user_routes(rt)`**

Inside the function, add:

```python
    # Admin password reset routes
    rt("/admin/users/{user_id}/reset", methods=["GET"])(_admin_user_reset_page)
    rt("/admin/users/{user_id}/reset_email", methods=["POST"])(_admin_user_reset_email)
    rt("/admin/users/{user_id}/reset_temp", methods=["POST"])(_admin_user_reset_temp)
    rt("/org/users/{user_id}/reset", methods=["GET"])(_org_user_reset_page)
    rt("/org/users/{user_id}/reset_email", methods=["POST"])(_org_user_reset_email)
    rt("/org/users/{user_id}/reset_temp", methods=["POST"])(_org_user_reset_temp)
```

The wrapper-name prefix `_admin_user_reset_*` / `_org_user_reset_*` mirrors how main already exposes private names with leading underscores (e.g. `_admin_user_list = require_role("admin")(admin_user_list)`). If the donor uses a different prefix, follow the donor verbatim.

- [ ] **Step 8: Run admin reset tests**

```bash
uv run pytest tests/test_admin_password_reset.py -v
```

Expected: PASS.

- [ ] **Step 9: Lint**

```bash
uv run ruff check app/auth/users.py
uv run pyright app/auth/users.py
```

Expected: clean.

- [ ] **Step 10: Commit**

```bash
git add app/auth/users.py tests/test_admin_password_reset.py
git commit -m "feat(admin): admin reset password — email link + temp password fallback (system + org)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: NEW — UI smoke regression tests (pure unit, no DB, no auth fixture)

**Files:**
- Create: `tests/test_password_ui_smoke.py`

These tests are the *prevention layer*. They render the two FT builders directly (`_login_form()` and `_user_table()`) and assert the entry-point HTML strings are present. Pure unit-level: no DB, no `TestClient`, no `admin_session` fixture (neither main nor donor has one — the existing auth tests construct sessions inline via `JWTAuthProvider` + `TestClient.cookies.set`).

If a future "UI follow-ups" commit drops the forgot link or reset button, these tests fail loudly with no environment dependencies.

- [ ] **Step 1: Confirm the two builders are importable**

```bash
grep -n "^def _login_form\|^def _user_table" app/auth/routes.py app/auth/users.py
```

Expected: `_login_form` in `routes.py`, `_user_table` in `users.py`. (If they're nested or renamed, adjust the imports below to match.)

- [ ] **Step 2: Write the tests**

```python
# tests/test_password_ui_smoke.py
"""Regression: the password-flow UI entry points must remain wired.

Background: in 2026-04 the admin reset button and login forgot link were
silently dropped from main during a "UI review follow-ups" sweep. These
smoke tests fail loudly if anyone removes them again. They render the FT
builders directly (no DB, no TestClient, no auth fixture) so the only
thing they can fail for is a missing string in the rendered output —
which is exactly the regression we're guarding against.
"""

from __future__ import annotations

from fasthtml.common import to_xml

from app.auth.routes import _login_form
from app.auth.users import _user_table


def test_login_form_has_forgot_link():
    """`_login_form()` must render an anchor to /auth/forgot with the
    Estonian label "Unustasid parooli?". If this fails, restore the line
        P(A("Unustasid parooli?", href="/auth/forgot"), cls="forgot-link"),
    in `_login_form` (app/auth/routes.py) between the password field and
    the submit button.
    """
    html = to_xml(_login_form())
    assert 'href="/auth/forgot"' in html, "login form missing /auth/forgot anchor"
    assert "Unustasid parooli" in html, "login form missing Estonian forgot label"


def test_admin_user_table_renders_reset_button_for_active_admin_target():
    """`_user_table` with base_path='/admin/users' must render
    'Lähtesta parool' for an active user row. If this fails, restore the
    `actions.append(A("Lähtesta parool", ...))` block in `_user_table.
    _actions_cell` (app/auth/users.py)."""
    users = [
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "full_name": "Test Drafter",
            "email": "drafter@example.com",
            "org_name": "Acme",
            "role": "drafter",
            "is_active": True,
        }
    ]
    html = to_xml(_user_table(users, show_org=True, base_path="/admin/users"))
    assert "Lähtesta parool" in html, "admin user table missing reset action label"
    assert "/admin/users/00000000-0000-0000-0000-000000000001/reset" in html, (
        "admin user table missing reset action href"
    )


def test_org_user_table_renders_reset_button_for_assignable_role():
    """`_user_table` with base_path='/org/users' must show the reset
    button for an active drafter (a role in ORG_ASSIGNABLE_ROLES)."""
    users = [
        {
            "id": "00000000-0000-0000-0000-000000000002",
            "full_name": "Org Drafter",
            "email": "org-drafter@example.com",
            "org_name": "Acme",
            "role": "drafter",
            "is_active": True,
        }
    ]
    html = to_xml(_user_table(users, show_org=False, base_path="/org/users"))
    assert "Lähtesta parool" in html
    assert "/org/users/00000000-0000-0000-0000-000000000002/reset" in html


def test_org_user_table_omits_reset_button_for_admin_target():
    """Org admin must NOT be able to reset another admin / org_admin."""
    users = [
        {
            "id": "00000000-0000-0000-0000-000000000003",
            "full_name": "Other Org Admin",
            "email": "other-org-admin@example.com",
            "org_name": "Acme",
            "role": "org_admin",
            "is_active": True,
        }
    ]
    html = to_xml(_user_table(users, show_org=False, base_path="/org/users"))
    assert "Lähtesta parool" not in html, (
        "org admin must not see reset button for another org_admin row"
    )


def test_user_table_omits_reset_button_for_inactive_user():
    """Inactive users must not show the reset button (covered by
    `if row['_is_active'] and show_reset:` guard)."""
    users = [
        {
            "id": "00000000-0000-0000-0000-000000000004",
            "full_name": "Inactive Drafter",
            "email": "inactive@example.com",
            "org_name": "Acme",
            "role": "drafter",
            "is_active": False,
        }
    ]
    html = to_xml(_user_table(users, show_org=True, base_path="/admin/users"))
    assert "Lähtesta parool" not in html
```

- [ ] **Step 3: Run the smoke tests, verify they pass**

```bash
uv run pytest tests/test_password_ui_smoke.py -v
```

Expected: 5 PASS. If any test fails because `_user_table` expects a different row-dict shape (e.g. uses `_is_active` instead of `is_active`), inspect the function and adjust the test fixtures — the *test* must mirror what the production code expects. Do not change production code to fit the test.

- [ ] **Step 4: Commit**

```bash
git add tests/test_password_ui_smoke.py
git commit -m "test(ui): regression smoke for password-flow entry points (login forgot, admin reset)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Update README env-var section + add prevention doc

**Files:**
- Modify: `README.md`
- Create: `docs/2026-04-30-password-management-prevention.md`

- [ ] **Step 1: Port the README env-var changes** from `f3c8148`

```bash
git -C .worktrees/password-mgmt show feature/password-management:README.md | diff - README.md | head -80
```

Apply the same additions to main's `README.md`. The donor's actual env-var names (verified against `app/email/service.py`):

- `POSTMARK_API_TOKEN` — required in production; the `get_email_provider()` gate raises `RuntimeError` at startup if `APP_ENV=production` and this is missing.
- `EMAIL_FROM` — optional; default `Seadusloome <noreply@sixtyfour.ee>`.
- `APP_BASE_URL` — used in reset-link templates so the email contains `https://seadusloome.sixtyfour.ee/auth/reset/<token>`.

Do NOT add `POSTMARK_SERVER_TOKEN`, `EMAIL_FROM_ADDRESS`, or `PASSWORD_RESET_EMAIL_SALT` — those names do not exist in the donor code. Use `Edit` not full `Write`.

- [ ] **Step 2: Write the prevention doc**

```markdown
# 2026-04-30 — Preventing password-management from disappearing again

## What happened

The password-management feature was developed on `feature/password-management`
(15 commits, complete with tests). It was never merged into `main`. Instead,
during the 2026-04-29 UI review follow-ups (commit `7c786ce`) a *partial*
reimplementation was added directly to `main`: just `validate_password`,
`change_password`, `/profile/password`, the must_change_password middleware
redirect, and migration `024_must_change_password.sql`.

Six weeks later it surfaced as "I wanted admin password reset, where is it?" —
because the visible entry points (login forgot link, admin reset button) were
on the orphaned feature branch.

## Why a UI smoke test is the most cost-effective guardrail

The test suite at the time had thorough unit coverage of `change_password`,
`issue_reset_token`, `claim_reset_token`, the rate limiter, and the email
provider — all on the feature branch. None of it ran on `main` because the
*entry points* into those flows were missing. A reviewer reading a 89-file
"UI follow-ups" diff cannot reasonably be expected to notice that one button
is gone. A regression test can.

## The rules going forward

1. **Every user-visible feature has a UI smoke test** that fetches a real
   page and asserts the entry-point HTML is present. See
   `tests/test_password_ui_smoke.py` for the canonical pattern.

2. **Long-lived feature branches must be merged via PR, not cherry-picked
   piecemeal.** If `feature/password-management` had been merged via a single
   `git merge` then `7c786ce` would have been a *sibling* commit and the
   conflict would have surfaced visibly.

3. **"Follow-ups" / "review fixes" / "polish" commits that touch >20 files
   require a code-review agent pass.** The 7c786ce commit message lists 12
   intentional changes; any other change in that diff is suspect by default.

4. **Maintain `docs/feature-inventory.md` (TODO).** A one-line-per-flow list
   of "Feature → Entry-point file:line → smoke test" makes orphaned features
   findable by `grep` rather than by user complaints.
```

- [ ] **Step 3: Commit**

```bash
git add README.md docs/2026-04-30-password-management-prevention.md
git commit -m "docs: Postmark env vars + prevention doc for orphaned features

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: Full verification

**Files:** none

- [ ] **Step 1: Full ruff**

```bash
uv run ruff check .
uv run ruff format --check .
```

Expected: clean. Fix anything flagged.

- [ ] **Step 2: Full pyright**

```bash
uv run pyright
```

Expected: clean. Fix anything flagged.

- [ ] **Step 3: Full pytest**

```bash
uv run pytest --tb=short
```

Expected: pre-task baseline + new tests, all PASS. Record the new count in the commit message of Task 11.

- [ ] **Step 4: Manual smoke (browser, optional but recommended)**

```bash
uv run python -m app.main &
APP_PID=$!
open http://127.0.0.1:5001/auth/login
# verify: "Unustasid parooli?" link visible below password field
# click it -> /auth/forgot
# submit a known email -> stub provider logs the email body
# log in as admin -> /admin/users -> "Lähtesta parool" button visible per active row
# click it -> /admin/users/<id>/reset -> two options (email link + temp password)
kill $APP_PID
```

- [ ] **Step 5: If anything failed, fix in place and re-run.**

---

## Task 11: Open the PR

**Files:** none (git/gh)

- [ ] **Step 1: Push the branch**

```bash
git push -u origin restore/password-management
```

- [ ] **Step 2: Create the PR**

```bash
gh pr create --title "Restore password-management UI + email module on main" --body "$(cat <<'EOF'
## Summary
- Surgical port of the never-merged `feature/password-management` branch onto main
- Adds `app/email/` module (provider ABC + Postmark + Stub + service gate + Estonian templates)
- Adds `/auth/forgot` and `/auth/reset/<token>` self-service flow
- Adds admin-initiated reset (system + org) with email-link + temp-password options
- Adds login "Unustasid parooli?" link and admin user-row "Lähtesta parool" button
- Adds migration 025 with `password_reset_tokens` + `password_reset_attempts` tables
- Adds 3 UI smoke tests as a regression guard against silent re-deletion

## Why not `git merge feature/password-management`?
The feature branch diverged at `5b4f483` and is missing 3 main-side commits
including the 89-file `7c786ce` ("UI review 2026-04-29 follow-ups"). A
straight merge produces overlapping conflicts in 15+ files unrelated to
passwords. Surgical port keeps the diff focused and reviewable.

## Test plan
- [ ] CI: ruff + pyright + pytest all green
- [ ] Manual: forgot flow on staging produces a Postmark email
- [ ] Manual: admin-initiated email reset on staging
- [ ] Manual: admin-initiated temp-password fallback on staging
- [ ] Manual: org admin can reset drafter/reviewer but not admin/org_admin

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 2: Return the PR URL** to the user.

---

## Self-Review

- **Spec coverage:** Every donor commit `d08e82a..f3c8148` is mapped to a task. Migration 025 (additive) covers the schema gap left by main's smaller 024.
- **Placeholder scan:** No "TBD"/"appropriate"/"similar to". Each step shows the actual code to add or the exact donor read command.
- **Type consistency (v2 — fixed during pre-execution review):**
  - `issue_reset_token(*, user_id, created_by, conn, ttl=...)` — keyword-only, takes `conn`, returns raw token string. (v1 had a hand-rolled implementation that opened its own connection and did not match donor callers.)
  - `claim_reset_token(raw_token, *, conn) -> tuple[str, str | None] | None` — returns `(user_id, created_by)`, not just `user_id`. (v1 returned `str | None`.)
  - `hash_email(email)` is unsalted: `sha256(email.strip().lower())`. There is no `PASSWORD_RESET_EMAIL_SALT` env var. (v1 invented one.)
- **File-path consistency (v2 — fixed):**
  - Token tests live in `tests/test_password_change.py` on the donor (integration parts). Main's mock-based `test_password_change.py` stays. New file: `tests/test_reset_tokens.py` (extracted integration tests).
  - Reset-route tests are in `tests/test_auth_reset_routes.py` (separate from `test_auth_forgot_routes.py`). Both ported.
  - Migration runner is `scripts/migrate.py`, not `scripts/run_migrations.py`.
  - Env vars: `POSTMARK_API_TOKEN`, `EMAIL_FROM` (defaults `Seadusloome <noreply@sixtyfour.ee>`), `APP_BASE_URL`. Not `POSTMARK_SERVER_TOKEN` / `EMAIL_FROM_ADDRESS`.
- **Import completeness (v2 — fixed):** `app/auth/routes.py` needs `import logging`, `import os`, `from app.auth.audit import log_action`, `from app.db import get_connection`, plus `hash_token` in the `app.auth.password` import. Without these the donor handler bodies raise `NameError` at import.
- **Local-helper removal (v2 — fixed):** Current main `app/auth/users.py` defines its own `_hash_password()` and `validate_password()` *locally*. Task 7 deletes both and switches callers to `app.auth.password.hash_password` / `validate_password`.
- **Smoke-test approach (v2 — fixed):** No `admin_session` / `org_admin_session` fixtures exist on either branch. Task 8 renders `_user_table()` and `_login_form()` directly via `to_xml(...)` — pure unit, no DB, no auth.
- **Risk:** Task 7 step 6 instructs to copy six handler bodies "verbatim". If the donor handler bodies depend on `app/auth/users.py` private helpers that *also* changed between main and feature branch, a runtime error will surface in Task 7 step 8. Mitigation: the test from `tests/test_admin_password_reset.py` exercises the full flow; if it passes, the dependencies are satisfied.
- **Untested locally:** Migration 025, integration token tests, and admin-reset tests all require a live Postgres. Manual `psql` smoke recommended before merging to staging.
