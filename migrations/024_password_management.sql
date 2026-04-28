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
