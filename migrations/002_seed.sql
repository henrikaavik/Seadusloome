-- Seed default organization and admin user
-- Password: admin (bcrypt hash)
INSERT INTO organizations (name, slug)
VALUES ('Seadusloome Admin', 'seadusloome-admin')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO users (org_id, email, password_hash, full_name, role)
SELECT o.id, 'admin@seadusloome.ee',
       '$2b$12$LJ3m4ys3Lf0Y3pKsfNMpBOjVFzVqFGCMfMqGhJKHZwRpYz0PQmXVi',
       'System Administrator', 'admin'
FROM organizations o
WHERE o.slug = 'seadusloome-admin'
ON CONFLICT (email) DO NOTHING;
