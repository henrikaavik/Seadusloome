# Password Management — Design

**Status:** Draft (awaiting user review)
**Author:** Henrik Aavik
**Date:** 2026-04-28
**Scope:** Self-service forgot password, in-account password change, and admin-initiated password reset.

---

## 1. Goal

Add three flows that let users and admins manage account passwords:

1. **Forgot password (self-service):** Public `/auth/forgot` flow that emails the user a one-time reset link.
2. **Change password (logged-in):** Authenticated users can change their own password via `/profile/password`.
3. **Admin reset:** System and org admins can reset other users' passwords either by sending a reset email (primary) or setting a temporary password (fallback for users without email access).

All three flows share a common `change_password()` core that bumps `users.token_version` (kills access tokens) and deletes the user's `sessions` rows (kills refresh tokens) so every active login is revoked at the moment the password changes.

## 2. Non-goals

- MFA / TOTP / WebAuthn — out of scope for this spec.
- Password complexity beyond the existing rule + email-substring forbidance.
- Email change (only password change here).
- Password history / reuse prevention.
- Breach-list password screening (e.g., HIBP API).
- TARA / OIDC SSO password reset (TARA users don't have a local password).

## 3. Constraints & context

- **Email infrastructure:** Project did not previously send transactional email. Postmark account configured during this design phase: server "Seadusloome" (ID `19043028`, Live mode); `From: noreply@sixtyfour.ee`; DKIM + Return-Path DNS records added at Hostinger DNS for `sixtyfour.ee`. Domain verification pending DNS propagation (auto-rechecked by Postmark; up to 48h).
- **Stub mode:** When `is_stub_allowed()` is true (every env except `APP_ENV=production`), email sends print to stdout instead of hitting Postmark — so dev / test / CI never need a real `POSTMARK_API_TOKEN`.
- **Auth model:** JWT access tokens (1h) + DB-backed refresh tokens in `sessions` (30d). `users.token_version` (#635) lets us invalidate every outstanding access token in O(1) by bumping the counter. Bcrypt password hashes.
- **Existing rules:** `validate_password()` requires ≥8 chars, ≥1 uppercase, ≥1 digit. UI is Estonian (`et`), all error/success messages must be in Estonian.
- **Authorization rules from #634:** Org admins can mutate only their own org's `drafter`/`reviewer` users. They cannot mutate `org_admin` or `admin` rows even within the same org. The admin reset flow must respect these rules.

## 4. Architecture

### 4.1 High-level component layout

```
app/
  email/                          # NEW — provider-abstracted transactional email
    __init__.py
    provider.py                   # EmailProvider ABC (mirrors LLMProvider pattern)
    postmark_provider.py          # PostmarkProvider (HTTP API via postmarker)
    stub_provider.py              # StubProvider — prints to stdout
    service.py                    # get_email_provider() lazy singleton
    templates.py                  # Estonian password_reset + password_reset_admin
  auth/
    routes.py                     # + forgot_page, forgot_post, reset_page, reset_post
    password.py                   # NEW — shared change_password() core, validate_password()
    users.py                      # + admin_user_reset routes (system & org variants)
    profile.py                    # NEW — /profile + /profile/password routes
migrations/
  024_password_reset_tokens.sql   # NEW — new password_reset_tokens table
```

### 4.2 Email module (`app/email/`)

Uses the same provider-abstraction pattern as `app/llm/` (Claude/Voyage):

- **`EmailProvider`** ABC — `send(to: str, subject: str, html: str, text: str, message_stream: str | None = None) -> None`.
- **`PostmarkProvider`** — uses `postmarker.core.PostmarkClient`. Lazy-initialised singleton with thread-safe lock; SDK is only imported on first real send so dev environments without `postmarker` installed still work.
- **`StubProvider`** — logs the rendered subject + body to `logger.info`, returns.
- **`get_email_provider()`** — single source of truth for which provider is active. Gating rule (mirrors `app/llm/claude.py`):
  - If `is_stub_allowed()` is true (any env except `APP_ENV=production`) AND `POSTMARK_API_TOKEN` is missing → return `StubProvider`.
  - If `is_stub_allowed()` is true AND `POSTMARK_API_TOKEN` is set → return `PostmarkProvider` (lets dev/staging exercise the real path when desired).
  - If `is_stub_allowed()` is false (production) AND `POSTMARK_API_TOKEN` is missing → raise `RuntimeError` at first call so deployment fails loudly. Production never falls back to stubs.

Templates live as Python module-level functions returning `(subject, html, text)` tuples (no Jinja, matches the pattern used elsewhere in the project for short messages).

### 4.3 Database schema — `password_reset_tokens`, `password_reset_attempts`, `users.must_change_password`, `users.password_changed_at`

```sql
-- One-time reset tokens (self-service or admin-initiated)
CREATE TABLE password_reset_tokens (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash    TEXT NOT NULL UNIQUE,        -- SHA-256 of the raw token
    expires_at    TIMESTAMPTZ NOT NULL,
    used_at       TIMESTAMPTZ,                 -- NULL = unused; set on consumption
    created_by    UUID REFERENCES users(id),   -- NULL for self-service; admin's id for admin-initiated
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_pwreset_user_id ON password_reset_tokens(user_id);
CREATE INDEX idx_pwreset_expires_at ON password_reset_tokens(expires_at);
CREATE INDEX idx_pwreset_created_by ON password_reset_tokens(created_by) WHERE created_by IS NOT NULL;

-- Per-(email, IP) probe tracking for rate-limiting BEFORE user lookup,
-- so unknown emails are throttled identically to known ones (preserves
-- email enumeration resistance).
CREATE TABLE password_reset_attempts (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email_hash    TEXT NOT NULL,               -- SHA-256 of lowercased email
    ip            TEXT NOT NULL,
    attempted_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_pwreset_attempts_email_hash_time ON password_reset_attempts(email_hash, attempted_at);
CREATE INDEX idx_pwreset_attempts_ip_time ON password_reset_attempts(ip, attempted_at);

-- Forced-change flag for the admin temp-password fallback (§5.4).
ALTER TABLE users ADD COLUMN must_change_password BOOLEAN NOT NULL DEFAULT FALSE;

-- For audit / future password-rotation policies.
ALTER TABLE users ADD COLUMN password_changed_at TIMESTAMPTZ;
```

**Token rules:**
- Raw token = 64-char hex (32 random bytes, `secrets.token_hex(32)`). Stored only as SHA-256 hex — same pattern as `sessions.token_hash`.
- Single-use: claimed atomically (see §4.6).
- TTL 1 hour (`expires_at = now() + 1 hour`).
- **Prior unused tokens for the same user are invalidated when a new token is issued** (`UPDATE password_reset_tokens SET used_at = now() WHERE user_id = %s AND used_at IS NULL`). Single-current-token policy; simpler operational mental model than letting them expire on their own.
- Periodic cleanup is unnecessary at the 5-50 user scale; if needed later, a `DELETE FROM password_reset_tokens WHERE expires_at < now() - interval '7 days'` cron will do. Same for `password_reset_attempts` with a tighter window (e.g. `attempted_at < now() - interval '24 hours'`).

### 4.4 Shared password-change core (`app/auth/password.py`)

```python
def change_password(
    user_id: UUID,
    new_password: str,
    *,
    conn: Connection,
    must_change: bool = False,
) -> None:
    """Update password_hash, bump token_version, delete sessions. Atomic.

    Caller passes ``must_change=True`` only for the admin temp-password
    flow (§5.4); all other paths (self-service forgot, /profile/password,
    admin email-link) set it to FALSE so the user is not forced to
    change again.
    """
    pw_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    with conn.transaction():
        conn.execute(
            "UPDATE users SET "
            "  password_hash = %s, "
            "  token_version = token_version + 1, "
            "  must_change_password = %s, "
            "  password_changed_at = now() "
            "WHERE id = %s",
            (pw_hash, must_change, user_id),
        )
        conn.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
```

Used by all three flows. Caller is responsible for validation, audit logging, and consuming the reset token (when applicable). The caller's HTTP handler is also responsible for clearing the browser's `access_token` and `refresh_token` cookies on the redirect — see §4.7.

### 4.5 Password validation (extended)

`validate_password(password: str, *, email: str | None = None) -> str | None`:
- Existing: ≥8 chars, ≥1 uppercase, ≥1 digit.
- New: if `email` given, the local-part (before `@`) lowercased must not appear as a substring in the lowercased password. Estonian error: `"Parool ei tohi sisaldada teie e-posti aadressi"`.

### 4.6 Atomic reset-token consumption

Pre-check + later update creates a race where two concurrent submissions of the same token can both pass validation and apply two password changes. The implementation MUST claim the token in a single SQL statement:

```sql
UPDATE password_reset_tokens
   SET used_at = now()
 WHERE token_hash = %s
   AND used_at IS NULL
   AND expires_at > now()
RETURNING user_id, created_by;
```

The handler:
1. Runs the UPDATE inside the same transaction as `change_password()`.
2. If `RETURNING` produces zero rows → token is missing/expired/already used → render the invalid-token page.
3. If `RETURNING` produces a row → use the returned `user_id` to call `change_password()`, then commit.
4. Same transaction; PostgreSQL serializes concurrent UPDATEs of the same row, so only one writer claims the token.

### 4.7 Authenticated middleware skip paths and cookie clearing

The forgot/reset routes are public — the user is by definition not logged in. They MUST be added to `app/auth/middleware.py::SKIP_PATHS`:

```python
SKIP_PATHS: list[str] = [
    r"/auth/login",
    r"/auth/forgot",          # NEW
    r"/auth/reset/.*",        # NEW
    r"/static/.*",
    ...  # unchanged
]
```

Without this, an unauthenticated user clicking the reset link would be 303-redirected to `/auth/login` before the reset handler runs.

After every successful password change (all three flows), the redirect response MUST clear the browser's auth cookies via `clear_auth_cookie(resp, "access_token")` and `clear_auth_cookie(resp, "refresh_token")`. Otherwise:
- Old cookies remain in the browser.
- The next request hits `auth_before`, which finds the now-stale access token, fails `tv` check (since `change_password()` bumped `token_version`), tries silent refresh, fails (sessions deleted), redirects to `/auth/login` anyway.
- This works, but it makes the post-change UX one extra round-trip and leaves the dead refresh cookie around. Clearing them explicitly is cleaner and avoids any "stale cookie" confusion in logs.

### 4.8 `must_change_password` enforcement

`auth_before` middleware: after authenticating, if `req.scope['auth']['must_change_password']` is `True` AND `req.url.path` is not `/profile/password`, `/auth/logout`, or under `/static/.*` / `/api/health`, return a `RedirectResponse('/profile/password', 303)`.

The `must_change_password` flag is fetched in the same DB read as `token_version` / `is_active` (already happening in `JWTAuthProvider.get_current_user`). Adding it to the existing SELECT keeps middleware overhead at one indexed read per request.

`change_password(..., must_change=False)` clears the flag automatically in its UPDATE, so completing `/profile/password` releases the user.

### 4.9 CSRF posture

The project does not currently install a CSRF middleware (verified at `app/chat/handlers.py:31-36` and `app/auth/middleware.py`). Existing protection is `SameSite=Lax` on the auth cookies (verified at `app/auth/cookies.py:22`), which prevents most cross-origin cookie-bearing POSTs in modern browsers.

For the new password flows, the inherent protections per flow are:

| Flow | CSRF mitigation in addition to SameSite=Lax |
|---|---|
| `/auth/forgot` (POST) | Pre-auth, no session to hijack. Worst-case attacker triggers a reset email to a known user (annoying, but the recipient must still click the link to act). Rate-limited. |
| `/auth/reset/<token>` (POST) | The token in the URL is a per-request secret an attacker cannot guess. Functions as the CSRF token. |
| `/profile/password` (POST) | Requires `current_password` in the body — an attacker cannot supply this without already knowing it. |
| `/admin/users/{id}/reset_email` and `/reset_temp` | Most exposed. Currently relies on SameSite=Lax + admin-only authorization. |

**Decision for this spec:** these flows ship with the project's existing SameSite-Lax-only posture. A CSRF middleware (e.g. `starlette-csrf`) is a separate, codebase-wide enhancement that should be evaluated as its own work item — wiring it in only for the four new admin-reset POSTs would create an inconsistent posture and miss the rest of the admin surface (role change, deactivate, etc.) which is equally exposed. **Open question:** confirm the SameSite-only posture is acceptable before this spec is approved (see §12).

## 5. Flows

### 5.1 Self-service forgot password

```
[Login page] → click "Unustasid parooli?"
  ↓
GET  /auth/forgot                    → form (email field only)
POST /auth/forgot  (email)
  ├─ validate email format
  ├─ INSERT row into password_reset_attempts (email_hash, ip)  (always — known and unknown emails alike)
  ├─ COUNT recent rows from password_reset_attempts:
  │   ├─ if >3 within last hour for this email_hash → render generic success page (do not send)
  │   └─ if >10 within last hour for this ip        → render generic success page (do not send)
  ├─ look up active user with that email
  │   ├─ user found     → invalidate prior unused tokens for user, generate new token, send Postmark email with /auth/reset/<token>
  │   └─ user not found → no-op
  └─ render: "Kui see e-post on registreeritud, saatsime parooli lähtestamise lingi" (always the same response)

[User opens email] → clicks link → GET /auth/reset/<token>
  ├─ pre-check token row (read-only, used for rendering): missing / expired / used → "Link on aegunud või vigane" page
  └─ valid                                                                          → form (new password + confirm)

POST /auth/reset/<token>  (new_password, new_password_confirm)
  ├─ validate password (incl. email substring check)
  ├─ confirm matches
  ├─ TX:
  │   ├─ atomic UPDATE password_reset_tokens SET used_at = now() WHERE token_hash = % AND used_at IS NULL AND expires_at > now() RETURNING user_id  (§4.6)
  │   ├─ if zero rows returned → render invalid-token page; do NOT change password
  │   └─ change_password(user_id, new_password, must_change=False)
  ├─ audit log: user.password_reset (self)
  └─ redirect /auth/login with cleared auth cookies + success flash
```

### 5.2 Change password while logged-in

```
GET  /profile                  → page with section list (currently: "Vaheta parool")
GET  /profile/password         → form (current_password, new_password, new_password_confirm)
POST /profile/password
  ├─ verify current_password against user's bcrypt hash
  │   └─ wrong → form re-render with error "Praegune parool on vale"
  ├─ validate new_password (incl. substring check)
  ├─ confirm matches
  ├─ TX: change_password()
  ├─ audit log: user.password_change (self)
  └─ since sessions are deleted, redirect → /auth/login with flash "Parool on muudetud, palun logi uuesti sisse"
```

### 5.3 Admin reset (path B — email link, primary)

Available at:
- `POST /admin/users/{user_id}/reset_email` — system admin, any user
- `POST /org/users/{user_id}/reset_email` — org admin, only own-org drafter/reviewer (#634 guards)

Flow:
- Look up target user; verify scope guards.
- Generate token (`created_by = caller_id`), store hash, send admin-flavored Postmark email ("An administrator has initiated a password reset…").
- Audit log: `user.password_reset_initiated` with `{target_user_id, mode: "email"}`.
- Redirect back to user list with flash "Parooli lähtestamise link saadetud".

### 5.4 Admin reset (path A — temporary password, fallback)

UI: secondary action "Määra ajutine parool" on the same admin reset page (§7.5), opens a focused form (admin types a new password). Used only when the target user has no email access.

- `POST /admin/users/{user_id}/reset_temp` (system) / `POST /org/users/{user_id}/reset_temp` (org).
- Validate password (admin's input must satisfy `validate_password(pw, email=target_email)`).
- TX: `change_password(target_user_id, new_password, must_change=True)`. The `must_change=True` flag forces the target user to change the password on their next login (§4.8) — admins must not retain knowledge of the long-term password.
- Audit log: `user.password_reset_initiated` with `{target_user_id, mode: "temp"}`.
- Audit log on completion: `user.password_change` with `{forced: true}` is automatically written by the `/profile/password` POST handler since `must_change_password` was true at that point. This gives admins a clean audit trail confirming the user picked their own password and the temp window closed.
- Show the temp password ONCE on the redirect target page (flash + a one-time reveal block) so the admin can copy it. Do NOT email the temp password.

### 5.5 Self-service "Set new password" (used by §5.1 reset)

The reset-link landing page is its own route (`/auth/reset/<token>`), shared only between self-service and admin-email flows. Both write the same kind of token with the only differentiator being `created_by`.

## 6. Security controls

| Control | Implementation |
|---|---|
| Token confidentiality | 32 random bytes hex (`secrets.token_hex(32)`), SHA-256-stored, never logged in plaintext |
| Atomic single-use | `UPDATE … SET used_at = now() WHERE used_at IS NULL AND expires_at > now() RETURNING user_id` (§4.6) — concurrent submissions of the same token cannot both succeed |
| Short TTL | 1 hour |
| Single-current-token | Issuing a new token marks all prior unused tokens for that user as used (§4.3) — simpler operational mental model |
| Email enumeration safe | Same response on found/not-found in `/auth/forgot`; rate limit applied BEFORE user lookup (per-email-hash and per-IP) so unknown emails are throttled identically to known ones |
| Rate limit forgot | 3 requests / `email_hash` / hour AND 10 / IP / hour, both DB-backed via `password_reset_attempts` (§4.3); rows recorded for every attempt regardless of whether the email is in `users` |
| Rate limit reset | Token is single-use; no separate counter needed — the atomic UPDATE caps usage at one |
| Bcrypt cost | Default `bcrypt.gensalt()` (12 rounds) |
| Session revocation | All flows: `change_password()` bumps `token_version` + deletes `sessions` rows + sets `password_changed_at`; HTTP handlers additionally clear `access_token` / `refresh_token` cookies on the redirect (§4.7) |
| Forced re-change for temp pw | `users.must_change_password = TRUE` is set by the admin temp-password flow (§5.4); middleware redirects every authenticated request to `/profile/password` until the user picks their own password (§4.8) |
| CSRF posture | Inherits the project's existing `SameSite=Lax` cookie posture (`app/auth/cookies.py:22`); per-flow analysis in §4.9. CSRF middleware is a separate, codebase-wide enhancement — open question §12 |
| Authorization for admin reset | Reuses `require_role` decorator + #634 scope checks (`role in ORG_ASSIGNABLE_ROLES`) |
| Audit logging | `user.password_reset_initiated` (admin, with `mode` ∈ `{email, temp}`), `user.password_change` (self, with optional `forced: true`), `user.password_reset` (self via reset link); all entries include `created_by` and target user IDs |
| Public-route auth | `/auth/forgot` and `/auth/reset/.*` added to `SKIP_PATHS` in `app/auth/middleware.py` (§4.7) |
| Secret in URL | The reset token IS in the URL — necessary for password-reset UX. Mitigated by short TTL, single use, hash storage, and the fact that the email-mailbox owner is implicitly trusted |
| Stub-mode safety | `StubProvider` is used in dev/test/CI; production (`APP_ENV=production`) without `POSTMARK_API_TOKEN` raises at first call so deployment fails loudly. Production never silently falls back to the stub (§4.2) |

## 7. UI/UX changes

### 7.1 Login page

- Add a small `Unustasid parooli?` link below the password field, linking to `/auth/forgot`.

### 7.2 New `/auth/forgot` page

- Single email field, `Saada lähtestamise link` button.
- On submit, always shows the generic "Kui see e-post on registreeritud, saatsime parooli lähtestamise lingi. Vaata e-postist." page.

### 7.3 New `/auth/reset/<token>` page

- Two password fields (new + confirm), `Salvesta uus parool` button.
- Invalid/expired token: `Alert(variant=danger)` and a link back to `/auth/forgot`.

### 7.4 New `/profile` and `/profile/password` pages

- `/profile` renders a `Card` listing actions; first item: `Vaheta parool` linking to `/profile/password`.
- `/profile/password`: three password fields + submit.
- Header avatar dropdown (next to logout) gets a new `Profiil` link.

### 7.5 Admin user list — both `/admin/users` and `/org/users`

- New `Lähtesta parool` link in the existing `Tegevused` cell on each user row, navigating to a dedicated reset page (matches the existing `/admin/users/{user_id}/role` pattern — no popovers, just a focused page).
- `GET /admin/users/{user_id}/reset` (or `/org/users/...`) renders a `Card` with:
  - Inline `AppForm` (single button `Saada e-postiga`) → POSTs to `…/reset_email` (no other fields).
  - Below it, a second `AppForm` with one password field + button `Määra ajutine parool` → POSTs to `…/reset_temp`.
- For org admin, the `Lähtesta parool` link is only rendered for users where `_is_active and role in ORG_ASSIGNABLE_ROLES`.
- After temp-password set, the redirect target (back at the reset page) shows the temp password in a copy-friendly `Alert(variant="success")` card (one-time view; refresh or navigation removes it).

## 8. Estonian copy

| Key | Estonian |
|---|---|
| Login forgot link | `Unustasid parooli?` |
| Forgot page title | `Parooli lähtestamine` |
| Forgot button | `Saada lähtestamise link` |
| Forgot success | `Kui see e-post on registreeritud, saatsime parooli lähtestamise lingi.` |
| Reset page title | `Määra uus parool` |
| Reset confirm button | `Salvesta uus parool` |
| Reset invalid token | `Lähtestamise link on aegunud või vigane.` |
| Profile page title | `Profiil` |
| Change password link | `Vaheta parool` |
| Current pw label | `Praegune parool` |
| New pw label | `Uus parool` |
| Confirm pw label | `Korda uut parooli` |
| Wrong current pw | `Praegune parool on vale.` |
| Forced-change banner (when `must_change_password` is true) | `Administraator on lähtestanud teie parooli. Palun määrake uus parool jätkamiseks.` |
| Pw mismatch | `Paroolid ei kattu.` |
| Pw contains email | `Parool ei tohi sisaldada teie e-posti aadressi.` |
| Change success | `Parool on muudetud. Palun logi uuesti sisse.` |
| Admin reset action | `Lähtesta parool` |
| Admin email choice | `Saada e-postiga` |
| Admin temp choice | `Määra ajutine parool` |
| Admin email sent | `Parooli lähtestamise link saadetud.` |
| Admin temp shown | `Ajutine parool on määratud. Edasta see kasutajale turvaliselt:` |

Email subjects:
- Self-service: `Parooli lähtestamine — Seadusloome`
- Admin-initiated: `Administraator on lähtestanud teie parooli — Seadusloome`

## 9. Configuration

New env vars:

| Name | Required when | Purpose |
|---|---|---|
| `POSTMARK_API_TOKEN` | `APP_ENV=production` | Server API token for the "Seadusloome" Postmark server |
| `EMAIL_FROM` | `APP_ENV=production` (default `Seadusloome <noreply@sixtyfour.ee>`) | From-address for transactional email |
| `APP_BASE_URL` | `APP_ENV=production` (default `http://localhost:8000` in dev) | Base URL used to build reset links in email bodies |

All of these go into Coolify env config; they remain absent in `.env.example` (which is committed) but documented in `README.md` deployment section.

## 10. Testing

### 10.1 Unit / integration tests (pytest)

- `tests/test_password_change.py` — `change_password()` core: bcrypt hash written, `token_version` bumped, sessions deleted, `password_changed_at` set, `must_change_password` set/cleared per `must_change` arg, transactional rollback on failure.
- `tests/test_password_validation.py` — extended `validate_password()` rejects email-substring; existing ≥8/upper/digit checks still pass.
- `tests/test_auth_forgot_routes.py` — `/auth/forgot`: identical generic response for known/unknown emails, `password_reset_attempts` row created in BOTH cases, per-email-hash and per-IP rate limits trigger after threshold, prior unused tokens for the user are invalidated when a new one is issued.
- `tests/test_auth_reset_routes.py` — `/auth/reset/<token>`: invalid/expired/used renders the invalid-token page; success path clears auth cookies; **concurrent submissions** (two threads call POST with the same token) — only one applies a password change (atomic UPDATE … RETURNING locks the row), the other returns the invalid-token page.
- `tests/test_profile_password.py` — wrong current pw rejected; new+confirm match required; redirect to `/auth/login` clears auth cookies; `must_change_password = TRUE` users are redirected to `/profile/password` from any other authenticated route until they complete the change.
- `tests/test_admin_password_reset.py` — `/reset_email` and `/reset_temp` for both system and org admin; org admin cannot reset admin/org_admin even in same org (#634); temp-password path sets `must_change_password = TRUE`; audit log entries with correct `mode` field.
- `tests/test_auth_middleware_skip.py` — `/auth/forgot` and `/auth/reset/<token>` are reachable without an `access_token` cookie (regression guard for SKIP_PATHS).
- `tests/test_email_provider.py` — `get_email_provider()` selection rule covers all four cases (stub-allowed × token-present grid); production without token raises at first send call; `StubProvider` writes to `logger.info` without importing `postmarker`.

### 10.2 Manual / browser checks

- Run `uv run python -m app` locally; trigger forgot flow; verify stub email logged with reset link; click link in browser; reset; log in.
- In a staging deploy with real `POSTMARK_API_TOKEN`: trigger forgot flow with a real test inbox; receive Postmark email; complete reset; verify all browser sessions on other devices are killed.

## 11. Rollout

- Single migration `024_password_reset_tokens.sql` (additive, zero downtime). Includes:
  - `CREATE TABLE password_reset_tokens` with indexes on `user_id`, `expires_at`, `created_by`.
  - `CREATE TABLE password_reset_attempts` with indexes on `(email_hash, attempted_at)` and `(ip, attempted_at)`.
  - `ALTER TABLE users ADD COLUMN must_change_password BOOLEAN NOT NULL DEFAULT FALSE`.
  - `ALTER TABLE users ADD COLUMN password_changed_at TIMESTAMPTZ`.
- The `auth_before` middleware change to fetch + redirect on `must_change_password` is a one-liner additive change — old tokens issued before the migration will simply have `False` after the column default kicks in, so the redirect never fires for existing users.
- New routes are additive; existing UI changes are confined to: login-page link, header avatar dropdown new entry, admin user-row new action.
- Stub provider means the code change is safe to merge before Postmark domain DNS verification finishes; once DNS verifies, production simply sets `POSTMARK_API_TOKEN` and emails start flowing.
- `SKIP_PATHS` middleware change must be deployed atomically with the route handlers (otherwise `/auth/forgot` and `/auth/reset/...` would 303 to login). They live in the same PR.
- No data migration needed.

## 12. Open questions

1. **CSRF posture (§4.9):** Confirm shipping these flows under the existing `SameSite=Lax`-only posture is acceptable for now, with CSRF middleware tracked as a separate, codebase-wide work item. Alternative is to wire `starlette-csrf` (or equivalent) into all mutating routes as part of this spec — wider scope, larger blast radius, but eliminates the gap on admin reset POSTs in particular.
2. **Temp-password fallback retention (§5.4):** The `must_change_password` flag mitigates the "admin knows the user's password" risk, but the admin still knows the temp password during the window between reset and first login. If even that window is undesirable, the fallback can be cut and admins routed exclusively through the email-link path. Confirm the temp-password fallback is still wanted.

## 13. Appendix — Postmark setup snapshot

(Captured during this design; not part of code.)

- **Server:** Seadusloome, ID `19043028`, Live mode.
- **Server API token:** stored in `POSTMARK_API_TOKEN` (Coolify secret); not committed.
- **Sender domain:** `sixtyfour.ee` (added to Postmark; DNS pending verification).
- **DNS records added at Hostinger DNS for `sixtyfour.ee`:**
  - **TXT** `20260322162950pm._domainkey` → `k=rsa;p=MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCqt5AldYwI+RJABMtERL/qJf8GJfA8VIe31xoNof3JG1XnJsCeKXCgeZN3f8ww+FudyzJRjvacMZMdlphgOMI49GKh/FKpghHs+Y/hZW4Kqo4OJU5wPSw1G3YFIxuIgETnS0/BJOg3kQdHMIVNHM+Bs0CpwKGGRANi6E1ffp6RzQIDAQAB` (TTL 14400)
  - **CNAME** `pm-bounces` → `pm.mtasv.net` (TTL 14400)
- **From address:** `Seadusloome <noreply@sixtyfour.ee>` (set via `EMAIL_FROM` env var).
