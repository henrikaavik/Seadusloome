# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Estonian Legal Ontology Advisory Software — a system that helps Estonian government officials in the law creation process. Users upload draft legislation or describe legislative intent in natural language; the system maps it against the existing legal framework (50,000+ enacted provisions across 615 laws, 22,832 drafts, 12,137 Supreme Court decisions, 33,242 EU legal acts, 22,290 EU court decisions) and shows connections, conflicts, and impacts.

The architecture plan is in `estonian-legal-ontology-plan.md`. The ontology source data lives in a separate repo: github.com/henrikaavik/estonian-legal-ontology.

## Architecture

**Layered design:**
- **Frontend:** D3.js force-directed graph + HTMX + Vanilla JS (no heavy JS framework)
- **API/Server:** FastHTML (Python) with Starlette routes — REST + WebSocket (explorer + chat)
- **Application Core:** Ontology query engine, document analyzer, impact mapper, conflict detector, AI law drafter (7-step pipeline), chat orchestrator (streaming + tool use), RAG retriever
- **Storage:** Apache Jena Fuseki (SPARQL triplestore for RDF ontology queries) + PostgreSQL 18 with pgvector (app state, vectors, chat history, RAG chunks)
- **AI Layer:** Claude API via abstract `LLMProvider` interface (pluggable — Claude is the default), Voyage AI embeddings (`voyage-multilingual-2`, 1024d) via abstract `EmbeddingProvider` interface, RAG pipeline with pgvector HNSW index
- **Document Processing:** Apache Tika (server-side .docx/.pdf parsing) + python-docx (report/draft export)
- **Background Jobs:** `FOR UPDATE SKIP LOCKED` job queue with worker thread for parse, extract, analyze, export pipelines
- **Cost Tracking:** Per-user and per-org LLM usage metering with configurable rate limits and monthly budgets
- **Deployment:** Coolify (self-hosted PaaS) on Hostinger VPS with Traefik reverse proxy

**Key data flow:** GitHub (JSON-LD source of truth) → sync pipeline → RDF conversion → Jena Fuseki (runtime query engine). Uploaded drafts become named graphs in Jena that persist until explicitly deleted by the owner; compensating controls include encryption at rest, strict org-scoped access control, full audit logging, and a 90-day auto-archive warning.

## Modules

1. **Core Infrastructure** [IMPLEMENTED] — FastHTML scaffolding, PostgreSQL schema (10 migrations), Jena Fuseki, GitHub-to-Jena sync pipeline, JWT auth + RBAC, Coolify deployment with Traefik, background job queue
2. **Ontology Explorer** [IMPLEMENTED] — D3.js interactive graph with SPARQL-backed lazy-loading, category drill-down, entity detail pages, WebSocket live updates
3. **Document Upload** [IMPLEMENTED] — Encrypted .docx/.pdf storage (Fernet), Apache Tika parsing, Claude-powered entity extraction, background job pipeline (parse → extract → analyze → export)
4. **Impact Analysis** [IMPLEMENTED] — SPARQL traversal, conflict detection, EU compliance checking, gap analysis, impact scoring, .docx report export
5. **AI Advisory Chat** [IMPLEMENTED] — Streaming WebSocket chat with tool use (SPARQL queries), RAG-grounded responses (Voyage AI + pgvector HNSW), per-user/per-org rate limiting, cost tracking
6. **AI Law Drafter** [IMPLEMENTED] — 7-step wizard pipeline: intent capture → clarification interview → ontology research → structure generation → clause-by-clause drafting → integrated review → .docx export
7. **User Management** [PLANNED] — Roles (drafter/reviewer/admin), shared workspaces, audit logging
8. **Public API + MCP Server** [PLANNED] — REST API + MCP protocol for third-party integrations
9. **Monitoring & Admin** [PLANNED] — Health dashboard, usage analytics, cost tracking (admin dashboard scaffolding exists with LLM cost/rate limit display)

## Technology Stack

| Component | Technology |
|-----------|-----------|
| Server framework | FastHTML (Python 3.13) |
| Frontend visualization | D3.js + HTMX + Vanilla JS |
| Triplestore | Apache Jena Fuseki (SPARQL) |
| Database | PostgreSQL 18 + pgvector |
| LLM | Claude API (Anthropic) via pluggable `LLMProvider` interface |
| Embeddings | Voyage AI (`voyage-multilingual-2`, 1024d) via `EmbeddingProvider` interface |
| RAG | pgvector HNSW index + chunker + retriever pipeline |
| Document parsing | Apache Tika (server) for .docx/.pdf ingestion |
| Document export | python-docx for .docx report/draft generation |
| Background jobs | `FOR UPDATE SKIP LOCKED` job queue with worker thread |
| Cost tracking | Per-user / per-org LLM usage metering (llm_usage table) |
| Auth | JWT + Authlib + OIDC (TARA-ready) |
| Deployment | Coolify on Hostinger VPS |
| CI/CD | GitHub Actions + Coolify webhooks |
| Linting | ruff + pyright |
| Package manager | uv |

## Development Context

- **Primary language:** Estonian (UI, legal text analysis, AI responses)
- **Target users:** 5-50 concurrent government officials
- **Ontology versioning:** Temporal model with `ProvisionVersion`, `DraftVersion`, `DraftingIntent` (VTK), and `Amendment` classes tracking full legislative lifecycle (VTK → Draft readings → Enacted law)
- **Internal service functions** should have clean signatures that can be wrapped as both REST endpoints and MCP tools (Phase 5 readiness)
- **Estonian legal NLP:** Start with rule-based regex for §-references and law names; layer ML (EstBERT) later
- **D3 performance:** Never render 90k+ nodes at once — use SPARQL LIMIT/OFFSET lazy-loading, category-level overview with drill-down
- **Draft sensitivity:** Pre-publication drafts are politically sensitive. Drafts persist until explicitly deleted by the owner; compensating controls are mandatory: AES-256-GCM file encryption, encrypted JSONB for parsed text, strict org-scoped access control, audit logging of every access, 90-day auto-archive warning with user action required, and explicit delete cascade that removes file + named graph + DB rows
- **Stub-mode gating:** `app/config.py::is_stub_allowed()` is the single source of truth for whether external service stubs (Tika, Claude, storage encryption) are permitted. Stubs are allowed unless `APP_ENV=production`. All three service modules (storage, LLM, Tika) must use this function rather than implementing their own gate
- **Lazy-init pattern:** Singletons for `ClaudeProvider`, `VoyageProvider`, and `SparqlClient` are lazily initialised with thread-safe locks. SDK clients (`anthropic.Anthropic`, `voyageai.AsyncClient`) are only constructed on first real call so stub users never need the packages installed
- **Retry-gating pattern:** Background job handlers receive `attempt` and `max_attempts` keyword arguments. Handlers should not flip domain rows to `failed` status until the retry budget is exhausted (final attempt). The job queue uses `FOR UPDATE SKIP LOCKED` for safe concurrent claiming
- **Cost tracking:** Every LLM call (Claude) and embedding call (Voyage AI) is logged to the `llm_usage` table via `app.llm.cost_tracker.log_usage()`. The `feature` label (e.g., `"drafter_clarify"`, `"chat"`, `"embedding"`) enables per-feature cost attribution

## Development Phases

| Phase | Scope | Status |
|-------|-------|--------|
| 1 | Core Infrastructure + Ontology Explorer (Modules 1-2) | COMPLETE |
| 1.5 | Design System Foundation (Estonia Brand, components, themes) | COMPLETE |
| 2 | Document Upload + Impact Analysis (Modules 3-4) | COMPLETE |
| 3 | AI Advisory Chat + AI Law Drafter (Modules 5-6) | COMPLETE |
| 4 | Collaboration + Admin (Modules 7, 9) | Planned — depends on Phase 1 auth; annotations require Phase 2 and Phase 3 targets |
| 5 | Public API + MCP Server (Module 8) | Planned — depends on Phases 1-4 |
