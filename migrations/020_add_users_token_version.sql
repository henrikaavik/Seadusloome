-- #635 — Token-version column for access-token revocation.
--
-- The JWT access token embeds the user's current ``token_version`` as the
-- ``tv`` claim. On every authenticated request the middleware rehydrates
-- the user from the DB and rejects the token when ``tv`` != ``token_version``
-- (or when ``is_active`` is FALSE, or when ``role`` / ``org_id`` have drifted).
--
-- Role updates and deactivations bump ``token_version`` in the same UPDATE,
-- which invalidates every previously-issued access token for that user in
-- O(1) DB work.

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 0;
