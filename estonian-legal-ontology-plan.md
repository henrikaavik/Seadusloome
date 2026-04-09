# Estonian Legal Ontology — Advisory Software for Law Creation

**Architecture & Development Plan**
Version 1.2 | April 2026 | CONFIDENTIAL

---

## 1. Executive Summary

This document defines the architecture and phased development plan for an advisory software system that assists Estonian government officials in the law creation process. The system uses the Estonian Legal Ontology as its knowledge base and provides intelligent analysis, visualization, AI-powered guidance, and AI-driven law drafting from scratch.

The core value proposition: when a government official uploads a draft law — or describes the *intent* of a new law in natural language — the system maps it against 615 enacted laws, 22,832 draft legislation items, 12,137 Supreme Court decisions, 33,242 EU legal acts, and 22,290 EU court decisions (total ~55,500 EU items), showing exactly how the new draft connects to and impacts the existing legal framework. The system tracks the full temporal history of legal provisions and the complete legislative lifecycle from VTK (Väljatöötamiskavatsus) through drafting to enactment, so users always have the historical context they need.

### 1.1 Project Parameters

| Parameter | Value |
|-----------|-------|
| Target audience | Estonian government officials (ministry staff, parliamentary drafters) |
| Primary language | Estonian (UI and legal text analysis) |
| Expected users | 5–50 concurrent (department-level) |
| Deployment | Cloud SaaS via **Coolify** (self-hosted PaaS on VPS) |
| AI backend | Pluggable LLM layer (Claude and Codex both supported; Claude is the default) |
| Ontology source | github.com/henrikaavik/estonian-legal-ontology |
| Visualization | D3.js force-directed graph engine |
| Future access | REST API + MCP server for third-party integrations (post-MVP) |

---

## 2. Architecture Overview

The system follows a layered architecture with clear separation between the ontology layer, the application logic, the AI reasoning engine, and the frontend visualization. This allows each layer to evolve independently and makes it possible to develop modules in parallel across the team.

### 2.1 High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Frontend (Browser)                                             │
│  D3.js visualization + Estonian UI + document upload + AI chat  │
└────────────────────────────┬────────────────────────────────────┘
                             │ HTTPS / WebSocket
┌────────────────────────────▼────────────────────────────────────┐
│  API Layer (FastHTML / Starlette)                                │
│  REST endpoints + WebSocket + session management + auth         │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  Future: Public REST API + MCP Server (post-MVP)        │    │
│  └─────────────────────────────────────────────────────────┘    │
└────────────────────────────┬────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────┐
│  Application Core                                               │
│  Ontology query engine │ Document analyzer │ Impact mapper       │
│  Conflict detector │ EU compliance │ AI Law Drafter              │
└──────┬──────────────────┬──────────────────┬────────────────────┘
       │                  │                  │
┌──────▼──────┐  ┌────────▼───────┐  ┌──────▼──────────┐
│  Apache     │  │  PostgreSQL    │  │  AI Layer       │
│  Jena       │  │  + pgvector    │  │  Pluggable LLM  │
│  Fuseki     │  │                │  │  + RAG pipeline  │
│  (SPARQL)   │  │  (app state,   │  │  + law drafting │
│             │  │   vectors,     │  │    engine       │
│             │  │   chat history)│  │                 │
└──────▲──────┘  └────────────────┘  └─────────────────┘
       │
┌──────┴──────────────────────────────────────────────────────────┐
│  Data Pipeline: GitHub webhook → JSON-LD → RDF → Jena loader   │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  Coolify (Self-hosted PaaS)                                     │
│  Container orchestration │ SSL/TLS │ Deployments │ Monitoring   │
│  Reverse proxy (Traefik) │ Backups │ Secrets management         │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 Why Apache Jena (Not Just GitHub)

Your GitHub repository is an excellent source of truth for the ontology definitions. However, for a production advisory system, relying solely on GitHub has critical limitations:

- **Query performance:** SPARQL queries across 90,000+ entities complete in milliseconds on Jena Fuseki, versus loading and traversing JSON-LD files which would take seconds.
- **Relationship traversal:** Finding all laws connected to a draft within 3 hops requires a graph database. Jena handles this natively with SPARQL property paths.
- **Named-graph integration:** When a user uploads a draft, the system creates a dedicated named graph in Jena. This lets the draft participate in SPARQL queries without modifying the base ontology. Draft named graphs persist until the owner explicitly deletes them, with mandatory compensating controls (see §9 and §11 for retention policy and security).
- **Inference:** Jena supports OWL/RDFS reasoning, which can derive implicit relationships (e.g., if Law A implements Directive B, and Directive B is superseded by Directive C, the system can flag this).

The recommended approach: GitHub remains the canonical source. A sync pipeline converts JSON-LD to RDF and loads it into Jena Fuseki on a schedule (or triggered by GitHub webhooks). This gives you version control on the source data and query performance on the serving side.

### 2.3 Why Coolify for Deployment

Coolify replaces the traditional Docker Compose → Kubernetes migration path with a self-hosted PaaS that provides most of what you need out of the box:

- **Built-in reverse proxy (Traefik):** Automatic SSL/TLS certificates via Let's Encrypt. No separate nginx container needed.
- **Git-push deployments:** Connect your repo, push to deploy. Supports Dockerfiles and Docker Compose natively.
- **Service management:** Run Jena Fuseki and PostgreSQL as persistent Coolify services with managed volumes, backups, and health checks.
- **Secrets management:** Environment variables and secrets stored encrypted, injected at deploy time — critical for LLM API keys and database credentials.
- **Monitoring dashboard:** Built-in container metrics, logs, and resource usage. Reduces the need for a custom admin dashboard in early phases.
- **Scaling:** Horizontal scaling when needed, without migrating to Kubernetes. Coolify supports multiple servers and load balancing.
- **Cost-effective:** Runs on a single VPS (Hetzner, DigitalOcean) — significantly cheaper than managed Kubernetes for your 5–50 user scale.

This means the deployment section of the tech stack simplifies considerably: instead of Docker Compose → nginx → manual SSL → eventual K8s migration, you get a production-ready deployment platform from day one.

### 2.4 Ontology Versioning Model

Legal systems are inherently temporal — laws change, court interpretations evolve, and understanding *when* a provision was in force is as important as understanding *what* it says. The system implements two complementary versioning mechanisms:

**Temporal versioning of enacted laws:**

When a law is amended, the previous version doesn't disappear. Court decisions rendered under the old version still reference it, draft legislation may have been written against it, and understanding the change itself is often as important as the current text. The ontology models this with:

- `estleg:ProvisionVersion` — a snapshot of a specific provision at a point in time, with `validFrom`, `validUntil`, and `amendedBy` properties
- `estleg:LegalProvision` becomes a persistent identity node representing the *concept* of that provision across all its versions, linked to ProvisionVersion nodes via `hasVersion`
- `estleg:Amendment` — represents the amending act itself, linking the old version to the new version and recording what changed

This enables critical queries like "give me the version of TsiviilS § 123 that was in force on 2023-06-15" or "show me all amendments to this provision and the court decisions that referenced each version."

**Legislative lifecycle versioning (VTK → Draft → Enacted Law):**

The Estonian legislative process follows a defined pipeline: Väljatöötamiskavatsus (VTK) → Draft through Riigikogu readings → Enacted law. Each stage produces versioned documents that the ontology tracks:

- `estleg:DraftingIntent` (VTK) — the formal statement of legislative intent, with `consultationStatus`, `proposedBy` (ministry), `publicationDate`, and `resultsInDraft` linking to the DraftLegislation it produces
- `estleg:DraftVersion` — each iteration of a draft as it moves through readings, with `versionNumber`, `stage` (first reading, second reading, committee amendments), `modifiedDate`, and `previousVersion` linking to the prior iteration
- `estleg:resultsIn` — connects the final DraftVersion to the enacted ProvisionVersion it creates or amends

The complete provenance chain looks like:

```
VTK (intent)
  → Draft v1 (submitted)
    → Draft v2 (after first reading amendments)
      → Draft v3 (after second reading)
        → Enacted ProvisionVersion N+1
          → (later) ProvisionVersion N+2 (when amended again)
```

**Impact on other modules:**

- **D3 visualization (Module 2):** Adds a "timeline" view — scrub through time to see how the legal landscape evolved. Nodes can expand to show version history. Edges can be filtered by time period ("show me the graph as it was on date X").
- **Impact analysis (Module 4):** Can now detect when a draft would amend a provision that was recently changed ("this paragraph was last amended 3 months ago — are you sure another change is needed?") and when court decisions interpreted a *previous* version that the current text has already addressed.
- **AI chat (Module 5):** Temporal context enriches answers — "this paragraph was last amended in 2021 to address EU Directive X; the Supreme Court interpreted the previous version in case Y, and that interpretation may no longer apply if you change the wording as proposed."
- **AI Law Drafter (Module 6):** When generating a VTK or draft, the system can reference the amendment history of related provisions to justify the proposed change.

**Data sources for version history:**

- Riigi Teataja publishes version histories of all enacted laws with timestamps
- EIS (Eelnõude Infosüsteem) tracks draft legislation through the parliamentary process with version metadata
- The sync pipeline (Module 1) can pull version data from both sources alongside the current ontology data

### 2.5 Ontology Extensibility (Määrused, VTK, and Beyond)

The ontology is designed to be extended with new domains without code changes. The current 5 domains (Enacted Law, Draft Legislation, Court Decisions, EU Legislation, EU Court Decisions) are not hardcoded — the system discovers class types and relationships dynamically from SPARQL.

**Planned extensions:**

- **Määrused (Government Regulations/Decrees):** New classes `estleg:GovernmentRegulation`, `estleg:RegulationType`, `estleg:IssuingMinistry`. Connected to existing ontology via `implementsProvision` (linking to LegalProvision), `transposesDirective` (linking to EULegislation), and `authorizedBy` (linking to the enabling act).
- **VTK (Väljatöötamiskavatsus):** New class `estleg:DraftingIntent` as described in the versioning model above. Connected via `resultsInDraft`, `addressesProvision`, `proposedBy`.
- **Future domains:** The same pattern applies to Kohaliku omavalitsuse õigusaktid (local government acts), Riigikohtu erikogu lahendid, or any other legal document category.

**How extension works:**

1. Define new classes and relationships in JSON-LD in the GitHub repo, following the existing schema patterns
2. Add SHACL validation shapes for the new classes
3. Push to GitHub — the sync pipeline converts to RDF and loads into Jena automatically
4. The D3 visualization discovers the new domain and renders it (you only need to assign a color in a small config file)
5. The impact analysis, AI chat, and law drafter all pick up the new domain through SPARQL and RAG — no code changes needed

**Domain registration pattern:** Each domain has a small metadata file in the ontology repo that declares its classes, their display properties (color, icon, label), and how they connect to other domains. The sync pipeline reads these to configure the visualization and admin dashboard automatically.

---

## 3. Technology Stack

| Layer | Technology | Rationale |
|-------|-----------|-----------|
| Frontend | D3.js + HTMX + Vanilla JS | D3 for ontology visualization; HTMX for dynamic UI without heavy JS framework; clean FastHTML integration |
| UI Framework | FastHTML (Python) | Your primary stack; serves HTML directly, excellent for HTMX; no separate frontend build step |
| API | Starlette / FastHTML routes | Built into FastHTML; REST for data, WebSocket for real-time graph updates and AI chat streaming |
| Future API | REST API + MCP Server | Public API for third-party integrations and MCP protocol support for AI tool ecosystems (post-MVP) |
| Triplestore | Apache Jena Fuseki | Industry-standard SPARQL endpoint; handles RDF/OWL natively; supports named graphs for temporary draft integration |
| App Database | PostgreSQL 16 | User accounts, sessions, document metadata, audit logs, chat history; robust for 5–50 users |
| AI / LLM | Pluggable adapter (Claude and Codex supported, Claude default) | Abstract `LLMProvider` interface. Both Claude and Codex adapters are first-class. Default is Claude; Codex can be activated via config. Future providers (MS AI Foundry, Ollama, local) plug into the same interface. |
| RAG Pipeline | LangChain or custom | Chunk ontology data + user documents; embed with multilingual model supporting Estonian; vector search via pgvector |
| Vector Store | pgvector (PostgreSQL ext.) | Keeps infrastructure simple — reuses Postgres; sufficient for your scale |
| Document Parser | Apache Tika / python-docx | Extract text from uploaded .docx/.pdf drafts for analysis |
| Auth | Authlib + OIDC | Government SSO integration ready (TARA); simple JWT sessions for initial version |
| Deployment | **Coolify** (self-hosted PaaS) | Replaces Docker Compose + nginx + manual SSL; built-in Traefik proxy, git-push deploys, secrets management, monitoring |
| Infrastructure | Single VPS (Hetzner/DO) | Coolify manages all containers on the server; scale to multiple servers later if needed |
| CI/CD | GitHub Actions + Coolify webhooks | GitHub Actions for tests/linting; Coolify handles deployment on push to main |
| Ontology Sync | Python worker + GitHub webhooks | Converts JSON-LD → RDF, loads into Jena; triggered on push or on schedule |
| Estonian NLP | EstBERT / multilingual-e5 | Estonian-capable embedding model for vector search; EstBERT for entity extraction from legal text |

---

## 4. System Modules

The system is organized into nine modules that can be developed semi-independently. Each module has a clear interface contract, allowing parallel development once the core infrastructure (Module 1) is in place.

### 4.1 Module 1: Core Infrastructure

**Purpose:** Foundation layer — data pipeline, database setup, authentication, and basic project scaffolding.

- FastHTML project structure with Estonian locale support
- PostgreSQL schema: users, sessions, documents, audit_log tables
- Apache Jena Fuseki setup with SPARQL endpoint configuration
- GitHub → Jena sync pipeline: JSON-LD parser, RDF converter, Fuseki bulk loader
- Webhook listener for automatic ontology updates on GitHub push
- Authentication module (JWT sessions, user roles: drafter / reviewer / admin)
- Coolify deployment configuration: Dockerfile, service definitions for Jena + Postgres, environment secrets, Traefik routing rules
- Basic health check and admin dashboard

### 4.2 Module 2: Ontology Explorer & D3 Visualization

**Purpose:** Interactive ontology browsing with the D3.js force-directed graph, allowing users to explore the legal knowledge graph.

- D3 force-directed graph engine (based on your approved demo) with smooth physics simulation
- Node rendering: 15 class types with category-based coloring, size proportional to entity count
- Edge rendering: relationship types with directional arrows, cross-category edges highlighted
- Interactive features: hover tooltips, click-to-pin, drag to rearrange, zoom/pan
- Layout modes: force-directed (default), group-by-category, hierarchical
- Search bar: find entities by name, filter by type, navigate to results on graph
- Detail panel: click a node to see full metadata, connected entities, relevant provisions
- SPARQL-backed data loading: lazy-load subgraphs on demand (not all 90k nodes at once)
- WebSocket updates: graph reflects real-time changes when drafts are uploaded
- Timeline view: temporal slider to visualize the legal landscape at any point in time; filter edges and nodes by validity period
- Version history panel: expand any provision node to see its full amendment history, with diff view between versions
- Legislative lifecycle view: visualize the VTK → Draft → Enacted flow for any piece of legislation, showing how it evolved through readings

### 4.3 Module 3: Document Upload & Temporary Integration

**Purpose:** Allow users to upload draft legislation (.docx/.pdf) and temporarily integrate it into the ontology graph for analysis.

- File upload interface with drag-and-drop support (.docx, .pdf, .txt)
- Document parser: extract structured text, identify sections/paragraphs/definitions using Apache Tika
- Estonian legal NLP pipeline: entity extraction (law references, legal terms, EU act citations) using EstBERT or rule-based patterns
- Temporary named graph: create an isolated RDF graph in Jena for the uploaded draft, linked to existing ontology entities
- Reference resolver: match extracted references (e.g., "Tsiviilseadustiku § 123") to existing LegalProvision nodes
- Visual integration: draft appears on D3 graph as a distinct node cluster (dashed borders, unique color) connected to matched entities
- Session-scoped: temporary graphs are tied to user sessions, cleaned up after configurable TTL
- Export: save the analysis results as a structured report (JSON or .docx)

### 4.4 Module 4: Impact Analysis Engine

**Purpose:** Automated analysis of how a draft law affects the existing legal framework — the core advisory intelligence.

- Impact mapper: SPARQL-based traversal to find all entities within N hops of the draft's connections
- Conflict detector: compare draft provisions against existing law provisions; flag contradictions, overlapping scope, or superseded clauses
- EU compliance checker: verify that referenced EU directives/regulations are correctly transposed; flag missing transpositions
- Gap analysis: identify legal concepts or topic clusters that the draft touches but doesn't adequately address
- Court decision cross-reference: find relevant Supreme Court and CJEU decisions that interpret related provisions
- Impact score: quantified measure of how many existing entities are affected, weighted by relationship type
- Visual overlay: impact results displayed as heatmap on D3 graph (affected nodes glow, severity color-coded)
- Temporal impact awareness: detect when a draft would amend a recently-changed provision; flag court decisions that interpreted previous versions and may need re-evaluation
- Amendment history context: show the full change history of affected provisions so drafters understand the legislative trajectory before proposing changes
- Report generation: structured impact report exportable as .docx with findings, affected laws, version history context, and recommendations

### 4.5 Module 5: AI Advisory Chat

**Purpose:** Conversational AI assistant that helps officials draft, review, and refine legislation using the ontology as its knowledge base.

- Chat interface: streaming responses via WebSocket, conversation history, Estonian language
- Pluggable LLM adapter: abstract LLMProvider interface with implementations for Claude API, OpenAI API, and local models (Ollama)
- RAG pipeline: chunk ontology data + uploaded documents, embed with multilingual model (multilingual-e5-large), retrieve relevant context for each query
- Ontology-aware prompting: system prompt includes relevant SPARQL query results, related provisions, and impact analysis context
- Tool use / function calling: AI can execute SPARQL queries, request impact analysis, search for specific provisions — presented as natural language results
- Drafting assistance: suggest clause wording based on similar existing provisions, flag common drafting issues
- Conversation modes: general Q&A about the legal framework, guided draft review, EU compliance walkthrough
- Chat history stored in PostgreSQL with full audit trail

### 4.6 Module 6: AI Law Drafter (Intent-to-Draft)

**Purpose:** Generate complete law drafts from natural language intent — the user describes *what* they want the law to achieve, and the AI produces a structured legal document grounded in the existing ontology.

This is a distinct workflow from the advisory chat. Where the chat is conversational and analytical, the law drafter is a guided, multi-step document generation pipeline:

**Step 0 — Workflow Selection:**
The user chooses whether to generate a VTK (Väljatöötamiskavatsus) or a full law draft. For VTK, the system produces the formal intent document including problem analysis, proposed solution, impact assessment, and planned timeline — suitable for publication to EIS for public consultation. For a full draft, the system proceeds through all steps below. A VTK created in the system can later be used as the starting point for the full draft workflow, maintaining the provenance chain.

**Step 1 — Intent Capture:**
The user provides a natural language prompt describing the legislative intent (e.g., "Soovin luua seaduse, mis reguleerib tehisintellekti kasutamist avalikus sektoris" — "I want to create a law regulating AI use in the public sector").

**Step 2 — AI Clarification Interview:**
The system asks structured follow-up questions, informed by the ontology. For the AI regulation example, it would ask about scope (which public institutions?), existing related legislation it found in the ontology (does this supplement or replace existing provisions?), EU compliance requirements (relevant AI Act provisions), enforcement mechanisms, and transition periods. This is an interactive Q&A session — typically 5–10 questions.

**Step 3 — Ontology-Grounded Research:**
Based on the intent + answers, the system runs SPARQL queries to identify all related existing provisions, relevant EU directives requiring transposition, court decisions interpreting similar concepts, and existing legal concepts/definitions that should be reused (not reinvented). This research feeds the drafting context.

**Step 4 — Structure Generation:**
The AI proposes a law structure: chapter/section/paragraph outline with titles, based on patterns found in similar existing laws in the ontology. The user can modify the structure before proceeding.

**Step 5 — Clause-by-Clause Drafting:**
The AI drafts each provision, with each clause grounded in specific ontology references: reusing existing definitions where they exist, matching the drafting style of related laws, and citing the EU provisions being transposed. Each generated clause shows its ontology sources as annotations.

**Step 6 — Integrated Review:**
The drafted law is automatically fed through Module 3 (temporary integration) and Module 4 (impact analysis), so the user immediately sees how their AI-generated draft connects to the existing legal framework, with conflict detection and gap analysis already applied.

**Step 7 — Export:**
The final draft is exported as a .docx following Estonian legislative formatting conventions (Õigustehnika reeglid), with an appendix containing the ontology research, impact analysis summary, and EU compliance checklist.

**Technical implementation:**
- Multi-turn LLM orchestration with structured state machine (intent → questions → research → structure → draft → review)
- Each step saves state to PostgreSQL, allowing the user to pause and resume
- SPARQL query templates triggered by the AI at each step (not freeform — curated queries for safety)
- Estonian legislative formatting templates for .docx generation
- Version tracking: each iteration of the draft is stored as a DraftVersion node in the ontology, allowing comparison between versions and maintaining the full VTK → Draft → Enacted provenance chain
- VTK generation mode: produces Väljatöötamiskavatsus documents with problem analysis, proposed solution, impact assessment, and timeline — formatted for EIS publication

### 4.7 Module 7: User Management & Collaboration

**Purpose:** Multi-user support with roles, shared workspaces, and audit logging for government compliance.

- Role-based access: drafter (upload + analyze + AI draft), reviewer (read + comment), admin (full access + user management)
- Shared workspaces: team members can view each other's uploaded drafts, AI-generated drafts, and analyses
- Commenting system: reviewers can annotate impact analysis results and graph nodes
- Audit logging: all actions logged (who uploaded what, when, which analyses were run, which AI drafts were generated) for compliance
- SSO/OIDC integration: ready for government identity providers (TARA — Estonian national authentication)
- Session management: concurrent session handling for 5–50 users

### 4.8 Module 8: Public API & MCP Server (Post-MVP)

**Purpose:** Expose the system's capabilities as a programmable API and as an MCP (Model Context Protocol) server, enabling third-party integrations and AI tool ecosystems.

This module is explicitly **not part of the MVP** but is architecturally planned from Phase 1 to ensure the internal service layer is API-ready.

**REST API:**
- Versioned API (v1) with OpenAPI/Swagger documentation
- Endpoints mirroring all core capabilities: ontology queries, document upload + analysis, impact reports, AI chat sessions, law drafting sessions
- API key authentication with rate limiting and usage quotas
- Webhook support: notify external systems when analyses complete or drafts are generated
- Bulk operations: batch ontology queries, batch document analysis

**MCP Server:**
- Expose the advisory system as an MCP-compatible tool server
- Tools available via MCP: `query_ontology` (SPARQL natural language interface), `analyze_draft` (upload and analyze a document), `get_impact_report` (retrieve impact analysis for a draft), `draft_law` (initiate AI law drafting from intent), `search_provisions` (find relevant legal provisions)
- Resources available via MCP: ontology class definitions, relationship schemas, entity metadata
- This allows any MCP-compatible AI assistant (Claude Desktop, Claude Code, custom agents) to use the Estonian Legal Ontology as a knowledge tool
- Use case: a ministry official using Claude Desktop could ask "What existing laws would be affected if we change the definition of 'digital service' in the cybersecurity act?" and Claude would call the MCP tools to query the ontology and run impact analysis

**Implementation notes:**
- The internal service layer (Modules 1–6) should use clean function signatures that can be wrapped as both REST endpoints and MCP tools
- FastHTML routes already use Starlette, which supports API middleware cleanly
- MCP server runs as a separate process (or Coolify service) using the Python MCP SDK, calling the same internal service functions
- Rate limiting and auth are handled at the API gateway level (Traefik middleware in Coolify)

### 4.9 Module 9: Monitoring, Analytics & Administration

**Purpose:** Operational visibility, usage analytics, and administrative controls.

- System health dashboard: Jena Fuseki status, API latency, LLM token usage, sync pipeline status (supplemented by Coolify's built-in container monitoring)
- Usage analytics: most-queried entities, popular search terms, frequently uploaded draft types, AI drafter usage patterns
- Ontology statistics: entity counts per category, relationship density, last sync timestamp
- Admin panel: user management, system configuration, LLM provider switching, sync triggers
- Cost tracking: LLM API token consumption per user/session for budget management
- Alerting: notifications when sync fails, Jena is unreachable, or LLM costs exceed thresholds
- Coolify integration: leverage Coolify's built-in log aggregation, container restart policies, and deployment notifications rather than rebuilding these

---

## 5. Development Phases

The project is divided into five phases, designed so each phase produces a usable system that builds on the previous one. This allows early validation with real government users while continuing to build advanced features.

### 5.1 Phase Overview

| Phase | Deliverables | Duration | Dependencies |
|-------|-------------|----------|--------------|
| Phase | Scope | Dependencies |
|-------|-------|--------------|
| Phase 1 | Core Infrastructure + Ontology Explorer + Versioning Schema (Modules 1–2) | None |
| Phase 1.5 | Design System Foundation (Estonia Brand tokens, core components, live reference) | Phase 1 |
| Phase 2 | Document Upload + Impact Analysis (Modules 3–4) | Phase 1, Phase 1.5 |
| Phase 3 | AI Advisory Chat + AI Law Drafter (Modules 5–6) | Phase 2 (uses LLMProvider, drafts, impact reports) |
| Phase 4 | Collaboration + Admin (Modules 7, 9) | Phase 1 (auth); annotations target drafts (Phase 2), chat messages (Phase 3), and drafter clauses (Phase 3) — so Phase 4 cannot fully complete until Phase 3 targets exist |
| Phase 5 | Public API + MCP Server (Module 8) | Phase 1–4 (API exposes all features) |

**Note on duration estimates:** Previous versions of this plan carried week-level estimates (e.g. "6–8 weeks per phase"). Those numbers were baselined against a much smaller backlog and are no longer reliable. The current backlog contains ~300 detailed issues across Phases 2–5, and work happens in bursts rather than a continuous sprint. Duration estimates have been removed until the team can rebaseline them against observed velocity after the first few Phase 2 epics land. For planning purposes, use **issue count per phase** (see GitHub milestones) as the primary scope signal.

### 5.2 Phase 1: Foundation (Weeks 1–8)

**Goal:** A working system where users can browse the Estonian Legal Ontology through an interactive D3 visualization, backed by a SPARQL triplestore, deployed via Coolify.

**Milestone:** User can log in, see the full ontology graph, search for specific laws, and explore relationships. The data pipeline automatically keeps the graph current with the GitHub repository. The entire system runs on a Coolify-managed VPS with automatic SSL.

**Coolify setup in this phase:**
- Provision VPS (Hetzner CPX31 or similar: 4 vCPU, 8GB RAM, sufficient for all services)
- Install Coolify, configure domain and wildcard SSL
- Define services: FastHTML app (git-push deploy), Jena Fuseki (Docker service with persistent volume), PostgreSQL (Coolify-managed database), sync worker (scheduled container)
- Configure GitHub webhook for automated deployments

**Versioning in this phase:** Design and implement the ProvisionVersion, DraftVersion, DraftingIntent (VTK), and Amendment classes in the ontology schema. Build the Riigi Teataja version history import into the sync pipeline. The D3 visualization includes a basic timeline slider from the start.

**Key risks:** Jena Fuseki configuration for Estonian text search; D3 performance with large subgraphs; completeness of Riigi Teataja version data for older laws. Mitigate by implementing lazy-loading early, testing with the full dataset, and accepting partial version chains with clear UI indicators.

### 5.3 Phase 2: Draft Analysis (Weeks 9–18)

**Goal:** Users can upload a draft law document and see it integrated into the ontology graph, with automated impact analysis.

**Milestone:** A drafter uploads a .docx file. The system extracts legal references, maps them to ontology entities, shows the draft on the graph, and produces an impact report with conflict detection and EU compliance flags.

**Key risks:** Estonian legal NLP accuracy for entity extraction; complexity of conflict detection rules. Mitigate by starting with rule-based reference matching (regex for "§ X" patterns) before adding ML-based extraction.

### 5.4 Phase 3: AI Advisory + Law Drafter (Weeks 19–30)

**Goal:** An AI chat assistant that can answer questions and guide draft review, plus the intent-to-draft pipeline for creating laws from scratch.

**Milestone (Chat):** A user can ask "Kuidas see eelnõu mõjutab kehtivat tsiviilseadustikku?" (How does this draft affect the civil code?) and receive a contextual answer grounded in the ontology and impact analysis results.

**Milestone (Drafter):** A user can describe a legislative intent in Estonian, go through a guided Q&A session, and receive a complete law draft with ontology annotations, impact analysis, and EU compliance checklist — all in a single workflow.

**Key risks:** Estonian language quality of LLM responses; hallucination control when generating legal text; ensuring AI-drafted clauses are legally sound. Mitigate by heavy use of RAG grounding, explicit citation of sources, mandatory human review step before any draft is considered "final," and using existing law patterns as templates rather than generating from scratch.

### 5.5 Phase 4: Production Readiness (Weeks 27–32)

**Goal:** Multi-user collaboration, government SSO integration, audit logging, and operational tooling.

**Milestone:** A team of 10–20 ministry officials can concurrently use the system, with shared workspaces, commenting, and full audit trails. TARA (Estonian government SSO) integration complete.

**Note:** Module 7 auth foundations begin in Phase 1. Phase 4 adds advanced collaboration features, SSO, and admin tooling. Can run partially in parallel with Phase 3.

### 5.6 Phase 5: API & MCP (Weeks 33–38)

**Goal:** Expose the system as a programmable API and MCP server for third-party integrations.

**Milestone:** External systems can query the ontology, submit documents for analysis, and initiate AI drafting sessions via REST API. MCP-compatible AI assistants can use the Estonian Legal Ontology as a tool.

**Prerequisite:** All internal service functions must have clean, documented interfaces (established in Phases 1–3). This phase wraps them with API authentication, rate limiting, versioning, and the MCP protocol adapter.

---

## 6. Key Data Flows

### 6.1 Ontology Sync Pipeline

```
GitHub push → Webhook triggers sync worker (Coolify scheduled service)
→ Worker pulls latest JSON-LD files (including version history data)
→ Python script converts JSON-LD to RDF/Turtle, preserving temporal metadata
  (validFrom/validUntil on ProvisionVersions, stage/dates on DraftVersions)
→ Jena Fuseki GRAPH STORE protocol loads into default graph
→ Version chain integrity check: verify all ProvisionVersion → previousVersion links
→ Frontend receives WebSocket notification to refresh
```

### 6.2 Draft Upload & Analysis Flow

```
User uploads .docx → Apache Tika extracts structured text
→ NLP pipeline extracts entities (law refs, legal terms, EU citations)
→ Reference resolver matches entities to ontology nodes via SPARQL
→ System creates temporary named graph in Jena
→ Impact engine runs traversal queries
→ Conflict detector compares provisions
→ Results rendered on D3 graph + impact report generated
```

### 6.3 AI Chat Flow

```
User sends message (Estonian)
→ RAG retriever embeds query, searches pgvector for relevant chunks
→ System runs SPARQL queries for structured context
→ System prompt assembled: message + chunks + SPARQL results + impact context
→ LLM generates streaming response
→ Response displayed via WebSocket with source citations
```

### 6.4 AI Law Drafter Flow

```
User selects workflow: VTK generation OR full law draft
→ User provides legislative intent (Estonian natural language)
→ System queries ontology for related existing legislation + version histories
→ AI generates clarification questions (5–10) based on ontology context
→ User answers questions interactively
→ System runs deep SPARQL research: related laws, EU directives, court decisions,
  amendment history of affected provisions, previous VTKs in related areas
→ AI proposes law structure (chapters/sections) based on similar existing laws
→ User approves/modifies structure
→ AI drafts clause-by-clause, each grounded in ontology references
→ Each draft iteration stored as DraftVersion node (maintaining provenance chain)
→ Draft auto-fed through temporary integration (Module 3) + impact analysis (Module 4)
→ User reviews integrated view: draft on graph + conflicts + gaps + version context
→ Export as .docx with Estonian legislative formatting + research appendix
```

### 6.5 API / MCP Flow (Post-MVP)

```
External client authenticates via API key
→ Sends request (REST JSON or MCP tool call)
→ API gateway (Traefik) validates auth + rate limits
→ Request routed to internal service function
→ Service executes (SPARQL query / document analysis / AI session)
→ Response returned as structured JSON (REST) or MCP tool result
→ Webhook fires if configured (async operations)
```

### 6.6 Temporal / Version Query Flow

```
User opens timeline view or requests version history
→ SPARQL query retrieves ProvisionVersion chain for target entity:
  SELECT ?version ?validFrom ?validUntil ?amendedBy
  WHERE { ?version estleg:isVersionOf <provision> ; estleg:validFrom ?validFrom ... }
→ For legislative lifecycle: retrieve full VTK → DraftVersion → ProvisionVersion chain
→ D3 renders temporal axis with version nodes connected by previousVersion edges
→ User can select any point in time → graph re-renders showing only entities valid at that date
→ Diff view: select two versions → system highlights textual changes between them
```

---

## 7. Ontology Storage: GitHub + Jena Recommendation

### 7.1 GitHub as Source of Truth

- Your existing repository structure is well-organized with JSON-LD files, SHACL validation, and CI/CD pipelines.
- Continue using GitHub for version control, collaborative editing, and schema evolution.
- The INDEX.json and per-law files remain the canonical data format.
- Schema changes go through PRs with SHACL validation — this workflow is proven and should not be disrupted.
- **Version history data:** Add version metadata to the JSON-LD files — each LegalProvision gains a `versions` array containing ProvisionVersion objects with `validFrom`, `validUntil`, `amendedBy`, and a reference to the full text snapshot. The sync pipeline converts these to RDF triples with proper temporal semantics.
- **New domain data (Määrused, VTK):** Added as new JSON-LD file collections following the existing patterns, with their own SHACL validation shapes and INDEX files.

### 7.2 Apache Jena Fuseki as Query Engine

Jena Fuseki serves as the runtime query layer, optimized for the access patterns the advisory system needs:

**SPARQL queries:** "Find all LegalProvisions that reference TsiviilS and are also connected to EU Directives" — this is a 3-hop graph traversal that completes in milliseconds on Jena, but would require loading and cross-referencing dozens of JSON-LD files.

**Named graphs:** Each uploaded draft gets its own named graph (e.g., `<urn:draft:session-abc123>`). This graph links to the base ontology but is isolated per session and auto-deleted after TTL expiry. No writes to the base ontology.

**Reasoning:** Jena's OWL reasoner can derive implicit relationships. Example: if your ontology states that "implementsEU" is a transitive relationship, Jena can infer that if Law A implements Directive B, and Directive B amends Directive C, then Law A has an indirect relationship to Directive C.

**Temporal queries:** Jena handles the version model efficiently with SPARQL 1.1 property paths. Queries like "find the version of § 123 that was valid on 2023-06-15" or "list all amendments to TsiviilS in the last 5 years" resolve in milliseconds, traversing the ProvisionVersion chain without loading full document histories.

### 7.3 Why Not Just GitHub?

For a read-only visualization (like the D3 demo), loading JSON-LD directly could work. But the advisory features require query patterns that file-based access cannot support efficiently: multi-hop relationship traversal, temporary graph merging, full-text search across 90,000 entities, and real-time SPARQL queries from the AI chat and law drafter. Jena provides all of these while your GitHub repo remains the clean, version-controlled source.

---

## 8. Cloud Deployment Architecture (Coolify)

### 8.1 Coolify Service Layout

| Service | Type | Configuration |
|---------|------|---------------|
| `legal-ontology-app` | Git-deploy (Dockerfile) | FastHTML app server; auto-deploy on push to main; Traefik routes `app.yourdomain.ee` |
| `jena-fuseki` | Docker service | Apache Jena Fuseki; persistent volume for TDB2 store; internal network only (not public) |
| `postgres` | Coolify-managed DB | PostgreSQL 16 + pgvector; automated backups via Coolify; internal network only |
| `sync-worker` | Scheduled container | Runs on GitHub webhook or cron; pulls ontology, converts, loads into Jena |
| `mcp-server` | Docker service (Phase 5) | MCP protocol server; public endpoint for tool integrations |

Coolify handles: TLS certificates (Let's Encrypt auto-renewal), reverse proxy routing (Traefik), container health checks and restart policies, log aggregation, deployment rollbacks, and secrets injection.

**Recommended VPS:** Hetzner CPX31 (4 vCPU, 8GB RAM, 160GB NVMe) — approximately €15/month. Sufficient for all services with 50 concurrent users. Scale to CPX41 or add a second server via Coolify if needed.

### 8.2 Security Considerations

Given that draft legislation may be sensitive prior to publication, the system must enforce: TLS everywhere (handled by Coolify/Traefik), **mandatory** encryption-at-rest for draft files (AES-256-GCM via Fernet) and encrypted storage of parsed text in PostgreSQL, strict org-scoped role-based API authorization, persistent draft named graphs with cascade delete on explicit removal, audit logging of all document uploads, draft accesses, and AI interactions, LLM API calls made server-side only (no client-side API keys), PII scrubbing before every LLM prompt, and API keys stored as Coolify encrypted secrets. Draft retention is "persistent until owner deletes" with a mandatory 90-day auto-archive warning requiring user action (keep or delete).

---

## 9. Key Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Estonian NLP quality | Legal entity extraction accuracy may be limited. Start with rule-based regex patterns for §-references and law names; layer ML (EstBERT) in Phase 2 iteration. |
| LLM hallucination | AI may generate plausible but incorrect legal advice or draft text. Heavy RAG grounding, mandatory source citations, disclaimer on all AI outputs, human-in-the-loop review. AI-drafted laws are always marked as "draft — requires legal review." |
| AI-drafted law quality | Generated legal text may not meet formal drafting standards. Mitigate by using existing laws as structural templates, training prompts on Õigustehnika reeglid (Estonian legislative drafting rules), and mandatory human review. |
| D3 performance at scale | Rendering 90k+ nodes is not feasible. Lazy-load subgraphs via SPARQL LIMIT/OFFSET; show overview at category level, drill into details on demand. |
| Jena Fuseki learning curve | Team may not have SPARQL experience. Provide pre-built query templates for common patterns; use the AI chat to generate SPARQL from natural language. |
| Data sensitivity | Pre-publication drafts are politically sensitive. Drafts persist until the owner explicitly deletes them; compensating controls are mandatory: AES-256-GCM file encryption, encrypted parsed-text columns, strict org-scoped access control, full audit logging of every access, 90-day auto-archive warning requiring user action, and explicit delete cascade (file + Jena named graph + DB rows + RAG chunks). |
| Ontology evolution | Schema changes in GitHub may break Jena mappings. SHACL validation in sync pipeline; reject loads that fail validation; alert on schema drift. |
| Version data completeness | Riigi Teataja version histories may have gaps for older laws. Accept incomplete version chains gracefully; mark provisions with `versionCoverage: partial` and surface this in the UI so users know when historical data is incomplete. |
| Version chain integrity | Amendments can be complex (partial paragraph changes, restructuring). Ensure the sync pipeline validates that every ProvisionVersion has a valid `previousVersion` link and that `validFrom`/`validUntil` ranges don't overlap. |
| API abuse (Phase 5) | Public API could be misused. Rate limiting via Traefik middleware, API key scoping (read-only vs. full access), usage quotas per key, monitoring dashboard. |

---

## 10. Immediate Next Steps

To begin Phase 1 development:

1. **Provision infrastructure:** Set up a Hetzner VPS, install Coolify, configure your domain DNS. Create the Coolify project with service definitions for FastHTML, Jena Fuseki, and PostgreSQL.

2. **Set up the project repository** with FastHTML scaffolding and Dockerfile. Connect to Coolify for automated deployments. Extend the existing ontology repo's GitHub Actions for the sync pipeline.

3. **Deploy Apache Jena Fuseki** as a Coolify service and write the JSON-LD → RDF conversion script for the existing ontology data. Validate that all 15 classes (plus new versioning classes: ProvisionVersion, DraftVersion, DraftingIntent, Amendment) and their relationships are correctly represented in SPARQL.

4. **Build the D3 visualization module** with SPARQL-backed lazy-loading, starting from the approved demo and adding search, filtering, and detail panels.

5. **Implement basic authentication** (JWT sessions) and the PostgreSQL schema for users, sessions, and audit logs.

6. **Define the LLMProvider interface contract** and the internal service function signatures so the AI module (Phase 3) and the API module (Phase 5) can be developed against stable interfaces.

7. **Design the versioning schema** for the ontology repo: define JSON-LD structures for ProvisionVersion, DraftVersion, DraftingIntent (VTK), and Amendment classes. Write SHACL shapes for validation. Prototype the Riigi Teataja version history scraper to assess data completeness for initial population.

8. **Plan the Määrused and VTK domain extensions** in the ontology repo: define classes, relationships to existing domains, SHACL shapes, and domain registration metadata files. This can proceed in parallel with the application development.
