-- =============================================================================
-- Migration 005: Phase 2 — Document Upload + Impact Analysis
-- =============================================================================
--
-- Adds the PostgreSQL tables needed for Phase 2 (Document Upload + Impact
-- Analysis) as specified in docs/superpowers/specs/2026-04-09-phase2-design.md.
--
-- Tables created:
--   1. drafts             — uploaded draft legislation documents
--   2. draft_entities     — extracted ontology references per draft
--   3. impact_reports     — generated impact analysis output per draft
--   4. background_jobs    — PostgreSQL-backed async job queue
--
-- Extensions enabled (if not already):
--   - pgvector  (Phase 3 RAG embeddings; harmless to enable now)
--   - pgcrypto  (provides gen_random_uuid())
--
-- All timestamps use TIMESTAMPTZ. UUIDs for entity PKs, BIGSERIAL for log/
-- queue PKs. Migration is idempotent: uses IF NOT EXISTS throughout.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Extensions
-- ---------------------------------------------------------------------------

-- pgvector: installed on prod image (pgvector/pgvector:pg18); enabling here
-- saves a separate migration when Phase 3 RAG pipeline begins.
create extension if not exists vector;

-- pgcrypto: provides gen_random_uuid() used below.
-- Note: 001_initial.sql already enabled uuid-ossp (uuid_generate_v4()).
-- Both coexist safely; new tables use gen_random_uuid() per convention.
create extension if not exists pgcrypto;

-- ---------------------------------------------------------------------------
-- 1. drafts
-- ---------------------------------------------------------------------------
-- Stores metadata for every uploaded draft legislation document.
-- The actual file is stored encrypted on disk; only the path is kept here.
-- Status column drives the async pipeline state machine:
--   uploaded → parsing → extracting → analyzing → ready
--                                               └→ failed (any stage)

create table if not exists drafts (
    id              uuid        primary key default gen_random_uuid(),
    user_id         uuid        not null references users(id) on delete cascade,
    org_id          uuid        not null references organizations(id) on delete cascade,
    title           text        not null,
    filename        text        not null,
    content_type    text        not null,
    file_size       bigint      not null check (file_size >= 0),
    storage_path    text        not null,    -- path to AES-256-GCM encrypted file on disk
    graph_uri       text        not null unique, -- Jena named graph URI, e.g. urn:draft:{id}
    status          text        not null check (status in (
                                    'uploaded',
                                    'parsing',
                                    'extracting',
                                    'analyzing',
                                    'ready',
                                    'failed'
                                )),
    -- TODO(#349): parsed_text must be migrated to an encrypted JSONB column in a
    -- later migration. Pre-publication drafts are politically sensitive; application-
    -- layer Fernet encryption (DRAFT_ENCRYPTION_KEY) is the primary control per
    -- docs/nfr-baseline.md §6 and the Phase 2 spec §11.1. For Phase 2 scaffolding
    -- this column is left as TEXT; do NOT store cleartext in production until
    -- the encrypted JSONB migration is applied.
    parsed_text     text,
    entity_count    integer,
    error_message   text,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

create index if not exists idx_drafts_user_id   on drafts(user_id);
create index if not exists idx_drafts_org_id    on drafts(org_id);
create index if not exists idx_drafts_status    on drafts(status);
create index if not exists idx_drafts_created_at on drafts(created_at);

-- ---------------------------------------------------------------------------
-- 2. draft_entities
-- ---------------------------------------------------------------------------
-- One row per legal reference extracted from a draft by the NLP pipeline.
-- entity_uri is NULL when the extractor found a reference but could not
-- map it to a known ontology URI (unmatched reference).

create table if not exists draft_entities (
    id          bigserial   primary key,
    draft_id    uuid        not null references drafts(id) on delete cascade,
    ref_text    text        not null,   -- raw extracted reference, e.g. "TsiviilS § 123 lg 2"
    entity_uri  text,                   -- matched ontology URI; NULL if unmatched
    confidence  real        check (confidence is null or (confidence >= 0.0 and confidence <= 1.0)),
    ref_type    text        not null check (ref_type in (
                                'law',
                                'provision',
                                'eu_act',
                                'court_decision',
                                'concept'
                            )),
    location    jsonb,                  -- structured position, e.g. {"section": "II", "paragraph": 5, "offset": 1234}
    created_at  timestamptz not null default now()
);

create index if not exists idx_draft_entities_draft_id   on draft_entities(draft_id);
create index if not exists idx_draft_entities_entity_uri on draft_entities(entity_uri);
create index if not exists idx_draft_entities_ref_type   on draft_entities(ref_type);

-- ---------------------------------------------------------------------------
-- 3. impact_reports
-- ---------------------------------------------------------------------------
-- Stores generated impact analysis output for a draft.
-- One draft may have multiple reports over time (re-runs after ontology
-- updates). The most recent report is the authoritative one.
-- ontology_version records the git SHA of the ontology repo at analysis time
-- so users can see if the report is stale relative to the current ontology
-- (see spec §11a).

create table if not exists impact_reports (
    id                  uuid        primary key default gen_random_uuid(),
    draft_id            uuid        not null references drafts(id) on delete cascade,
    generated_at        timestamptz not null default now(),
    affected_count      integer     not null default 0,
    conflict_count      integer     not null default 0,
    gap_count           integer     not null default 0,
    impact_score        integer     not null check (impact_score >= 0 and impact_score <= 100),
    report_data         jsonb       not null,   -- full findings: conflicts, EU issues, gaps, court decisions
    docx_path           text,                   -- path to exported .docx; NULL until export job completes
    ontology_version    text        not null default 'unknown' -- git SHA of ontology repo at analysis time (spec §11a)
);

create index if not exists idx_impact_reports_draft_id     on impact_reports(draft_id);
create index if not exists idx_impact_reports_generated_at on impact_reports(generated_at);

-- ---------------------------------------------------------------------------
-- 4. background_jobs
-- ---------------------------------------------------------------------------
-- Lightweight PostgreSQL-backed async job queue (no Celery, no Redis).
-- Workers claim jobs atomically using SELECT ... FOR UPDATE SKIP LOCKED.
--
-- Status state machine:
--   pending → claimed → running → success
--                    └→ retrying → pending  (up to max_attempts)
--                    └→ failed              (after max_attempts exhausted)
--
-- scheduled_for enables delayed execution and exponential-backoff retries:
-- a worker only claims jobs where scheduled_for <= now().
--
-- priority: higher integer = more urgent (dequeued first).
-- claimed_by: identifies the worker instance that claimed the job (for
-- debugging dangling jobs after a crash).

create table if not exists background_jobs (
    id              bigserial   primary key,
    job_type        text        not null,   -- 'parse_draft' | 'extract_entities' | 'analyze_impact' | 'export_report'
    payload         jsonb       not null,   -- job-specific arguments, e.g. {"draft_id": "..."}
    status          text        not null check (status in (
                                    'pending',
                                    'claimed',
                                    'running',
                                    'success',
                                    'failed',
                                    'retrying'
                                )),
    priority        integer     not null default 0,     -- higher = more urgent
    attempts        integer     not null default 0,
    max_attempts    integer     not null default 3,
    claimed_by      text,                               -- worker ID, set when status → claimed
    claimed_at      timestamptz,
    started_at      timestamptz,
    finished_at     timestamptz,
    error_message   text,
    result          jsonb,                              -- handler return value on success
    scheduled_for   timestamptz not null default now(), -- supports delayed jobs and retry backoff
    created_at      timestamptz not null default now()
);

-- Partial index used by the dequeue query:
--   SELECT ... WHERE status = 'pending' AND scheduled_for <= now()
--   ORDER BY priority DESC, created_at ASC
--   FOR UPDATE SKIP LOCKED LIMIT 1
-- The partial predicate (status = 'pending') keeps the index small and
-- avoids scanning completed/failed rows.
create index if not exists idx_bg_jobs_pending_priority
    on background_jobs(priority desc, created_at asc, scheduled_for)
    where status = 'pending';

create index if not exists idx_bg_jobs_status   on background_jobs(status);
create index if not exists idx_bg_jobs_job_type on background_jobs(job_type);
