# 2026-05-24 — Issue cleanup + roadmap

> **Format note.** This doc is rewritten in *directive* form. The top three sections — **Now → Next → After**, **Execution policy**, **Sprints** — answer "what do I do this turn?" without anyone needing to ask. Update them at the end of every session so the next session can pick up cold. Everything below the `---` is reference material (backlog, audit history, reusable patterns).

---

## Now → Next → After

> **Status as of 2026-05-25 PM:** Sprints 1, 2, 3 all merged to main (PRs #835/#837/#838). All major roadmap §A/§D work is shipped. Remaining work is small follow-ups (#836, #309 finish) + the deferred VCR session, then the Phase 5A decision.

| When | What | Where | Stop and ask if |
|---|---|---|---|
| **NOW** | (1) Implement **#836** — bump worker + archive-scheduler join timeout 5s → 30s in `app/main.py:134` + `:140` per the original #304 DoD. 2-line code change. (2) **Finish #309** — extend `tests/test_phase2_edge_cases.py` from 9 tests to comprehensive `extract_handler` + `analyze_handler` coverage using the existing `tests/fixtures/drafts/` fixtures. Open one combined PR (`feat/sprint4-followups`). | `app/main.py` + `tests/test_phase2_edge_cases.py`. | The 30s timeout breaks the test suite (it shouldn't — `DISABLE_BACKGROUND_WORKER=1` skips the threads in tests). |
| **NEXT** | **VCR cassette recording session** for #102 / #316 / #317 — needs `ANTHROPIC_API_KEY` + `VOYAGE_API_KEY` in env. Agent can scaffold vcrpy fixtures + write test shells offline, but the actual cassette content requires live API hits from the user. | `tests/cassettes/` + `tests/test_*_vcr.py`. | The user has not authorized burning live LLM tokens for cassette recording. |
| **AFTER** | **Phase 5A decision point.** The doc's gating rules are encoded in §E below; starting Phase 5A is a multi-week, multi-sprint commitment touching ~60 issues. Worth a fresh planning conversation before launch. | See §E. | Always — Phase 5A start is a stop-and-ask threshold, even though the gating-order rule is satisfied. |

---

## Execution policy (defaults — don't re-ask)

These rules let any session pick up and execute. Override only when the user explicitly asks.

- **Rebase**: when `git merge-tree $(git merge-base A B) A B` shows no `<<<<` markers, rebase without asking. If the real rebase produces conflicts, attempt resolution; only stop if a single hunk is >50 lines or breaks a public API contract.
- **Re-audit tickets** (like #304): read the cited code, verify the DoD, then either close with an evidence link in the issue comment or open a 1-line follow-up issue. Do not surface as a question.
- **PR opening vs merging**: open the PR (push + `gh pr create`). **Never merge** — the PR-review-gate memory rule keeps merge under the user's control.
- **Agent scope creep** (e.g., #196 bundled into #354): if the resulting commit is sound + tested, accept the bundling. Note it in the PR body. Do not try to unbundle.
- **Sprint progression**: when the current sprint's PR enters review (pushed + opened), start the next sprint on a new branch off `origin/main`. Do not wait for merge.
- **Doc + memory updates**: when the world shifts (parallel PRs land, scope changes, decisions made), update this doc + the relevant memory entry in the same session. Do not open a separate "audit" issue.
- **Parallel-session reality**: this project frequently has 5–10 sibling agent worktrees racing. Re-check `gh pr list --state merged --search "merged:>=$(date -u +%Y-%m-%d)"` and `git log origin/main` before starting any sprint to absorb whatever shipped since the last update.
- **Format-before-commit**: pre-commit hook runs `ruff format`. Run it yourself first so the hook is a no-op.
- **Stop-and-ask thresholds** (the *only* things worth surfacing):
  - Destructive git ops on shared state (force-push to main, `git reset --hard` on a pushed branch, dropping a branch with unmerged commits).
  - Migration changes, schema drops, anything touching prod data.
  - Plan changes that drop a whole sprint or reverse the gating order (e.g., starting Phase 5A before Sprint 3).
  - **Phase 5A launch** — even with the gating-order rule satisfied (Sprint 3 in review/merged), starting Phase 5A is a multi-week commitment touching ~60 issues. Always get explicit go-ahead.
  - Anything outside the scope encoded in **Now → Next → After**.

---

## Sprints

### Sprint 1 — Admin data + visible admin win — ✅ **MERGED** via PR #835 (commit `fec5340`, 2026-05-25)

- **#195** job_execution_ms collector in `app/jobs/worker.py` (records `{handler, status}` with `status="success"|"failed"` post-review fix).
- **#196** llm_call_ms collector — shipped earlier via PR #831 (bundled into #354 retry refactor).
- **#197** sparql_query_ms collector in `app/ontology/sparql_client.py` (`_execute` choke-point).
- **#323** rag_retrieval_ms collector in `app/rag/retriever.py` (with `feature` kwarg + 3 callers updated).
- **#322** `/admin/sync/history` paginated page.
- **Stub-mode smoke test** in `tests/test_import_safety.py` — locks down the lazy-init SDK contract.

### Sprint 2 — Admin panels on real data + test fixtures — ✅ **MERGED** via PR #837 (commit `8db03ff`, 2026-05-25)

- **#198** Performance tab — all 5 metric series surfaced with p50/p95/p99 + breakdowns + window selector.
- **#186** LLM cost dashboard polish — window + org filter + top-10 spenders + sparkline + CSV export.
- **#185** Usage analytics page — window + per-org + refresh button + CSV export + sparklines.
- **#187** Audit log enhancements — multi-select filters + JSONB detail expander + filter-aware CSV.
- **#308 + #680 + #309-start** — draft fixtures + migration 021 SQL-execution test + 9 Phase 2 edge-case tests.
- **Review fixes**: pyright (8 sites), interval `%s` SQL bug (7 sites), monthly trend org filter.

### Sprint 3 — Shim refactor + remaining admin — ✅ **MERGED** via PR #838 (commit `90d11b8`, 2026-05-25)

- **#182** admin shim refactor — `app/templates/admin_dashboard.py` 291 → 37 lines; `register_admin_routes` lives in `app/admin/routes.py` as the single source of truth; `_rebind()` machinery + `_EXPECTED_PAGE_HANDLERS` invariant deleted; 14 test patch sites migrated to real modules.
- **#183** system health aggregator card + `/admin/health/aggregator` page.
- **#188** job monitor polish — filtering, per-handler 24h stats, HTMX detail expand at `/admin/jobs/{id}/detail`.
- **#324** Sentry errors link panel — env-gated, three render modes, no new dependency.
- **Review fixes**: 5 cross-sprint routes preserved (`/admin/sync/history`, `/admin/audit/detail/{id}`, `/admin/analytics/refresh`, `/admin/analytics/export`, `/admin/costs/export`); job monitor `IN ('ok', 'success')` for backward compat with pre-2026-05-25 metric rows.

### Sprint 4 follow-ups — IN FLIGHT on `feat/sprint4-followups`

- **#836** — bump worker + archive-scheduler lifespan join timeout 5s → 30s (the #304 follow-up).
- **#309 finish** — extend Phase 2 edge-case tests to full `extract_handler` + `analyze_handler` coverage (now unblocked since `tests/fixtures/drafts/` landed via Sprint 2).
- **Doc refresh** (this commit).

### Phase 5A gate — open per execution policy, but NEEDS USER GO-AHEAD before launch

Sprint 3 is merged, so the technical gate is open. But Phase 5A is a multi-week / ~60-issue commitment, so per Stop-and-ask thresholds above it warrants a fresh planning conversation before any agent dispatch.

Foundation/governance slice scope (unchanged from prior versions): **#202, #214, #215–#219, #220–#229, #230, #291, #333**. **Land #221 (auth middleware) and #285 (API auth tests) in the same slice.**

5B endpoint groups gating (unchanged):
- **Ontology** — hold raw SPARQL (#234) until #357 + #358 hardening land.
- **Drafts** — only with #334 + #360 ownership enforcement.
- **Webhooks** — only with #336 secret encryption (NFR §6) + #335 rotation + #359 stale-payload guard.
- **MCP** — only with #337 audit + per-tool rate limits + #361 long-op audit.

---

## Backlog (the 160 open issues organised by area)

### A. Bugs / quality — DONE except #836 (follow-up)

Shipped 2026-05-24 PM via PRs **#823–#833**: #176 #180 #299 #306 #307 #311 #315 #347 #348 #352 #354 (the last also bundled the **#196** metric collector). **#304** re-audited 2026-05-25 and closed-with-evidence pointing at `app/main.py:60–141`.

| # | Status |
|---|---|
| **#836** | OPEN — the 2-line follow-up from the #304 audit. Bumps worker + archive-scheduler lifespan join timeout 5s → 30s. Being shipped in Sprint 4 follow-ups (see Sprints section). |

### B. Test coverage hardening — pulled into Sprints 2-3

| # | Title | Effort | Notes |
|---|---|---|---|
| #680 | Migration 021 — SQL execution test | S | Sprint 2 quick win |
| #308 | `tests/fixtures/drafts/` sample data | S | Sprint 2, blocks #309 |
| #309 | Per-module Phase 2 unit tests | M | Sprint 2-3 |
| #102 | VCR cassettes for LLM extraction | M | Sprint 3 |
| #316 | Chat unit tests with VCR | M | Sprint 3 |
| #317 | Drafter unit tests with VCR | M | Sprint 3 |

### C. Eval framework — EPIC #112 — Q3-2026

Scaffolded (`scripts/run_evals.py`, dependency pinned) but every scenario is a `skip` stub. Items: #112 (epic), #151 #152 #153 #154 #156. **Blocker is subject-matter scenario authoring, not code** — needs user time, not a sprint slot.

### D. Observability + admin polish — EPICS #163, #164, #165 — Sprints 1–3

Infrastructure wired; admin dashboard already registers routes for **audit / performance / analytics / costs / jobs / sync** (`app/templates/admin_dashboard.py:278–292`). Gaps: collectors (Sprint 1 ✅) + data completeness in existing panels (Sprint 2-3).

**Logging stack:** #189 structlog config · #190 logger-call migration · #192 Sentry DSN in Coolify · (#191 Sentry SDK already integrated — closed 2026-05-24).

**Metrics collectors:** all 4 done — #195 worker ✅ Sprint 1 · #196 LLM ✅ via PR #831 · #197 SPARQL ✅ Sprint 1 · #323 RAG ✅ Sprint 1.

**Admin panels** (priority order, mapped to Sprints):
1. #198 Performance tab — Sprint 2
2. #186 LLM cost dashboard — Sprint 2
3. #185 Usage analytics — Sprint 2
4. #187 Audit log viewer — Sprint 2
5. #322 Sync history — ✅ Sprint 1
6. #182 Shim refactor — Sprint 3
7. #183 Health aggregator — Sprint 3
8. #188 Job monitor polish — Sprint 3
9. #324 Sentry errors panel — Sprint 3
10. #230 Rate-limit config per API key — Phase 5A (depends on `api_keys` table)
11. #293 API metrics tab — Phase 5A (depends on `api_usage`)

**RAG admin:** #121 (incremental ingestion hook), #123 (admin RAG stats page) — backlog, after Sprint 3.

### E. Phase 5 — Public API + MCP Server — Q4-2026

`app/api/`, `app/mcp/`, public `app/webhooks/` don't exist yet (the existing `app/sync/webhook.py` is the *inbound* ontology-sync webhook — different surface).

**Framing rule (do not relax):** security/governance controls are **gating** per-endpoint, not a final 5E sweep. Per Phase 5 design (`docs/superpowers/specs/2026-04-09-phase5-design.md:7`) and `docs/nfr-baseline.md` §5/§6/§8.3/§9 — they ship on day one of each endpoint.

**5A — Foundation + governance (must-first):** #202 epic · #214 tables (`api_keys`, `api_usage`, `webhook_subscriptions`, `webhook_deliveries`) · #215–#219 API key CRUD/scopes/expiry/rotation/revocation · #216 management UI · **#333 API key audit events (NFR §5 — gates every endpoint)** · #203 epic · #220 `app/api/v1/` router · #221 auth middleware · #204 epic · #222–#229 envelope/error/pagination/rate-limit helpers · **#230** admin rate-limit config per key · **#291** feature flag for API endpoints.

**5B — Endpoints (parallelizable after 5A), each surface ships with its security controls:**
- Ontology (#205 + #231–#237) — **#234 raw SPARQL blocked on #357 + #358 hardening (NFR §9)**
- Provisions (#206 + #238–#241)
- Drafts (#207 + #242–#248) — **with #334 ownership enforcement + #360 API-key scope check**
- Chat + Drafter (#208 + #249–#255)
- Meta + Reports (#327 + #256–#258, #328–#332)

**5C — Webhooks + MCP (parallel with 5B):**
- Webhooks: #210 + #266–#272 + **#336 encrypt `webhook_subscriptions.secret` (NFR §6)** + #335 rotation + #359 stale-payload guard
- MCP: #211 + #273–#284 tools + **#337 audit + per-tool rate limit (NFR §5, §8.3)** + #361 long-op audit + #292 feature flag

**5D — Docs + deploy + cross-cutting testing:**
- #209 + #259–#265 OpenAPI / Swagger / docs sites
- **#212 epic + #285–#290** — auth tests, per-resource endpoint tests, webhook delivery tests, OpenAPI validation, MCP protocol + e2e tests
- #213 deploy epic, #294 Traefik routing, #295 admin API-key creation tool

**5E — Post-launch governance + TARA:** #338 AI-generated draft provenance · #350 CodexProvider · #362 NFR baseline enforcement audit · #166, #199–#201 TARA activation. (#319/#320/#321 already closed — access-control landed in Phase 3 audit.)

### F. Phase 4 leftover / design tickets — defer until specified

Open-ended "edge case" tickets. Either pull into an active sprint with a concrete spec, or close: #341 file ingestion · #342 legal-reference edges · #343 workflow races · #344 first-class uncertainty UI · #351 AI governance edges · #355 annotation collaboration edges · #149 VTK .docx template (orphaned child of closed #111) · #622 EU directive transposition deadlines (mostly covered by A6 #800).

---

## Cleanup pass (historical audit evidence)

2026-05-24 audit closed **157 tickets** in the morning sweep, then a same-day **wave 2 of 10** Section A items shipped via PRs #823–#833. Net: `327 − 160 open = 167 closed`.

| Group | Count | Range / notes |
|---|---|---|
| Recent prod-verification bugs | 16 | #801–#817 (excl. #816 already closed). PRs #818–#822. #804's TDZ fix silently unblocked #806 + #807. |
| Epic #784 ontology six use cases | 17 | #784 + #785–#800 (C0–C6, B1–B3, A1–A6) |
| Phase 2 (Modules 3+4) | 34 | #69–#101, #103 |
| Phase 3 (Modules 5+6) | 48 | #104, **#105 (RAG epic — children #121 + #123 still open)**, #106–#110, **#111 (VTK epic — child #149 still open)**, #113–#120, #122, #124–#148, #150, #155, #157–#160. The 48-count includes both parent epics — closed `2026-05-24T10:42 UTC`. |
| Phase 4 Annotations | 14 | #161, #167–#175, #297, #325, #326, #356 |
| Phase 4 Notifications | 11 | #162, #177–#179, #296, #298, #300–#303, #346 |
| Phase 4 Admin/Observability (subset) | 5 | #184, #191, #193, #194, #340 |
| Misc completed | 12 | #305, #310, #312–#314, #318–#321, #345, #349, #353 |
| **Subtotal (morning audit)** | **157** |  |
| Section A wave 2 (PRs #823–#833) | 10 | #176 (#825), #180 (#823), #299 (#824), #306 (#826), #307 (#827), #311 (#830), #315 (#832), #347 (#828), #348 (#829), #352 (#833). #354 (PR #831) also bundled the **#196** LLM-latency collector. |
| **Total closed** | **167** |  |

> **Note (revision pass, 2026-05-24 PM):** original draft listed #105 + #111 as "Skipped"; both parent epics were closed by the same audit run (only the children #121, #123, #149 remain). Counts unchanged.
>
> **Note (evening pass, 2026-05-24 PM):** Section A drained almost entirely during the audit afternoon — parallel agent-driven PRs. #354's PR #831 also pulled #196 along for the ride.

Zero `bug`-labeled tickets remain — all 17 prod-verification bugs from 2026-05-19 are closed.

GitHub anti-abuse rate-limits `addComment` hard at >10/min, so closure comments fail silently while the close itself succeeds. For bulk closes: prefer no-comment close + a single audit doc.

---

## Cross-cutting patterns to reuse

- **#180 pattern (PR #823, `app/notifications/`):** durable DB row + outbound delivery + graceful polling fallback. Template for Phase 5C webhook delivery/retry (#268, #270, #359).
- **#311 filters (PR #830, `app/rag/retriever.py`):** `Retriever.retrieve(filters=…, feature=…)` composes for `/api/v1/provisions/search` (Phase 5B) and the chat retriever.
- **#354 retry (PR #831, `app/llm/retry.py`):** `retry_sync` / `retry_async` generic enough to wrap Voyage embeddings + Phase 5C webhook deliveries. Same backoff schedule + classification.

---

## Reference

- Per-ticket audit evidence: closure comments on each issue.
- Phase status: `CLAUDE.md` "Development Phases".
- NFR baseline: `docs/nfr-baseline.md`.
- Phase 5 design: `docs/superpowers/specs/2026-04-09-phase5-design.md`.
- Ontology data model: `estonian-legal-ontology-plan.md`.
- Stub-mode test contract: `tests/test_import_safety.py::test_app_main_imports_without_constructing_sdk_singletons` — keep this green; the four Sprint-1 collectors all rely on it.
