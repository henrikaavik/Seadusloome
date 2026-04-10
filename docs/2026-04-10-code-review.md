# Code Review

Date: 2026-04-10

Scope: repository-wide review with emphasis on security boundaries, logic/usability errors, corner cases, operational failure modes, and big-picture maintainability. Verification run: `uv run pytest -q` -> 975 passed, 7 warnings.

## Findings

### 1. [High] Chat draft context can cross organisation boundaries and leak draft metadata

Files:
- `app/chat/routes.py:303-349`
- `app/chat/routes.py:364-375`
- `app/chat/routes.py:586-595`
- `app/chat/orchestrator.py:68-103`
- `app/chat/orchestrator.py:233-242`
- `migrations/008_chat_tables.sql:3-10`

Why this matters:
- `new_conversation()` accepts any `?draft=<uuid>` and stores it as `context_draft_id` after only UUID parsing.
- `_get_draft_title()` reads `drafts.filename` by `id` only, with no `org_id` check.
- `_load_impact_summary()` reads the latest `impact_reports` row by `draft_id` only, again with no `org_id` check.
- The `conversations.context_draft_id` foreign key guarantees the draft exists, but it does not guarantee that `conversation.org_id == draft.org_id`.

Impact:
- A user can create their own conversation bound to another organisation's draft UUID.
- The chat UI will then display that draft title, and the orchestrator will inject the foreign draft's latest impact summary into the system prompt.
- This is a real tenant-isolation break, not just a cosmetic mismatch.

Corner cases:
- If the foreign draft later becomes inaccessible or deleted, the conversation still carries stale context and the UI link points to a route that will 404.
- There is already stricter org-scoping in `get_draft_impact`; this path bypasses that protection entirely.

Recommended fix:
- Validate draft ownership before creating the conversation.
- Re-check ownership when rendering the conversation page and when loading impact context.
- Change the helper queries to join through `drafts` and require the authenticated `org_id`.
- Add a regression test for "user in org A cannot start a conversation with org B draft UUID".

### 2. [High] `get_provision_details` interpolates unvalidated URIs into SPARQL syntax

Files:
- `app/chat/tools.py:287-305`
- `app/ontology/sparql_client.py:18-28`

Why this matters:
- `_sanitize_sparql_value()` only escapes characters needed for string literals.
- `_exec_get_provision_details()` then inserts that value into either `BIND(estleg:...)` or `BIND(<...>)`, which is not a string-literal context.
- Characters such as `<`, `>`, whitespace, braces, or extra prefixed-name syntax are not rejected.

Impact:
- A hostile or malformed `provision_uri` can break the query shape or inject extra SPARQL tokens.
- Best case: tool failures and noisy logs.
- Worse case: the model can be induced to run broader or more expensive queries than intended, defeating the "single provision lookup" contract.

Recommended fix:
- Replace the current sanitisation with strict validation.
- Allow only a narrow `estleg:` local-name pattern for prefixed names and a strict absolute-URI regex for full URIs.
- Reject everything else before query construction.
- Add tests for malformed values such as embedded `>`, spaces, braces, and extra clauses.

### 3. [Medium] Sync clears the live ontology before the replacement upload is known to be good

Files:
- `app/sync/orchestrator.py:195-203`
- `app/sync/jena_loader.py:72-121`

Why this matters:
- `run_sync()` deletes the default graph first and only then uploads the new Turtle payload.
- If the upload fails after the delete succeeds, the runtime ontology is left empty until the next successful sync.
- `clear_default_graph()` returns a boolean, but the caller ignores it.

Impact:
- A transient Fuseki/network error during deploy can turn into a full read outage for explorer, impact analysis, and chat ontology queries.
- The failure mode is especially bad because it happens exactly during an admin-initiated sync.

Recommended fix:
- Move to a two-phase publish flow: upload into a staging graph, validate/query it, then swap/promote.
- At minimum, stop immediately if `clear_default_graph()` fails and add a rollback strategy or health gate on post-sync triple count.

### 4. [Low] Large impact reports expose a dead-end CTA

File:
- `app/docs/report_routes.py:281-289`

Why this matters:
- When the affected-entities table exceeds `_MAX_INLINE_ROWS`, the UI renders "Vaata kõiki" with `href="#"`.
- This only appears on larger reports, which is exactly when users most need a real drill-down path.

Impact:
- The report page promises additional detail but provides no navigation.
- For users doing legal impact review, that is a usability failure in a high-value workflow.

Recommended fix:
- Either remove the CTA until the full view exists, or wire it to a real paginated route/modal.

## Broader Risks

### Authorization logic is spread across very large route files

Evidence:
- `app/drafter/routes.py` is 2453 lines.
- `app/docs/routes.py` is 879 lines.
- `app/docs/report_routes.py` is 783 lines.
- `app/chat/routes.py` is 731 lines.
- `app/auth/users.py` is 718 lines.

Why it matters:
- These modules mix HTML rendering, auth checks, DB calls, and orchestration in the same file.
- The concrete chat draft leak above looks like the kind of inconsistency that becomes more likely when authorization is route-local instead of enforced through a narrower service layer.

Recommendation:
- Split by use case and move org-scoped read/write operations behind small service functions with explicit auth contracts.

### Background job throughput is intentionally serial

Files:
- `app/main.py:67-80`
- `app/jobs/worker.py:93-196`

Why it matters:
- One process starts one daemon worker thread.
- Each tick claims and executes at most one job.
- For long-running parse, extraction, impact, or export jobs, queue latency will rise quickly under concurrent use.

Recommendation:
- If the target remains 5-50 concurrent officials, measure queue wait time under realistic job mixes and decide whether to add worker concurrency before production usage expands.

### RAG is safe for the current public corpus, but not yet for private draft retrieval

Files:
- `app/chat/orchestrator.py:244-273`
- `app/rag/retriever.py:55-106`
- `scripts/ingest_rag.py:74-126`

Why it matters:
- Current ingestion only loads public ontology, court, and EU material, so the present implementation is acceptable.
- But the retriever API has no org/user filter, and the chat path blindly injects retrieved chunks into the prompt.
- If private draft chunks are later added to `rag_chunks`, the current retrieval contract becomes a tenant-leak vector.

Recommendation:
- Treat org-scoped/private retrieval as a required design constraint before any draft content is ingested into the shared vector store.

### Import-time coupling to PostgreSQL reduces modularity and test ergonomics

Files:
- `app/auth/__init__.py:1-7`
- `app/auth/audit.py:12-45`
- `app/db.py:7-31`

Why it matters:
- Importing `app.auth` eagerly imports `app.auth.audit`, which imports `app.db`, which imports `psycopg` immediately.
- That makes broad parts of the codebase non-importable without DB driver availability, even when the caller only wants auth helpers.

Recommendation:
- Keep package `__init__` files light and avoid pulling DB-backed modules into import side effects.

## Positive Notes

- The test suite is substantial and fast enough to run routinely.
- Org-scoping is implemented consistently in many draft/report routes and in `get_draft_impact`; the main security issue here is inconsistency, not total absence of access-control thinking.
- The codebase shows deliberate attention to operational notes and migration history, which makes review easier.

## Verification

- `uv run pytest -q` on 2026-04-10: 975 passed, 7 warnings.
