# Non-Functional Requirements Baseline

**Status:** Canonical. Every phase spec and GitHub issue must conform to this document.
**Last updated:** 2026-04-09
**Owner:** Henrik Aavik

---

## 1. Purpose

This document is the single source of truth for security, privacy, access control, retention, audit, accessibility, and operational requirements. When any other document (CLAUDE.md, AGENTS.md, phase specs, GitHub issues) contradicts this file, **this file wins**. Ambiguities are to be resolved here and propagated outward.

---

## 2. Draft Retention Policy

**Decision:** Drafts persist in the system until the owner explicitly deletes them.

**Rationale:** Legal drafters work on documents over days or weeks and need to come back to previous analyses. Session-scoped deletion would force constant re-uploads and destroy analysis context.

**Mandatory compensating controls:**

1. **File encryption** — AES-256-GCM via Fernet with key versioning; `DRAFT_ENCRYPTION_KEY` in Coolify secrets
2. **Encrypted parsed text** — `drafts.parsed_text` column encrypted at rest via Fernet (not just "assumed" DB-level encryption)
3. **Encrypted drafting sessions** — `drafting_sessions.draft_content` JSONB encrypted via the same Fernet key
4. **Strict org-scoped access** — every query includes `WHERE org_id = %s`; cross-org access returns 404 (not 403) to prevent enumeration
5. **Full audit logging** — every access (view, download, reanalyze, delete) produces an `audit_log` entry
6. **90-day auto-archive warning** — daily cron job checks `drafts.last_accessed_at`; sends notification if >90 days; user must confirm "keep" or "delete"
7. **Explicit delete cascade** — removes DB rows, decrypts + unlinks file, drops Jena named graph, removes RAG chunks, preserves audit events
8. **Ownership model** — each draft has exactly one `user_id` owner; sharing is explicit via Phase 4 collaboration features

**Retention limits:**
- No maximum retention period — users decide
- Annual admin review of drafts with no access in 180+ days (admin notification, not auto-delete)
- If a user is deactivated, their drafts are assigned to their org admin

---

## 3. LLM Provider Policy

**Decision:** Both Claude and Codex are first-class LLM providers. Claude is the default.

**Implementation:**
- `LLMProvider` ABC with two concrete implementations: `ClaudeProvider` and `CodexProvider`
- Provider selected at app startup via `LLM_PROVIDER` env var (values: `claude`, `codex`)
- Default: `claude`
- Both adapters support: complete, stream, tool_use
- Cost tracking and PII scrubbing apply equally to both

**Secrets:**
- `ANTHROPIC_API_KEY` (required if provider = claude)
- `CODEX_API_KEY` (required if provider = codex)
- `LLM_PROVIDER` (optional, default `claude`)

**Why both matter:**
- Claude: better Estonian legal language handling in internal testing
- Codex: available as a fallback if Claude is unavailable or if a ministry mandates a different provider

**Future providers:** Ollama (local), MS AI Foundry, Voyage AI (embeddings only) plug into the same abstraction.

---

## 4. Authentication & Authorization

### 4.1 Authentication sources

1. **JWT** (Phase 1) — email + password, HttpOnly cookies, refresh tokens in `sessions` table, `is_active` enforced
2. **TARA SSO** (Phase 4+) — Estonian government OIDC provider, stub implementation in Phase 4, activation post-Phase 5

### 4.2 Cookie requirements

- `HttpOnly`, `SameSite=Lax`, `Secure=true` in production (via `COOKIE_SECURE` env var, default `true`)
- Access token: 1h lifetime
- Refresh token: 30d lifetime, rotated on each refresh
- Logout deletes session row

### 4.3 Role matrix

| Role | Scope |
|------|-------|
| `drafter` | Upload drafts, analyze, view own + org drafts, use chat/drafter, view explorer |
| `reviewer` | Read-only for org drafts, comment (Phase 4), use explorer |
| `org_admin` | Drafter + reviewer capabilities + manage users in own org |
| `admin` | System-wide access, all orgs, admin dashboard |

### 4.4 Permission matrix (canonical)

| Resource | Owner | Same org (drafter) | Same org (reviewer) | Same org (org_admin) | System admin |
|----------|-------|---------------------|----------------------|-----------------------|--------------|
| Draft (R/W) | R/W | R/W | R only | R/W | R/W |
| Draft (delete) | Yes | No | No | No | Yes |
| Impact report | R | R | R | R | R |
| Chat conversation | R/W (own) | No | No | No | R only |
| Drafter session | R/W (own) | No | No | No | R only |
| Annotation (create) | — | Yes | Yes | Yes | Yes |
| Annotation (delete own) | Yes | — | — | — | Yes |
| Annotation (delete any) | No | No | No | Yes | Yes |
| Organization | — | R (own only) | R (own only) | R/W (own only) | R/W (all) |
| User list (org) | — | No | No | R/W (own) | R/W (all) |
| API key (own) | R/W | — | — | — | R/W (all) |
| Webhook subscription | R/W (own org) | — | — | R/W (own org) | R/W (all) |
| MCP tool call | Yes (own key) | — | — | — | Yes |
| Audit log | No | No | No | Own org only | All |
| Sync status | No | No | No | No | Yes |
| System health | No | No | No | No | Yes |
| Cost dashboard | No | No | No | Own org only | All |

**Enforcement rule:** Every route handler must either call a `require_role` / `require_org_member` decorator or implement equivalent checks inline with tests.

**Enumeration defense:** Cross-org or cross-user access returns `404 Not Found`, not `403 Forbidden`, to prevent enumeration attacks.

### 4.5 API key scopes (Phase 5)

| Scope | Grants |
|-------|--------|
| `read:ontology` | Ontology GET endpoints |
| `sparql:execute` | Raw SPARQL (with hard limits — see §10) |
| `read:drafts` | List and read drafts in key's org |
| `write:drafts` | Upload/delete/reanalyze drafts in key's org |
| `read:reports` | Impact reports |
| `use:chat` | Create chat sessions |
| `use:drafter` | Create drafting sessions |
| `admin:webhooks` | Manage webhook subscriptions |
| `mcp:query` | Read-only MCP tools |
| `mcp:analyze` | Analyze draft tool |
| `mcp:draft` | Law drafter tool |

---

## 5. Audit Logging

**Principle:** Every security-relevant action produces an `audit_log` entry. Audit log rows are never deleted (even when the target resource is deleted).

### 5.1 Required audit events

**Authentication:**
- `auth.login`, `auth.login_failed`, `auth.logout`, `auth.token_refresh`, `auth.token_expired`

**Users:**
- `user.create`, `user.update`, `user.role_change`, `user.deactivate`, `user.reactivate`

**Organizations:**
- `org.create`, `org.update`, `org.delete`

**Drafts:**
- `draft.upload`, `draft.view`, `draft.download`, `draft.parse`, `draft.extract`, `draft.analyze`, `draft.reanalyze`, `draft.report.view`, `draft.report.export`, `draft.delete`

**Chat & Drafter:**
- `chat.conversation.create`, `chat.message.send`, `chat.message.tool_call`, `chat.conversation.delete`
- `drafter.session.create`, `drafter.step.submit`, `drafter.session.export`, `drafter.session.delete`

**LLM:**
- `llm.call` — per-call record with feature, model, tokens, cost (NOT prompts or responses)

**Annotations:**
- `annotation.create`, `annotation.reply`, `annotation.resolve`, `annotation.delete`

**API & MCP:**
- `api_key.create`, `api_key.use` (first successful), `api_key.expired`, `api_key.revoke`
- `mcp.tool_call` (tool name, input hash, result size)

**Webhooks:**
- `webhook.subscription.create`, `webhook.delivery.sent`, `webhook.delivery.failed`

**Sync:**
- `sync.started`, `sync.completed`, `sync.failed`

### 5.2 Audit log schema

```sql
audit_log (
    id          BIGSERIAL PRIMARY KEY,
    user_id     UUID REFERENCES users(id),   -- NULL for system events
    org_id      UUID REFERENCES organizations(id),
    action      TEXT NOT NULL,
    resource_type TEXT,
    resource_id TEXT,
    detail      JSONB,
    ip_address  INET,
    user_agent  TEXT,
    created_at  TIMESTAMPTZ DEFAULT now()
)
```

### 5.3 Retention

- Minimum 7 years (government compliance)
- Stored in same Postgres as application data
- Backed up via Coolify managed backups
- Not purged by any code path

---

## 6. Encryption at Rest

### 6.1 Mandatory encryption

| Data | Mechanism | Key source |
|------|-----------|------------|
| Draft file content | AES-256-GCM (Fernet) | `DRAFT_ENCRYPTION_KEY` (Coolify secret) |
| `drafts.parsed_text` | Application-level Fernet encryption | same key |
| `drafting_sessions.draft_content` | Application-level Fernet | same key |
| `messages.content` (chat) | Application-level Fernet | same key |
| `webhook_subscriptions.secret` | Application-level Fernet | same key |
| Postgres data at rest | Coolify-managed volume encryption | OS-level |
| Jena TDB2 store | Coolify-managed volume encryption | OS-level |

**"Assumed DB-level encryption" is NOT an acceptable control.** Every sensitive column must be explicitly wrapped by application code using Fernet. DB-level volume encryption is a defense-in-depth second layer.

### 6.2 Key rotation

- Keys versioned via `key_version` columns on sensitive tables
- New writes use current version; old writes remain readable
- Rotation is a manual operation requiring admin action + re-encryption job

### 6.3 In transit

- TLS 1.3 only, enforced by Traefik
- All internal service-to-service calls use internal Docker network (not exposed)
- Coolify Let's Encrypt auto-renewal

---

## 7. PII Scrubbing

**Principle:** User personally identifiable information must never be sent to LLM providers, error tracking, or webhooks.

### 7.1 LLM prompt scrubbing

Before every LLM call, the prompt is passed through a scrubber that removes:
- User email addresses, names, phone numbers
- User/org UUIDs (replaced with placeholder tokens)
- API keys, tokens, session IDs

Draft content is sent verbatim (it's the analysis target), but draft metadata (uploader name, org name) is stripped.

### 7.2 Sentry scrubbing

`sentry_sdk` configured with `send_default_pii=False` and a custom `before_send` hook that strips:
- Request cookies, Authorization headers
- User object fields except `id`
- Request body (forms may contain passwords)

### 7.3 Webhook payload scrubbing

Webhook events include only:
- Resource identifiers (UUIDs)
- Status codes and timestamps
- NOT content (no draft text, no chat messages, no user PII)

---

## 8. Rate Limiting & Abuse Prevention

### 8.1 Web UI

- Login: 5 failed attempts per IP per 15 minutes → temporary block
- Upload: 10 drafts per user per hour
- Chat: 100 messages per user per hour
- Drafter sessions: 5 per user per day

### 8.2 Public API

- Default: 1000 requests/hour per API key
- Expensive operations cost 10 tokens each (`sparql:execute`, chat, drafter)
- Per-key customization by admin
- `X-RateLimit-*` headers on every response
- 429 with `Retry-After` when exceeded

### 8.3 MCP

Inherits API key rate limits. Additional per-tool limits:
- `analyze_draft`: 10 calls/hour (expensive)
- `draft_law`: 5 calls/day (very expensive)

---

## 9. Raw SPARQL Endpoint Hardening

**Concern:** A public `sparql:execute` scope lets callers run arbitrary queries against Jena. Even with rate limiting, expensive queries can DoS the database.

**Mandatory controls:**

1. **Read-only enforcement** — parser rejects `INSERT`, `DELETE`, `CLEAR`, `LOAD`, `CREATE`, `DROP` at application layer
2. **Hard query timeout** — 30 seconds, enforced via Jena `timeout` parameter
3. **Result cap** — 1000 rows max, enforced via `LIMIT 1000` injection if missing
4. **Byte cap** — 5 MB response size max
5. **Complexity analysis** — reject queries with >5 joins or >3 property paths
6. **Query pattern allow-list** (optional, off by default for internal use, on for public API)
7. **Audit every query** — full query text logged to `audit_log`
8. **Scope gate** — only keys with `sparql:execute` scope can reach the endpoint, scope granted sparingly

---

## 10. Accessibility

**Principle:** WCAG 2.1 AA is the baseline. Not a "follow-up task."

### 10.1 Design system requirements

Every component must ship with:
- Focus ring on all interactive elements (`:focus-visible`)
- Minimum 4.5:1 contrast for text (3:1 for large text and UI chrome)
- ARIA labels on icon-only buttons
- Proper semantic HTML (headings in order, landmarks)
- Keyboard navigation (Tab order matches visual order)
- Form labels linked via `for`/`id`

### 10.2 Component-specific requirements

- **Modal**: focus trap, restore focus on close, `role="dialog"` + `aria-modal="true"` + `aria-labelledby`
- **Tabs**: arrow key navigation, `role="tablist"` + `role="tab"` + `aria-selected`
- **Toast**: `role="status"` + `aria-live="polite"`
- **DataTable**: sortable columns announce sort state
- **FormField**: error state announced via `aria-invalid` and `aria-describedby`

### 10.3 Testing

- Every new component tested with axe-core before merge
- Keyboard-only navigation smoke test on every PR
- Screen reader testing checklist for each epic completion

---

## 11. Accuracy & Uncertainty UX

**Principle:** LLM output and entity extraction have uncertainty. Users must see it.

**UI requirements:**

- Extracted references display a confidence badge:
  - Green: ≥0.9 (high confidence)
  - Yellow: 0.6–0.89 (medium confidence, review recommended)
  - Red: <0.6 (low confidence, manual verification required)
- Unmatched references shown in a distinct "Unmatched" section, not hidden
- Ambiguous matches (multiple candidates) shown with a disambiguation picker
- AI-generated clauses marked with `AI-generated — requires legal review` watermark (both in UI and .docx exports)
- Chat responses cite sources with links; uncited claims get an explicit "(no source)" indicator

---

## 12. Reproducibility & Ontology Snapshots

**Concern:** Ontology changes mid-analysis can make reports non-reproducible.

**Requirements:**

1. Every `impact_reports` row stores `ontology_version` (the git SHA of the ontology repo at analysis time)
2. Every `drafting_sessions` row stores `ontology_version` captured at research step
3. Report view shows the ontology version prominently
4. If ontology has changed since report generation, a banner offers "Re-run against current ontology"
5. Sync pipeline refuses to start if any `analyze_impact` job is currently running (cooperative flag in Postgres)

---

## 13. Async Reliability (Durable Status)

**Concern:** If a user closes their browser during a 2-minute analysis, WebSocket toasts miss them and they have no way to know when it's done.

**Requirements (Phase 2, not Phase 4):**

1. Notifications are durable — stored in `notifications` table from Phase 2 onwards (not Phase 4)
2. Bell icon in TopBar shows unread count on every page load
3. Status-tracker component on draft detail page shows real-time status via HTMX polling (fallback when WebSocket fails)
4. Email notifications (opt-in) for long-running operations (>5 min)

---

## 14. Worker Process Isolation

**Concern:** In-process thread pool shares resources with interactive web traffic.

**Requirements (Phase 2):**

1. Initial implementation: in-process thread pool (acceptable for pilot)
2. **Before production deployment with 20+ active users:** migrate to a separate Coolify scheduled container worker
3. Both modes must work from the same codebase (entry point flag)
4. Resource limits: worker process capped at 4 GB RAM, 2 CPUs
5. Restart policy: worker container restarts on failure, jobs in `running` state resumed (or marked failed + retry)

---

## 15. Definition of Done — Security & Compliance Checklist

Every phase epic must verify before closure:

- [ ] All new DB columns with sensitive data use application-level Fernet encryption
- [ ] All new routes have permission tests (owner, same org, cross-org)
- [ ] All new user actions produce audit log entries
- [ ] All new LLM calls use PII scrubber
- [ ] All new endpoints have rate limits (or documented exemption)
- [ ] All new components meet WCAG AA
- [ ] All new uncertainty surfaces (LLM outputs, extractions) show confidence/status
- [ ] Phase retrospective confirms this document is still canonical; any deviations documented in a follow-up ticket

---

## 16. Change Management

This document changes via PR only. Every phase spec must cite this file and note any intentional deviations.

**Previous versions:**
- 2026-04-09: Initial version (post plan-analysis.md reconciliation)
