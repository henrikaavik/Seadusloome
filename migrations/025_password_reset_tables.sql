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
