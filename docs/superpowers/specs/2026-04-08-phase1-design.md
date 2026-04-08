# Phase 1 Design: Core Infrastructure + Ontology Explorer

**Status:** Approved
**Date:** 2026-04-08
**Approach:** Infra-first, then Explorer (Approach A)

---

## 1. Project Structure & Tooling

```
Seadusloome/
├── app/                    # FastHTML application
│   ├── __init__.py
│   ├── main.py             # FastHTML app entry point
│   ├── auth/               # JWT auth module (Authlib, OIDC-ready)
│   ├── ontology/           # SPARQL query engine, Jena client
│   ├── sync/               # GitHub → RDF → Jena pipeline
│   ├── explorer/           # D3 visualization routes + data endpoints
│   ├── static/             # JS (D3), CSS
│   └── templates/          # FastHTML components/pages
├── scripts/                # CLI utilities (sync trigger, DB migrations)
├── tests/
├── docker/
│   ├── Dockerfile          # FastHTML app
│   └── docker-compose.yml  # Local dev (Jena + Postgres + app)
├── docs/
├── pyproject.toml          # uv + Python 3.13
└── .github/workflows/      # CI: lint, test
```

- **uv** for dependency management
- **Python 3.13**
- **docker-compose** for local dev; Coolify for production
- FastHTML serves everything — no separate frontend build step

---

## 2. Coolify Deployment Architecture

Four services on Coolify, single VPS:

| Service | Type | Access | Persistence |
|---------|------|--------|-------------|
| `seadusloome-app` | Git-deploy (Dockerfile) | Public: `seadusloome.sixtyfour.ee` | None (stateless) |
| `seadusloome-jena` | Docker image (Apache Jena Fuseki) | Internal only | Persistent volume for TDB2 store |
| `seadusloome-postgres` | Coolify-managed PostgreSQL 16 | Internal only | Coolify-managed backups |
| `seadusloome-sync` | Scheduled container (cron) | Internal only | None (reads GitHub, writes to Jena) |

- Traefik handles TLS (Let's Encrypt auto-renewal) and routing
- Jena and Postgres on internal Docker network — not exposed publicly
- App connects to Jena via `http://seadusloome-jena:3030/` and Postgres via internal hostname
- GitHub webhook triggers sync on push to ontology repo; daily cron as fallback
- Environment secrets stored in Coolify's encrypted secrets
- DNS: A record for `seadusloome.sixtyfour.ee` → VPS IP

---

## 3. Sync Pipeline (GitHub → Jena)

**Flow:**
1. Triggered by GitHub webhook (on push to `estonian-legal-ontology`) or daily cron
2. Clones/pulls the ontology repo
3. Reads `INDEX.json` to discover all law files
4. Parses `combined_ontology.jsonld` + individual `_peep.json` files
5. Converts JSON-LD → RDF/Turtle using `rdflib`
6. Validates against SHACL shapes (reuses the repo's existing shapes)
7. Bulk-loads into Jena Fuseki via Graph Store Protocol (`POST /data`)
8. Sends WebSocket notification to connected frontends to refresh

**Key decisions:**
- Full reload (drop graph, reload all) — ~90k entities completes in under a minute. Incremental diffing deferred.
- SHACL validation failure rejects the load; previous graph stays. Alert logged.
- Base ontology in default graph. Uploaded drafts (Phase 2) get session-scoped named graphs.

**Dependencies:** `rdflib`, `pyshacl`, `requests`

---

## 4. PostgreSQL Schema

Postgres handles app state — not ontology data (that's Jena).

```sql
organizations (
    id              UUID PRIMARY KEY,
    name            TEXT UNIQUE NOT NULL,
    slug            TEXT UNIQUE NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT now()
)

users (
    id              UUID PRIMARY KEY,
    org_id          UUID REFERENCES organizations(id),
    email           TEXT UNIQUE NOT NULL,
    password_hash   TEXT NOT NULL,
    full_name       TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('drafter', 'reviewer', 'org_admin', 'admin')),
    created_at      TIMESTAMPTZ DEFAULT now(),
    last_login_at   TIMESTAMPTZ
)

sessions (
    id              UUID PRIMARY KEY,
    user_id         UUID REFERENCES users(id),
    token_hash      TEXT NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL
)

audit_log (
    id              BIGSERIAL PRIMARY KEY,
    user_id         UUID REFERENCES users(id),
    action          TEXT NOT NULL,
    detail          JSONB,
    created_at      TIMESTAMPTZ DEFAULT now()
)

sync_log (
    id              BIGSERIAL PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL,
    finished_at     TIMESTAMPTZ,
    status          TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed')),
    entity_count    INTEGER,
    error_message   TEXT
)
```

- `pgvector` extension installed but unused until Phase 3 (RAG)
- Migrations: numbered SQL files (`001_initial.sql`, etc.)
- Audit log captures all user actions for government compliance

---

## 5. Authentication, User Management & Dashboards

### Auth flow
- Login: `POST /auth/login` — email + password → bcrypt verify → JWT (access token, 1h) + refresh token (in `sessions`, 7d)
- JWT stored in `HttpOnly` cookie (browser-friendly for HTMX)
- Auto-refresh via middleware when access token expires but refresh token valid
- Logout: delete session, clear cookie

### Auth module design
- Abstract `AuthProvider` interface: `authenticate()`, `get_current_user()`, `logout()`
- `JWTAuthProvider` for Phase 1
- `TARAAuthProvider` (OIDC) swaps in later — config change, no route changes

### Organization accounts
- Organizations represent ministries/departments
- Each user belongs to one organization
- Org admins manage their own org's users
- System admins manage all orgs and users

### Roles
- `drafter` — upload, analyze, view org members' work
- `reviewer` — read-only + commenting within org
- `org_admin` — manage users within own org + drafter capabilities
- `admin` — system-wide: all orgs, all users, system config

### Personal dashboard (per user)
- Recent activity (searches, viewed entities, uploads)
- Saved/bookmarked entities from the ontology explorer
- Quick access to own analyses (Phase 2+)

### Admin dashboard
- User & organization CRUD
- Sync pipeline status (last run, success/fail, entity count)
- System health (Jena status, Postgres status)
- Audit log viewer with filters
- User activity summary per org

### User management pages
- Org admin: invite/remove users in own org, assign roles (drafter/reviewer)
- System admin: create/edit/delete orgs, assign org admins, full user CRUD

**Dependencies:** `PyJWT`, `bcrypt`, `authlib`

---

## 6. D3 Ontology Explorer

### Data loading — lazy SPARQL
- Initial view: category-level overview — 5 domain nodes with aggregate counts and inter-domain edges
- Click category → top entities within it (paginated, top 50 by connection count)
- Click entity → 1-hop neighbors expand
- Search → SPARQL → results appear on graph with connections
- Never more than ~500 nodes rendered at once

### API endpoints
- `GET /api/explorer/overview` — category aggregates for initial view
- `GET /api/explorer/category/{name}` — entities within a category (paginated)
- `GET /api/explorer/entity/{id}` — entity detail + 1-hop neighbors
- `GET /api/explorer/search?q=...` — full-text search
- `WS /ws/explorer` — real-time updates on sync completion

### Visual features
- Force-directed layout with glow effects, category colors (from demo)
- Hover: highlight connected nodes, tooltip with metadata
- Click: pin/unpin, open detail panel (right sidebar)
- Controls: reheat, toggle labels, group by category, reset
- Zoom/pan with smooth transitions on subgraph expansion
- Cross-category edges highlighted in gold

### Timeline view
- Temporal slider at bottom — select a date
- Graph re-renders for entities valid at that date (`validFrom`/`validUntil`)
- Version history panel: click provision → amendment chain with dates, diffs
- Legislative lifecycle view: VTK → Draft readings → Enacted flow

### Detail panel (right sidebar)
- Full metadata (title, identifier, dates, status)
- Connected entities grouped by relationship type
- For provisions: version history timeline
- For drafts: legislative phase progress
- Links to source (Riigi Teataja, EUR-Lex)

---

## 7. CI/CD & Development Workflow

### Local development
- `docker compose up` — Jena Fuseki + PostgreSQL
- `uv run app/main.py` — FastHTML dev server with hot reload
- `uv run scripts/sync.py` — manual ontology sync against local Jena

### CI (GitHub Actions)
- On push/PR: lint (`ruff`), type check (`pyright`), tests (`pytest`)
- No deployment from CI — Coolify handles deployment via its own webhook

### Production deployment
- Push to `main` → Coolify webhook → build Dockerfile → deploy → Traefik routes traffic
- Zero-downtime: new container built before old one stopped

### Testing strategy
- Unit tests: sync pipeline, auth module, SPARQL query builders
- Integration tests: against local Jena + Postgres via docker compose (not mocks)
- No frontend JS tests in Phase 1

### Linting/formatting
- `ruff` for linting + formatting
- `pyright` for type checking (strict mode)
