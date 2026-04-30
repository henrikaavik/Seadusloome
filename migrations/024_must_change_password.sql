-- 024_must_change_password.sql
--
-- Adds a forced first-login password change flag for the seeded admin
-- and any future temporary-password resets. Mitigates P0 from the
-- 2026-04-29 UI review: the deployed site accepted login as
-- admin@seadusloome.ee with the seed password 'admin' (hash hardcoded
-- in 004_admin_seed_fix.sql). The seed migration's comment promised
-- "the admin MUST change this from the UI on first login" but no UI
-- or middleware enforced it. This migration adds the column the
-- middleware now reads to redirect seeded-credential admins to
-- /profile/password until they pick a real password.
--
-- ``password_changed_at`` is added at the same time so we can track
-- the most recent change for audit / future password-rotation policies
-- and so the seeded admin row can be uniquely targeted (rows whose
-- password has never been changed are exactly those with
-- ``password_changed_at IS NULL``).

ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_changed_at TIMESTAMPTZ;

-- Force the seeded admin to change their password on next login.
-- Guarded by ``password_changed_at IS NULL`` so re-running this
-- migration after the admin has rotated their password does not
-- re-flip the flag.
UPDATE users
   SET must_change_password = TRUE
 WHERE email = 'admin@seadusloome.ee'
   AND password_changed_at IS NULL;
