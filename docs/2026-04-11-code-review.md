# Code Review

Date: 2026-04-11

Scope: repository-wide review with emphasis on authorization, security/compliance against the project NFRs, destructive flows, logic corner cases, and operational failure modes.

Verification runs:
- `uv run pytest -q` -> 1152 passed, 7 warnings
- `uv run ruff check` -> all checks passed
- `uv run pyright` -> 0 errors, 0 warnings

## Findings

### 1. [High] Draft deletion is org-scoped in code, but owner-only in the permission matrix

Files:
- `docs/nfr-baseline.md:90-91`
- `app/docs/routes.py:642-659`
- `app/docs/routes.py:767-785`

Why this matters:
- The NFR matrix says `Draft (delete)` is allowed only for the owner and system admin.
- The draft detail page renders the `Kustuta eelnõu` form unconditionally for any viewer of the draft detail page.
- The delete handler authorizes only with `draft.org_id == auth.org_id`; it never checks `draft.user_id`, `auth.role`, or any admin override.

Impact:
- Any same-org drafter, reviewer, or org admin who can open a draft can also delete it.
- Because the drafts list is intentionally org-visible, this is not a hard-to-reach edge case; it is a routine same-org destructive action path.
- The implementation also blocks a system admin outside the draft's org, which is the inverse of the documented rule.

Recommended fix:
- Gate both the delete button and the delete handler on `owner || system_admin`.
- Introduce a shared authorization helper for draft deletion instead of re-implementing policy in the route.
- Add regression tests for owner, same-org non-owner, reviewer, org admin, and system admin cases.

### 2. [High] Owner-only chat and drafter resources are effectively org-shared on direct routes

Files:
- `docs/nfr-baseline.md:93-94`
- `app/chat/routes.py:196-224`
- `app/chat/routes.py:582-583`
- `app/chat/routes.py:706-707`
- `app/chat/orchestrator.py:224-227`
- `app/drafter/routes.py:238-262`
- `app/drafter/routes.py:535-536`
- `app/drafter/routes.py:1400-1401`
- `app/drafter/routes.py:1445-1446`

Why this matters:
- The chat list and drafter list are explicitly user-scoped (`list_conversations_for_user`, `fetch_sessions_for_user`), which matches the NFR matrix: conversations and drafter sessions are owner-only resources.
- But direct view/mutate paths check only `org_id`.
- In chat, the page view, delete route, and websocket/orchestrator path all allow access when the caller belongs to the same org as the conversation.
- In drafter, representative detail and step-submission routes do the same for drafting sessions.

Impact:
- A same-org user who obtains a conversation/session UUID can read or mutate another user's private work.
- For chat, that includes deleting the conversation and sending new websocket turns into it.
- For drafter, that includes opening another user's session and advancing or editing workflow steps.
- The documented system-admin read-only exception is also not implemented here.

Recommended fix:
- Treat these as owned resources, not org-shared resources.
- Query by `(id, user_id)` for normal users, then add a narrow admin override where the policy allows it.
- Add permission-matrix tests for both direct HTTP routes and websocket message handling.

### 3. [High] Chat transcripts are stored in plaintext despite the Phase 3 encryption-at-rest requirement

Files:
- `docs/nfr-baseline.md:203`
- `docs/superpowers/specs/2026-04-09-phase3-design.md:7`
- `docs/superpowers/specs/2026-04-09-phase3-design.md:722-723`
- `migrations/008_chat_tables.sql:13-24`
- `app/chat/models.py:291-310`
- `app/chat/orchestrator.py:281-285`
- `app/chat/orchestrator.py:358-369`
- `app/chat/orchestrator.py:397-404`
- `app/chat/orchestrator.py:416-425`

Why this matters:
- The NFR and Phase 3 design both say chat transcripts must be encrypted at rest.
- The schema defines `messages.content` as plain `TEXT`, and the model layer inserts raw user text, raw assistant text, and raw tool-result JSON strings directly into that column.
- `tool_input`, `tool_output`, and `rag_context` also remain plaintext JSONB, which can contain sensitive legal analysis context.

Impact:
- A database snapshot, replica, accidental SQL export, or overly broad read access exposes full chat history in cleartext.
- For this product, those histories can contain politically sensitive draft reasoning and legal strategy, not just generic support chat.

Recommended fix:
- Migrate chat content to encrypted storage at the application layer, matching the draft/drafter pattern.
- Encrypt/decrypt in the chat model layer so routes and orchestrator code stay simple.
- Backfill existing rows or explicitly document/execute a safe data migration plan.

### 4. [Medium] The required LLM prompt scrubber does not exist in the actual LLM call path

Files:
- `docs/nfr-baseline.md:228-235`
- `docs/superpowers/specs/2026-04-09-phase3-design.md:7`
- `docs/superpowers/specs/2026-04-09-phase3-design.md:722`
- `app/observability.py:44-74`
- `app/chat/orchestrator.py:306-314`
- `app/chat/orchestrator.py:470-479`
- `app/drafter/handlers.py:328-335`
- `app/docs/entity_extractor.py:178-181`
- `app/llm/claude.py:129-139`
- `app/llm/claude.py:379-391`

Why this matters:
- The NFR requires every LLM prompt to pass through a scrubber that removes user emails, names, phone numbers, UUIDs, and secrets before transmission.
- The only scrubber implementation in the codebase is the Sentry `before_send` hook in `app/observability.py`.
- Chat, drafter, and entity extraction all build raw prompt strings and pass them straight into the Claude provider, which then forwards `prompt` verbatim to Anthropic.

Impact:
- Any PII or secret-like values embedded in chat history, drafter intent/clarifications, or extracted draft text leave the system unsanitized.
- That contradicts a documented security control for exactly the LLM paths most likely to carry sensitive government drafting material.

Recommended fix:
- Add a shared prompt-scrubbing layer in the LLM adapter boundary, not ad hoc in individual callers.
- Use an allowlist/escape hatch for the few cases where draft text must remain verbatim as the analysis target.
- Add tests proving that representative emails, UUIDs, names, and tokens are redacted before provider calls.

### 5. [Medium] The mandatory 90-day auto-archive warning is not implemented at all

Files:
- `docs/nfr-baseline.md:28`
- `estonian-legal-ontology-plan.md:556`
- `estonian-legal-ontology-plan.md:569`
- `migrations/005_phase2_document_upload.sql:44-72`
- `app/notifications/wire.py:23-120`
- `migrations/012_fix_cascade_and_constraints.sql:61-76`

Why this matters:
- The project documents repeatedly call the 90-day archive warning a mandatory compensating control for sensitive drafts.
- The `drafts` table has no `last_accessed_at` field, so the system cannot even compute inactivity age.
- The notification wiring and notification-type constraint do not include an archive-warning event.

Impact:
- Sensitive pre-publication drafts persist indefinitely without the required “keep or delete” checkpoint.
- This is not just a missing convenience feature; it is a missing retention/safety control that the architecture treats as mandatory.

Recommended fix:
- Add `last_accessed_at` to `drafts` and update it on draft/report/download access.
- Add an archive-warning notification type plus a daily scheduled job that emits the warning and requires explicit user action.
- Add tests for the 90-day threshold, repeat-warning suppression, and user “keep” acknowledgement flow.

### 6. [Medium] Sync still deletes the live ontology before the replacement dataset is proven healthy

Files:
- `app/sync/orchestrator.py:195-229`
- `app/sync/jena_loader.py:99-113`

Why this matters:
- The sync flow still clears the default graph first and only then uploads the replacement Turtle.
- The new code improves logging and aborts if the clear step itself fails, but the core publish pattern is unchanged.
- If upload fails after the clear, or if upload reports success but leaves zero triples, the runtime ontology is empty.

Impact:
- An admin-initiated sync can still cause a full explorer/chat/impact-analysis read outage.
- The system now tells operators that it is degraded, but it does not prevent the degraded state.

Recommended fix:
- Move to a staged publish flow: upload to a staging graph, validate/query it, then promote/swap.
- At minimum, treat a post-upload zero triple count as a failed publish and keep serving the previous graph until promotion succeeds.

### 7. [Medium] The documented file-encryption control and the implemented primitive do not match

Files:
- `docs/nfr-baseline.md:23`
- `estonian-legal-ontology-plan.md:556`
- `estonian-legal-ontology-plan.md:569`
- `migrations/005_phase2_document_upload.sql:52`
- `app/storage/encrypted.py:3-6`
- `app/storage/__init__.py:3-5`

Why this matters:
- The design docs and migration comments say draft files are protected by “AES-256-GCM”.
- The implementation explicitly uses Fernet, which is AES-128-CBC + HMAC-SHA256.
- That means the shipped control is not the same as the documented one.

Impact:
- Security reviews and deployment sign-off can be based on a stronger control than the one actually in production.
- Even if the current primitive is still authenticated encryption, the mismatch is a real compliance and expectation bug for a government-facing system.

Recommended fix:
- Decide which statement is authoritative.
- Either update the documentation everywhere to the exact primitive being used, or replace the implementation with the required scheme and key-management story.

## Broader Risks

### Authorization policy is drifting because it is enforced route-by-route

Evidence:
- `app/docs/routes.py`, `app/chat/routes.py`, and `app/drafter/routes.py` all mix UI rendering, data access, and authorization checks inline.
- The concrete permission bugs above are not isolated mistakes; they all come from the same pattern of “fetch row by id, then do a local check”.

Recommendation:
- Move owner/org/admin authorization into small service-layer helpers with explicit contracts and shared tests.

### Private-draft RAG is still structurally unsafe for future rollout

Evidence:
- `migrations/009_rag_chunks.sql:4-13` has no `org_id` and no FK back to `drafts`.
- `app/rag/retriever.py` returns chunks with no tenant filter.
- The current code comments already acknowledge this is only safe while RAG stays public-corpus-only.

Recommendation:
- Before ingesting any private draft content into `rag_chunks`, add tenant scoping, deletion hooks, and a schema that can support explicit draft cascade.

## Positive Notes

- The repo currently passes `pytest`, `ruff`, and `pyright`.
- The recent fixes for the previous cross-org chat-draft leak and the SPARQL URI injection issue appear to be in place.
- The test suite is large enough that adding regression coverage for the permission matrix and retention/compliance flows should be practical.
