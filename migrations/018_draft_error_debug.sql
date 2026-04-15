-- =============================================================================
-- Migration 018: drafts.error_debug -- actionable error messages (#609)
-- =============================================================================
--
-- Adds a second error column so we can separate the user-facing Estonian
-- message (``error_message``) from the raw technical detail
-- (``error_debug``) shown to admins.
--
-- Before this migration, the three draft processing handlers
-- (parse / extract / analyze) wrote ``str(exc)[:500]`` directly into
-- ``error_message``, which is rendered verbatim into the Alert banner
-- via ``app/docs/routes.py:236``. Ministry lawyers saw strings like
-- ``"anthropic.BadRequestError: messages: at least one message is required"``
-- with no guidance on what to do next.
--
-- After this migration:
--   - ``error_message`` holds the short Estonian user-facing string
--     produced by ``app.docs.error_mapping.map_failure_to_user_message``.
--   - ``error_debug`` holds the raw technical detail (exception class
--     name + message, truncated to 2000 chars) for admin triage and
--     audit. It is never surfaced in the drafter UI.
--
-- Both columns are nullable: only populated when a draft transitions to
-- ``status='failed'``. Successful pipelines clear both back to NULL.
-- =============================================================================

ALTER TABLE drafts
    ADD COLUMN IF NOT EXISTS error_debug TEXT NULL;
