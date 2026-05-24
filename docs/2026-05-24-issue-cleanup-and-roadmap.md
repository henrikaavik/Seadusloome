# 2026-05-24 — Issue cleanup + post-Phase-4 roadmap

## Context

Starting state: **327 open issues**. Most were bulk-created on 2026-04-09 as a Phase-4/5 tracking backlog. Phase 1, 1.5, 2, 3, 4.7, 4.8 shipped between 2026-04 and 2026-05 without auto-closing their sub-tickets.

This document records the 2026-05-24 audit + cleanup pass and lays out the next dev plan.

---

## Cleanup pass (closed by audit)

157 tickets closed as already-shipped in the morning audit, then a same-day **wave 2 of 10 Section A items shipped via PRs #823–#833** (driven from sibling agent worktrees while this doc was being drafted). Audit evidence (file paths, migration numbers, PR/commit SHAs) lives in each ticket's closure comment. Net: `327 − 160 open = 167 closed`.

| Group | Count | Range / notes |
|---|---|---|
| Recent prod-verification bugs | 16 | #801–#817 (excl. #816 already closed). All have explicit fix commits or PRs #818–#822. #804's TDZ fix also silently unblocked #806 and #807. |
| Epic #784 ontology six use cases | 17 | #784 + #785–#800 (C0–C6, B1–B3, A1–A6) |
| Phase 2 (Modules 3+4) | 34 | #69–#101, #103 |
| Phase 3 (Modules 5+6) | 48 | #104, **#105 (RAG epic, parent of still-open #121 + #123)**, #106–#110, **#111 (VTK epic, parent of still-open #149)**, #113–#120, #122, #124–#148, #150, #155, #157–#160. The 48-item count includes both parent epics — they were closed by the same sweep at `2026-05-24T10:42 UTC`. |
| Phase 4 Annotations | 14 | #161, #167–#175, #297, #325, #326, #356 |
| Phase 4 Notifications | 11 | #162, #177–#179, #296, #298, #300–#303, #346 |
| Phase 4 Admin/Observability (subset) | 5 | #184, #191, #193, #194, #340 |
| Misc completed | 12 | #305, #310, #312–#314, #318–#321, #345, #349, #353 |
| **Subtotal (morning audit)** | **157** |  |
| Section A wave 2 (shipped via PRs #823–#833) | 10 | #176 (#825), #180 (#823), #299 (#824), #306 (#826), #307 (#827), #311 (#830), #315 (#832), #347 (#828), #348 (#829), #352 (#833). #354 closed via #831 also bundled the #196 LLM-latency collector. |
| **Total closed** | **167** |  |

> **Note (revision pass, 2026-05-24 PM):** the original draft listed #105 and #111 as "Skipped"; in fact both parent epics were closed by the same audit run (their still-open *children* #121, #123, #149 are what was skipped). Row description corrected above; counts unchanged.
>
> **Note (evening pass, 2026-05-24 PM):** Section A drained almost entirely during the audit afternoon — 10 items shipped via parallel agent-driven PRs #823–#833, and #354 (PR #831) also bundled the #196 LLM-call-latency collector (a Sprint 1 / §D item). Only #304 remains in §A as a re-audit task.

Result: **160 open issues remain** (real work). Zero `bug`-labeled tickets remain — all 17 prod-verification bugs from 2026-05-19 are closed.

---

## Remaining open — grouped + prioritized

### A. Bugs / quality fixes — **DONE except #304** (shipped 2026-05-24 PM via PRs #823–#833)

The original Section A table (12 items) is collapsed below. 11 items shipped on 2026-05-24 PM; only **#304** remains as a re-audit task. The cross-cutting #180 pattern note (durable row + outbound delivery + graceful fallback) is now live in `app/notifications/` — see Phase 5C webhooks for reuse.

| # | Title | Status |
|---|---|---|
| #180 | WebSocket notification delivery | ✅ PR #823 |
| #299 | Wire notify(): draft_shared event | ✅ PR #824 |
| #176 | @mention autocomplete frontend typeahead | ✅ PR #825 |
| #306 | Draft re-analyze button + handler | ✅ PR #826 |
| #307 | Expiring signed URL for impact-report .docx | ✅ PR #827 |
| #347 | HTMX status polling fallback on draft detail | ✅ PR #828 |
| #348 | Worker process standalone entrypoint | ✅ PR #829 |
| #311 | Retriever metadata filtering (org/date/source) | ✅ PR #830 |
| #354 | LLM call retry with backoff (*also bundles the #196 metric collector*) | ✅ PR #831 |
| #315 | Persist tool_use/tool_result with parent message id | ✅ PR #832 |
| #352 | Chat cites outdated-ontology warning | ✅ PR #833 |
| **#304** | **JobWorker startup/shutdown in FastHTML lifespan** | **OPEN — re-audit:** `app/main.py:60–141` already wires the worker + archive-scheduler into the lifespan with `_stop_*` events and a 5 s join. Original DoD wanted a 30 s join timeout; verify, then either close as already-shipped or open a 1-line follow-up to bump the timeout. |

### B. Test coverage hardening — TARGET: rolling

| # | Title | Effort | Notes |
|---|---|---|---|
| #102 | VCR cassettes for LLM extraction | M | tests/cassettes/ is empty |
| #316 | Chat unit tests with VCR | M | same |
| #317 | Drafter unit tests with VCR | M | same |
| #308 | tests/fixtures/drafts/ sample data | S | needed for #309 |
| #309 | Per-module Phase 2 unit tests | M | parse, extract, analyze edge cases |
| #680 | Migration 021 — test by SQL execution, not text search | S | quick win |

### C. Eval framework — EPIC #112 — TARGET: Q3-2026

Currently scaffolded (scripts/run_evals.py exists, dependency pinned) but every scenario is a `skip` stub.

- #112 (epic), #151 (chat accuracy), #152 (chat citations), #153 (drafter scenarios), #154 (LLM judge), #156 (weekly CI job)
- Effort: ~2 weeks once scenarios are written. Blocker is **subject-matter scenario authoring**, not code.

### D. Observability + admin polish — EPICS #163, #164, #165 — TARGET: Q3-2026

Infrastructure (Sentry init, `MetricsMiddleware`, `llm_usage`, `audit_log`, sync-status card, jobs page with retry/purge) is wired and the dashboard already registers routes for **audit / performance / analytics / costs / jobs / sync** (`app/templates/admin_dashboard.py:278–292`). The real gaps are (a) data-collectors that populate the metrics table, (b) data completeness inside the existing panels, and (c) the routes that still don't have their own surface.

**Logging stack (#189, #190, #192):**
- #189 install structlog + processor config
- #190 migrate logger calls to structured
- #192 Sentry DSN in Coolify (infra task)
- (#191 Sentry SDK already integrated — closed 2026-05-24 in the audit sweep.)

**Metrics collectors (#195–#197, #323):**
- #195 job execution time (wire `track_duration` in worker)
- #196 LLM call latency (wire in `app/llm/claude.py`)
- #197 SPARQL duration (wire in `app/ontology/sparql_client.py`)
- #323 RAG retrieval latency (wire in `app/rag/retriever.py`)

**Admin panels (#163, #182, #183, #185, #186, #187, #188, #198, #230, #293, #322, #324):**
- Reframed: most routes are *registered*; the gap is data completeness + polish + a few missing surfaces. Finish in priority order:
  1. #182 finish `app/templates/admin_dashboard.py` → `app/admin/` package split (refactor, not a new page)
  2. #198 Performance tab — populate latency percentiles once #195–#197 collectors land
  3. #186 LLM cost dashboard — surface `llm_usage` aggregates by feature/user/org
  4. #322 Sync status panel polish (the card itself ships; this is UX polish + history view)
  5. #185 Usage analytics page — data exists in `usage_daily` view
  6. #187 Enhanced audit log viewer — current route is minimal
  7. #324 Sentry errors link panel
  8. #183 System health aggregator (rollup of /api/health subsystems)
  9. #188 Job monitor polish — retry/purge already shipped, this is filtering + per-handler stats
  10. **#230** Rate-limit config per API key (depends on Phase 5A `api_keys` table — see §E)
  11. **#293** API metrics tab — calls / 429s / latency per API key (depends on Phase 5A + collectors)

**RAG admin (#121, #123):**
- #121 Incremental RAG ingestion hook (sync pipeline → RAG)
- #123 Admin RAG stats page

### E. Phase 5 — Public API + MCP Server — TARGET: Q4-2026

App still does not have an `app/api/`, `app/mcp/`, or a public `app/webhooks/` module (the existing `app/sync/webhook.py` is the *inbound* ontology-sync webhook handler — a different surface). Real future work. Plan in 5 sub-phases.

**Important framing change vs the original draft:** security/governance controls (audit events, SPARQL hardening, webhook secret encryption, MCP audit + rate limits) are **gating** for the endpoints they protect, not a final 5E sweep. The Phase 5 design (`docs/superpowers/specs/2026-04-09-phase5-design.md:7`) and `docs/nfr-baseline.md` §5, §6, §8.3, §9 require them on day one of each endpoint. The mapping below moves them inline.

**5A — Foundation + governance (must-first):**
- #202 epic (API key management), #214 tables (`api_keys`, `api_usage`, `webhook_subscriptions`, `webhook_deliveries`)
- #215–#219 API key CRUD / scopes / expiry / rotation / revocation, #216 management UI
- **#333 API key audit events** — *gates* every endpoint below (NFR §5)
- #203 epic, #220 `app/api/v1/` router, #221 auth middleware
- #204 epic, #222–#229 envelope / error / pagination / rate-limit helpers
- **#230 admin: rate-limit config per key** — ships with rate limiting, not as later admin polish
- **#291 feature flag for API endpoints** — kill-switch before any endpoint goes live

**5B — Endpoints (parallelizable after 5A) — each surface ships with its security controls:**
- Ontology (#205 + #231–#237) — **the SPARQL endpoint #234 is blocked on #357 + #358 SPARQL hardening (NFR §9). Land hardening first or ship #234 last in the group.**
- Provisions (#206 + #238–#241)
- Drafts (#207 + #242–#248) — ships **with** **#334 API draft ownership enforcement** and **#360 API-key scope check for draft ownership**
- Chat + Drafter (#208 + #249–#255)
- Meta + Reports (#327 + #256–#258, #328–#332)

**5C — Webhooks + MCP (parallel with 5B) — security controls inline, not deferred:**
- Webhooks: #210 + #266–#272, **#336 encrypt `webhook_subscriptions.secret` at rest (NFR §6 — required before first delivery), #335 rotation, #359 retry/stale-payload guard**
- MCP: #211 + #273–#284 (tools), **#337 MCP call audit + per-tool rate limit (NFR §5, §8.3 — required before tool exposure), #361 long-running-op audit, #292 MCP feature flag**

**5D — Docs + deploy + cross-cutting testing:**
- #209 + #259–#265 OpenAPI / Swagger / Getting Started / Webhooks guide / MCP setup guide / rate-limits docs
- **#212 epic + #285–#290** — API auth tests, per-resource endpoint tests, webhook delivery tests, OpenAPI spec validation, MCP protocol + e2e tests. (The original draft tucked #285–#290 under the MCP epic; they belong with the cross-cutting test epic #212.)
- #213 Phase 5 deploy epic, #294 Traefik routing, #295 admin API-key creation tool

**5E — Post-launch governance + TARA:**
- #338 AI-generated draft provenance metadata
- #350 CodexProvider implementation (LLM provider swap-readiness)
- #362 epic — NFR baseline enforcement audit across all phases (close the loop on §5/§6/§8/§9 controls)
- #166, #199–#201 TARA activation (stub provider → activation docs → env vars)
- (#319, #320, #321 already closed — access-control work landed in Phase 3 audit)

### F. Phase 4 leftover / design tickets — defer until specified

These are open-ended "edge case" tickets. Either pull into an active sprint with a concrete spec, or close.

- #341 File ingestion edge cases
- #342 Legal-reference edge cases in extractor
- #343 Workflow race conditions
- #344 First-class uncertainty UI
- #351 AI governance edge cases
- #355 Annotation collaboration edge cases
- #149 VTK .docx template (orphaned child of closed VTK epic #111 — either spec it or close)
- #622 EU directive transposition deadlines (#597 reference; mostly covered by A6 #800)

---

## Concrete sprint plan

Section A is done bar #304 (re-audit). The plan below sequences D + B before Phase 5 so that public surfaces launch with metrics, audit visibility, fixtures, and regression coverage already in place.

### Sprint 1 — Admin data + visible admin win — **IN FLIGHT (2026-05-24 PM)**

| # | Status |
|---|---|
| **#182** estimate → **DEFER to Sprint 3** | Done — shim is load-bearing test infrastructure (`_rebind()` rewires `__globals__` so `@patch("app.templates.admin_dashboard.X")` reaches call-time; 14 sites in `tests/test_dashboard.py` depend on this). Not package cleanup. |
| **#195** job execution time (`app/jobs/worker.py`) | Done — on `feat/sprint1-collectors` (`record_metric("job_execution_ms", …)` with `{handler, status}`). |
| **#196** LLM call latency (`app/llm/claude.py`) | ✅ **shipped to origin/main via PR #831** (bundled into the #354 retry refactor at the agent's discretion — the metric wraps the retry-wrapped call cleanly). |
| **#197** SPARQL duration (`app/ontology/sparql_client.py`) | Done — on `feat/sprint1-collectors` (`_execute` is the single instrumented choke-point; `ask()` refactored to route through it). |
| **#323** RAG retrieval latency (`app/rag/retriever.py`) | Done — on `feat/sprint1-collectors` (`feature` kwarg + `record_metric` wrap; 3 callers updated to tag retrievals). |
| **#322** sync-status polish + history view (`app/admin/sync.py`) | Done — on `feat/sprint1-collectors` (`/admin/sync/history` paginated page with "Vaata ajalugu →" link from the card; full shim integration + 7 new tests). |
| Stub-mode smoke test (`tests/test_import_safety.py`) | Done — subprocess test blocks `anthropic` + `voyageai` at `builtins.__import__`, asserts `app.main` imports clean and both SDK singletons stay `None`. |

**Sprint 1 status:** all 5 work-items done; `feat/sprint1-collectors` is 2 commits ahead of origin/main but **11 commits behind** (origin advanced via the Section A wave-2 merges). Needs a rebase before PR. Touched files overlap with merged PRs (notably #311's `retriever.py` signature change and #354's `claude.py` rewrite), so rebase warrants careful conflict review — but `git merge-tree` dry-run shows no marker collisions.

### Sprint 2 — Admin panels on top of real data + test fixtures

1. **#198** Performance tab — backed by #195–#197 collectors.
2. **#186** LLM cost dashboard polish — `llm_usage` aggregates by feature/user/org.
3. **#185** Usage analytics page — `usage_daily` view already exists.
4. **#187** Enhanced audit log viewer — filtering + export.
5. **#308** draft fixtures (`tests/fixtures/drafts/`) + **#680** migration 021 SQL-execution test (quick win) + start **#309** Phase 2 edge-case tests on top of #308.

### Sprint 3 — Test hardening + remaining admin

1. Finish **#309** (parser/extractor/analyzer edge cases).
2. **#102, #316, #317** VCR coverage (LLM extraction, chat, drafter). Cassettes narrow + secrets redacted aggressively.
3. **#182** admin shim refactor (now that polish is in place, the rewrite is a focused move + test-import update of 14 sites).
4. **#183** health aggregator, **#188** job monitor polish, **#324** Sentry errors link panel.

### Phase 5A gate (after Sprint 3)

Start the foundation/governance slice only: **#202, #214–#229, #230, #291, #333**. **Land #221 (auth middleware) and #285 (API auth tests) in the same slice** — auth middleware without auth tests is a regression waiting to happen.

Do *not* start any 5B endpoint group until its gating controls from §E are scheduled:
- Ontology endpoints — hold raw SPARQL (#234) until #357 + #358 hardening land.
- Draft endpoints — only with #334 + #360 ownership enforcement.
- Webhooks — only with #336 secret encryption (NFR §6) + #335 rotation + #359 stale-payload guard.
- MCP — only with #337 audit + per-tool rate limits + #361 long-op audit.

### Cross-cutting

- **#180 pattern (shipped via PR #823):** WebSocket notification delivery now lives in `app/notifications/`. When Phase 5C webhook delivery (#268, #270) and retry/stale-payload guard (#359) start, copy the pattern — durable DB row + outbound delivery + graceful fallback to polling — rather than reinventing it.
- **#311 retriever filters (shipped via PR #830):** the new `filters` kwarg on `Retriever.retrieve()` composes with Sprint 1's `feature` kwarg on the same surface; Phase 5B `/api/v1/provisions/search` and the chat retriever can both use both.
- **#354 retry pattern (shipped via PR #831, file `app/llm/retry.py`):** `retry_sync` / `retry_async` are now generic enough to wrap Voyage embeddings too. Phase 5C webhook delivery retries (#270, #359) should consider reusing the same backoff schedule + classification logic instead of writing their own.
- **Continuously:** section B test-coverage backlog (any agent dispatched on a bug should leave a cassette or fixture behind).
- **Triage call:** section F design tickets — either commit a spec or close as "WONTFIX (specify when needed)".

## Reference

- Per-ticket audit evidence: closure comments on each issue
- Phase status: `CLAUDE.md` "Development Phases"
- NFR baseline: `docs/nfr-baseline.md`
- Phase 5 design: `docs/superpowers/specs/2026-04-09-phase5-design.md`
- Ontology data model: `estonian-legal-ontology-plan.md`
