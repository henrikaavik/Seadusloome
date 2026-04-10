-- =============================================================================
-- Migration 011: Phase 4 — Collaboration, Notifications, and Admin Metrics
-- Ticket #532
-- =============================================================================
--
-- Adds the PostgreSQL tables and materialized view needed for Phase 4
-- (Collaboration + Admin), covering in-line annotations, threaded replies,
-- user notifications, per-org daily usage rollups, and a generic metrics sink.
--
-- Objects created:
--   1. annotations          — inline comments on drafts, sections, impacts,
--                             graph nodes, chat messages, and drafting clauses
--   2. annotation_replies   — threaded replies to annotations
--   3. notifications        — per-user notification inbox
--   4. usage_daily          — materialized view: per-org daily activity summary
--   5. metrics              — append-only metrics/telemetry sink
--
-- All timestamps use TIMESTAMPTZ. UUIDs for entity PKs (gen_random_uuid()).
-- Migration is idempotent: uses IF NOT EXISTS throughout.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. annotations
-- ---------------------------------------------------------------------------
-- Stores inline comments attached to a specific target within the application.
-- target_type controls which domain object is referenced; target_id carries
-- the opaque identifier of that object (UUID, URI, or composite key as text).
-- target_metadata holds additional context supplied by the client at creation
-- time (e.g. paragraph offset, graph node label) so the UI can render the
-- annotation without a separate lookup.
-- resolved/resolved_by/resolved_at track the review lifecycle: an annotation
-- can be marked resolved by any user with write access to the target org.

create table if not exists annotations (
    id              uuid        primary key default gen_random_uuid(),
    user_id         uuid        not null references users(id) on delete cascade,
    org_id          uuid        not null references organizations(id) on delete cascade,
    target_type     text        not null check (target_type in (
                                    'draft',
                                    'draft_section',
                                    'impact_report_item',
                                    'graph_node',
                                    'chat_message',
                                    'drafting_clause'
                                )),
    target_id       text        not null,
    target_metadata jsonb,
    content         text        not null,
    resolved        boolean     not null default false,
    resolved_by     uuid        references users(id) on delete set null,
    resolved_at     timestamptz,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

-- Composite index for the most common query: fetch all annotations for a
-- given target object (e.g. all annotations on a specific draft section).
create index if not exists idx_annotations_target
    on annotations(target_type, target_id);

create index if not exists idx_annotations_user_id
    on annotations(user_id);

create index if not exists idx_annotations_org_id
    on annotations(org_id);

-- ---------------------------------------------------------------------------
-- 2. annotation_replies
-- ---------------------------------------------------------------------------
-- Threaded replies to an annotation.  Each reply is append-only; deleting the
-- parent annotation cascades and removes all replies.

create table if not exists annotation_replies (
    id              uuid        primary key default gen_random_uuid(),
    annotation_id   uuid        not null references annotations(id) on delete cascade,
    user_id         uuid        not null references users(id) on delete cascade,
    content         text        not null,
    created_at      timestamptz not null default now()
);

-- Index for loading all replies for a given annotation in creation order.
create index if not exists idx_annotation_replies_annotation_id
    on annotation_replies(annotation_id);

-- ---------------------------------------------------------------------------
-- 3. notifications
-- ---------------------------------------------------------------------------
-- Per-user notification inbox.  Rows are written by server-side event handlers
-- (new annotation on your draft, reply to your annotation, etc.) and consumed
-- by the HTMX polling endpoint or WebSocket push.
-- read = false means the notification is unread (the default).
-- link is an optional deep-link URL the frontend renders as a CTA button.

create table if not exists notifications (
    id          uuid        primary key default gen_random_uuid(),
    user_id     uuid        not null references users(id) on delete cascade,
    type        text        not null,
    title       text        not null,
    body        text,
    link        text,
    metadata    jsonb,
    read        boolean     not null default false,
    created_at  timestamptz not null default now()
);

-- Composite index optimised for the unread-count query and the inbox list:
--   SELECT ... WHERE user_id = $1 AND read = false ORDER BY created_at DESC
create index if not exists idx_notifications_user_read_created
    on notifications(user_id, read, created_at desc);

-- ---------------------------------------------------------------------------
-- 4. usage_daily  (materialized view)
-- ---------------------------------------------------------------------------
-- Aggregates per-org activity into daily buckets for the admin dashboard.
-- Refreshed by a background job (or pg_cron if available) on a daily schedule.
-- The unique index on (day, org_id) enables REFRESH CONCURRENTLY so the view
-- can be updated without taking an AccessExclusiveLock on the table.
--
-- Sources:
--   draft_uploads    — rows in drafts (any status counts as one upload event)
--   chat_messages    — user-role messages in conversations
--   drafter_sessions — rows in drafting_sessions

create materialized view if not exists usage_daily as
select
    date_trunc('day', created_at)::date as day,
    org_id,
    count(*) filter (where source = 'draft')   as draft_uploads,
    count(*) filter (where source = 'chat')    as chat_messages,
    count(*) filter (where source = 'drafter') as drafter_sessions
from (
    select created_at, org_id, 'draft' as source
    from   drafts
    union all
    select m.created_at, c.org_id, 'chat'
    from   messages m
    join   conversations c on c.id = m.conversation_id
    where  m.role = 'user'
    union all
    select created_at, org_id, 'drafter'
    from   drafting_sessions
) combined
group by 1, 2;

-- Unique index required for REFRESH CONCURRENTLY (avoids full-table lock).
create unique index if not exists idx_usage_daily_day_org
    on usage_daily(day, org_id);

-- ---------------------------------------------------------------------------
-- 5. metrics
-- ---------------------------------------------------------------------------
-- Generic append-only telemetry sink for application-level metrics (request
-- latency, SPARQL query duration, LLM token cost per request, etc.).
-- name identifies the metric (e.g. 'sparql_query_ms', 'llm_tokens_total').
-- labels is a free-form JSONB object for dimensions (e.g. {"model": "claude-3-7-sonnet"}).
-- This table intentionally has no foreign keys so it can be written from any
-- context, including health-check probes that run outside a user session.

create table if not exists metrics (
    id          bigserial   primary key,
    name        text        not null,
    value       numeric     not null,
    labels      jsonb,
    recorded_at timestamptz not null default now()
);

-- Composite index for time-series queries over a named metric:
--   SELECT ... WHERE name = $1 AND recorded_at >= $2 ORDER BY recorded_at
create index if not exists idx_metrics_name_recorded_at
    on metrics(name, recorded_at);
