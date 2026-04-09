# Phase 4 Design: Collaboration + Admin

**Status:** Approved
**Date:** 2026-04-09
**Depends on:** Phase 1 (auth, orgs, users), Phase 2 (drafts, reports), Phase 3 (chat, drafter)

---

## 1. Goals

Phase 4 turns the single-user advisory tool into a **multi-user collaboration platform** for ministry teams and adds the **operational admin tooling** for running it in production.

**End-to-end milestones:**

> **Collaboration:** A reviewer opens a draft uploaded by a colleague, reads the impact report, highlights a specific conflict entry, and adds an inline comment "See võib konfliktida VTKga #234". The drafter sees a notification, replies, and resolves the thread.

> **Admin:** The system admin opens `/admin` in the morning and sees: sync pipeline status, Sentry errors from the last 24h, LLM cost burn chart, top users, recent audit events, and RAG index health — all at a glance.

---

## 2. Architecture Additions

### 2.1 New PostgreSQL tables

```sql
-- Inline annotations (comments attached to specific elements)
CREATE TABLE annotations (
    id              UUID PRIMARY KEY,
    user_id         UUID REFERENCES users(id),
    target_type     TEXT NOT NULL CHECK (target_type IN (
        'draft',                  -- whole draft
        'draft_section',          -- specific section in the draft
        'impact_report_item',     -- specific conflict/gap/EU item
        'graph_node',             -- specific entity in the explorer
        'chat_message',           -- specific chat response
        'drafting_clause'         -- specific generated clause
    )),
    target_id       TEXT NOT NULL,            -- URI, UUID, or composite
    target_metadata JSONB,                     -- extra context (draft_id, report_id, etc.)
    content         TEXT NOT NULL,
    resolved        BOOLEAN DEFAULT FALSE,
    resolved_by     UUID REFERENCES users(id),
    resolved_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_annotations_target ON annotations(target_type, target_id);
CREATE INDEX idx_annotations_user ON annotations(user_id);

CREATE TABLE annotation_replies (
    id              UUID PRIMARY KEY,
    annotation_id   UUID REFERENCES annotations(id) ON DELETE CASCADE,
    user_id         UUID REFERENCES users(id),
    content         TEXT NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- Notifications
CREATE TABLE notifications (
    id              UUID PRIMARY KEY,
    user_id         UUID REFERENCES users(id),
    type            TEXT NOT NULL,            -- 'annotation_reply', 'draft_shared', 'analysis_done', etc.
    title           TEXT NOT NULL,
    body            TEXT,
    link            TEXT,                      -- deep link to relevant resource
    metadata        JSONB,
    read            BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_notifications_user_read ON notifications(user_id, read, created_at DESC);

-- Usage analytics (lightweight aggregations)
CREATE MATERIALIZED VIEW usage_daily AS
SELECT
    date_trunc('day', created_at) AS day,
    org_id,
    user_id,
    action,
    COUNT(*) AS action_count
FROM audit_log
GROUP BY day, org_id, user_id, action;

CREATE INDEX idx_usage_daily_day ON usage_daily(day DESC);
CREATE INDEX idx_usage_daily_org ON usage_daily(org_id, day DESC);
```

### 2.2 New Python modules

```
app/
├── annotations/
│   ├── __init__.py
│   ├── db.py                 # CRUD for annotations + replies
│   ├── routes.py             # POST /annotations, GET /annotations, etc.
│   ├── components.py         # Annotation popover, thread UI components
│   └── notifications.py      # Trigger notifications on reply/mention
├── notifications/
│   ├── __init__.py
│   ├── db.py
│   ├── routes.py             # GET /notifications, POST /notifications/{id}/read
│   ├── components.py         # Bell icon + dropdown
│   └── websocket.py          # Push notifications via WebSocket
├── admin/                     # Expand existing admin_dashboard.py
│   ├── __init__.py
│   ├── dashboard.py          # Refactored main dashboard
│   ├── analytics.py          # Usage graphs + tables
│   ├── cost_tracking.py      # LLM cost dashboard
│   ├── health.py             # System health aggregator
│   ├── audit_viewer.py       # Enhanced audit log viewer
│   └── users_admin.py        # Extended user admin (moved from auth/users.py)
├── observability/
│   ├── __init__.py
│   ├── logging.py            # Structured JSON logging setup
│   ├── sentry.py             # Sentry initialization
│   └── metrics.py            # Request timing, job counts, etc.
```

---

## 3. Inline Annotation System

### 3.1 Annotation targets

Annotations can attach to any of these targets:

| Target type | target_id format | Example |
|-------------|------------------|---------|
| `draft` | UUID | `550e8400-...` |
| `draft_section` | `{draft_id}#section-{n}` | `550e8400-...#section-3` |
| `impact_report_item` | `{report_id}#conflict-{n}` or `#gap-{n}`, `#eu-{n}` | `abc...#conflict-2` |
| `graph_node` | ontology URI | `https://data.riik.ee/ontology/estleg#TsiviilS_Par_123` |
| `chat_message` | message UUID | `660e8400-...` |
| `drafting_clause` | `{session_id}#clause-{n}` | `770e8400-...#clause-5` |

### 3.2 Annotation UI

**Trigger:** Every annotatable element has a small "💬 Add comment" button that appears on hover (or via keyboard shortcut `C` when focused).

**Popover:** Clicking opens a popover anchored to the element:
- Textarea for comment
- Save / Cancel buttons
- If comments already exist, shows thread with replies
- "Resolve" toggle for owner
- @mentions via autocomplete (simple user list, no fuzzy matching for now)

**Components:**

```python
def AnnotationButton(target_type: str, target_id: str) -> FT:
    return Button(
        Icon("message-circle"),
        variant="ghost", size="sm",
        hx_get=f"/annotations?target_type={target_type}&target_id={target_id}",
        hx_target="#annotation-popover",
        hx_trigger="click",
    )

def AnnotationThread(annotations: list[Annotation]) -> FT:
    return Div(
        *[AnnotationItem(a) for a in annotations],
        Form(
            Textarea(name="content", placeholder="Lisa kommentaar..."),
            Button("Saada", variant="primary"),
            hx_post="/annotations",
            hx_swap="beforeend",
            hx_target="#annotation-thread",
        ),
        id="annotation-thread",
    )
```

### 3.3 Routes

- `GET /annotations?target_type=X&target_id=Y` — list annotations for a target (HTMX partial)
- `POST /annotations` — create annotation (HTMX form submit)
- `POST /annotations/{id}/reply` — add reply
- `POST /annotations/{id}/resolve` — mark resolved
- `DELETE /annotations/{id}` — delete (own only, or admin)
- `GET /annotations/mine` — list annotations created by or replied to by current user

### 3.4 Permissions

- Create annotation: any user with read access to the target (org-scoped)
- Edit/delete own annotation: always
- Edit/delete any annotation: org_admin, admin
- Resolve: owner of the target resource (e.g., draft owner) or any reviewer/org_admin/admin

---

## 4. Notification System

### 4.1 Notification types

| Type | Trigger | Target user |
|------|---------|-------------|
| `annotation_reply` | Someone replies to your annotation or mentions you | annotation author + mentioned users |
| `analysis_done` | Phase 2 impact analysis finishes for a draft you uploaded | draft owner |
| `draft_shared` | A draft is uploaded in your org | org members with `drafter` or `reviewer` role |
| `drafter_session_complete` | AI law drafter session completes | session owner |
| `sync_failed` | Ontology sync pipeline fails | all admins |
| `cost_alert` | Org LLM cost reaches 80% of monthly budget | org admins |

### 4.2 Notification delivery

**In-app (primary):**
- Bell icon in TopBar with unread count badge
- Dropdown shows recent notifications
- Each notification links to the relevant resource
- Real-time via WebSocket `/ws/notifications`

**Email (opt-in, future):**
- User preferences control per-type email delivery
- Email template with deep links
- Not built in Phase 4 — table schema supports it, implementation in Phase 6+

### 4.3 WebSocket push

```python
# Global notification sender
async def notify(user_id: UUID, notification: Notification):
    save_notification(notification)
    await notification_ws.send_to_user(user_id, {
        "type": "notification",
        "data": notification.to_dict(),
    })
```

### 4.4 TopBar integration

```python
def TopBar(user: UserDict, theme: str, unread_count: int = 0) -> FT:
    return Div(
        Logo(),
        Nav(...),
        NotificationBell(unread_count),
        ThemeToggle(theme),
        UserMenu(user),
        cls="top-bar",
    )
```

The `unread_count` is loaded on every page render from the DB. WebSocket updates it live.

---

## 5. Admin Dashboard Expansion

Phase 1 has a basic `/admin` dashboard. Phase 4 expands it significantly.

### 5.1 Dashboard layout

```
┌─ /admin ─────────────────────────────────────────────┐
│ System Health                    Sync Status        │
│ ┌──────────┐ ┌──────────┐       ┌─────────────┐     │
│ │ Jena: OK │ │ PG: OK   │       │ Last: 2h ago│     │
│ │ Tika: OK │ │ App: OK  │       │ Status: ✓   │     │
│ └──────────┘ └──────────┘       │ Triples: 1M │     │
│                                  └─────────────┘     │
│ ──────────────────────────────────────────────────── │
│ LLM Cost This Month              Top Users           │
│ [bar chart per day]              1. user@x.ee  120h  │
│ Total: €234.50 / €500 budget     2. ...              │
│ ──────────────────────────────────────────────────── │
│ Recent Errors (Sentry)           Recent Audit Events │
│ 5 errors in last 24h             [scrollable list]   │
│ [link to Sentry]                                     │
│ ──────────────────────────────────────────────────── │
│ Quick Links                                          │
│ [Users] [Orgs] [Jobs] [Sync] [Audit] [Settings]     │
└──────────────────────────────────────────────────────┘
```

### 5.2 System health

Aggregates health across all services:

```python
async def get_system_health() -> SystemHealth:
    return SystemHealth(
        app=True,  # if we're responding, we're up
        postgres=await check_postgres(),
        jena=await check_jena(),
        tika=await check_tika(),
        llm=await check_llm_api(),         # ping Claude
        embeddings=await check_voyage(),    # ping Voyage
        queue_depth=get_queue_depth(),
        queue_health=get_queue_depth() < 100,
    )
```

### 5.3 Sync status panel

Shows:
- Last sync timestamp + status
- Entity count
- Failed syncs in last 7 days
- "Trigger sync now" button (admin only)
- Link to sync logs page

### 5.4 Usage analytics

**Daily active users** — users with any audit event in the day
**Feature usage** — counts per action type per day (uploads, chat messages, drafter sessions)
**Top users** — most active users in the last 30 days
**Per-org stats** — usage breakdown by organization

Data source: the `usage_daily` materialized view, refreshed hourly via a background job.

### 5.5 LLM cost dashboard

- Burn chart: cost per day, color-coded by feature (chat, extract, drafter)
- Monthly total vs. budget (with visual warning at 80%)
- Top consumers: users with highest cost this month
- Per-org breakdown
- Drill-down: click a day → see individual `llm_usage` rows

### 5.6 Audit log viewer (enhanced)

Upgrades over Phase 1's basic viewer:
- Filters: user, action type, date range, org, IP
- Full-text search on `detail` JSONB
- Export selected rows to CSV
- Drill-down: click an event → see related events for the same user/resource
- Saved filter views ("Recent logins", "Failed auth", "Draft deletions")

### 5.7 Job monitor

- List of background jobs with status, type, attempts, duration
- Filter by status (running, failed, completed)
- Retry failed jobs manually
- Cancel running jobs (cooperative — sets a flag)
- Stats: avg duration per job type, throughput

---

## 6. Observability

### 6.1 Structured logging

Replace all `logger.info(...)` calls with structured format:

```python
import structlog

logger = structlog.get_logger(__name__)

logger.info("draft_uploaded",
    draft_id=str(draft.id),
    user_id=str(user.id),
    org_id=str(user.org_id),
    filename=draft.filename,
    size=draft.file_size,
)
```

Output (JSON):
```json
{"event": "draft_uploaded", "draft_id": "...", "user_id": "...", "timestamp": "...", "level": "info", "logger": "app.documents.upload"}
```

Coolify aggregates stdout logs; we just need to emit them as JSON.

### 6.2 Sentry integration

- SDK: `sentry-sdk` with FastAPI/Starlette integration
- Captures uncaught exceptions + explicit `capture_exception()` calls
- Environment: `SENTRY_DSN` from Coolify secrets
- Release tracking via git SHA
- Source maps for JS errors (D3 explorer)
- Privacy: PII scrubbing configured to strip email, names, tokens from error context
- Free tier (5k events/month) sufficient for 5-50 users

Setup:
```python
# app/observability/sentry.py
import sentry_sdk
from sentry_sdk.integrations.starlette import StarletteIntegration

def init_sentry():
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        return
    sentry_sdk.init(
        dsn=dsn,
        integrations=[StarletteIntegration()],
        environment=os.environ.get("APP_ENV", "development"),
        release=os.environ.get("GIT_SHA", "unknown"),
        traces_sample_rate=0.1,
        send_default_pii=False,
        before_send=scrub_pii,
    )
```

### 6.3 Metrics (simple)

Lightweight metrics stored in a single Postgres table:

```sql
CREATE TABLE metrics (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    value           DOUBLE PRECISION NOT NULL,
    labels          JSONB,
    recorded_at     TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_metrics_name_time ON metrics(name, recorded_at DESC);
```

Tracked:
- Request duration per route
- Job execution time per type
- LLM call latency per feature
- SPARQL query duration
- RAG retrieval latency

Admin dashboard has a "Performance" tab showing trends over time.

**Why not Prometheus:** Overkill for our scale. Postgres works fine, queries are simple.

---

## 7. TARA SSO Preparation (code only, not activated)

Per Q18 decision: defer activation to post-Phase 5. But we ensure the architecture is ready.

- `AuthProvider` interface already exists (Phase 1)
- Create `TARAAuthProvider` stub in `app/auth/tara_provider.py` with OIDC client setup (no active routes)
- Document the activation steps in `docs/tara-activation.md`
- Add env var placeholders to `.env.example`

---

## 8. Dependencies

New Python packages:
- `structlog` — structured logging
- `sentry-sdk[starlette]` — error tracking

---

## 9. Security & Compliance

- Annotations are subject to the same org-scoping as drafts — no cross-org leakage
- Delete cascade: when a draft is deleted, all its annotations are deleted
- Audit log retains annotation create/delete events even after deletion
- Notifications never include sensitive content (e.g., draft text excerpts) — only metadata and deep links
- Admin dashboard access requires `admin` role; cost data requires `admin` only (not `org_admin`)

---

## 10. GitHub Issues

### Epic: Inline Annotations
1. Create `annotations` and `annotation_replies` tables (migration 005)
2. Implement annotation CRUD module
3. Implement annotation routes (create, list, reply, resolve, delete)
4. Create annotation popover component
5. Create annotation thread display component
6. Add annotation button to draft sections
7. Add annotation button to impact report items
8. Add annotation button to graph nodes (explorer integration)
9. Add annotation button to chat messages
10. Add annotation button to drafter clauses
11. Implement @mention autocomplete

### Epic: Notifications
12. Create `notifications` table
13. Implement notification CRUD
14. Implement notification routes
15. Create notification bell + dropdown components
16. Wire up WebSocket delivery
17. Create `notify()` helper used by all notification sources
18. Wire up annotation_reply notification
19. Wire up analysis_done notification (Phase 2 hook)
20. Wire up draft_shared notification
21. Wire up drafter_session_complete notification
22. Wire up sync_failed and cost_alert (admin notifications)
23. Implement mark-as-read (single + all)

### Epic: Admin Dashboard Expansion
24. Refactor existing `admin_dashboard.py` into `app/admin/` package
25. Implement system health aggregator
26. Implement sync status panel
27. Create `usage_daily` materialized view + refresh job
28. Implement usage analytics page
29. Implement LLM cost dashboard with burn chart
30. Enhance audit log viewer (filters, search, export)
31. Implement job monitor page
32. Add saved filter views to audit viewer
33. Implement metrics table + tracking

### Epic: Observability
34. Set up structured logging with structlog
35. Migrate all logger calls to structured format
36. Integrate Sentry SDK
37. Configure PII scrubbing
38. Add release tracking via git SHA
39. Add Sentry DSN to Coolify secrets
40. Create admin "Errors" link to Sentry dashboard

### Epic: Metrics & Performance
41. Create `metrics` table
42. Add request timing middleware
43. Add job execution time tracking
44. Add LLM call latency tracking
45. Add SPARQL query duration tracking
46. Create performance tab on admin dashboard

### Epic: TARA Preparation
47. Create `TARAAuthProvider` stub (not activated)
48. Document activation steps
49. Add TARA env var placeholders

**Total: 49 issues for Phase 4**

---
