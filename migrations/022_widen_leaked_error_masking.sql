-- =============================================================================
-- Migration 022: Widen leaked-error masking patterns in drafts.error_message
-- =============================================================================
--
-- Follow-up to migration 021. The initial set of leak patterns covered
-- the obvious Python-side config (``ANTHROPIC_API_KEY``,
-- ``STORAGE_ENCRYPTION_KEY``, ``TIKA_URL``, ``APP_ENV=``,
-- ``VOYAGE_API_KEY``) but post-incident review (#667) surfaced three
-- more leak shapes that were still reaching end users:
--
--   1. Deployment-topology credentials / URLs:
--        DATABASE_URL, FUSEKI_URL, JWT_SECRET,
--        SMTP_HOST, SMTP_USER, SMTP_PASSWORD
--   2. Raw Python stack-trace signatures, e.g.
--        ``Traceback (most recent call last):``
--        ``File "/app/...`` frame lines
--   3. Anything that *looks* like an env-var assignment string and is
--      long enough to clearly be a dump rather than a short human
--      message. Detected via a case-sensitive regex for
--      ``[A-Z_]{6,}=`` on messages longer than 200 characters.
--
-- Behaviour mirrors migration 021 exactly:
--
--   * ``drafts.error_debug`` preserves the original raw string, but
--     only when ``error_debug`` IS NULL so we never clobber either
--     (a) a richer debug string a later handler wrote, or
--     (b) a value already preserved by migration 021.
--   * ``drafts.error_message`` is replaced with the canonical Estonian
--     fallback from :data:`app.docs.error_mapping.MSG_UNKNOWN`.
--
-- Idempotency
-- -----------
-- Safe to re-run. The masking UPDATE excludes rows whose message is
-- already the canonical fallback, and the error_debug UPDATE is gated
-- on ``error_debug IS NULL`` so it only ever fills blanks.
--
-- Revert
-- ------
-- No DOWN migration. The original text lives on in ``error_debug`` for
-- operator recovery; see migration 021 for the recovery recipe.
-- =============================================================================

-- Step 1: preserve the original raw message in error_debug. Only
-- populate when error_debug IS NULL so we never overwrite a value a
-- previous migration (021) or a subsequent retry already wrote.
UPDATE drafts
SET error_debug = error_message
WHERE error_debug IS NULL
  AND error_message IS NOT NULL
  AND (
    -- Deployment-topology secrets and URLs.
    error_message LIKE '%DATABASE_URL%'
    OR error_message LIKE '%FUSEKI_URL%'
    OR error_message LIKE '%JWT_SECRET%'
    OR error_message LIKE '%SMTP_HOST%'
    OR error_message LIKE '%SMTP_USER%'
    OR error_message LIKE '%SMTP_PASSWORD%'
    -- Python stack-trace signatures.
    OR error_message LIKE '%Traceback (most recent call last):%'
    OR error_message LIKE '%File "/app/%'
    -- Env-var-assignment-looking dump. Requires SIMILAR TO because
    -- POSIX regex + length guard is cleaner than multiple LIKEs.
    -- Case-sensitive on purpose: human Estonian copy shouldn't have
    -- SHOUTCASE_IDENT= tokens.
    OR (
      char_length(error_message) > 200
      AND error_message ~ '[A-Z_]{6,}='
    )
  );

-- Step 2: replace the user-facing message with the canonical Estonian
-- fallback. The string must match MSG_UNKNOWN in
-- app/docs/error_mapping.py exactly (kept in sync with migration 021).
UPDATE drafts
SET error_message = 'Töötlemine ebaõnnestus tehnilisel põhjusel. Meeskond on teavitatud.',
    updated_at = now()
WHERE error_message IS NOT NULL
  AND error_message != 'Töötlemine ebaõnnestus tehnilisel põhjusel. Meeskond on teavitatud.'
  AND (
    error_message LIKE '%DATABASE_URL%'
    OR error_message LIKE '%FUSEKI_URL%'
    OR error_message LIKE '%JWT_SECRET%'
    OR error_message LIKE '%SMTP_HOST%'
    OR error_message LIKE '%SMTP_USER%'
    OR error_message LIKE '%SMTP_PASSWORD%'
    OR error_message LIKE '%Traceback (most recent call last):%'
    OR error_message LIKE '%File "/app/%'
    OR (
      char_length(error_message) > 200
      AND error_message ~ '[A-Z_]{6,}='
    )
  );
