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
- **`StubProvider`** — logs the rendered subject + body to `logger.info`, returns. Used unconditionally when `is_stub_allowed()` is true OR when `POSTMARK_API_TOKEN` is empty.
- **`get_email_provider()`** — single source of truth for which provider is active. Gated by `APP_ENV` and presence of `POSTMARK_API_TOKEN`, mirroring the LLM gate pattern.

Templates live as Python module-level functions returning `(subject, html, text)` tuples (no Jinja, matches the pattern used elsewhere in the project for short messages).

### 4.3 Database schema — `password_reset_tokens`

```sql
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
```

- Raw token = 64-char hex (32 random bytes, `secrets.token_hex(32)`). Stored only as SHA-256 hex — same pattern as `sessions.token_hash`.
- Single-use: `used_at` flips to `now()` on consumption inside the same transaction that updates the password.
- TTL 1 hour (`expires_at = now() + 1 hour`).
- Issuing a new token does NOT invalidate prior unused tokens — they expire on their own. (Acceptable: each is single-use and short-lived.)
- A periodic cleanup is unnecessary at the 5-50 user scale; if needed later, a `DELETE FROM password_reset_tokens WHERE expires_at < now() - interval '7 days'` cron will do.

### 4.4 Shared password-change core (`app/auth/password.py`)

```python
def change_password(user_id: UUID, new_password: str, *, conn: Connection) -> None:
    """Update password_hash, bump token_version, delete sessions. Atomic."""
    pw_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    with conn.transaction():
        conn.execute(
            "UPDATE users SET password_hash = %s, token_version = token_version + 1 "
            "WHERE id = %s",
            (pw_hash, user_id),
        )
        conn.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
```

Used by all three flows. Caller is responsible for validation, audit logging, and consuming the reset token (when applicable).

### 4.5 Password validation (extended)

`validate_password(password: str, *, email: str | None = None) -> str | None`:
- Existing: ≥8 chars, ≥1 uppercase, ≥1 digit.
- New: if `email` given, the local-part (before `@`) lowercased must not appear as a substring in the lowercased password. Estonian error: `"Parool ei tohi sisaldada teie e-posti aadressi"`.

## 5. Flows

### 5.1 Self-service forgot password

```
[Login page] → click "Unustasid parooli?"
  ↓
GET  /auth/forgot                    → form (email field only)
POST /auth/forgot  (email)
  ├─ validate email format
  ├─ rate-limit check (see §6)
  ├─ look up active user with that email
  │   ├─ user found     → generate token, store hash, send Postmark email with /auth/reset/<token>
  │   └─ user not found → no-op (BUT same response)
  └─ render: "Kui see e-post on registreeritud, saatsime parooli lähtestamise lingi"

[User opens email] → clicks link → GET /auth/reset/<token>
  ├─ token row missing / expired / used → "Link on aegunud või vigane" page
  └─ valid                              → form (new password + confirm)

POST /auth/reset/<token>  (new_password, new_password_confirm)
  ├─ token re-check (expired/used race-safe)
  ├─ validate password (incl. email substring check)
  ├─ confirm matches
  ├─ TX: change_password() + mark token used_at
  ├─ audit log: user.password_reset (self)
  └─ redirect /auth/login with success flash
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

UI: secondary action "Määra ajutine parool" on the same admin user-row, opens a small form (admin types a new password).

- `POST /admin/users/{user_id}/reset_temp` (system) / `POST /org/users/{user_id}/reset_temp` (org).
- Validate password (admin's input must satisfy `validate_password(pw, email=target_email)`).
- TX: `change_password()`. Audit log: `user.password_reset_initiated` with `{target_user_id, mode: "temp"}`.
- Show the temp password ONCE on the redirect target page (flash + a one-time reveal block) so the admin can copy it. Do NOT email the temp password.

### 5.5 Self-service "Set new password" (used by §5.1 reset)

The reset-link landing page is its own route (`/auth/reset/<token>`), shared only between self-service and admin-email flows. Both write the same kind of token with the only differentiator being `created_by`.

## 6. Security controls

| Control | Implementation |
|---|---|
| Token confidentiality | 32 random bytes hex, SHA-256-stored, never logged in plaintext |
| Single-use | `used_at` flipped in same TX as password update |
| Short TTL | 1 hour |
| Email enumeration safe | Same response on found/not-found in `/auth/forgot` |
| Rate limit forgot | 3 requests / email / hour (DB-backed counter on `password_reset_tokens` rows) AND 10 / IP / hour (in-memory leaky bucket; small scale acceptable) |
| Rate limit reset | 5 / token / hour (defensive — token is single-use anyway) |
| Bcrypt cost | Default `bcrypt.gensalt()` (12 rounds) |
| Session revocation | All flows: bump `token_version` + delete `sessions` rows |
| Admin reset CSRF | Existing CSRF protection on POST forms (already in `app/ui/forms/app_form.py`) |
| Authorization for admin reset | Reuses `require_role` decorator + #634 scope checks (`role in ORG_ASSIGNABLE_ROLES`) |
| Audit logging | `user.password_reset_initiated` (admin) and `user.password_change` / `user.password_reset` (self), with `created_by` and target user IDs |
| Secret in URL | The reset token IS in the URL — necessary for password-reset UX. Mitigated by short TTL, single use, hash storage, and the fact that the email-mailbox owner is implicitly trusted |
| Stub-mode safety | `StubProvider` is automatic when `APP_ENV != production`; production deploy that's missing `POSTMARK_API_TOKEN` fails loudly at startup (mirrors LLM pattern) |

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

- New `Lähtesta parool` action button on each user row that opens a small popover with two choices:
  - **`Saada e-postiga`** → POST to `…/reset_email`
  - **`Määra ajutine parool`** → opens form with one password field
- For org admin, the action is only rendered for users where `_is_active and role in ORG_ASSIGNABLE_ROLES`.
- After temp-password set, the redirect target shows the temp password in a copy-friendly card (one-time view; refresh removes it).

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

- `tests/test_password_change.py` — `change_password()` core: bcrypt hash present, `token_version` bumped, sessions deleted, transactional rollback on failure.
- `tests/test_password_validation.py` — extended `validate_password()` rejects email-substring; ASCII-only check still works.
- `tests/test_auth_forgot_routes.py` — `/auth/forgot` rate limits, generic response on unknown email, token row created with hash + TTL.
- `tests/test_auth_reset_routes.py` — `/auth/reset/<token>` invalid/expired/used; success path; race condition (concurrent uses → only one wins).
- `tests/test_profile_password.py` — wrong current pw rejected; matching new + confirm required; redirect to login on success.
- `tests/test_admin_password_reset.py` — system admin can reset any user; org admin scope guards (cannot reset admin/org_admin even in same org); audit log entries written.
- `tests/test_email_provider.py` — `StubProvider` returns without network; `PostmarkProvider` lazy-init mock.

### 10.2 Manual / browser checks

- Run `uv run python -m app` locally; trigger forgot flow; verify stub email logged with reset link; click link in browser; reset; log in.
- In a staging deploy with real `POSTMARK_API_TOKEN`: trigger forgot flow with a real test inbox; receive Postmark email; complete reset; verify all browser sessions on other devices are killed.

## 11. Rollout

- Single migration `024_password_reset_tokens.sql` (additive, zero downtime).
- New routes are additive; nothing existing is changed except minor UI tweaks (login link, profile dropdown, admin user-row actions).
- Stub provider means the change is safe to merge before Postmark domain is verified; once DNS verifies, production simply sets `POSTMARK_API_TOKEN` and emails start flowing.
- No data migration needed.

## 12. Open questions

None — all decisions made during brainstorming.

## 13. Appendix — Postmark setup snapshot

(Captured during this design; not part of code.)

- **Server:** Seadusloome, ID `19043028`, Live mode.
- **Server API token:** stored in `POSTMARK_API_TOKEN` (Coolify secret); not committed.
- **Sender domain:** `sixtyfour.ee` (added to Postmark; DNS pending verification).
- **DNS records added at Hostinger DNS for `sixtyfour.ee`:**
  - **TXT** `20260322162950pm._domainkey` → `k=rsa;p=MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCqt5AldYwI+RJABMtERL/qJf8GJfA8VIe31xoNof3JG1XnJsCeKXCgeZN3f8ww+FudyzJRjvacMZMdlphgOMI49GKh/FKpghHs+Y/hZW4Kqo4OJU5wPSw1G3YFIxuIgETnS0/BJOg3kQdHMIVNHM+Bs0CpwKGGRANi6E1ffp6RzQIDAQAB` (TTL 14400)
  - **CNAME** `pm-bounces` → `pm.mtasv.net` (TTL 14400)
- **From address:** `Seadusloome <noreply@sixtyfour.ee>` (set via `EMAIL_FROM` env var).
