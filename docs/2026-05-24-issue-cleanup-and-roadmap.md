# 2026-05-24 — Issue cleanup + roadmap

> **Format note.** This doc is rewritten in *directive* form. The top three sections — **Now → Next → After**, **Execution policy**, **Sprints** — answer "what do I do this turn?" without anyone needing to ask. Update them at the end of every session so the next session can pick up cold. Everything below the `---` is reference material (backlog, audit history, reusable patterns).

---

## Now → Next → After

| When | What | Where | Stop and ask if |
|---|---|---|---|
| **NOW** | (1) Rebase `feat/sprint1-collectors` onto `origin/main`, run touched-module tests + ruff + pyright, push, open PR with the body explaining #196-bundled-with-#354. (2) Re-audit **#304** by reading `app/main.py:60–141` — close with evidence link if the 5 s join is acceptable, or open a 1-line follow-up to bump 5 s → 30 s. | `feat/sprint1-collectors` → PR; #304 evidence comment on the issue. | Rebase conflict involves >50 changed lines in one hunk OR breaks a public API contract (signature change in `Retriever.retrieve` or `SparqlClient.query`). |
| **NEXT** | Start **Sprint 2** on a fresh `feat/sprint2-admin-panels` branch off `origin/main`. First commit: **#198 Performance tab** in `app/admin/performance.py` — query the `metrics` table for the four collector series + the existing `http_request_duration_ms`, render p50/p95/p99 via the existing `DataTable` + `Card` primitives. | `app/admin/performance.py` + `tests/test_admin_performance.py`. | The performance tab needs a schema change nobody approved (it shouldn't — `metrics` already has `(name, value, labels)`). |
| **AFTER** | Sprint 2 items 2–5, then Sprint 3 (test hardening + #182 shim refactor + #183/#188/#324). Then **Phase 5A gate** check before any API work. | See Sprints section. | — |

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
  - Anything outside the scope encoded in **Now → Next → After**.

---

## Sprints

### Sprint 1 — Admin data + visible admin win — **DONE pending rebase + PR**

| Item | Status |
|---|---|
| **#182** estimate → DEFER to Sprint 3 | Done. Shim is load-bearing test infrastructure (`_rebind()` rewires `__globals__` so `@patch("app.templates.admin_dashboard.X")` reaches call-time; 14 sites in `tests/test_dashboard.py` depend on it). Not package cleanup. |
| **#195** job execution time (`app/jobs/worker.py`) | Done on `feat/sprint1-collectors` — `record_metric("job_execution_ms", …)` with `{handler, status}`. |
| **#196** LLM call latency (`app/llm/claude.py`) | ✅ Shipped to origin/main via **PR #831** (bundled into the #354 retry refactor — the metric wraps the retry-wrapped call cleanly). |
| **#197** SPARQL duration (`app/ontology/sparql_client.py`) | Done on `feat/sprint1-collectors` — `_execute` is the single instrumented choke-point; `ask()` refactored to route through it. |
| **#323** RAG retrieval latency (`app/rag/retriever.py`) | Done on `feat/sprint1-collectors` — `feature` kwarg + `record_metric` wrap; 3 callers updated to tag retrievals. |
| **#322** sync-status polish + history view (`app/admin/sync.py`) | Done on `feat/sprint1-collectors` — `/admin/sync/history` paginated page + shim integration + 7 new tests. |
| Stub-mode smoke test (`tests/test_import_safety.py`) | Done on `feat/sprint1-collectors` — subprocess test blocks `anthropic`+`voyageai` at `builtins.__import__`, asserts `app.main` imports clean and both SDK singletons stay `None`. |

Branch state: `feat/sprint1-collectors` = 2 ahead / 11 behind `origin/main`. `git merge-tree` shows no conflict markers. Per execution policy → rebase, push, open PR. Then on to **NEXT**.

### Sprint 2 — Admin panels on real data + test fixtures

First commit: **#198 Performance tab** in `app/admin/performance.py`. The route is already registered (`/admin/performance`, see `app/templates/admin_dashboard.py:283`); fill in the page to query the `metrics` table for the four Sprint-1 series + the existing `http_request_duration_ms`. Render p50/p95/p99 via existing `DataTable` + `Card` primitives. Add `tests/test_admin_performance.py` with at least one happy-path + empty-state test.

Then:
2. **#186** LLM cost dashboard polish — `llm_usage` aggregates by feature/user/org. Data + route already exist; flesh out the panel.
3. **#185** Usage analytics page — `usage_daily` view already exists; build the page.
4. **#187** Enhanced audit log viewer — filtering + export. Current route minimal.
5. **#308** draft fixtures (`tests/fixtures/drafts/`) + **#680** migration 021 SQL-execution test (quick win) + start **#309** Phase 2 edge-case tests on top of #308.

### Sprint 3 — Test hardening + remaining admin

First commit: **#309** Phase 2 edge-case tests (parser / extractor / analyzer) on top of #308 fixtures.

Then:
2. **#102, #316, #317** VCR coverage (LLM extraction, chat, drafter). Cassettes narrow + secrets redacted aggressively.
3. **#182** admin shim refactor — focused move + test-import update of 14 sites in `tests/test_dashboard.py`.
4. **#183** health aggregator, **#188** job monitor polish, **#324** Sentry errors link panel.

### Phase 5A gate — DO NOT START until Sprint 3 is in review

Foundation/governance slice only: **#202, #214, #215–#219, #220–#229, #230, #291, #333**. **Land #221 (auth middleware) and #285 (API auth tests) in the same slice** — middleware without tests is a regression waiting.

5B endpoint groups have per-group gating (see §E for the why):
- **Ontology** — hold raw SPARQL (#234) until #357 + #358 hardening land.
- **Drafts** — only with #334 + #360 ownership enforcement.
- **Webhooks** — only with #336 secret encryption (NFR §6) + #335 rotation + #359 stale-payload guard.
- **MCP** — only with #337 audit + per-tool rate limits + #361 long-op audit.

---

## Backlog (the 160 open issues organised by area)

### A. Bugs / quality — DONE except #304

Shipped 2026-05-24 PM via PRs **#823–#833**: #176 #180 #299 #306 #307 #311 #315 #347 #348 #352 #354 (the last also bundled the **#196** metric collector).

| # | Status |
|---|---|
| **#304** | OPEN — re-audit. `app/main.py:60–141` already wires worker + archive-scheduler lifespan with `_stop_*` events + 5 s join. Original DoD asked for 30 s. Per execution policy: verify, then close-with-evidence or open 1-line follow-up. |

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
