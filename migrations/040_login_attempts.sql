-- migrations/040_login_attempts.sql
--
-- Issue #851 (D1): login brute-force throttling. `POST /auth/login` had
-- no rate limiting while the forgot-password flow already throttled via
-- `password_reset_attempts` (migration 025). This table mirrors that
-- pattern 1:1 so the login flow can count recent FAILED attempts per
-- normalized email (SHA-256 of lowercased address — works for unknown
-- emails too, so throttling cannot leak account existence) and per
-- validated client IP (post-#851 ProxyHeaders hardening).
--
-- Only failures are recorded; a successful login deletes the rows for
-- that email_hash so a legitimate user is not locked out after they
-- recover their password. Rows are tiny and both lookups are bounded by
-- the composite time indexes, matching the 025 retention posture
-- (no scheduled pruning).

CREATE TABLE IF NOT EXISTS login_attempts (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email_hash    TEXT NOT NULL,
    ip            TEXT NOT NULL,
    attempted_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_login_attempts_email_hash_time ON login_attempts(email_hash, attempted_at);
CREATE INDEX IF NOT EXISTS idx_login_attempts_ip_time ON login_attempts(ip, attempted_at);
