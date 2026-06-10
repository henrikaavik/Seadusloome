# Seadusloome — Whole-System Code Review

**Date:** 2026-06-10
**Branch reviewed:** `main` @ `44cb63a`
**Method:** 29 parallel domain-specialist review agents, each reading an assigned non-overlapping file scope line-by-line, plus a whole-system architecture pass and a full lint/type/test baseline (31 agents total). Read-only — no files were modified.
**Scope:** All of `app/` (≈70k LOC Python), `app/static/js` + CSS (≈11.6k LOC), all 37 SQL migrations, `scripts/`, Docker/Fuseki/CI deploy config.

---

## 1. Executive summary

The codebase is **mature and disciplined at the hygiene level** and **mostly sound on the access-control fundamentals**, but it has a **small number of genuinely serious cross-tenant data-isolation and sensitive-data-handling defects** that matter disproportionately because the product handles politically sensitive pre-publication legislation for multiple government organisations.

**The headline risks, in order:**

1. **Cross-tenant leakage through the single shared Jena dataset.** Org isolation for uploaded drafts is enforced in *Postgres* but **not in SPARQL**. The chat SPARQL tool, the impact "conflicts" query, global search, and the competency institution-label lookup all run unscoped queries over a dataset that also holds every org's draft named graphs. At least one path (chat `query_ontology`) lets a user exfiltrate **all** orgs' draft text; others leak draft titles/URIs cross-org. This is the most important cluster to fix.
2. **Sensitive draft content escaping its protections.** Rendered impact reports are written *inside the repo working tree* (one is already git-tracked), are never deleted on draft delete, and drafter exports land as plaintext `.docx`. Reset/bearer tokens in URLs reach Sentry SaaS unscrubbed. The encryption-at-rest is real but is Fernet (AES-128-CBC+HMAC), not the AES-256-GCM the docs claim.
3. **Fail-open stub gating.** `is_stub_allowed()` fails *open* on any unrecognised `APP_ENV`, and the LLM/embedding providers don't consult it at all — a production env-var slip silently serves canned advice and random-vector embeddings (which a sync would then persist over real data).
4. **A versioning regression that breaks every v2+ upload** (graph-URI allowlist mismatch) and **temporal-correctness gaps** across the Analüüsikeskus engines (repealed + current law mixed in every aggregate).
5. **No CSRF layer anywhere** (SameSite=Lax only), no login rate-limiting, and several **infra exposures** (unauthenticated Fuseki write endpoints, DB/triplestore bound to `0.0.0.0`).

**Toolchain baseline (clean):** ruff 0 issues / 427 files · ruff format 0 · pyright 0 errors/0 warnings · pytest **3,792 passed, 45 skipped (DB-gated), 0 failed** in 18s. The test suite is healthy; these findings are not caught by it because they are design/architecture/security-policy issues, several of which the tests actively mask (e.g. notification disconnect cleanup, `id(send)` stability).

**Severity tally:** 5 Critical · ~50 High · ~40 Medium · ~50 Low.

A recurring meta-observation from multiple reviewers: **the security *mechanisms* are well-built (constant-time HMAC, 404-not-403 existence hiding, parameterized SQL everywhere, allowlist SPARQL escaping, mistune→bleach XSS defense, owner-only ACLs).** The failures are almost all at the *boundaries between subsystems* — where Postgres ACLs meet the shared Jena dataset, where the worker meets external services, where one WebSocket pattern was copied four times with drift.

---

## 2. Critical findings

### C1 — Chat SPARQL tool exfiltrates every org's draft text
`app/chat/tools.py:282-297`
`_exec_query_ontology` runs LLM/user-authored SPARQL against the shared `JENA_DATASET` with **no graph scoping**, while persistent draft named graphs (`…/estleg/drafts/<uuid>`, loaded as plaintext by the analyze pipeline) live in that same dataset. A prompt like `SELECT ?g ?s ?p ?o WHERE { GRAPH ?g {?s ?p ?o} }` returns other orgs' pre-publication draft contents. SPARQL *mutation* is doubly blocked (read-only validator + query-only endpoint), but **read-side graph scoping is entirely absent** — the asymmetry with the explorer (which does org-scope drafts) is the bug.
**Fix:** run chat SPARQL against a draft-free public-graph view, or reject `GRAPH`/`FROM NAMED` and force the default graph. Add a hard result-row/byte cap (see H-cluster below).

### C2 — Rendered exports written into the repo working tree; one is git-tracked
`storage/exports/drafter-44444444-…-444444444444.docx` · `app/docs/docx_export.py:124`
`EXPORT_DIR` defaults to `./storage/exports` (inside the source tree), there is no `.gitignore` entry, and a rendered impact `.docx` is already tracked/dirty in git. Sensitive draft content can be committed and pushed.
**Fix:** gitignore `storage/`, `git rm --cached` the tracked `.docx`, and never default `EXPORT_DIR` to a path inside the repo. (Closely related: H-cluster "sensitive-data-at-rest".)

### C3 — v2+ version-graph URIs fail the safety allowlist → every versioned re-upload breaks
`app/sync/jena_loader.py:59` (+ `graph_builder.py:330`, `impact/queries.py:402-423`)
Version graphs are minted as `…/drafts/{uuid}/v{n}` (`upload.py:571`), but `put_named_graph`, `write_doc_lineage`, and every impact query builder enforce a `(?:drafts|adhoc)/[0-9a-f-]{36}$` `fullmatch`, which rejects the `/v2` suffix. Verified empirically: analysis of every v2+ upload raises `ValueError("Unsafe graph URI")`, retries exhaust, the draft flips to `failed`, and version-graph cleanup deletes also raise.
**Fix:** add an optional `(?:/v\d+)?` arm to the single shared `_SAFE_GRAPH_URI` regex so both the loader and query layers inherit it.

### C4 — Stored XSS via bookmark `entity_uri` on the dashboard
`app/templates/dashboard.py:1295, 1450-1472`
The `/api/bookmarks` POST stores a user-controlled `entity_uri` unvalidated, then the dashboard renders `A(uri, href=uri)`. FastHTML does **not** block `javascript:` scheme URIs in `href`, so a crafted POST yields stored XSS on every dashboard load.
**Fix:** validate the scheme server-side in `add_bookmark` (reuse `explorer.routes._validate_uri` / `^https?://`) and reject non-http(s) URIs before insert.

### C5 — No temporal/version filtering in the Analüüsikeskus engines → repealed law counted as current
`app/analyysikeskus/burden.py:301-364` (also `sanctions.py`, `competency.py`, `court_practice.py`)
None of the four analysis engines apply any validity/version filter (`validFrom`/`validUntil`/`temporalStatus`/version-chain), so burden counts, sanctions, competences, and court-practice **all mix repealed provisions and superseded `ProvisionVersion`s with current law**, overstating every aggregate. For a legal-advisory tool whose ontology explicitly models temporal versioning, this is the highest-impact correctness defect.
**Fix:** add an `OPTIONAL`+`FILTER` on `temporalStatus`/`repealDate` (or join only current `ProvisionVersion`s) and expose a "kehtiv õigus" vs "kogu ajalugu" scope toggle (the `?oigus=` param is already parsed but unwired).

---

## 3. High-severity findings, grouped by cross-cutting theme

### Theme A — Cross-tenant isolation via the shared Jena dataset *(the dominant risk)*
| ID | Location | Problem | Fix |
|----|----------|---------|-----|
| A1 | `app/analyysikeskus/routes.py:1809,6555` | Adhoc/intent impact pages surface the shared `CONFLICTS` query whose `GRAPH ?otherGraph` arm matches **all** named graphs → "Kõrge riskiga seosed" exposes other orgs' private draft URIs+labels for any shared provision. | Scope the cross-draft conflict arm to the caller's org graphs (or exclude `drafts/*` from adhoc/intent conflict detection). |
| A2 | `app/analyysikeskus/routes.py:4874` | `padevused` passes user `?sisend=http(s)://…` straight to `get_institution_label`, whose SPARQL (`competency.py:285`) has no `a estleg:Institution` type filter → returns the `rdfs:label` of *any* node, including another org's draft node. | Constrain the label query to `?institution a estleg:Institution` and reject non-`estleg:` URIs. |
| A3 | `app/docs/impact/queries.py:233` | Other-draft conflict arm matches all orgs' graphs; the foreign draft's UUID graph URI is persisted in `impact_reports` and rendered — inconsistent with the deliberate masking in `similarity.py`. | Post-filter `_detect_conflicts` rows against org-owned graph URIs, masking foreign rows. |
| A4 | `app/ontology/queries.py:111` → `ui/components/search_routes.py:113` | Global search's `SEARCH_ENTITIES` has no `FROM`/`GRAPH` scope; draft isolation rests **entirely** on the implicit TDB2 default-graph-excludes-named-graphs behavior. Enabling `unionDefaultGraph` (a plausible ops change) instantly leaks every org's draft titles through the top-bar search. | Add explicit `FROM <urn:x-arq:DefaultGraph>` and a regression test asserting draft labels never appear in search. |
| A5 | `app/docs/impact/queries.py:233` (self-conflict) | The same draft's *own* persisted earlier version graphs self-report as "Teine eelnõu viitab juba sellele sättele", inflating `conflict_count` (+10 score each) once C3 is fixed. | Exclude via `!STRSTARTS(str(?otherGraph), ".../drafts/{draft_id}")`. |

**Root cause:** org isolation is a Postgres concept that was never extended to the SPARQL layer. Recommend a single shared "public-graph-only query view" helper that all read paths (chat, search, conflicts, institution labels) route through, plus an explicit decision on `unionDefaultGraph`.

### Theme B — Sensitive draft content at rest / in transit
| ID | Location | Problem | Fix |
|----|----------|---------|-----|
| B1 | `app/observability.py:104` | `_scrub_pii` never touches `event["request"]` and no `before_send_transaction` is set, so ~10% of requests (`traces_sample_rate=0.1`) ship live `/auth/reset/<token>` and `?token=` report-download URLs to Sentry SaaS — no scrub regex matches them. | Scrub `event["request"]` (url/query/data/headers) and register the same fn as `before_send_transaction`. |
| B2 | `app/docs/cleanup_handler.py:104` | On draft delete, the cleanup job purges the encrypted source + Jena graph but **never deletes the rendered `<draft_id>-<report_id>.{docx,pdf}` artefacts** in `EXPORT_DIR` → sensitive reports persist after the owner deletes the draft (violates the delete-cascade mandate). | `unlink` the draft's export artefacts (`glob EXPORT_DIR/<draft_id>-*`) in `draft_cleanup`. |
| B3 | `app/docs/cleanup_handler.py:130` | `delete_named_graph()` returns `False` on a Jena outage but the handler counts every call as success → a Jena outage silently orphans named graphs holding draft data, with zero retries. | Treat `False` as an error (append to `errors`, don't increment) so the retry budget engages. |
| B4 | `app/drafter/docx_builder.py:141,198` + `session_model.py:212` / `jobs/archive_warning.py:31` | Drafter exports written as plaintext `.docx`, never cleaned up; and `drafting_sessions` (encrypted draft content + intent) are **excluded from the 90-day auto-archive sweep** that scans only `drafts`. | Write exports to temp deleted after response; extend the archive scan to stale `active` drafting sessions. |
| B5 | `app/storage/encrypted.py:125` | Single static Fernet key, no `MultiFernet` rotation path (rotating a leaked key bricks all drafts); docs claim "AES-256-GCM" but it's Fernet = AES-128-CBC+HMAC. | Accept a comma-separated key list via `MultiFernet`; correct the documented claim. |

### Theme C — Stub gating fails open / providers bypass it
| ID | Location | Problem | Fix |
|----|----------|---------|-----|
| C-a | `app/config.py:44` | `is_stub_allowed()` fails **open**: any unrecognised `APP_ENV` (`"Production"`, `"prod"`, trailing space) enables stubs in prod — ephemeral Fernet key, stub LLM/Tika/email — while `get_worker_mode()` fail-closes. Inconsistent. | `.strip().lower()` and invert to an explicit allowlist (`{development,test,ci,staging}`) so unknown values fail closed. |
| C-b | `app/llm/claude.py:47` + `app/rag/embedding.py:64` | `ClaudeProvider`/`VoyageProvider` gate stub mode only on a missing API key, **never** on `is_stub_allowed()` (whose docstring falsely claims they do). A prod key loss serves canned advice + random-vector embeddings, and a prod sync would overwrite real embeddings with random vectors via `ON CONFLICT DO UPDATE`. | Raise in both `__init__`s when key missing **and** `not is_stub_allowed()`. |

### Theme D — Auth perimeter & CSRF (no CSRF layer anywhere)
| ID | Location | Problem | Fix |
|----|----------|---------|-----|
| D1 | `app/auth/routes.py:101` | `login_post` has **no rate limiting / lockout / CAPTCHA** (forgot-flow is throttled, login is not) → unlimited online brute force against government accounts. | Reuse the `password_reset_attempts` throttling pattern for login (per-IP/per-email backoff). |
| D2 | login / chat / drafts / admin purge-retry / annotations | **No CSRF protection** (only SameSite=Lax). Login CSRF is concretely exploitable (auto-submitted POST logs victim into attacker's account, capturing their uploads); destructive POSTs (draft delete, job purge/retry) are unprotected. | Add CSRF beforeware (token or `Sec-Fetch-Site`/origin check) across mutating routes; at minimum auth + admin + draft-delete. |
| D3 | `app/main.py:306` | `ProxyHeadersMiddleware(trusted_hosts="*")` trusts client `X-Forwarded-For` → spoofable `req.client.host` bypasses the forgot-password IP rate limit and forges audit-log IPs. | Restrict `trusted_hosts` to the Traefik/Docker network range. |
| D4 | `app/auth/users.py:140` | `create_user` never sets `must_change_password` → admin-created accounts keep the admin-known initial password indefinitely. | Insert `must_change_password=TRUE` for admin-created accounts. |
| D5 | `app/main.py:284` | No security headers anywhere (no CSP, HSTS, X-Content-Type-Options, X-Frame-Options, Referrer-Policy). | Add a header middleware (CSP w/ nonce for the inline theme script, nosniff, frame-ancestors 'none', HSTS). |

### Theme E — Job queue & pipeline robustness
| ID | Location | Problem | Fix |
|----|----------|---------|-----|
| E1 | `app/jobs/queue.py:198` | **No visibility timeout / reaper**: a worker crash or deploy-kill leaves rows stuck in `claimed`/`running` forever (`claim_next` only selects `pending`). | Add a startup/periodic reaper that re-pends stale `claimed`/`running` rows past a threshold. |
| E2 | `app/drafter/handlers.py:913` | `extract_json` returns `{"error": "failed to parse"}` instead of raising → `drafter_draft` persists empty-text clauses, the job "succeeds", **retry-gating never engages**, and blank clauses pass into review. | Treat missing `text`/`"error"` key as a raised failure so retry budget + abandon gating apply. |
| E3 | `app/drafter/routes.py:1124-1154` | `submit_review` re-runs `_trigger_integrated_review` on every POST when `integrated_draft_id` is null → double-click creates duplicate draft rows + named graphs + jobs. | Claim atomically (`UPDATE … WHERE id=%s AND integrated_draft_id IS NULL RETURNING`) before building. |
| E4 | `app/llm/pricing.py:6` | Prices wrong: `claude-opus-4-6` listed $15/$75 (actual $5/$25, 3× overcharge), `claude-haiku-4-5` $0.80/$4.00 (actual $1/$5); these feed monthly budget enforcement. No Voyage entry → every embedding logs `cost_usd=0`. | Correct prices, add current model ids + Voyage pricing, warn on unknown (provider,model). |
| E5 | `app/llm/retry.py:62,42` | Voyage errors **never retry** (exceptions carry `http_status`, not `status_code`; `_http_status()` returns None) → one 429 aborts a ~1,400-batch ingest. `_RETRYABLE_HTTP` also omits Anthropic's 529 `overloaded_error` (its most common transient failure). | Read `http_status` in `_http_status()`; add 529/408/409 to retryable set. |
| E6 | `app/docs/extract_handler.py:77-85` + `analyze_handler.py:107-139` | Decrypt/precondition checks run **before** the retry-gated `try`, so a `DecryptionError` never flips the draft to `failed` even on the final attempt → stuck in `extracting` with no retry button. | Move the precondition block inside the gated `try` (or mark-failed on final attempt for pre-try exceptions). |

### Theme F — WebSocket pattern drift (4 hand-rolled copies)
| ID | Location | Problem | Fix |
|----|----------|---------|-----|
| F1 | `app/chat/websocket.py:432-445` | `_ws_close` sends a raw ASGI close dict through FastHTML's wrapped `send` (which does `to_xml`+`send_text`) → the socket is **never closed** on the fail-closed JWT path; it falls through to sending a JSON error instead. | Receive the unannotated `ws` param and call `await ws.close(code=1011)`. |
| F2 | `app/explorer/websocket.py:25-88` | `/ws/explorer` is in `SKIP_PATHS` and never authenticates → any anonymous client joins an unbounded `_connected_clients` set (FD/memory DoS). Also **no heartbeat** (unlike the sibling notifications channel) → NAT/proxy idle timeouts silently drop every subscriber long before a sync event fires. | Authenticate the handshake like `/ws/chat`; spawn a heartbeat task in `_on_connect`. |
| F3 | `app/notifications/websocket.py:496` | `_on_disconnect`'s `if send in sends` can never match (FastHTML rebuilds `partial(_send_ws, conn)` per dispatch; `partial` compares by identity) → connection registry **never cleaned up**, slow unbounded leak. Tests mask it by passing the same object to both hooks. | Key the registry on `id(ws)` (stable across hooks, as `_heartbeats` already proves) and pop on disconnect. |
| F4 | `app/chat/websocket.py:485` | Per-connection task registry keyed by `id(send)`, which is **not stable** across messages (in-code comment is wrong) → `stop_generation` from a later frame can miss the in-flight task. | Key on a stable per-connection token (`id(conn)`/scope). |
| F5 | architecture | chat / docs / ws_export_progress / notifications each contain byte-identical cookie-extraction + JWT-lazy-init + heartbeat plumbing ("see chat module for the pattern"). | Extract `app/auth/ws_auth.py` and have all WS handlers call it. |

### Theme G — Resource exhaustion / unbounded inputs
| ID | Location | Problem | Fix |
|----|----------|---------|-----|
| G1 | `app/chat/tools.py:296` | `query_ontology` has no LIMIT/result-size cap → `SELECT * WHERE {?s ?p ?o}` over 50k+ provisions blows context, token cost, and Fuseki. | Clamp returned rows + serialized bytes; inject a hard LIMIT. |
| G2 | `app/docs/upload.py:308` | `await upload.read()` buffers the whole body into RAM before `_validate_size` → multi-GB POST OOMs the app. | Reject on `upload.size`/Content-Length first, then read incrementally with a hard cap. |
| G3 | `app/docs/routes/_upload.py` / `upload.py:305` | No magic-byte sniffing, no zip-bomb / decompression-ratio guard on `.docx` (ZIP) before handing to Tika. | Sniff true type; validate ZIP central directory + cap uncompressed size. |
| G4 | `app/docs/tika_client.py:200-221` | No response-size cap on `PUT /tika`; a zip-bomb `.docx` expands to GB-scale text, encrypted into Postgres, then fanned into thousands of LLM extraction calls (unbounded cost). | Stream the response with a hard byte ceiling; cap chunk count in the extractor. |
| G5 | `app/analyysikeskus/burden.py:559-564` | `burden_delta_for_draft` issues **one SPARQL round-trip per provision** (up to 500 sequential) — N+1 that stalls the request and hammers Fuseki. | Single query binding the provision set via `VALUES ?provision {…}`. |

### Theme H — Deploy / infrastructure
| ID | Location | Problem | Fix |
|----|----------|---------|-----|
| H1 | `docker/fuseki-config/ontology.ttl:18-25` | Fuseki exposes `update`/`gsp-rw`/`upload` endpoints with **no auth guard** → any process on the Docker network can run arbitrary SPARQL UPDATE / overwrite the whole ontology. | Add `fuseki:allowedUsers ("admin")` to each write endpoint, or move writes behind an authed service. |
| H2 | `docker/docker-compose.yml:86-90,117` | Postgres (5432) and Jena (3030) bound to `0.0.0.0` → reachable externally on permissive-iptables Linux dev hosts. | Bind `127.0.0.1:5432` / `127.0.0.1:3030`. |
| H3 | `docker/Dockerfile:72-74` | `libreoffice-core/writer` installed in the runtime layer despite nothing using it yet → +400-600 MB image + attack surface on every deploy. | Gate behind `ARG INSTALL_LIBREOFFICE=false`. |
| H4 | `app/sync/orchestrator.py:71` + `webhook.py:35` | The advertised "DB-level lock" **does not exist** — only an in-memory per-process flag + a non-atomic `has_recent_running_row()` check → two racing webhooks corrupt the shared `urn:estleg:staging` graph. | Wrap `run_sync` in `pg_try_advisory_lock` (single well-known key). |
| H5 | `app/sync/webhook.py:20-25` | Webhook signature has **no replay protection** (no delivery-id dedup / timestamp window) → a captured valid `(body,sig)` re-triggers full resyncs indefinitely. | Record processed `X-GitHub-Delivery` ids and reject duplicates. |

### Theme I — Migrations & schema integrity
| ID | Location | Problem | Fix |
|----|----------|---------|-----|
| I1 | `migrations/036_*.sql` (two files) | **Two files numbered 036**, no 029; runner tracks by full stem so it works today, but ordering between the two is filesystem-sort-dependent and number-based tooling is ambiguous. | Rename one to 037, shift `037_message_ontology_version` → 038 for a linear sequence. |
| I2 | `migrations/031_annotations_extensions.sql:1` | File is `031_*` but its header says "Migration 029" and its rollback deletes version `031_…` — self-contradictory; no 029 exists. | Reconcile filename/header/rollback. |
| I3 | `migrations/019_*.sql:41` | Five `CREATE INDEX` + two `ADD COLUMN` omit `IF NOT EXISTS` → non-idempotent; replay after partial failure aborts. | Add `IF NOT EXISTS` throughout. |
| I4 | `scripts/migrate_chat_encryption.py:155` | Key guard only aborts when `APP_ENV=production`; on staging a missing key silently encrypts with an unset key. | Require the key unconditionally. |

### Theme J — Architecture (structure, not line-level)
| ID | Area | Problem | Fix |
|----|------|---------|-----|
| J1 | `analyysikeskus` ↔ `docs.impact` | **Circular dependency** kept compiling only by 6 function-local imports + a comment admitting it; rooted in the 6.8k-line eager-imported `routes.py`. | Move `burden`/`sanctions`/scoring constants to a neutral lower layer. |
| J2 | `app/docs/impact/` | The impact engine is a hidden shared core (consumed by analyysikeskus, explorer, sync, dashboard) misplaced inside the document-management module. | Promote to top-level `app/impact/`. |
| J3 | `analyysikeskus/routes.py` (6,785 lines) | Monolith mixing routing + SPARQL + parsing + scoring + rendering for ~9 workflows; the size is what makes J1 unbreakable. | Split into a `routes/` package per workflow (mirrors the proven `docs/routes/` split). |
| J4 | Phase-5 readiness | Business logic lives inside HTTP handlers almost everywhere (only `app/email/service.py` has the clean wrappable shape CLAUDE.md mandates for REST+MCP). | Extract framework-free `*_service.py` (input→typed result) per workflow. |

---

## 4. Medium-severity findings (by module)

**Auth**
- `jwt_provider.py:84` — user enumeration via timing (no bcrypt work on unknown email) + synchronous reset email only for known emails. Run a dummy `checkpw`; move reset email to the job queue.
- `users.py:797` — revealed temp password stashed in the signed-but-unencrypted, non-Secure session cookie (`sess_https_only` not set). Pass `sess_https_only=True`; store a server-side reference.
- `routes.py:101-130` — login success/failure/logout/refresh are **not** audit-logged (gap vs the full-audit mandate). `log_action` on all auth lifecycle events with source IP.
- `users.py:144` — emails stored/matched case-sensitively while `forgot_post` lowercases → mixed-case accounts can never get a reset email. Normalize + unique index on `lower(email)`.

**Core infra**
- `app/db.py:31` — `get_connection()` opens a brand-new psycopg connection per request/metric/job-poll (no pool) → connection churn / `max_connections` exhaustion at 50 users. Back with `psycopg_pool.ConnectionPool`. *(This recurs in dashboard ~10 widget queries serially, and docs detail pages opening 3 connections per render.)*
- `app/metrics.py:118` — MetricsMiddleware added last → runs outermost (before auth, contradicting its comment) and inserts every unauthenticated raw path into Postgres with no cardinality cap → disk-fill vector. Record the matched route template; skip unauthenticated/404.
- `email/templates.py:9` — `full_name`/`admin_name` interpolated into email HTML without `html.escape()` → stored HTML/link injection. Escape every interpolated value.

**Docs / impact**
- `impact/queries.py:233` — cross-org other-draft conflict arm is unmasked (see A3). `analyzer.py:555` — C6 burden section is silently always zero (graph-URI vs `#self` mismatch). `reference_resolver.py:108` — `_PROVISION_RE` rejects dotted abbreviations ("§ 211 lg. 2") and superscript sections ("§ 113¹"). `similarity.py:184` — one query per candidate, no cap.
- `docs/status.py:349-359` — version-row UPDATE ignores `expected_status` → lost OCC race still flips the authoritative version row.
- `docs/entity_extractor.py:37-67` — hostile document text fenced only by triple backticks → prompt-injection of the impact report; `ref_text` length/count unbounded.

**Analüüsikeskus**
- `routes.py:4667` — `padevused` "Küsi nõustajalt" links to `/chat/new?q=…` but the handler ignores `q` → seed silently dropped. Mint a real `/chat/seed`.
- `routes.py:1-44` — module docstring claims only 2 workflows wired, but 11 routes are registered. Rewrite the header.
- `burden.py:330,731` — act query `LIMIT 500` is a **row** cap while a provision emits multiple rows → straddling provisions lose values; `truncated` is computed in provision terms. Paginate by distinct provision.
- `sanctions.py:302` — similar-sanction overlap compares amounts but **ignores units** → "3 years" overlaps "3 EUR" within a type. Filter on unit.
- `court_practice.py:308` — type OPTIONAL can bind base `CourtDecision` vs subtype arbitrarily → EU cases misrouted to `muu`. Prefer most-specific type.
- `history.py:808-812` — `list_impact_reports` does `report_data::text ILIKE %{uri}%`: `_` in estleg URIs acts as a wildcard (over-match) **and** the leading-wildcard cast scans every row (unindexable). Escape `_`/`%`; query a structured JSON path with a GIN index.

**Chat**
- `orchestrator.py:1372-1374,1632-1637` — RAG chunks + tool-result JSON concatenated verbatim into the prompt with no data/instruction delimiting → prompt injection from org-shared drafts / ontology labels. Fence as untrusted data.
- `orchestrator.py:955-992` vs `1046` — rate-limit check runs **before** the ownership check and reads the count before the user-message insert → N concurrent sends all pass (no serialization). Move after ownership; gate with the advisory lock.
- `rate_limiter.py:135` — the documented TOCTOU advisory lock is **void**: the only caller commits immediately, releasing the lock before any spend lands → concurrent turns at 99% all pass. Hold the lock across the insert, or document the residual race honestly.
- `llm/claude.py:534-571` — `_log_cost` in a `finally` after `yield stop` is bypassed on mid-stream error/cancel → already-billed tokens never reach `llm_usage`. Log in the outer finally.
- `handlers.py:928` — transcript export writes no audit row (every other chat action does). `audit.py:37` — `log_chat_message_send` has zero production callers (docstring claims sends are recorded). `models.py:226` — `DecryptionError` silently renders content as `""` → key rotation blanks all history. `rate_limiter.py:195` — `notify_cost_alert` fires on every check in the 80-100% band with no dedupe → admin spam.

**Drafter**
- `routes.py:1235` — `GET /export` never checks `current_step` → any step-1 session exports an empty docx and flips to `completed`. `state_machine.py:123` — `can_advance` ignores `session.status` → auto-abandoned sessions stay advanceable.
- `handlers.py:876-932` — one LLM call per section (~40) in a single try → a failure at section N discards all and retries redo every call (~3× spend). Checkpoint partials.
- `docx_builder.py:231` — LLM/user clause text → python-docx unsanitized; XML-incompatible control chars (legal in JSON) make lxml raise on save → unexportable draft. Strip control chars.
- `handlers.py:983-1053` — `regenerate_clause` read-modify-writes the whole clauses blob with no version check → a concurrent user edit is silently overwritten. Re-read in the write txn or add a version counter.

**Admin**
- `cost_dashboard.py:154,605` — window-scoped spend (7d/90d/ytd) divided by the **monthly** budget → YTD shows spurious >100%, 7d under-reports. Scale budget to window.
- `job_monitor.py:332` — `_retry_job` resets `attempts=0` → a poison job can be retried indefinitely, defeating retry-gating. Preserve attempts.
- `job_monitor.py:892,911` — purge/retry POSTs have **no CSRF and no audit log** → forged cross-site POST or confused admin deletes job history with no trail. Add CSRF + audit.
- `audit.py:107` — `query` filter builds `%{query}%` without escaping `%`/`_` → LIKE-injection / full-table ILIKE on JSONB. Escape + `ESCAPE '\'`.
- `audit.py:730` — audit viewer has **no org scoping** → any admin reads every org's audit entries (contradicts org-scope mandate). Scope to `org_id` for non-super-admins.
- `health.py:457` — unauthenticated `/api/health` leaks app version + git SHA + Jena/Postgres up/down to anonymous callers. Return only `{status}` publicly.

**Annotations**
- `routes.py:159,214` — legacy create/reply never call `parse_mentions` → `@name` mentions silently no-op outside the row-annotation surface. Route through `parse_mentions`+`notify`.
- `ui/primitives/annotation_button.py:63` + `surfaces/annotation_popover.py:98` — container and popover share `id=popover_id` → duplicate DOM id breaks reload-after-create. Distinct ids.
- `routes.py:441` — legacy create accepts any `target_id` for a valid type with no ownership check (stamps caller's org). Authorize the target like row annotations. `routes.py:497-628 vs 1148` — row resolve/reopen lacks the author/admin gate the legacy path enforces. `models.py:534` — `parse_mentions` local-part `LIKE` interpolates the token unescaped (wildcard injection).

**Notifications**
- `routes.py:205-220` — `_is_safe_redirect` accepts `/\evil.com` (browsers normalize to `//evil.com`) → open redirect. Reject `\`.
- `wire.py:337-387` — `notify_cost_alert` no dedupe (re-notifies all admins on every LLM call in-band). Add a 24h dedupe window.

**Sync**
- `webhook.py:25` — `compare_digest` on two `str` raises `TypeError` on non-ASCII signature header → 500 instead of clean 401. Compare bytes. `webhook.py:49` — `await request.body()` no size cap. `jena_loader.py:36` — `FUSEKI_ADMIN_PASSWORD` defaults to literal `"localdev"` → silent known-password auth if unset in a deploy. `orchestrator.py:413-443` — SHACL violations downgraded to warnings and always proceed (contradicts the stated "SHACL failure = rejection" rule).

**SPARQL core**
- `sparql_client.py:128` — bare `httpx.post` per call (no client/pool/keep-alive) → multiplies latency for the 3-5 queries per request. Module-level `httpx.Client`. `count():272` — hardcodes the swallow path → a Fuseki outage renders explorer pagination as "total: 0" instead of an error.

**LLM/RAG/jobs**
- `scrubber.py:80` — scrub patterns miss the Estonian **isikukood** (primary PII in legal text) + IBAN → reach Anthropic + Sentry unredacted. `scripts/ingest_rag.py:243` — shrinking an entity's chunk count leaves stale higher-index rows retrievable forever; `:349` re-embeds the whole 90k corpus on every sync (cost invisible due to E4).

**UI / dashboard / explorer**
- `ui/data/data_table.py:95` — sort-header link `?sort=…&dir=…` discards all other query params (page, filters, search) → sorting resets filters. Merge onto current URL.
- `templates/dashboard.py:1344-1434` — Töölaud is fully static (no HTMX/WS refresh) → stale work queue until manual reload. `:1108` — EU-deadlines "Näita kõiki" link dead-ends on a route that requires `sisend`.
- `explorer/routes.py:465-503` — `explorer_search` returns `len(results)` as `meta.total` while capped at ≤50 → UI shows a capped count as the true total. `routes.py:744-772` — URI-less conflict synthetic id keys on the growing `len(nodes)` → non-deterministic, undercuts the "reproducible picture" contract.
- `explorer.js:2438-2477` — WS reconnect backoff order inverted, no jitter, no attempt cap. `:1244-1280` — `expandCategory` silently no-ops (or negative-slices) when the graph is already at `MAX_NODES`.

**Frontend JS**
- `chat.js:672` — fallback tool name (`event.tool`, LLM-stream-originated) → `innerHTML` unescaped (only unescaped dynamic→innerHTML path). `escapeHtml` it.
- `chat.js:302` — reconnect-after-mid-stream-drop never calls `clearThinking()` → orphaned "Mõtlen…" bubble animates forever. `:804` — follow-up chips / regenerate don't check `streaming` → concurrent turn garbles the transcript.
- `annotation_mentions.js:418` — `attach()` registers 3 global listeners + a body `<ul>`, `destroy()` never called → leak per HTMX swap.

---

## 5. Low-severity findings (compact)

These are polish/hardening items — full file:line references are preserved in the per-module agent outputs. Highlights:

- **Auth:** stolen access token valid up to 60min after logout (no `tv` bump); org-less org_admin can manage other org-less users; `SECRET_KEY` strength not enforced (<32 bytes); self-service reset skips the no-email-substring rule; no max password length (bcrypt 72-byte truncation).
- **Estonian orthography drift:** un-diacriticked strings in `annotation_popover.py` ("Markused" → "Märkused"), `chat/websocket.py` ("Vigane sonum"), explorer start-panel label verb inconsistency ("Alusta Normi mõjuahelat" vs bare-noun nav). The repo's own memory flags diacritics as a shipped concern.
- **Back-link glyph inconsistency** in drafter (`<` vs `←`).
- **Dead code:** `chat/slash.py:expand()` (client does it), `templates/admin_dashboard.py` (37-line shim — intentional back-comat, document it), `scripts/migrate_chat_encryption.py`/`probe_ontology_shape.py`/`run_evals.py` (orphaned one-offs), `_planned_workflow_card` paths in analyysikeskus.
- **Signed-URL hygiene:** `signed_urls.py:81` reuses the raw JWT `SECRET_KEY` with no domain separation; `:204` doesn't re-validate the minter still belongs to the draft's org.
- **Modal/global-search UX:** `modal.js:44` no dedupe on double-open; `global_search.js:31` Cmd/Ctrl+K steals focus from textareas despite its own comment.
- **`status.py:313` f-string-interpolates `extras` column names** guarded only by a docstring — latent injection if a future caller passes a dynamic key. Allowlist the keys.
- **`input_parser.py:59`** `_EE_CASE_RE` matches ISO dates (`2020-01-01`) as case numbers.
- **Tika `error_mapping.py:143`** the most common real failure (scanned/image-only PDF → empty text) falls through to a generic message.

---

## 6. Prioritized remediation roadmap

**P0 — fix before any further multi-org production use (data isolation + sensitive data):**
1. C1 / A1-A5 — close the cross-tenant Jena read paths (shared public-graph query view + org-scoped conflicts + scoped institution labels + `FROM` on search + decide `unionDefaultGraph`). *This is one coherent workstream.*
2. C2 / B2 / B4 — get sensitive content off disk-in-repo and into the delete cascade + archive sweep; `.gitignore storage/`, `git rm --cached` the tracked docx.
3. B1 — scrub Sentry request context + transactions (token leak to SaaS).
4. C-a / C-b — make `is_stub_allowed()` fail closed and have the providers consult it.
5. C4 — validate bookmark URI scheme (stored XSS).

**P1 — correctness regressions & control gaps:**
6. C3 — version-graph URI allowlist (every v2+ upload is currently broken).
7. C5 — temporal filtering in the four analysis engines.
8. D1/D2/D3 — login rate limiting + CSRF layer + trusted-proxy range.
9. E1/E2/E3 — job reaper + drafter retry-gating + double-submit guard.
10. H1/H2/H4/H5 — Fuseki write auth + bind DB/Jena to localhost + real sync advisory lock + webhook replay protection.
11. E4/E5 — fix pricing table + Voyage/529 retries (budget enforcement depends on it).

**P2 — robustness, hygiene, structure:**
12. F1-F5 — WebSocket: shared helper, fix `_ws_close`, explorer auth+heartbeat, notification registry leak.
13. B5 / D4 / D5 — MultiFernet key rotation + force password rotation + security headers.
14. G1-G5 — input/result size caps (chat SPARQL, uploads, Tika, burden N+1).
15. I1-I4 — migration numbering + idempotency.
16. J1-J4 — extract `app/impact/`, split `analyysikeskus/routes.py`, break the cycle, begin the service-layer extraction for Phase-5 MCP/REST readiness.
17. Medium/Low cluster — connection pool, audit gaps, Estonian diacritics, dead-code cleanup.

---

## 7. What's genuinely solid (so it isn't regressed)

Multiple independent reviewers confirmed these are correct and well-built — preserve them:
- **Toolchain & tests:** 3,792 passing, zero lint/type debt.
- **SQL:** uniformly parameterized; org-scoping enforced at the SQL layer in listing helpers; no `interval %s` psycopg bug anywhere in current code (the past regression is fixed and stayed fixed).
- **SPARQL injection defense:** `_inject_uri_bindings` allowlist + `_sanitize_sparql_value` literal escaping; the cross-cutting sweep found no construction site that bypasses them.
- **XSS:** chat goes mistune→bleach (SRI-pinned client DOMPurify); FastHTML auto-escaping covers draft/review/version/annotation render paths; the few `innerHTML`/`NotStr` uses are static constants (except C4 + the chat tool-label lapse).
- **Access control:** owner-only ACLs with 404-not-403 existence hiding; the #634 org-admin escalation guards; the #843 unverified-citation handling (verification recomputed from resolver, never trusted from storage); the #841 chat URI-citation sanitizer.
- **Sync data safety:** staged-graph publish, triple-count regression floor, atomic `COPY … TO DEFAULT` on TDB2, draft-graph isolation, constant-time HMAC, no command injection, correct git-LFS pointer detection (#840).
- **RAG tenant scoping** (migration 016 applied at all three call sites), HNSW/1024-d consistency, archive-warning timezone correctness, the FOR UPDATE SKIP LOCKED claim flow and per-job exception isolation.

---

*Generated by a 31-agent parallel review swarm (security · correctness · architecture · UX · performance) — 29 scope reviewers + architecture pass + toolchain baseline, one specialist per non-overlapping file scope, synthesized into this report. Per-finding agent transcripts retain exact line references and verification notes.*
