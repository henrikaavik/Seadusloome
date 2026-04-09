# Seadusloome — Estonian Legal Ontology Advisory Software

Advisory software that helps Estonian government officials in the law creation process. Upload a draft law or describe legislative intent in natural language — the system maps it against the existing legal framework, showing connections, conflicts, and impacts.

**[Kanban Board](https://github.com/users/henrikaavik/projects/2)**

## What It Does

A government official uploads a draft law (or describes what a new law should achieve). The system maps it against:

- **615** enacted Estonian laws
- **22,832** draft legislation items
- **12,137** Supreme Court decisions
- **33,242** EU legal acts
- **22,290** EU court decisions

The result: an interactive graph showing exactly how the draft connects to and impacts the existing legal framework, with conflict detection, gap analysis, EU compliance checking, and AI-powered drafting assistance.

## Architecture

```mermaid
graph TB
    subgraph Frontend["Frontend (Browser)"]
        D3["D3.js Graph Explorer"]
        HTMX["HTMX Dynamic UI"]
        Chat["AI Chat Interface"]
    end

    subgraph API["API Layer (FastHTML / Starlette)"]
        REST["REST Endpoints"]
        WS["WebSocket"]
        Auth["JWT Auth + RBAC"]
    end

    subgraph Core["Application Core"]
        QE["Ontology Query Engine"]
        DA["Document Analyzer"]
        IM["Impact Mapper"]
        CD["Conflict Detector"]
        LD["AI Law Drafter"]
    end

    subgraph Storage["Storage Layer"]
        Jena["Apache Jena Fuseki\n(SPARQL Triplestore)"]
        PG["PostgreSQL 16\n+ pgvector"]
        LLM["Pluggable LLM\n(Claude API)"]
    end

    subgraph Pipeline["Data Pipeline"]
        GH["GitHub\n(JSON-LD Source)"]
        Sync["Sync Worker"]
    end

    Frontend --> API
    API --> Core
    Core --> Jena
    Core --> PG
    Core --> LLM
    GH -->|webhook| Sync
    Sync -->|"JSON-LD → RDF"| Jena

    subgraph Deployment["Coolify (Self-hosted PaaS)"]
        Traefik["Traefik Reverse Proxy\nTLS + Let's Encrypt"]
    end

    Traefik --> API
```

## Database Schema

```mermaid
erDiagram
    organizations ||--o{ users : "has members"
    users ||--o{ sessions : "has sessions"
    users ||--o{ audit_log : "generates"
    users ||--o{ bookmarks : "saves"

    organizations {
        uuid id PK
        text name UK
        text slug UK
        timestamptz created_at
    }

    users {
        uuid id PK
        uuid org_id FK
        text email UK
        text password_hash
        text full_name
        text role "drafter | reviewer | org_admin | admin"
        timestamptz created_at
        timestamptz last_login_at
    }

    sessions {
        uuid id PK
        uuid user_id FK
        text token_hash
        timestamptz created_at
        timestamptz expires_at
    }

    audit_log {
        bigserial id PK
        uuid user_id FK
        text action
        jsonb detail
        timestamptz created_at
    }

    sync_log {
        bigserial id PK
        timestamptz started_at
        timestamptz finished_at
        text status "running | success | failed"
        integer entity_count
        text error_message
    }

    bookmarks {
        uuid id PK
        uuid user_id FK
        text entity_uri
        text label
        timestamptz created_at
    }
```

## Ontology Data Model

```mermaid
graph LR
    subgraph enacted["Enacted Law"]
        LP["LegalProvision"]
        TC["TopicCluster"]
        LC["LegalConcept"]
        PV["ProvisionVersion"]
        AM["Amendment"]
    end

    subgraph draft["Draft Legislation"]
        DL["DraftLegislation"]
        DV["DraftVersion"]
        DI["DraftingIntent\n(VTK)"]
    end

    subgraph court["Court Decisions"]
        CD["CourtDecision"]
    end

    subgraph eu_leg["EU Legislation"]
        EU["EULegislation"]
    end

    subgraph eu_court["EU Court Decisions"]
        ECD["EUCourtDecision"]
    end

    LP -->|hasTopic| TC
    LP -->|definesConcept| LC
    LP -->|hasVersion| PV
    PV -->|amendedBy| AM
    PV -->|previousVersion| PV

    DI -->|resultsInDraft| DL
    DL -->|hasVersion| DV
    DV -->|previousVersion| DV
    DL -->|referencedLaw| LP

    CD -->|interpretsProvision| LP
    LP -->|implementsEU| EU
    ECD -->|interpretsAct| EU
    CD -->|citesEUCase| ECD
    DL -->|transposesDirective| EU
```

## Legislative Lifecycle

```mermaid
graph LR
    VTK["VTK\n(Intent)"]
    V1["Draft v1\n(Submitted)"]
    V2["Draft v2\n(1st Reading)"]
    V3["Draft v3\n(2nd Reading)"]
    EN["Enacted\nProvisionVersion N+1"]
    EN2["ProvisionVersion N+2\n(Later Amendment)"]

    VTK --> V1 --> V2 --> V3 --> EN --> EN2

    style VTK fill:#a78bfa,stroke:#7c3aed,color:#fff
    style V1 fill:#a78bfa,stroke:#7c3aed,color:#fff
    style V2 fill:#a78bfa,stroke:#7c3aed,color:#fff
    style V3 fill:#a78bfa,stroke:#7c3aed,color:#fff
    style EN fill:#38bdf8,stroke:#0284c7,color:#fff
    style EN2 fill:#38bdf8,stroke:#0284c7,color:#fff
```

## Sync Pipeline

```mermaid
flowchart LR
    GH["GitHub Push"] --> WH["Webhook"]
    WH --> Clone["Clone/Pull\nOntology Repo"]
    Clone --> Parse["Parse\nJSON-LD Files"]
    Parse --> Convert["Convert to\nRDF/Turtle"]
    Convert --> Validate["SHACL\nValidation"]
    Validate -->|Pass| Load["Bulk Load\ninto Jena"]
    Validate -->|Fail| Reject["Reject\n(Keep Previous)"]
    Load --> Notify["WebSocket\nNotify Clients"]

    style Reject fill:#ef4444,stroke:#dc2626,color:#fff
    style Load fill:#22c55e,stroke:#16a34a,color:#fff
```

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Server | FastHTML (Python 3.13) |
| Frontend | D3.js + HTMX + Vanilla JS |
| Triplestore | Apache Jena Fuseki (SPARQL) |
| Database | PostgreSQL 16 + pgvector |
| AI | Pluggable LLM (Claude API primary) |
| Embeddings | multilingual-e5-large / EstBERT |
| Auth | JWT (TARA SSO-ready via OIDC) |
| Deployment | Coolify on Hetzner VPS |
| CI/CD | GitHub Actions + Coolify webhooks |
| Linting | ruff + pyright |
| Package Manager | uv |

## Deploying

Every push to `main` runs lint + type-check + tests via GitHub Actions. On
green CI, a `deploy` job fires a Coolify webhook which rebuilds and
redeploys the container.

**One-time setup (required after cloning the repo into a new GitHub org):**

1. In Coolify, open the Seadusloome application → **Deployments** → **Webhooks** → copy the *Deploy* URL.
2. In GitHub, go to **Settings → Secrets and variables → Actions → New repository secret**.
3. Name: `COOLIFY_DEPLOY_HOOK_URL` · Value: *the URL from step 1*.

Without the secret, the CI `deploy` job gracefully no-ops (it logs a
skip notice and exits `0`), so the rest of the pipeline still passes.
You can still trigger manual deploys from the Coolify UI.

## Development Phases

```mermaid
gantt
    title Development Roadmap
    dateFormat YYYY-MM-DD
    axisFormat %b %Y

    section Phase 1
    Core Infrastructure + Ontology Explorer :p1, 2026-04-09, 8w

    section Phase 2
    Document Upload + Impact Analysis      :p2, after p1, 10w

    section Phase 3
    AI Advisory Chat + AI Law Drafter      :p3, after p2, 12w

    section Phase 4
    Collaboration + Admin                  :p4, after p1, 6w

    section Phase 5
    Public API + MCP Server                :p5, after p3, 6w
```

| Phase | Scope | Dependencies |
|-------|-------|-------------|
| 1 | Core Infrastructure + Ontology Explorer | None |
| 2 | Document Upload + Impact Analysis | Phase 1 |
| 3 | AI Advisory Chat + AI Law Drafter | Phase 2 |
| 4 | Collaboration + Admin | Phase 1 |
| 5 | Public API + MCP Server | Phase 3 |

## Modules

1. **Core Infrastructure** — FastHTML scaffolding, PostgreSQL, Jena Fuseki, sync pipeline, JWT auth, Coolify deployment
2. **Ontology Explorer** — D3.js interactive graph with SPARQL-backed lazy loading, timeline view, version history
3. **Document Upload** — .docx/.pdf parsing, Estonian legal NLP, temporary named graph integration
4. **Impact Analysis** — SPARQL traversal, conflict detection, EU compliance, gap analysis
5. **AI Advisory Chat** — RAG pipeline, ontology-aware prompting, streaming Estonian responses
6. **AI Law Drafter** — Intent-to-draft pipeline: VTK or full law from natural language description
7. **User Management** — Organizations, roles, shared workspaces, audit logging
8. **Public API + MCP Server** — REST API + MCP protocol for third-party integrations (post-MVP)
9. **Monitoring & Admin** — Health dashboard, usage analytics, cost tracking

## Local Development

```bash
# Prerequisites: Python 3.13, uv, Docker

# Install dependencies
uv sync

# Start Jena Fuseki + PostgreSQL
docker compose -f docker/docker-compose.yml up -d

# Run migrations
uv run scripts/migrate.py

# Sync ontology data (first run)
uv run scripts/sync.py

# Start dev server
uv run app/main.py
```

## Data Sources

- **Ontology:** [github.com/henrikaavik/estonian-legal-ontology](https://github.com/henrikaavik/estonian-legal-ontology)
- **Enacted Laws:** [Riigi Teataja](https://www.riigiteataja.ee)
- **Draft Legislation:** [Eelnõude Infosüsteem (EIS)](https://eelnoud.valitsus.ee)
- **Court Decisions:** [Riigikohus](https://www.riigikohus.ee)
- **EU Legislation:** [EUR-Lex](https://eur-lex.europa.eu)
- **EU Court Decisions:** [CURIA](https://curia.europa.eu)

## License

Proprietary. All rights reserved.
