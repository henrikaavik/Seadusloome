-- Fix the admin seed from migration 002_seed.sql.
--
-- The original hash in 002_seed.sql was a placeholder string that did NOT
-- match the documented password 'admin', so any fresh deploy created an
-- unusable admin row. This migration does two things so both fresh
-- deployments AND older dev DBs converge to a working state:
--
--   1. Idempotently insert the admin organization + admin user if they
--      don't exist (covers a fresh DB where 002_seed.sql was never run
--      because it too was new).
--   2. If the admin user exists, update its password hash to a real
--      bcrypt hash of the password 'admin'. The admin MUST change this
--      from the UI on first login.
--
-- Password: admin
-- Hash:     $2b$12$hPGA.n3ZowI.KAEWFljo9.kHrEJzCA103gEdq1V3sFtSivoY5gP02
--           (bcrypt, 12 rounds — generated with bcrypt.hashpw in Python)

INSERT INTO organizations (name, slug)
VALUES ('Seadusloome Admin', 'seadusloome-admin')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO users (org_id, email, password_hash, full_name, role)
SELECT o.id,
       'admin@seadusloome.ee',
       '$2b$12$hPGA.n3ZowI.KAEWFljo9.kHrEJzCA103gEdq1V3sFtSivoY5gP02',
       'System Administrator',
       'admin'
FROM organizations o
WHERE o.slug = 'seadusloome-admin'
ON CONFLICT (email) DO UPDATE
    SET password_hash = EXCLUDED.password_hash,
        role = 'admin',
        is_active = TRUE;
