# Usability-Test Fixes — Development Plan (v3)

Date: 2026-05-19
Revision history:
- v1 → v2: counts reconciled, #814 scope tightened, #816 unified across renderer+state-machine, #817 switched to `draft_reviews` table, #813 moved to Phase 1.5 as mechanical hardening, per-ticket prod verification checklist added, older issues explicitly out of scope
- v2 → v3 (post technical review):
  - #817 `reviewer_id` made nullable (NOT NULL + ON DELETE SET NULL was self-contradictory per `migrations/012:31-34`)
  - #805/#815 unresolved-EU warning re-pointed to `app/analyysikeskus/routes.py:2334` (`_render_eu_unresolved`) and `app/docs/analyze_handler.py:107-120` (where draft_entities are accessible pre-filter); `ImpactAnalyzer` is explicitly Postgres-free per its module docstring
  - #813 grep widened to catch `attrs["X"] = True` dict-assignment pattern + added missed sites in primitives `Option`/`Radio`, `_list.py:352`, `tabs.py:84`, `global_search.py:101`
  - #814 no longer reuses `entity_extractor.py` (it's literal-only with explicit "Never invent references" rule per `entity_extractor.py:61`); new `intent_extractor.py` with `feature="intent_analysis"` cost-tracking
  - Parallel-safety table fixed: #805/#815 and #814 both touch `routes.py` — bundle or sequence

Source: Issues #800–#817 + epic #784. Validates `docs/2026-05-18-social-ministry-usability-testing-plan.md` execution evidence from 2026-05-19.

## 1. Issue inventory — reconciled

**Total open: 50.**

| Bucket | Count | Issues | Treatment in this plan |
|---|---|---|---|
| Older (out of scope) | 16 | #349-#362, #622, #680 | Out of scope — separate triage |
| Epic #784 (parallel track) | 17 | #784-#800 | Status snapshot in §6, not driven here |
| Usability stale-open | 11 | #801, #802, #803, #804, #806, #807, #808, #809, #810, #811, #812 | Phase 1: per-ticket prod verify + close |
| Usability real fixes | 6 (5 workstreams) | #805+#815 bundled, #813, #814, #816, #817 | Phase 1.5 + Phase 2 |

Sum check: 16 + 17 + 11 + 6 = 50. ✓

## 2. Stale-open — per-ticket prod verification checklist

Each ticket gets an exact prod URL + action + expected result + regression check. **Don't blanket "re-run all stories."** Verify only the path the fix touches; smoke-test adjacent surfaces separately.

| # | Fix commit | Prod check (URL/action) | Expected result | Regression check |
|---|---|---|---|---|
| #801 | `2d5d2ec`, `9ce24b6`, `3ae05d6` | Upload a draft mentioning "AvTS § 35" and "KarS § 121" → wait for `Valmis` → open impact report | At least one affected entity per high-confidence ref; no 0/0/0/0 reports | Confirm impact_reports.entity_uri column populated; spot-check 3 random recent drafts |
| #802 | `b901455` | Open `/chat`, send "Tere" with no draft context | Response within 30s; no infinite hang | Send 5 messages in a row; observe orchestrator deadline metric |
| #803 | `2d5d2ec`, `fd1156f` | `/analyysikeskus/normi-mojuahel?sisend=AvTS § 35` | Workflow renders impact table for AvTS (Avaliku teabe seadus); not "ei tuvastatud" | Try 3 more bare-law refs: "VTMS § 18", "TsÜS § 12", "KarS § 121" |
| #804 | `8c2d606` | `/explorer` cold load → DevTools console | No `ReferenceError: Cannot access 'minimapViewportRect'` | Reload 5×; check Network panel for failed bootstrap fetch |
| #806 | `8c2d606` (same TDZ) | `/explorer?search=AvTS%20%C2%A7%2035` | Toolbar input populated; result panel non-empty OR "Tulemusi ei leitud" empty state | Try `?search=KarS § 121` and `?search=tervis` |
| #807 | `8c2d606` (same TDZ) | `/explorer?vaade=koik` | 5 category-level nodes render (kehtiv seadus / eelnõu / kohtulahend / EL õigusakt / EL kohtulahend) | Mini-map populated; click each category to drill in |
| #808 | `4b65dce`, `da51a5d`, `222dab4` | Login as a user whose org has 0 VTKs → `/drafts/upload` → click VTK dropdown | Dropdown shows "Selles töötsoonis pole veel VTK-sid" disabled state, not empty `— vali —` | Toggle doc_type Eelnõu/VTK 3× — dropdown state persists |
| #809 | `9ce32b0` | Open a draft with `Valmis` impact report → click "Küsi nõustajalt" | System prompt includes real impact counts (e.g., "12 mõjutatud sätet, 2 konflikti") not "pole saadaval" | Ask 3 questions; chat answers reference the actual report, not unrelated context |
| #810 | `fb2528c` | Upload a doc as `Eelnõu` → version history sidebar | v1 row shows "Esitatud" (not "VTK") | Upload as VTK → v1 shows "VTK"; upload doc_type=eelnou → v1 shows "Esitatud" |
| #811 | `36565df` | Open a report → Safari → click "Lae alla DOCX" and "Lae alla PDF" | File downloads directly; no spinner / no HTMX POST roundtrip | Confirm in Chrome too (regression on the previous path) |
| #812 | `754b9af` | Safari → `/drafter/new` → select "Täielik seadus" → click "Alusta" | Form submits; user lands on step 2 (intent capture) | Confirm in Chrome; confirm the workflow_type radio is preselected |

**Phase 1 execution:**
1. Verify Coolify deploy actually pushes current main (check `/version` endpoint or container SHA).
2. Walk the 11 checks above (estimated ~60 min, not 30 — corrected per feedback).
3. For each pass: close the issue with verification comment quoting the fix commit + prod URL tested.
4. For each fail: re-open with fresh prod evidence (screenshot, console log).

## 3. Phase 1.5 — #813 mechanical hardening (do before UI work)

**Why first:** root cause is in `app/ui/primitives/input.py` and ripples to every form via wrappers. Doing the UI fixes (#814, #817) before #813 risks introducing new bool-true patterns. The primitives change is mechanical and well-scoped.

**Fix scope — two patterns to convert:**

Pattern A (keyword form): `attr=True` passed to FT component
```python
Button("Submit", disabled=True)      # → disabled="disabled"
Details(..., open=True)               # → open="open"
Form(..., novalidate=True)            # → novalidate="novalidate"
Input(..., autofocus=True)            # → autofocus="autofocus"
```

Pattern B (dict-assignment form): `attrs["..."] = True` then spread into FT component (grep for Pattern A misses these)
```python
attrs["required"] = True              # → attrs["required"] = "required"
attrs["checked"] = True               # → attrs["checked"] = "checked"
attrs["hidden"] = True                # → attrs["hidden"] = "hidden"
input_attrs["autofocus"] = True       # → input_attrs["autofocus"] = "autofocus"
```

**Pattern B sites — primitives (highest leverage):**
- `app/ui/primitives/input.py:113` — `Option(opt_label, selected=(value is not None and opt_value == value))` returns boolean from ternary; needs `selected="selected" if matches else None` or equivalent
- `app/ui/primitives/input.py:121,123` — `attrs["required"] = True`, `attrs["disabled"] = True` in `Select`
- Plus other primitives' similar lines (53, 55, 57, 85, 87, 148, 150) — read the file end-to-end and convert all such writes
- Verify `Radio` and `Checkbox` primitives same way

**Pattern B sites — direct (newly identified, beyond v2 list):**
- `app/docs/routes/_list.py:352` — `attrs["checked"] = True`
- `app/ui/navigation/tabs.py:84` — `attrs["hidden"] = True`
- `app/ui/components/global_search.py:101` — `input_attrs["autofocus"] = True`

**Pattern A sites (~20, from v2 inventory):**
- `app/analyysikeskus/result_shell.py:121,126,146,168`
- `app/analyysikeskus/routes.py:747,759,766,3245,4328,4931`
- `app/auth/routes.py:75`
- `app/chat/routes.py:1290,1314`
- `app/chat/actions.py:406`
- `app/docs/report_routes.py:1127,1224`
- `app/docs/routes/_detail.py:460`
- `app/docs/routes/_upload.py:262,465`
- `app/ui/components/search_routes.py:243`
- `app/ui/data/pagination.py:65`
- `app/ui/design_system_pages.py:264`
- `app/ui/surfaces/modal.py:84`

**Acceptance criteria:**
- [ ] Pattern A grep: `grep -rnE '\b(required|disabled|readonly|checked|selected|novalidate|hidden|open|autofocus|multiple|reversed)= *True\b' app/` returns 0 hits in FT-component context
- [ ] Pattern B grep: `grep -rnE '\b(input_)?attrs?\[["'\''](required|disabled|readonly|checked|selected|novalidate|hidden|open|autofocus|multiple|reversed)["'\'']] *= *True' app/` returns 0 hits
- [ ] Curl an actual rendered page and `grep -E 'disabled="disabled"|required="required"|checked="checked"'` finds matches — confirms the FastHTML HTTP renderer kept them
- [ ] Existing form/component tests pass
- [ ] One new test per primitive asserts the HTML output contains string form (e.g., `assert 'disabled="disabled"' in html`)
- [ ] One integration test renders the login form and asserts `required="required"` in output

**Effort:** 3-4 h (revised up: Pattern B sites + Option/Radio + ternary fix). Single agent, single PR.

## 4. Phase 2 — real fixes (parallel, after Phase 1.5)

File scopes verified non-overlapping after #813 lands.

### 4.1 #816 — Koostaja continue button + state-machine guard (P1, quick win)

**Scope:** Both renderer and state-machine count "answered" not "total."

**Files:**
- `app/drafter/_step_renderers.py:260` — change `all_answered = unanswered_idx is None and len(clarifications) >= 3` → `can_advance = sum(1 for c in clarifications if c.get("answer")) >= 3`. Move continue button out of the `if all_answered` branch.
- `app/drafter/state_machine.py:68-73` — `_can_leave_clarify` currently returns `len(clarifications) >= 3` (counts even unanswered rows). Change to count only answered: `return sum(1 for c in clarifications if c.get("answer")) >= 3`.
- Optional copy update: "Võite jätkata 3 vastuse järel — ülejäänud küsimused on valikulised."

**Acceptance criteria:**
- [ ] After answering 3 of 8 questions, "Jätka uurimisega" button visible and enabled
- [ ] After answering 0 of 8: no button (prevents trivial skip)
- [ ] State-machine guard rejects step transition when fewer than 3 are answered, even if 3+ unanswered question rows exist
- [ ] Test: post 3 answers via the htmx endpoint, GET the step page, assert button is present
- [ ] Test: state-machine guard test with `clarifications=[{q:"x", answer:None}, {q:"y", answer:None}, {q:"z", answer:None}]` returns False

**Effort:** 45 min.

### 4.2 #805 + #815 — graceful unresolved-EU-act behavior (P1)

**Scope: app-side fallback regardless of ontology coverage.** Don't frame as "data problem."

**The bug from the user perspective:**
- #805: user enters `32016R0679` into EL ülevõtt → sees "Ei tuvastanud EL õigusakti" (no signal that it's a known-good CELEX missing from data)
- #815: user uploads doc mentioning GDPR → impact report shows "EL-i õigusaktide seoseid ei tuvastatud" (no warning that EU refs were detected but unresolvable)

**Architectural constraint (post-review):**
- `ImpactAnalyzer` is **explicitly Postgres-free** (`app/docs/impact/analyzer.py:11` — "The analyzer never touches Postgres itself"). The unresolved-EU warning must come from a layer that has draft_entities access.
- `analyze_handler.analyze_impact` (`app/docs/analyze_handler.py:107-120`) already filters `WHERE (entity_uri is not null OR partial_match is not null)`, dropping fully unresolved EU refs before they hit the analyzer. The warning must be assembled in `analyze_handler` *before* this filter, by also querying for fully-unresolved `eu_act` refs and persisting them into `report_data`.

**Fix path:**

1. **Recognize CELEX-shaped strings.** In `app/analyysikeskus/eu_lookup.py` (lines 38-52) and `app/analyysikeskus/input_parser.py` (CELEX regex at lines 207-214), add a `is_canonical_celex_shape(s)` check matching `^[1-9][0-9]{4}[A-Z][0-9]{4}$`. Export the helper for the route renderer.

2. **EL ülevõtt route message (#805).** In `app/analyysikeskus/routes.py:2334` (`_render_eu_unresolved` — the actual EL ülevõtt unresolved-page renderer, NOT `eu_transposition.py` which is the dashboard widget), branch on shape:
   - If `sisend` matches canonical-CELEX shape: render "EL õigusakt CELEX-numbriga {X} ei ole veel ontoloogias kaardistatud — kontrollige käsitsi või proovige akti pealkirja."
   - Otherwise: keep existing generic "Ei tuvastanud EL õigusakti."

3. **Document-pipeline EU warnings (#815).** In `app/docs/analyze_handler.py` before the filtered SELECT at line 111:
   - Add a second query: `SELECT ref_text, ref_type, confidence FROM draft_entities WHERE draft_id = %s AND ref_type = 'eu_act' AND entity_uri IS NULL AND partial_match IS NULL` — fetches the dropped rows.
   - Build an `unresolved_eu_refs` list and persist it into the `report_data` JSONB column (new key, e.g., `report_data["unresolved_eu_refs"]`).
   - Renderer (`app/docs/report_routes.py`) reads the new key and surfaces a warning block: "Tuvastasime dokumendis viiteid EL õigusele ({N} CELEX-numbrit), mida ei õnnestunud ontoloogias kaardistada: {list}. Kontrollige käsitsi."
   - Same key surfaced in `app/docs/docx_export.py` for the exported .docx report.

4. **Update the in-product example.** EL ülevõtt's "Näide: 32016R0679" hint should either (a) point to a CELEX that IS in data, or (b) be replaced with a more reliable example. Decide based on whether ontology PR lands same week.

5. **Ontology PR (separate track):** Add `EU_32016R0679` (GDPR), `EU_32019L1152` (Working Conditions), and other Social-Ministry-relevant canonicals to `eurlex_regulations_peep.json`. Open as a separate PR against `estonian-legal-ontology` repo. Not blocking on this for Seadusloome ship.

**Acceptance criteria:**
- [ ] EL ülevõtt with `32016R0679` shows the canonical-CELEX-not-mapped warning (route renderer test against `routes.py:2334`)
- [ ] EL ülevõtt with `12abc34` shows the generic "ei tuvastatud" message (shape discrimination)
- [ ] Document impact report on a draft mentioning GDPR shows an "Unresolved EU references" section with the CELEX listed (handler test against new `report_data["unresolved_eu_refs"]` key)
- [ ] Exported .docx report includes the same unresolved-EU section
- [ ] If/when ontology data is updated, the same input resolves cleanly (no app change needed; warning disappears because the resolver populates `entity_uri`)
- [ ] Tests: route renderer test for canonical-CELEX path; analyze_handler test asserting `unresolved_eu_refs` is populated; renderer/docx tests asserting the warning section

**Effort:** 3-4 h app-side (revised up — needs analyze_handler + report renderer + docx changes, not just analyzer). Ontology PR is +2-3 h separate.

**Files touched:**
- `app/analyysikeskus/eu_lookup.py` (new shape helper)
- `app/analyysikeskus/input_parser.py` (CELEX shape check)
- `app/analyysikeskus/routes.py:2334` (`_render_eu_unresolved` branch on shape)
- `app/docs/analyze_handler.py` (second query + `unresolved_eu_refs` persistence)
- `app/docs/report_routes.py` (renderer block for the new key)
- `app/docs/docx_export.py` (.docx export block)
- **Not touched:** `app/docs/impact/analyzer.py`, `app/analyysikeskus/eu_transposition.py`

**Parallelism warning:** `app/analyysikeskus/routes.py` is also touched by #814 (new route + `_ANALYYSIKESKUS_INPUTS` row). See §5 for sequencing.

### 4.3 #814 — guided intent → impact (P1, biggest scope)

**MVP design (per user direction):**

Plain-language policy intent is **not** yet an analyzable ontology entity. The MVP bridges from intent to `entity_uri`, then runs the proven per-URI analyzer.

**Flow:**
1. User opens `/analyysikeskus/moju-poliitikamottest` (new card)
2. Koostaja-style intake form: goal (free text), target group (optional), affected area (chips), known law/§ refs (optional structured input)
3. On submit: LLM-driven extraction proposes candidate references from the intent text (reuses `app/docs/entity_extractor.py` if compatible, or builds a thin extractor wrapping the same LLM)
4. `reference_resolver.resolve()` over candidates → URI options
5. User confirms / removes / adds references manually (HTMX-driven list with add/remove rows)
6. For each confirmed `entity_uri`, call `run_adhoc_impact_analysis(entity_uri)` (proven path)
7. Combined result page: findings grouped by confirmed target, with traceability "see mõjuahel tuleneb sätte AvTS § 35 analüüsist"

**Why not composite ephemeral graph:** the user explicitly rejected this for MVP — it blurs accountability, and a second analysis path before confidence in extraction/disambiguation is risky.

**Files:**
- `app/ui/capabilities.py` — add `Capability(slug="moju-poliitikamottest", ..., status="live")`
- `app/analyysikeskus/intent_extractor.py` (new) — **dedicated semantic-intent extractor**, NOT reusing `app/docs/entity_extractor.py` (which is explicitly literal-only per its module docstring: "Never invent references — extract only what is literally in the text"). The new extractor uses a different prompt that invites semantic inference (e.g., "describe the legal acts most likely affected by this policy intent — propose candidates the user should confirm"). All LLM calls go through `cost_tracker.log_usage` with `feature="intent_analysis"` (the existing `entity_extractor.extract_entities` does not currently pass a feature tag — don't inherit that bug).
- `app/analyysikeskus/intent_analysis.py` (new) — intake form renderer + extractor orchestration + per-URI aggregation
- `app/analyysikeskus/routes.py` — wire the new route, add to `_ANALYYSIKESKUS_INPUTS`
- `app/templates/dashboard.py` — capability registry already drives the tile; verify it appears
- `tests/test_analyysikeskus_intent_extractor.py` (new) — extractor prompt + cost-tracking-tag tests with stubbed LLM
- `tests/test_analyysikeskus_intent.py` (new) — full flow with stubbed extractor → stubbed resolver → real (in-memory) analyzer

**Acceptance criteria:**
- [ ] Capability card visible on Töölaud "Mida soovid teha?" and /analyysikeskus directory
- [ ] Submitting "Soovin lihtsustada puudega inimese toetuse taotlemist nii, et osa andmeid liiguks automaatselt Tervisekassast ja Töötukassast" produces ≥1 candidate ref (likely "Puuetega inimeste sotsiaaltoetuste seadus")
- [ ] User can confirm, remove, add references before running analysis
- [ ] Final result page shows findings grouped by each confirmed target with the source attribution
- [ ] If LLM returns 0 candidates, page shows empty state with manual-add affordance
- [ ] Cost-tracked: LLM call logged with `feature="intent_analysis"`
- [ ] Test: full flow with stubbed extractor → stubbed resolver → real (in-memory) analyzer

**Effort:** 5-6 h (revised up from 4h — the confirmation step is non-trivial).

### 4.4 #817 — `draft_reviews` table + reviewer outcome UI (P1)

**Data model: separate table** (per user direction — supports history, multiple reviewers, comments, audit).

**Migration `035_draft_reviews.sql`:**
```sql
CREATE TABLE draft_reviews (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    draft_id UUID NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    -- reviewer_id is NULLABLE: ON DELETE SET NULL preserves the review record
    -- when the reviewer's user account is later deleted. NOT NULL would
    -- contradict SET NULL (rejected write at user deletion time). Same
    -- pattern as annotations.user_id and annotation_replies.user_id —
    -- see migrations/012_fix_cascade_and_constraints.sql:31-34 for the
    -- documented rule.
    reviewer_id UUID REFERENCES users(id) ON DELETE SET NULL,
    -- Snapshot of the reviewer's display name at review time, so the UI
    -- can show "Anne Tamm (kustutatud kasutaja)" instead of just "—"
    -- when reviewer_id has been nulled out by a user deletion.
    reviewer_name_snapshot TEXT,
    outcome TEXT NOT NULL CHECK (outcome IN ('no_issue', 'issue_found', 'needs_discussion')),
    comment TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_draft_reviews_draft_id ON draft_reviews(draft_id, created_at DESC);
CREATE INDEX idx_draft_reviews_reviewer ON draft_reviews(reviewer_id) WHERE reviewer_id IS NOT NULL;
```

**Files:**
- `migrations/035_draft_reviews.sql` (new)
- `app/docs/review_model.py` (new) — DraftReview dataclass, list_reviews_for_draft, create_review
- `app/docs/routes/_review.py` (new) — `POST /drafts/{id}/review-outcome` handler
- `app/docs/routes/_detail.py` — render review section: reviewer-only outcome buttons + current reviews list
- `app/auth/policy.py` — `can_review_draft(auth, draft)` helper, uses existing `ROLE_REVIEWER` from policy.py
- `app/templates/dashboard.py` — reviewer dashboard: surface drafts awaiting review, outcome chip per draft
- `app/docs/audit.py` — emit review-outcome audit event

**Acceptance criteria:**
- [ ] Reviewer (role `reviewer`, not drafter) sees three outcome buttons on draft detail view
- [ ] Drafter (own draft) does NOT see outcome buttons
- [ ] Outside-org user sees nothing (existing `can_view_draft` gate)
- [ ] POST creates a `draft_reviews` row; current state updates without page reload (HTMX swap)
- [ ] Reviewer can post multiple reviews (e.g., update from "needs discussion" to "no issue"); both rows persist
- [ ] Comment field optional but supported; rendered in chronological list
- [ ] Audit log records reviewer_id, outcome, draft_id, timestamp
- [ ] Reviewer Töölaud surfaces "Ülevaatuse järgi ootavad" (drafts in org that have no review from this reviewer)
- [ ] After review posted, the draft moves OUT of the awaiting-review queue for this reviewer
- [ ] Tests: migration applies cleanly; route requires reviewer role; outcome enum validated; comment optional

**Effort:** 5-6 h (migration + model + route + UI + dashboard surfacing + tests).

## 5. Execution sequence

```
Phase 1   (today, ~60min)   → Deploy main + per-ticket prod verify + close 11 stale-open
Phase 1.5 (~3-4h)           → #813 primitives + Pattern A + Pattern B sites + tests
Phase 2a  (parallel, ~6h)   → #816 (45min) || #817 (5-6h) || #805/#815 (3-4h) || #814-prep (4-5h, no routes.py)
                              Four agents in parallel — no file conflicts
                              #814-prep builds intent_extractor.py, intent_analysis.py, capabilities entry, tests
                              Wall time ≈ longest = 6h
Phase 2b  (~30-60min, after #805/#815 lands) → #814 routes.py wiring only
                              Add new entry to _ANALYYSIKESKUS_INPUTS + register the new route
Phase 3   (separate)        → Ontology PR for GDPR + canonical CELEXes (in estonian-legal-ontology repo)
```

**Updated parallel-safety table (post-review):**
| Workstream | Files | Conflicts? |
|---|---|---|
| #816 | `app/drafter/_step_renderers.py`, `app/drafter/state_machine.py` | None |
| #817 | `migrations/035_*.sql`, `app/docs/review_model.py` (new), `app/docs/routes/_review.py` (new), `app/docs/routes/_detail.py`, `app/auth/policy.py`, `app/templates/dashboard.py` | None |
| #805/#815 | `app/analyysikeskus/eu_lookup.py`, `app/analyysikeskus/input_parser.py`, `app/analyysikeskus/routes.py:2334` (`_render_eu_unresolved`), `app/docs/analyze_handler.py`, `app/docs/report_routes.py`, `app/docs/docx_export.py` | Touches `routes.py` |
| #814 | `app/ui/capabilities.py`, `app/analyysikeskus/intent_extractor.py` (new), `app/analyysikeskus/intent_analysis.py` (new), `app/analyysikeskus/routes.py` (new `_ANALYYSIKESKUS_INPUTS` entry + new route handler) | **Touches `routes.py` — sequence after #805/#815** |

**Conflict-resolution choice for routes.py contention:**

Option A (chosen + refined per user 2026-05-19): **#814 can build everything EXCEPT the routes.py wiring in parallel with #805/#815.** The agent creates the new files (`intent_extractor.py`, `intent_analysis.py`), the `Capability` entry in `app/ui/capabilities.py`, and the dashboard wiring — but stops short of touching `app/analyysikeskus/routes.py`. After #805/#815 lands, the final wiring (a small diff: one `_ANALYYSIKESKUS_INPUTS` entry + one new route handler import/wiring line) happens as a follow-up commit. This recovers most of the parallel wall time without the merge-conflict risk.

Option B (alternative): Bundle #805/#815 + #814 into one analyysikeskus agent. Saves wall time but one agent owns ~10h of work in one PR — harder to review.

Option C (rejected): Run both in separate worktrees with full routes.py edits. Risk: when both modify the same `routes.py` near `_render_eu_unresolved` (#805/#815) and the new `_ANALYYSIKESKUS_INPUTS` row (#814), Git will produce conflict markers at merge.

## 6. Epic #784 — status snapshot (not in this plan)

Per memory `project_ontology_six_use_cases_plan.md` (2 days old — verify):
- Branches pending merge: C0 (#785), B3 (#794), A1 (#795), A6 (#800)
- Not yet started: C1, C2, C3, C4, C5, C6, B1, B2, A2, A3, A4, A5 (12 issues)

Recommended after Phase 2: separate epic-resumption session that verifies branch state and dispatches next wave with `isolation: "worktree"`.

## 7. Out of scope

- 16 older issues (#349-#362, #622, #680) — separate triage pass after Phase 2
- Epic #784 work — separate session
- Re-running all 18 stories from the usability test plan — Phase 1 uses targeted regression only

## 8. Pending decisions

**v3 raises one new decision (routes.py contention between #805/#815 and #814):**
- Option A — Sequence #814 after #805/#815 (chosen as default; safest, +6h wall time)
- Option B — Bundle into one analyysikeskus agent (faster, harder to review)
- Option C — Two worktrees (rejected; merge-conflict risk)

All other decisions resolved 2026-05-19. Plan ready to execute pending confirmation of the sequencing choice.
