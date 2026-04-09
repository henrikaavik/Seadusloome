# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Estonian Legal Ontology Advisory Software — a system that helps Estonian government officials in the law creation process. Users upload draft legislation or describe legislative intent in natural language; the system maps it against the existing legal framework (615 enacted laws, 22,832 drafts, 12,137 Supreme Court decisions, 33,242 EU legal acts, 22,290 EU court decisions) and shows connections, conflicts, and impacts.

The architecture plan is in `estonian-legal-ontology-plan.md`. The ontology source data lives in a separate repo: github.com/henrikaavik/estonian-legal-ontology.

## Architecture

**Layered design:**
- **Frontend:** D3.js force-directed graph + HTMX + Vanilla JS (no heavy JS framework)
- **API/Server:** FastHTML (Python) with Starlette routes — REST + WebSocket
- **Application Core:** Ontology query engine, document analyzer, impact mapper, conflict detector, AI law drafter
- **Storage:** Apache Jena Fuseki (SPARQL triplestore for RDF ontology queries) + PostgreSQL 18 with pgvector (app state, vectors, chat history)
- **AI Layer:** Pluggable LLM via abstract `LLMProvider` interface (both Claude and Codex supported; Claude is the default), RAG pipeline with multilingual embeddings
- **Deployment:** Coolify (self-hosted PaaS) on Hetzner VPS with Traefik reverse proxy

**Key data flow:** GitHub (JSON-LD source of truth) → sync pipeline → RDF conversion → Jena Fuseki (runtime query engine). Uploaded drafts become named graphs in Jena that persist until explicitly deleted by the owner; compensating controls include encryption at rest, strict org-scoped access control, full audit logging, and a 90-day auto-archive warning.

## Modules

1. **Core Infrastructure** — FastHTML scaffolding, PostgreSQL schema, Jena setup, GitHub-to-Jena sync pipeline, auth (JWT/TARA SSO), Coolify deployment
2. **Ontology Explorer** — D3.js interactive graph with SPARQL-backed lazy-loading, timeline view, version history
3. **Document Upload** — .docx/.pdf parsing (Apache Tika), Estonian legal NLP entity extraction, temporary named graph creation
4. **Impact Analysis** — SPARQL traversal, conflict detection, EU compliance checking, gap analysis, impact scoring
5. **AI Advisory Chat** — Streaming chat via WebSocket, RAG pipeline (pgvector), ontology-aware prompting, tool use for SPARQL
6. **AI Law Drafter** — Multi-step intent-to-draft pipeline: intent capture → clarification interview → ontology research → structure generation → clause-by-clause drafting → integrated review → .docx export
7. **User Management** — Roles (drafter/reviewer/admin), shared workspaces, audit logging
8. **Public API + MCP Server** (post-MVP) — REST API + MCP protocol for third-party integrations
9. **Monitoring & Admin** — Health dashboard, usage analytics, cost tracking

## Technology Stack

| Component | Technology |
|-----------|-----------|
| Server framework | FastHTML (Python) |
| Frontend visualization | D3.js + HTMX |
| Triplestore | Apache Jena Fuseki (SPARQL) |
| Database | PostgreSQL 18 + pgvector |
| LLM | Pluggable (Claude and Codex both supported; Claude is the default) |
| Embeddings | multilingual-e5-large / EstBERT |
| Document parsing | Apache Tika / python-docx |
| Auth | Authlib + OIDC (TARA-ready) |
| Deployment | Coolify on Hetzner VPS |
| CI/CD | GitHub Actions + Coolify webhooks |

## Development Context

- **Primary language:** Estonian (UI, legal text analysis, AI responses)
- **Target users:** 5-50 concurrent government officials
- **Ontology versioning:** Temporal model with `ProvisionVersion`, `DraftVersion`, `DraftingIntent` (VTK), and `Amendment` classes tracking full legislative lifecycle (VTK → Draft readings → Enacted law)
- **Internal service functions** should have clean signatures that can be wrapped as both REST endpoints and MCP tools (Phase 5 readiness)
- **Estonian legal NLP:** Start with rule-based regex for §-references and law names; layer ML (EstBERT) later
- **D3 performance:** Never render 90k+ nodes at once — use SPARQL LIMIT/OFFSET lazy-loading, category-level overview with drill-down
- **Draft sensitivity:** Pre-publication drafts are politically sensitive. Drafts persist until explicitly deleted by the owner; compensating controls are mandatory: AES-256-GCM file encryption, encrypted JSONB for parsed text, strict org-scoped access control, audit logging of every access, 90-day auto-archive warning with user action required, and explicit delete cascade that removes file + named graph + DB rows

## Development Phases

| Phase | Scope | Depends on |
|-------|-------|-----------|
| 1 | Core Infrastructure + Ontology Explorer (Modules 1-2) | None |
| 1.5 | Design System Foundation (Estonia Brand, components, themes) | Phase 1 |
| 2 | Document Upload + Impact Analysis (Modules 3-4) | Phase 1, Phase 1.5 |
| 3 | AI Advisory Chat + AI Law Drafter (Modules 5-6) | Phase 2 |
| 4 | Collaboration + Admin (Modules 7, 9) | Phase 1 (auth); annotations require Phase 2 and Phase 3 targets |
| 5 | Public API + MCP Server (Module 8) | Phase 1-4 |
