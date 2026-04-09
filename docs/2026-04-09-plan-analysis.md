# Seadusloome Plan Analysis

Date: 2026-04-09

## Scope reviewed

- Main architecture plan: `estonian-legal-ontology-plan.md`
- Repo guidance/context: `AGENTS.md`, `README.md`
- Detailed specs:
  - `docs/superpowers/specs/2026-04-08-phase1-design.md`
  - `docs/superpowers/specs/2026-04-09-phase2-design.md`
  - `docs/superpowers/specs/2026-04-09-phase3-design.md`
  - `docs/superpowers/specs/2026-04-09-phase4-design.md`
  - `docs/superpowers/specs/2026-04-09-phase5-design.md`
  - `docs/superpowers/specs/2026-04-09-design-system.md`
- GitHub backlog: issues `#1-#340`

Backlog snapshot at review time:

- Total issues: `340`
- Closed: `43`
- Open: `297`

## Executive assessment

The plan is strong as an architecture vision and weak as an execution plan.

What is strong:

- The system concept is coherent and differentiated.
- The GitHub -> RDF -> Jena runtime model is sensible.
- The temporal/versioning model is one of the strongest parts of the design.
- The issue breakdown is concrete enough to execute.

What is weak:

- Several core requirements contradict each other across documents.
- Security, retention, and access control are not treated as phase-wide acceptance criteria.
- The roadmap duration is no longer credible relative to the actual backlog.
- Multiple critical usability and operational edge cases appear only later as follow-up issues.

Short version: architecture quality is high; product governance and delivery quality are currently medium to low.

## Main strengths

1. The plan has good modular decomposition. FastHTML, Jena, Postgres, D3, sync pipeline, AI layer, and later API/MCP surfaces are separated cleanly enough to build in slices.
2. The legal-temporal model is thoughtful. `ProvisionVersion`, `DraftVersion`, `DraftingIntent`, and `Amendment` make the product more than just a search tool.
3. The plan correctly avoids rendering the whole graph at once. Lazy loading and category-level drill-down are the right choices for this dataset size.
4. GitHub as source of truth plus Jena as runtime query engine is a solid architecture choice for this domain.
5. The backlog is unusually actionable. The repo does not suffer from vague "build X" issues; most work items are decomposed to implementation level.

## Critical logic errors and contradictions

1. Draft retention policy is contradictory.
   - `AGENTS.md:58` says pre-publication drafts are politically sensitive and should use session-scoped temp graphs with no persistent draft storage beyond session TTL.
   - `estonian-legal-ontology-plan.md:235` and `estonian-legal-ontology-plan.md:564` say the same.
   - `docs/superpowers/specs/2026-04-09-phase2-design.md:444` changes this to "persistent until explicitly deleted", with a 90-day archive warning at `:447`.
   - This is not a small implementation detail. It changes the data model, privacy posture, audit scope, and legal/compliance story. The project needs one policy, not two incompatible ones.

2. Primary LLM provider is contradictory.
   - `AGENTS.md:18` and `AGENTS.md:43` say Codex API is primary.
   - `estonian-legal-ontology-plan.md:22` and `:178` say Claude is primary.
   - Phase 2 and Phase 3 specs are written around Anthropic/Claude.
   - This affects SDK choice, prompt format, tool-use behavior, cost tracking, rate limiting, eval baselines, and deployment secrets. It should be resolved before Phase 2/3 build-out continues.

3. Phase dependency model is inconsistent.
   - The main plan says Phase 4 depends only on Phase 1 auth (`estonian-legal-ontology-plan.md:366`).
   - The Phase 4 spec says it depends on Phase 1, Phase 2, and Phase 3 (`docs/superpowers/specs/2026-04-09-phase4-design.md:5`).
   - The Phase 4 spec is logically correct: annotations and notifications attach to drafts, reports, chat messages, and drafter clauses. The top-level roadmap currently understates dependencies and schedule risk.

4. The design system is treated as both optional and blocking.
   - Phase 2 says "Design System Foundation (blocks Phase 2)" at `docs/superpowers/specs/2026-04-09-phase2-design.md:755`.
   - But the master phase plan does not account for this as a gating milestone.
   - If it truly blocks Phase 2, it belongs in Phase 1 or in an explicit Phase 1.5.

5. Delivery scope is heavily underestimated.
   - The master plan still presents a `38` week roadmap (`estonian-legal-ontology-plan.md:363-367`).
   - But the backlog now contains `340` issues, with only the first `43` closed and `297` still open.
   - This is strong evidence that the current schedule is obsolete.

6. Even the basic sizing narrative is not fully consistent.
   - `AGENTS.md:7` describes `55,000+ EU legal acts`.
   - `README.md:14-15` splits that into `33,242` EU legal acts and `22,290` EU court decisions.
   - `estonian-legal-ontology-plan.md:12` describes `55,000+ EU legal acts and court decisions`.
   - This is a smaller issue than retention or LLM choice, but it still signals that the documents are drifting from each other.

## Security and compliance gaps

The backlog shows that foundational security controls are being discovered after the plan was already "approved", rather than being part of the definition of done for each phase.

Examples already visible in the issue tracker:

- Phase 1 security fixes had to be added after implementation: `#35`, `#36`, `#37`, `#40`, `#41`, `#42`.
- Draft access control appears later as `#101`.
- PII scrubbing from LLM prompts appears later as `#157`.
- Chat transcript encryption appears later as `#160`.
- Drafting session content encryption appears later as `#319`.
- Chat and drafter access control appear later as `#320` and `#321`.
- API ownership enforcement appears later as `#334`.
- Webhook secret rotation/encryption appear later as `#335` and `#336`.
- MCP audit logging and rate limiting appear later as `#337`.
- AI-generated draft provenance metadata appears later as `#338`.

Conclusion: the plan currently treats security as a stream of follow-up tasks. For a politically sensitive government drafting tool, that is not good enough. Security, privacy, auditability, and tenant isolation should be acceptance criteria in every phase.

## Operational risks

1. The Phase 2 background worker design is too fragile for the stated sensitivity level.
   - `docs/superpowers/specs/2026-04-09-phase2-design.md:557` puts the worker pool inside the main FastHTML process.
   - That is acceptable for a prototype, but it creates restart-loss, deployment coupling, and resource-contention risk in production.
   - Long-running Tika, LLM, and Jena calls should not compete with interactive web traffic inside the same process without a very clear failure model.

2. Results are not obviously reproducible across ontology changes.
   - The plan allows ontology syncs while draft analysis is ongoing, but it does not clearly snapshot the ontology version used for a report.
   - That can produce "why did this report change?" problems later.

3. Public raw SPARQL is dangerous as currently framed.
   - `docs/superpowers/specs/2026-04-09-phase5-design.md:132` and `:236` expose raw SPARQL behind a scope.
   - Rate limiting alone is not enough. You also need hard query timeouts, result caps, read-only guarantees, and likely allow-listing or query templates for public consumers.

4. The plan assumes database-level encryption without making it a real deployment requirement.
   - Phase 2 explicitly stores `parsed_text` in plain text and says DB-level encryption is merely assumed (`docs/superpowers/specs/2026-04-09-phase2-design.md:42`, `:693`).
   - "Assumed" is not a control.

## Usability problems

1. The product is still too graph-first for its target users.
   - Government drafters will usually want an answer-first workflow: report, checklist, explanation, and only then graph exploration.
   - The graph is valuable, but it should be a drill-down tool rather than the primary mental model.

2. Phase 2 async completion UX is brittle.
   - The plan relies on WebSocket toasts when analysis completes.
   - If the user navigates away or loses the socket, the main feedback loop is gone.
   - Durable notifications should arrive earlier than Phase 4, or Phase 2 needs a stronger status/inbox model.

3. Uncertainty is not first-class enough.
   - The DB design supports unmatched references and confidence scores, but the user-facing plan does not emphasize "unmatched", "ambiguous", and "low confidence" as primary UI states.
   - Without that, users will over-trust the output.

4. Theme behavior is inconsistent.
   - The design system supports light/dark mode.
   - The explorer is declared dark-only and ignores the global theme (`docs/superpowers/specs/2026-04-09-design-system.md:143-145`).
   - That may be visually defensible, but it is still a usability inconsistency that should be tested with real users.

5. Accessibility is not integrated early enough.
   - Accessibility shows up later as `#339`, not as a non-negotiable design-system baseline.
   - For a public-sector-adjacent product, that should be baked in from the start.

6. The font licensing decision is unresolved but is already part of the visual system.
   - The design system assumes Aino is permitted for use, but the license confirmation is still TODO (`docs/superpowers/specs/2026-04-09-design-system.md:61`).
   - The backlog later adds `#340` to fix this.

## Unaddressed corner cases

1. File ingestion edge cases:
   - scanned/image-only PDFs
   - password-protected documents
   - corrupted DOCX/PDF files
   - documents with tracked changes, comments, footnotes, annexes, or embedded tables
   - very large appendices that are legally important but structurally hard to parse

2. Legal reference edge cases:
   - ambiguous short names
   - repealed acts and historical references
   - references to a provision number that changed across versions
   - one draft amending multiple laws at once
   - references that only make sense when combined across paragraphs/subclauses

3. Workflow edge cases:
   - user clicks re-analyze while analyze/export is already running
   - draft is deleted while background work is still active
   - ontology sync happens mid-analysis
   - user loses org membership or is deactivated while they have running jobs or active chat/drafter sessions

4. Collaboration edge cases:
   - two users reviewing the same draft at once
   - stale annotations after a report is regenerated
   - comments attached to graph nodes that disappear under a different timeline/date filter

5. AI governance edge cases:
   - generated clause has no reliable citation
   - low-confidence extraction still flows into impact analysis
   - AI draft is exported after human edits, but provenance/watermarking is unclear
   - chat answers cite outdated ontology state after sync

6. API/MCP edge cases:
   - abusive SPARQL queries
   - API key with org scope reading another org's draft by ID
   - webhook retries replaying stale sensitive payloads
   - long-running MCP operations with incomplete audit trail

## What the issue backlog reveals

1. Phase 1 is essentially the only completed phase.
   - Issues `#1-#42`: all closed.

2. Every later phase remains largely untouched.
   - Issues `#43-#103`: all open.
   - Issues `#104-#160`: all open.
   - Issues `#161-#201`: almost entirely open.
   - Issues `#202-#340`: all open.

3. Many later issues are not "nice to have" enhancements. They are missing base requirements.
   - Examples: `#101`, `#157`, `#160`, `#319`, `#320`, `#321`, `#334`, `#335`, `#336`, `#337`, `#338`, `#339`, `#340`.

4. The backlog shape suggests scope discovery is still happening, not just execution.
   - That means the plan is not yet stable enough to use as a high-confidence delivery forecast.

## Recommended corrections before further expansion

1. Freeze one source of truth for non-functional requirements.
   - retention
   - authz matrix
   - audit events
   - encryption requirements
   - accessibility baseline
   - primary LLM provider

2. Resolve the two hard contradictions immediately.
   - draft retention policy
   - primary LLM/provider stack

3. Rebaseline the roadmap.
   - Stop using the current phase durations as planning commitments until they are reconciled with the `340` issue backlog.

4. Move security and privacy into every phase definition of done.
   - Each phase should explicitly include authz, audit, encryption, rate limiting, and failure-mode requirements where relevant.

5. Strengthen Phase 2 before adding more Phase 3/5 scope.
   - durable async job handling
   - reproducible analysis against an ontology snapshot
   - strong draft access control
   - first-class uncertainty UI

6. Add a formal permission matrix.
   - drafts
   - reports
   - annotations
   - conversations
   - drafting sessions
   - API keys
   - webhook subscriptions
   - MCP tool calls

7. Decide whether the design system is a hard dependency or not.
   - If yes, schedule it as a gating milestone.
   - If not, remove it as a blocker from the phase plan.

8. Treat API/MCP exposure as a security program, not just a wrapper layer.
   - raw SPARQL, tenant isolation, auditability, secret hygiene, and policy enforcement should be designed before public exposure.

## Bottom line

The project plan is promising and technically thoughtful, but it is not yet internally consistent enough to be considered execution-ready at government-grade quality.

The biggest problems are not missing features. They are:

- contradictory product/security decisions
- underestimated delivery scope
- late discovery of foundational compliance requirements
- insufficiently specified access-control and retention rules

If those are corrected now, the plan becomes much stronger. If not, later phases will keep generating "surprise" security, usability, and governance issues after implementation has already started.
