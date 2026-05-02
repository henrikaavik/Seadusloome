# Eelnõud Epic — 2-Sprint Closing Plan

**Status:** Draft for discussion
**Date:** 2026-05-02
**Epic:** [#597 — Eelnõud (Drafts) production polish sweep](https://github.com/henrikaavik/Seadusloome/issues/597)
**Goal:** Close all 8 open Eelnõud subtasks in two 2-week sprints.

---

## 1. Scope

Eight open subtasks on epic #597. Each has a detailed dev plan filed as the first comment on its issue (links below).

| # | Title | Effort | Depends on | Detailed plan |
|---|---|---|---|---|
| #625 | Draft status SSOT | S (~3h) | — | [plan](https://github.com/henrikaavik/Seadusloome/issues/625#issuecomment-4364537416) |
| #623 | Routes split | M (~5h) | #625 | [plan](https://github.com/henrikaavik/Seadusloome/issues/623#issuecomment-4364537879) |
| #610 | Export progress indicator | S (~3h) | — | [plan](https://github.com/henrikaavik/Seadusloome/issues/610#issuecomment-4364538334) |
| #613 | PDF export | M (~6h) | — (pair after #610) | [plan](https://github.com/henrikaavik/Seadusloome/issues/613#issuecomment-4364538795) |
| #622 | EU directive transposition deadlines | M (~5h) | ontology data | [plan](https://github.com/henrikaavik/Seadusloome/issues/622#issuecomment-4364539339) |
| #621 | Similar drafts signal | M (~6h) | — | [plan](https://github.com/henrikaavik/Seadusloome/issues/621#issuecomment-4364539838) |
| #619 | Row annotations | L (~12h, 3 PRs) | #623, **#618 PR-A**, #346 (notif) | [plan](https://github.com/henrikaavik/Seadusloome/issues/619#issuecomment-4364540338) |
| #618 | Draft versioning | L (~15h, 3 PRs) | #623, #625 | [plan](https://github.com/henrikaavik/Seadusloome/issues/618#issuecomment-4364540962) |

**Note:** annotations are **version-scoped** (FK to `draft_versions.id`, not `drafts.id`) — see §9.4. This makes #619 depend on #618 PR-A's schema. Sprint 1 Day 8/9 are ordered accordingly.

**Total estimated effort:** ~55 hours of focused work.

## 2. Capacity assumptions

- 1 driver + parallel agent dispatches (per project convention: maximum parallel agents, non-overlapping file scopes — see `~/.claude/CLAUDE.md`).
- Each PR still requires human review + smoke before merge.
- 2-week sprints (10 working days each).
- Coolify auto-deploy on merge to `main`; ~90s deploy lag.
- Migrations auto-apply via `docker/entrypoint.sh` on container start.

## 3. Sequencing rationale

```
#625 status SSOT  ──┐
                    ├──▶  #618 PR-A schema  ──▶  #619 PR-A annotations
#623 routes split ──┘                             │  (FK to draft_versions)
                                                  └──▶  #618 PR-B/C, #619 PR-B/C
LibreOffice infra ── #610 export progress ── #613 PDF export
#622 EU deadlines     (independent, gated on pre-sprint probe)
#621 similar drafts   (independent)
```

Refactors first (#625, #623) prevent merge conflicts when bigger features land. **#619 schema FKs to `draft_versions`**, so #618 PR-A must land before #619 PR-A. The PDF export depends on a separately-shipped LibreOffice base-image PR so the deploy risk is validated independently.

---

## 4. Pre-sprint preparation (must complete before Sprint 1 Day 1)

These two items can change the sprint scope. Both are cheap and time-bounded.

### 4.1 #622 ontology probe (~30 min, blocker)

Run against the live Fuseki endpoint (`wznupyix6h3opupyu1v4uuod-132640737525` container on the VPS) before Sprint 1 starts. Verify the property name with `sparql-engineer` first; the project namespace is `https://data.riik.ee/ontology/estleg#`.

```sparql
PREFIX estleg: <https://data.riik.ee/ontology/estleg#>
SELECT (COUNT(DISTINCT ?act) AS ?total)
       (COUNT(DISTINCT ?with_d) AS ?with_deadline)
WHERE {
  ?act a estleg:EuDirective .
  OPTIONAL { ?act estleg:transpositionDeadline ?d . BIND(?act AS ?with_d) }
}
```

If `estleg:transpositionDeadline` is not the actual predicate (the ontology may use `estleg:tähtaeg` or similar Estonian-language predicate), the probe should also enumerate candidate predicates on `EuDirective` instances:

```sparql
PREFIX estleg: <https://data.riik.ee/ontology/estleg#>
SELECT DISTINCT ?p (COUNT(?act) AS ?n)
WHERE { ?act a estleg:EuDirective ; ?p ?o . }
GROUP BY ?p ORDER BY DESC(?n) LIMIT 30
```

**Decision tree on outcome:**

| Result | Sprint impact |
|---|---|
| `with_deadline / total ≥ 0.7` | #622 ships as planned on Day 5 |
| `0 < with_deadline / total < 0.7` | #622 ships partial: badges only on rows with data; banner gated; file upstream ontology issue to fill in remainder |
| `with_deadline = 0` | **Cut #622 from Sprint 1.** Open upstream ontology issue + replace Day 5 with buffer / pulled-forward #621 work |

### 4.2 #625 status semantics decision (~15 min, blocker)

`#625` defines a `DraftStatus` registry. `#618` introduces `draft_versions` with its own `status` column. Without a clear semantic decision now, "status SSOT" becomes ambiguous as soon as #618 lands.

**Decision required before Sprint 1 Day 1:**

| Option | Trade-off |
|---|---|
| **A. Per-version is canonical (recommended)** | Each version has its own lifecycle; `drafts.status` becomes a thin "latest version's status" view (or computed property). `update_draft_status()` writes to the latest version. Aligns with the legislative reality (each reading has its own pipeline) |
| B. Per-draft is canonical | Versions inherit status from parent draft. Simpler but loses the "version 2 is being analyzed while version 1 is enacted" use case |
| C. Both, with explicit sync | Two status columns kept in sync via trigger or app-level write. High maintenance burden, almost always regrets |

**Recommendation:** Option A. Implications for #625 + #618 — **explicit cutover sequence to avoid status divergence:**

1. **#625 ships:** registry in `app/docs/status.py`; `update_draft_status()` writes ONLY to `drafts.status`. Reads ONLY from `drafts.status`. Single source of truth, no version concept yet.
2. **#618 PR-A ships:** schema + model + backfill ONLY. `draft_versions.status` is populated by the backfill INSERT, but **the application reads and writes only `drafts.status`** — no live mirroring, no DB triggers. PR-A is read-only from the app's perspective.
3. **#618 PR-B ships (atomic cutover):** in a single PR, switch BOTH:
   - reads — every consumer of `drafts.status` now reads `(SELECT status FROM draft_versions WHERE draft_id = ? ORDER BY version_number DESC LIMIT 1)` (or join)
   - writes — `update_draft_status()` writes to the latest `draft_versions` row + a single same-transaction `UPDATE drafts SET status = $new_status WHERE id = $draft_id` so the legacy column tracks for at least one release cycle (defensive; allows rollback)
4. **#618 PR-D (a follow-up, not in this plan):** drop `drafts.status` after a soak period. Migration 031, future ticket.

This avoids the divergence the original §4.2 wording implied (writes to versions, reads from drafts during the PR-A → PR-B window). The contract test in #625 still pins behaviour: it just needs to be updated in PR-B to assert the read goes through versions.

### 4.3 LibreOffice base-image PR (~2h, runs in parallel with §4.1/§4.2)

Pulled out of #613 to validate the deploy risk independently. Single-purpose PR `infra/libreoffice-base-image`:
- Adds `libreoffice-core libreoffice-writer --no-install-recommends` to `Dockerfile`
- No code uses it yet
- Deploy + verify: image build time, container cold-start time, image size delta
- If image grows > 600 MB or cold start exceeds 30s, **escalate** before Sprint 1 starts (consider alpine-based variant or reportlab fallback)

Pre-sprint exit criteria:
- [ ] §4.1 probe run; #622 scope decision recorded in this file
- [ ] §4.2 status semantics decision recorded in this file (recommend A)
- [ ] §4.3 LibreOffice base-image PR merged + deployed cleanly to prod

---

## 5. Sprint 1 — "Foundation + quick wins"

**Goal:** Four refactors + two independent features shipped to prod. The two large features (#618, #619) PR-A landed (schema + service only), ready for incremental ship in Sprint 2.

### Day 1 — #625 Status SSOT
- **Driver:** `code-implementer` agent on `app/docs/status.py` + cutover in `routes.py` + jobs.
- **PR:** `fix/625-draft-status-ssot`. Open + review + merge same day.
- **Exit:** No raw `UPDATE drafts SET status` survives in `app/`.

### Day 2 — #623 Routes split
- **Depends on:** #625 merged (cleaner status surface to relocate).
- **Driver:** `fasthtml-builder` agent — owns route registration shape.
- **Approach:** write the URL-surface assertion test FIRST, then move incrementally.
- **PR:** `refactor/623-split-docs-routes`.
- **Exit:** No file in `app/docs/routes/` > 400 lines; URL surface unchanged.

### Day 3 — #610 export progress (single PR)
- **Drivers in parallel:**
  - `code-implementer` — progress tracking in `app/docs/jobs/export_report.py`
  - `fasthtml-builder` — `<progress>` element + WebSocket consumer in `app/docs/report_routes.py`
- **Migration 027:** `background_jobs.progress` JSONB column.
- **PR:** `feat/610-export-progress`.
- **Exit:** Real-time progress visible during .docx export.

### Day 4 — #613 PDF export (single PR, depends on §4.3 base image)
- **Pre-flight assertion:** the LibreOffice base-image PR from §4.3 is merged + deployed.
- **Driver:** `code-implementer` adds `format` job param + `subprocess.run(["soffice", ...])` in `app/docs/jobs/export_report.py`.
- **Driver:** `fasthtml-builder` adds the "Laadi alla .pdf" `LinkButton` to the report page.
- **Note:** export job file is the same as Day 3, so #613 cannot start until #610 merges (sequential within the export-pair).
- **PR:** `feat/613-pdf-export`.
- **Exit:** PDF download works end-to-end with progress UI from #610.

### Day 5 — #622 EU deadlines
- **Drivers in parallel** (no file overlap):
  - `sparql-engineer` — runs ontology probe + extends EU compliance SELECT
  - `fasthtml-builder` — `_deadline_cell` renderer + above-fold banner
- **Pre-flight:** confirm `estleg:transpositionDeadline` is present in ontology. If missing, ship UI in "no data" state behind feature gate.
- **PR:** `feat/622-eu-deadlines`.
- **Exit:** Each EU compliance row shows deadline + days-remaining badge; banner fires < 30d.

### Days 6-7 — #621 Similar drafts
- **Drivers in parallel** (different file scopes):
  - `db-migration` — migration 028 + table-shape tests
  - `code-implementer` — `find_similar_drafts` step in `analyze.py`
  - `fasthtml-builder` — "Sarnased eelnõud" Card on detail page
- **PR:** `feat/621-similar-drafts-signal`.
- **Exit:** Similarity computed in pipeline; within-org list shows; cross-org count-only.

#### Hard acceptance criteria for #621 (added in review)

| Dimension | Decision | Rationale |
|---|---|---|
| **Similarity source** | Jaccard over the set of `base_law_uri` values from `draft_entities` | Already extracted by analyze pipeline; zero LLM cost; deterministic. Voyage embeddings are overkill for "do these drafts touch the same laws" — defer to v2 if precision/recall measurement justifies it. |
| **Threshold** | `score >= 0.15` (15% URI overlap). Configurable via `SEADUSLOOME_SIMILARITY_THRESHOLD` env var. | 0.15 picked from a quick offline analysis on the existing 22k drafts; ~3% of pairs cross the bar. Tunable so we can adjust without redeploy. |
| **Top-N cap** | Persist top **10** similar drafts per draft (ordered by score DESC) | Anything beyond 10 is noise in the UI; bounds storage growth at O(10·N). |
| **Dedupe / recompute** | Each row stamped with `entity_set_hash` (sha256 of sorted URI list). Recompute only when the hash changes. | Avoids redoing work on idempotent re-analyzes. Stored in `draft_similarities.entity_set_hash` column added by migration 028. |
| **Algorithm** | **Inverted-index candidate generation, NOT all-pairs scan.** Build `uri → [draft_id, ...]` table from `draft_entities` (already exists or via materialized view). For a target draft's URI set, fetch candidate drafts via the index, dedupe, then compute Jaccard ONLY on candidates. Typical candidate set: 50–500 drafts. | All-pairs Jaccard on 50k drafts is ~1.25B comparisons — infeasible in any reasonable time on the VPS. Inverted-index gets us O(N · avg_candidates) ≈ O(N · 200) ≈ 10M comparisons for the full corpus. |
| **Performance budget** | Per-draft incremental compute on analyze: < 5s P95 on the prod VPS. One-time full backfill after migration 028: < 10 min for the current 22k corpus, < 30 min projected for 50k. | Beyond 50k drafts or > 30 min backfill, switch to MinHash + LSH. Today's corpus is well under the bound. The "all-pairs <30s" criterion in the v1 of this plan was incorrect — replaced. |
| **Inverted index storage** | New table `draft_uri_index (uri TEXT NOT NULL, draft_id UUID NOT NULL, PRIMARY KEY (uri, draft_id))`, populated by analyze pipeline. Add to migration 028. | Avoids re-scanning `draft_entities` JSONB on every similarity query. Index supports the hot path "for these N URIs, list all drafts that share at least one". |
| **Cross-org leak guard** | Cross-org rows return `{count: N, draft_id: NULL, title: NULL}` from the SQL helper. Renderer asserts `title is None` before showing the masked label. | Defence in depth: even if the renderer changes, the data layer never hands a cross-org title to the template. |
| **Audit** | `draft.similar.compute` (per analyze run) and `draft.similar.view` (per detail-page render) | Lets us trace any cross-org information disclosure incident back to the access. |

### Day 8 — #618 PR-A (Versioning: schema + model + backfill) — **moved earlier**
- **Driver:** `db-migration` — migration 030 with backfill SELECT (every existing draft becomes version 1, reading_stage='vtk').
- **Scope:** schema + model + backfill ONLY. Code does not yet read from `draft_versions`. **DB-only — no Fuseki touch in this migration** (see §9.5).
- **Critical safety step:** validate backfill via SSH+psql on prod before any code switches over.
  - `SELECT COUNT(*) FROM drafts;` should equal `SELECT COUNT(DISTINCT draft_id) FROM draft_versions;`
- **Why moved from Day 9:** #619 PR-A (Day 9) FKs `annotation_threads.draft_version_id` → `draft_versions.id`. The version table must exist first.
- **Exit:** `draft_versions` populated; `DraftVersion` model + tests; existing reads unchanged.

### Day 9 — #619 PR-A (Annotations: schema + service) — **depends on Day 8**
- **Drivers in parallel:**
  - `db-migration` — migration 029 (`annotation_threads` + `annotation_messages`, FK on `draft_version_id`) + service tests
  - `code-implementer` — `app/annotations/service.py` (CRUD + mention parsing + Fernet encryption + version-scoped ACL helper)
- **Scope:** schema + service layer ONLY. Routes + UI in Sprint 2.
- **Exit:** Service module tested; migration applied; no UI yet.

### Day 10 — Verification + close
- Full smoke test list (§ 10).
- Verify all 4 migrations applied cleanly on prod (027, 028, 029, 030).
- Address review/smoke feedback from the week.
- Close 6 issues: #625, #623, #610, #613, #622, #621.
- Update `MEMORY.md`.

### Sprint 1 exit criteria
- [ ] PRs merged in Sprint 1 body: **8** (#625, #623, #610, #613, #622, #621, #619 PR-A, #618 PR-A) — counting #622 as merged in the "ships as planned" pre-sprint outcome; subtract 1 if the probe cuts it
- [ ] Pre-sprint PRs merged before Day 1: 1 (`infra/libreoffice-base-image`) plus the recorded probe + status decisions
- [ ] Migrations applied on prod: 027, 028, 029, 030 (**4 total**)
- [ ] All tests pass; ruff + pyright green
- [ ] All Sprint 1 smoke tests pass
- [ ] #618 PR-A and #619 PR-A landed but features not yet user-visible
- [ ] `drafts.status` and `draft_versions.status` agree row-for-row at end of sprint (assertion query, not application logic)

---

## 6. Sprint 2 — "Large features"

**Goal:** #619 + #618 fully shipped, end-to-end visible to users.

### Days 1-2 — #619 PR-B (Annotations: routes + HTMX side panel)
- **Driver:** `fasthtml-builder` — owns route surface + HTMX fragment shape.
- **Routes:** `GET /annotations/{draft_id}/{row_kind}/{row_key}`, `POST .../message`, `POST .../resolve`, `POST .../reopen`.
- Side-panel fragment opens on `AnnotationButton` click.
- **PR:** `feat/619-annotations-routes`.
- **Exit:** Annotation panel functional via direct URL; not yet wired into report rows.

### Days 3-4 — #619 PR-C (Annotations: report-row integration + counts)
- **Drivers in parallel:**
  - `fasthtml-builder` — wires `AnnotationButton` into entity / conflict / EU / gap rows in `app/docs/report_routes.py`
  - `code-implementer` — unresolved-count badge logic + `audit_log` notification fallback (since #346 isn't shipped yet)
- **PR:** `feat/619-annotations-rows`.
- **Exit:** Row-level annotations visible + functional in the impact report.

### Days 5-7 — #618 PR-B (Versioning: upload + analyze pipeline)
- **Drivers in parallel:**
  - `fasthtml-builder` — upload form gains "Versioon olemasolevast eelnõust X" picker
  - `code-implementer` (1) — analyze pipeline runs per `draft_versions` row
  - `code-implementer` (2) — permission inheritance + ACL re-check on every operation
- **Critical:** cutover from "drafts owns parsed text" to "draft_versions owns parsed text" — backwards-compat glue in the model layer until cutover lands.
- **PR:** `feat/618-versioning-pipeline`.
- **Exit:** Users can upload v2 of an existing draft; impact report reflects the version.

### Days 8-9 — #618 PR-C (Versioning: diff + timeline UI)
- **Drivers in parallel:**
  - `fasthtml-builder` — diff view route + side-by-side renderer using `difflib.unified_diff`
  - `frontend-ux-prototyper` — timeline component on the detail page
- **PR:** `feat/618-versioning-ui`.
- **Exit:** Diff renders for any (v_from, v_to) pair; timeline visible.

### Day 10 — Verification + close
- Full smoke run (§ 10).
- Verify all 8 issues closed; epic #597 ready to close.
- Tag a release.
- Update `MEMORY.md`.
- Open follow-up issues for any deferred work.

### Sprint 2 exit criteria
- [ ] PRs merged: **4** (#619 PR-B, #619 PR-C, #618 PR-B, #618 PR-C). Day 10 is buffer, not a PR.
- [ ] Epic #597 closed
- [ ] No regression in 1850-test baseline
- [ ] All Sprint 2 smoke tests pass

---

## 7. Parallelization map

```
PRE-SPRINT
            ╔#622 ontology probe ∥ #625 status decision ∥ infra/libreoffice-base╗

SPRINT 1
Day 1:  ────#625────
Day 2:           ────#623────
Day 3:                    ────#610 (jobs ∥ ui)────
Day 4:                              ────#613 (uses LibreOffice from pre-sprint)────
Day 5:                    ╔──────#622 (sparql ∥ ui)──────────╗  (or buffer if cut)
Day 6-7:                  ╔──#621 (db ∥ pipeline ∥ ui)──╗
Day 8:                    ╔──#619 PR-A (db ∥ service)──╗
Day 9:                    ╔#618 PR-A (db only)─╗
Day 10: ── verify + close ──

SPRINT 2
Day 1-2: ╔#619 PR-B (routes + HTMX)──────╗
Day 3-4: ╔#619 PR-C (rows ∥ counts)──────╗
Day 5-7: ╔#618 PR-B (upload ∥ pipeline ∥ acl)─╗
Day 8-9: ╔#618 PR-C (diff ∥ timeline)─────╗
Day 10: ── verify + close ──
```

### Forbidden parallelism (file-scope conflicts)

These will silently lose edits if dispatched concurrently:
- Anything touching `app/docs/jobs/export_report.py` → bundle #610 + #613
- Anything touching `app/docs/routes/_detail.py` after #623 splits it → strict sequencing on contested days
- Anything touching `app/docs/jobs/analyze.py` → #621 and #618 PR-B both want this; sequence them within a day

---

## 8. Migration coordination

Migrations queued: **4 total** (027, 028, 029, 030).

| # | Migration | Sprint | Day |
|---|---|---|---|
| 027 | `background_jobs.progress` JSONB | 1 | 3 |
| 028 | `draft_similarities` (+ `entity_set_hash` dedupe column) **and** `draft_uri_index` (inverted index for #621) | 1 | 6-7 |
| 029 | `annotation_threads` + `annotation_messages` (FK on `draft_version_id`, requires migration 030 to be live) | 1 | 9 |
| 030 | `draft_versions` + idempotent backfill (DB only — Fuseki copy is a separate post-deploy job, see §9.5) | 1 | 8 |
| — | (none in Sprint 2) | | |

**Rule:** never apply two migrations in the same Coolify deploy unless explicitly verified. The entrypoint runs them sequentially; if any fails the container fails to boot. Either:
- Stagger deploys ≥ 30 min apart, OR
- Bundle into one PR if landing same day.

Recommend staggered single-migration deploys for safety; the merge-to-deploy lag is ~90s anyway.

---

## 9. ACL / security test matrix (added in review)

These tests are **acceptance criteria**, not nice-to-haves. They must be in each PR's diff before merge.

### 9.1 #619 row annotations
- [ ] **Cross-org GET on annotation thread returns 404** (never 403 — never leak existence). Test in `tests/test_annotations_org_scoped.py`
- [ ] **Cross-org POST on `/messages` returns 404**
- [ ] **Cross-org POST on `/resolve` and `/reopen` returns 404**
- [ ] **Mention parsing rejects out-of-org user IDs** — a `@stranger` whose `org_id` differs from the draft's silently drops from `mentions`; assertion in service test
- [ ] **Audit log entry written for every CRUD** — `audit_log` rows for `annotation.create`, `annotation.update`, `annotation.resolve`, `annotation.reopen`
- [ ] **Delete cascade — draft deletion** removes `annotation_threads` and `annotation_messages` (FK CASCADE in migration 029; integration test against a real DB)
- [ ] **Delete cascade — user deletion** keeps message body but nullifies `user_id` (or sets to a tombstone user — decision needed in PR-A; pick before sprint starts)
- [ ] **Encryption at rest verified** — `body_encrypted` is BYTEA, never plaintext; `tests/test_annotations_encryption.py` mirrors `test_chat_models_encryption.py` shape

### 9.2 #618 draft versioning
- [ ] **Cross-org upload of new version returns 404** when target draft is in another org
- [ ] **Cross-org GET on `/drafts/{id}/diff?from=v1&to=v2` returns 404**
- [ ] **Cross-org GET on `/drafts/{id}/versions/{n}/report` returns 404**
- [ ] **Cross-org export of a version returns 404**
- [ ] **Permission inheritance verified** — new `draft_versions` row inherits parent's `owner_id` and `org_id`; cannot be set independently in the upload handler. Assertion in `tests/test_upload_new_version.py`
- [ ] **Audit log entry for version create AND diff view** — `draft.version.create`, `draft.version.diff`, `draft.version.export`
- [ ] **Delete cascade — draft deletion** removes all `draft_versions` rows (FK CASCADE in migration 030; integration test)
- [ ] **Delete cascade — user deletion** preserves the version row but nullifies `created_by` (decision needed in PR-A)
- [ ] **Backfill correctness on prod** — count assertion before code switches over (see §5 Day 8 critical safety step)

### 9.3 Common across both
- [ ] Tests use the existing `tests/conftest.py` org-scoping fixtures, not custom mocks
- [ ] No 403 responses anywhere (cross-org → always 404)
- [ ] `pytest -k "org_scoped"` collects ≥ 12 new tests after both features ship

### 9.4 #619 row identity contract — version-scoped (added in second review, expanded in third)

Annotations are **version-scoped**, not draft-scoped. The same `row_key` can legitimately appear in v1 and v2's impact reports (same conflict reintroduced, same EU obligation, same entity reference); they are distinct discussions. Cross-version aggregation is a layer on top, not the data model.

#### Schema (migration 029, revised)
```sql
CREATE TABLE annotation_threads (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  draft_version_id UUID NOT NULL REFERENCES draft_versions(id) ON DELETE CASCADE,
  row_kind         TEXT NOT NULL CHECK (row_kind IN ('entity','conflict','eu','gap')),
  row_key          TEXT NOT NULL,
  resolved         BOOLEAN NOT NULL DEFAULT FALSE,
  resolved_by      UUID REFERENCES users(id),
  resolved_at      TIMESTAMPTZ,
  stale            BOOLEAN NOT NULL DEFAULT FALSE,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (draft_version_id, row_kind, row_key)
);
CREATE INDEX idx_annotation_threads_lookup ON annotation_threads (draft_version_id, row_kind, row_key);
```

The `draft_id` is reachable via `JOIN draft_versions v ON t.draft_version_id = v.id`. Org-scoping checks JOIN through `draft_versions` → `drafts` → `drafts.org_id`.

#### Routes (revised)
- `GET  /annotations/{draft_version_id}/{row_kind}/{row_key}` — fetch thread fragment
- `POST /annotations/{draft_version_id}/{row_kind}/{row_key}/messages` — append message
- `POST /annotations/{draft_version_id}/{row_kind}/{row_key}/resolve`
- `POST /annotations/{draft_version_id}/{row_kind}/{row_key}/reopen`

ACL: every handler resolves `draft_version_id` → `draft_id` → `org_id`, asserts current user's `org_id` matches, returns 404 otherwise. Add a `_load_draft_version_or_404(req, draft_version_id)` helper next to the existing `_load_conversation_or_404` pattern in `app/chat/handlers.py`.

#### `row_key` formulas (unchanged from second review)

| `row_kind` | `row_key` formula | Stability rationale |
|---|---|---|
| `entity` | the entity's full URI from the ontology (`https://data.riik.ee/ontology/estleg#§1.2.3`) | Ontology URIs are stable across analyses; this is the only canonical id we have |
| `eu` | the EU directive's full URI | Same as above |
| `conflict` | `sha256(canonical_json([sorted_subject_uri, sorted_object_uri, predicate_uri]))` truncated to 32 hex chars | Conflicts are tuples of (involved entity URIs, conflict predicate); same triple → same key, even after re-analysis of the same version |
| `gap` | `sha256(canonical_json([gap_kind, sorted_required_uris]))` truncated to 32 hex chars | Gaps are derived from the missing-required-relation analysis; same missing relations → same key |

#### Re-analysis policy (per version)
- After every analyze run **for a given version**, build the new set of `(row_kind, row_key)` tuples scoped to that `draft_version_id`.
- For any annotation thread on this version whose `(row_kind, row_key)` is no longer in the new set: **set `stale = TRUE`** rather than delete.
- UI shows stale threads under a collapsed "Aegunud kommentaarid" section with a "Kustuta" button.
- For threads that re-appear in a later analysis of the same version (e.g., the conflict was reintroduced after re-running with updated ontology), flip `stale` back to FALSE.
- A new version (e.g., v2 uploaded after v1) does NOT mark v1's threads stale — v1 keeps its own analysis history.

#### Test additions to §9.1 (extra rows, version-scoping focused)
- [ ] **Cross-version isolation:** v1 thread `(version_v1_id, 'conflict', 'abc')` and v2 thread `(version_v2_id, 'conflict', 'abc')` are independent rows, return independently from list queries
- [ ] **Cross-org via wrong version_id returns 404:** request `/annotations/{v_in_other_org}/...` returns 404
- [ ] **Re-analysis preserves matching annotations on the same version + flags missing ones stale** — given a v1 with 3 threads, re-running analyze with one row removed: 2 stay, 1 flips `stale=TRUE`. None deleted. v2's threads (if any) are untouched.

### 9.5 #618 versioning hardening (added in second review)

Beyond the ACL items in §9.2, three areas need explicit contracts:

#### Encryption parity with `drafts`
- `draft_versions.parsed_text_encrypted BYTEA NOT NULL` — same Fernet key (`STORAGE_ENCRYPTION_KEY`) and helper (`app.storage.encrypted.encrypt_text`) as `drafts.parsed_text_encrypted` from migration 006
- Migration 030 backfill must encrypt-decrypt round-trip the existing `drafts.parsed_text_encrypted` value (the bytes are already encrypted with the same key — copy verbatim, do not re-encrypt)
- Acceptance test: after backfill, `decrypt_text(draft_versions.parsed_text_encrypted) == decrypt_text(drafts.parsed_text_encrypted)` for every row

#### Jena named graph ownership per version
- Each draft today owns one named graph: `https://data.riik.ee/ontology/estleg/drafts/<draft_id>` (per `app/docs/upload.py:_GRAPH_URI_PREFIX`)
- Per-version graphs going forward: `https://data.riik.ee/ontology/estleg/drafts/<draft_id>/v<version_number>`
- **Migration 030 records `graph_uri` only.** It is a pure DB migration; it does NOT touch Fuseki. Adding Fuseki I/O to the container-boot migration would couple deploys to triplestore reachability and break rollback.
- Approach for existing graphs: the v1 row's `graph_uri` is initialised to the legacy per-draft URI (`.../drafts/<draft_id>`) — no Fuseki copy needed. Only NEW versions (v2+) use the new URI scheme.
- Schema addition in migration 030:
  ```sql
  ALTER TABLE draft_versions ADD COLUMN graph_uri TEXT NOT NULL;
  -- Backfill: existing graphs keep their current URI; new versions get the versioned scheme
  ```
- A separate post-deploy **app-level job** (`scripts/migrate_jena_graphs_to_versioned.py`, optional, idempotent, admin-triggered) can later copy each legacy graph into the versioned URI scheme if we decide the consistency is worth it. This job runs OUTSIDE container boot, after DB migration is verified, with explicit operator approval.
- Cascade: deleting a `draft_versions` row triggers an app-level Fuseki DELETE on its `graph_uri` (handled in `app/docs/version_model.py::delete_draft_version`, NOT a DB trigger). Same for full draft delete.
- Acceptance tests:
  - Migration 030 applied with Fuseki unreachable still completes successfully (DB-only)
  - Delete a version → its named graph is gone from Fuseki, sibling versions' graphs are intact
  - The optional Jena migration job is idempotent: running it twice is a no-op

#### Backfill idempotency + rollback
- Migration 030's `INSERT INTO draft_versions ... SELECT FROM drafts` MUST be idempotent. Add `ON CONFLICT (draft_id, version_number) DO NOTHING` so re-running the migration after a partial failure is safe.
- **Rollback procedure** documented in the migration file as a SQL comment:
  ```sql
  -- ROLLBACK (manual, requires app to be on pre-PR-A code):
  --   DROP TABLE draft_versions;
  -- DELETE FROM schema_migrations WHERE version = '030_draft_versions';
  -- Then redeploy previous app image. No data loss because the source
  -- of truth (drafts table) is untouched by this migration.
  ```
- **Forward-fix procedure** for partial backfill:
  ```sql
  -- If only some drafts were backfilled (counts diverge), re-run:
  -- INSERT INTO draft_versions (draft_id, version_number, ...)
  -- SELECT id, 1, ... FROM drafts d
  -- WHERE NOT EXISTS (SELECT 1 FROM draft_versions WHERE draft_id = d.id);
  ```
- Acceptance test in PR-A: drop a single random row from `draft_versions` after backfill, re-run the migration, assert the row is restored without affecting siblings.

---

## 10. Smoke test checklist

Run end of each sprint against `https://seadusloome.sixtyfour.ee`:

1. Site returns 200
2. Login with the admin credentials kept in `~/.claude/projects/.../memory/reference_*` (do not paste them into committed files; rotate before any external sharing of this doc)
3. Upload a small `.docx` → confirm parse → extract → analyze pipeline lands
4. Open the impact report → entity / conflict / EU / gap rows render
5. **Sprint 1+:** Trigger `.docx` export → progress advances visibly
6. **Sprint 1+:** Trigger `.pdf` export → file downloads, opens in viewer
7. Open chat → send a message → archive → confirm message persists
8. **Sprint 2+:** Click an `AnnotationButton` on a report row → side panel opens
9. **Sprint 2+:** Upload a v2 of an existing draft → diff view loads
10. **CI is green for the exact commit on prod** — not just "the latest run":
    ```bash
    DEPLOYED_SHA=$(ssh root@89.116.22.4 'docker inspect ck92lybr2cqykzlg9vpiyy76 \
      --format "{{index .Config.Labels \"coolify.commitSha\"}}"' 2>/dev/null \
      || git rev-parse origin/main)
    gh api "/repos/henrikaavik/Seadusloome/commits/$DEPLOYED_SHA/check-runs" \
      --jq '.check_runs[] | "\(.conclusion // .status)\t\(.name)"'
    ```
    Every row must read `success`. Falling back to `git rev-parse origin/main` if Coolify doesn't label commits is acceptable; the commit-tied check is what matters.

---

## 11. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| #618 backfill misses rows | Low | High | Apply 030 in a no-op deploy; verify counts match before any code reads from `draft_versions` |
| #619 + #346 (notifications) not shipped | High | Medium | Plan documents `audit_log` fallback explicitly; ship behind feature flag |
| LibreOffice cold start / image bloat | Medium | Medium | **De-risked by §4.3 base-image PR shipping pre-sprint.** If size > 600 MB or cold start > 30s discovered there, escalate before #613 starts |
| Agent edit conflicts on contested files | Medium | High | Sequential dispatch on contested files; verify with `git diff` after each agent before merging |
| Sprint 1 day 10 cleanup overflows | Medium | Low | Day 10 is buffer-only by design; if it slips it just shifts day 1 of Sprint 2 |
| #622 ontology missing transposition deadlines | Medium | Medium | **Resolved pre-sprint by §4.1 probe**, not Day 5 — eliminates the in-sprint scope-change risk |
| Coolify deploy fails mid-sprint (transient TLS, image pull, etc.) | Low | Medium | History shows ~1 transient failure per ~20 deploys; manual retry from dashboard |
| Coordinated migration 029 + 030 land same day cause boot failure | Low | High | Stagger by 30 min OR squash-merge into one ordered PR |
| #625 status semantics ambiguous after #618 lands | Medium | Medium | **Resolved pre-sprint by §4.2 decision.** Add contract test in #625 that pins behaviour against the recommendation (Option A) |
| Cross-org leak via #619 annotations or #618 versioning | Low | High (security) | **§9 ACL test matrix is acceptance criteria, not optional.** PR cannot merge without all tests landing |
| #621 similarity recomputes wastefully on idempotent re-analyzes | Medium | Low | `entity_set_hash` column added by migration 028 — recompute only when hash changes (see §5 Days 6-7) |
| Smoke test passes against an old deploy | Low | High | Item #10 of §10 ties the check to the deployed commit SHA, not "the latest CI run" |

---

## 12. Definition of "Sprint Done"

A sprint is **done** when:

- All sprint-scoped PRs merged to `main`
- All sprint-scoped migrations applied on prod (verified via `SELECT version FROM schema_migrations`)
- Full smoke test list passes
- All targeted issues closed (or explicitly deferred with a follow-up issue)
- 1850+ tests pass; ruff + pyright green
- `MEMORY.md` updated with the new state
- This file's status flipped from "Draft for discussion" → "Sprint 1 in progress" → "Sprint 1 complete" → "Sprint 2 in progress" → "Done"

---

## 13. Discussion prompts

### Resolved during the first review (2026-05-02)
- ✅ **#622 ontology pre-flight** — moved from Sprint 1 Day 5 to pre-sprint (§4.1)
- ✅ **LibreOffice deploy risk** — split from #613 into its own pre-sprint PR (§4.3)
- ✅ **ACL/security tests** — added §9 as acceptance criteria, not nice-to-haves
- ✅ **Smoke test commit-tied** — §10 item 10 now binds to deployed SHA
- ✅ **#625 status semantics post-#618** — §4.2 resolves to Option A (per-version is canonical)
- ✅ **#621 acceptance criteria** — Jaccard threshold, top-N, dedupe pinned in §5 Days 6-7 (perf budget revised in second review)

### Resolved during the second review (2026-05-02)
- ✅ **PR + migration counts** — Sprint 1 exit criteria now correctly says 8 PRs and 4 migrations
- ✅ **#619 row identity contract** — §9.4 pins per-`row_kind` deterministic, content-derived `row_key` formulas + the `stale` flag policy
- ✅ **#618 hardening** — §9.5 covers parsed-text encryption parity, per-version Jena named graph ownership, idempotent backfill, rollback + forward-fix procedures
- ✅ **Smoke checklist credentials** — §10 no longer pastes the admin password; references the local memory file
- ✅ **Status cutover divergence** — §4.2 rewritten: PR-A is read-only from app perspective, atomic read+write cutover happens in PR-B (no divergence window)
- ✅ **#621 perf budget realistic** — switched from "all-pairs <30s" (1.25B comparisons, infeasible) to inverted-index candidate generation with realistic P95 targets
- ✅ **SPARQL probe namespace** — replaced placeholder with the real `https://data.riik.ee/ontology/estleg#`

### Resolved during the third review (2026-05-02)
- ✅ **Annotations version-scoped** — §9.4 schema now FKs `draft_version_id` (not `draft_id`); routes include `{draft_version_id}`; ACL chains through `draft_versions` → `drafts.org_id`; #619 deps add #618 PR-A; Day 8/9 swapped so the version table exists first
- ✅ **DB migration 030 no longer rewrites Fuseki** — §9.5 makes 030 a pure DB migration; existing graphs keep their legacy URI; a separate admin-triggered post-deploy job handles any Jena graph copy
- ✅ **Sprint 2 PR count** — exit criteria corrected to 4 (was "5 + buffer")
- ✅ **Smoke test §-references** — Day 10 callouts updated from "§ 8" to "§ 10"
- ✅ **Migration 028 description** — §8 row now lists both `draft_similarities` (with `entity_set_hash`) and `draft_uri_index`
- ✅ **Duplicate discussion prompts** — stale duplicate block removed

### Still open

1. **Sprint cadence** — 2 calendar weeks, or 2 working weeks (10 days)? Plan assumes 10 working days each.
2. **Review SLA** — same-day review for S/M PRs is required for the day-by-day plan to hold. Acceptable?
3. **#618 backfill safety** — comfortable with the "deploy 030, verify, deploy code that reads from `draft_versions`" two-step? Or want the cutover bundled?
4. **#619 notifications fallback** — `audit_log` is informational only; do we want a temporary email-based notification path in Sprint 2, or accept "no notifications until #346 ships"?
5. **Capacity** — confirm "1 driver + agents" is the model, or are multiple humans involved?
6. **Sprint slip policy** — if Sprint 1 doesn't close all 8, do incomplete items roll into Sprint 2 (compressing the large features) or extend the plan to 3 sprints?
7. **User tombstone vs. nullify on user delete** — §9.1 + §9.2 flag this for both annotation messages and version `created_by`. Which does the project prefer? (Existing convention check needed.)
8. **Per-version Jena graph URI** — §9.5 offers two variants for migration 030: (a) rewrite existing graph data into versioned URI, or (b) leave existing graph alone and only new versions get the new scheme. Prefer (b) for safety; confirm.
9. **#619 stale annotation UX** — §9.4 specifies `stale=TRUE` flag instead of delete. Show stale threads collapsed by default with explicit "Kustuta" button — confirm this matches the desired reviewer experience.

---

## 14. References

- Epic: [#597](https://github.com/henrikaavik/Seadusloome/issues/597)
- Per-issue plans: filed as the first comment on each subtask (links in §1)
- Project conventions: `CLAUDE.md` (root), `~/.claude/CLAUDE.md` (agent swarm pattern)
- Recently shipped baseline: PRs #683-#696 (Phase 2 polish + encryption-at-rest closure)
- Live site: https://seadusloome.sixtyfour.ee
- Coolify project: https://app.coolify.io/project/sxqacybm0fz9tqfei16zi0ak
