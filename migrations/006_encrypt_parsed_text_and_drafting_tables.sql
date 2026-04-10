-- =============================================================================
-- Migration 006: Phase 3 Batch 0 — encrypt parsed_text + create drafting tables
-- =============================================================================
--
-- NFR §2 compliance: all pre-publication draft content must be encrypted at
-- rest. This migration:
--
--   1. Converts drafts.parsed_text from plaintext TEXT to encrypted BYTEA.
--      The old column is dropped and a new `parsed_text_encrypted` column is
--      added. Existing data is discarded intentionally: prod contains only
--      smoke-test uploads and no real government drafts (confirmed in ticket
--      #488). If this migration were ever run after real data exists, a
--      separate data-migration script would need to read, encrypt, and rewrite
--      each row first.
--
--   2. Creates `drafting_sessions` — the Phase 3A drafter state machine table.
--      Encrypted BYTEA columns hold LLM research output and draft content so
--      no pre-publication material lands in the DB as cleartext.
--
--   3. Creates `drafting_session_versions` — snapshot audit trail for each
--      session step. Full step snapshots are stored encrypted so the rollback
--      path never exposes cleartext.
--
-- All timestamps use TIMESTAMPTZ. UUIDs for entity PKs, BIGSERIAL for the
-- version audit table. Migration is idempotent where possible.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. Convert drafts.parsed_text → parsed_text_encrypted (BYTEA)
-- ---------------------------------------------------------------------------
-- Drop the old plaintext TEXT column. The TODO comment in migration 005
-- flagged this follow-up explicitly. No data migration is needed because
-- prod only has test uploads from smoke testing (see #488 context).

alter table drafts drop column if exists parsed_text;
alter table drafts add column if not exists parsed_text_encrypted bytea;

-- ---------------------------------------------------------------------------
-- 2. drafting_sessions
-- ---------------------------------------------------------------------------
-- Stores the state for the Phase 3A multi-step AI law drafter workflow.
-- The `current_step` integer tracks position in the 7-step pipeline:
--   1 = intent capture, 2 = clarification interview, 3 = ontology research,
--   4 = structure generation, 5 = clause drafting, 6 = review, 7 = export.
--
-- Columns with potentially sensitive pre-publication content are BYTEA so
-- the application layer (Fernet via STORAGE_ENCRYPTION_KEY) encrypts them
-- before writing. Columns that hold only structural/UI metadata (e.g.
-- `proposed_structure`, `clarifications`) are JSONB — these contain
-- workflow configuration, not extracted legislative text.

create table if not exists drafting_sessions (
    id                      uuid        primary key default gen_random_uuid(),
    user_id                 uuid        not null references users(id) on delete cascade,
    org_id                  uuid        not null references organizations(id) on delete cascade,
    -- 'full_law' = complete act from intent; 'vtk' = VTK pre-analysis document
    workflow_type           text        not null check (workflow_type in ('full_law', 'vtk')),
    current_step            integer     not null default 1
                                check (current_step between 1 and 7),
    -- intent is short free-text entered by the drafter; not encrypted because
    -- it describes legislative purpose (not draft content) and is needed for
    -- UI display without an extra decrypt round-trip.
    intent                  text,
    -- clarifications: list of Q/A pairs gathered during the interview step;
    -- structural metadata, not sensitive legislative text.
    clarifications          jsonb       default '[]'::jsonb,
    -- research_data_encrypted: Fernet-encrypted JSON blob of SPARQL findings
    -- and LLM analysis from step 3 (ontology research). May contain references
    -- to unpublished provisions — must be encrypted.
    research_data_encrypted bytea,
    -- proposed_structure: section/chapter outline proposed by the LLM after
    -- research. Structural metadata only (headings, numbering), not prose text.
    proposed_structure      jsonb,
    -- draft_content_encrypted: the clause-by-clause draft prose (step 5+).
    -- This is the core pre-publication legislative text; encryption is mandatory
    -- per NFR §2.
    draft_content_encrypted bytea,
    -- integrated_draft_id: set in step 7 when the session produces a finalized
    -- Draft row in the `drafts` table (linked to the Jena named graph).
    integrated_draft_id     uuid        references drafts(id) on delete set null,
    status                  text        not null default 'active'
                                check (status in ('active', 'completed', 'abandoned')),
    created_at              timestamptz not null default now(),
    updated_at              timestamptz not null default now()
);

create index if not exists idx_drafting_sessions_user   on drafting_sessions(user_id);
create index if not exists idx_drafting_sessions_org    on drafting_sessions(org_id);
create index if not exists idx_drafting_sessions_status on drafting_sessions(status);

-- ---------------------------------------------------------------------------
-- 3. drafting_session_versions
-- ---------------------------------------------------------------------------
-- Immutable audit trail: one row per step completion. `snapshot_encrypted`
-- is the full Fernet-encrypted JSON representation of the session state at
-- that step so users can roll back to any prior checkpoint and so the audit
-- log captures every state transition for government compliance.
-- BIGSERIAL PK follows the log-table convention (see audit_log, sync_log).

create table if not exists drafting_session_versions (
    id                  bigserial   primary key,
    session_id          uuid        not null references drafting_sessions(id) on delete cascade,
    step                integer     not null,
    snapshot_encrypted  bytea       not null,
    created_at          timestamptz not null default now()
);

create index if not exists idx_dsv_session on drafting_session_versions(session_id);
