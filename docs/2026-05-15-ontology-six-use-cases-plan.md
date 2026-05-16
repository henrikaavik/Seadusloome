# Surfacing the Six Ontology Use Cases — Plan

**Date:** 2026-05-15 (revised same day after ontology audit + user direction)
**Status:** Draft for discussion (not yet committed). Updates folded in: ontology audit results (section 2.5), the four design decisions (Q2 hybrid similarity, Q3 inline+collapsible+summary report, Q4 mobile-pass on B-suund), and three new tasks (C0 SPARQL bug fix, two ontology issue drafts).
**Related:** [Estonian Legal Ontology overview, section 2](https://htmlpreview.github.io/?https://github.com/henrikaavik/estonian-legal-ontology/blob/main/docs/eesti-oigusontoloogia-ulevaade.html)
**Predecessors:** `docs/2026-05-11-ministry-lawyer-ui-structure.md`, `docs/2026-05-12-oiguskaart-evidence-map.md`

---

## 1. Context

The Estonian Legal Ontology (the source repo at `github.com/henrikaavik/estonian-legal-ontology`) advertises **six primary use cases** in section 2 of its overview document. They are:

1. **Semantic Legal Information Search** — load the ontology into RDF and run structured SPARQL queries over provisions, acts, court decisions, EU directives, and concepts.
2. **Cross-Reference Tracking** — identify which provisions reference which others, and follow norm-dependency chains.
3. **Draft Legislation Impact Mapping** — connect proposals from the EIS (Eelnõude Infosüsteem) to existing acts and reveal which provisions are affected.
4. **Judicial Practice Integration** — link Supreme Court decisions to the provisions they interpret, so case law for a given norm is discoverable.
5. **EU Law Traceability** — incorporate EUR-Lex materials with CELEX/ELI identifiers and transposition relationships.
6. **Analytics and Quality Control** — surface deontic classification, sanctions indexing, competency mapping, temporal validity, and similarity relationships for audits and compliance.

Seadusloome is the consumer-facing application built on top of this ontology. Today the application implements most of the underlying capability (the SPARQL queries, the impact analyzer, the chat tools, the explorer graph), but the surfacing is uneven: some use cases have dedicated, well-labelled workflows; others are reachable only as raw SPARQL through the chat or as filter presets inside the Õiguskaart. A first-time lawyer landing in the app cannot, in many cases, tell that the system can answer a given question without trial and error.

The goal of this plan is to bring each of the six use cases to **discoverable parity** — every advertised capability should be reachable through at least one verb-named, single-purpose workflow, and every surface in the app should be able to hand the user off to the right workflow without forcing them back to the navigation menu.

---

## 2. Current State Audit

Six parallel exploration agents mapped the codebase. Findings are summarised in the coverage matrix below; "✅" means a dedicated, discoverable surface exists; "⚠️" means the capability is reachable but only through raw-SPARQL, filter presets, or implicit knowledge; "❌" means there is no surface at all.

| # | Use case | Õiguskaart | Nõustaja (chat) | Koostaja (drafter) | Analüüsikeskus | Eelnõud (impact) | Töölaud |
|---|---|---|---|---|---|---|---|
| 1 | Semantic search | ✅ search + presets | ✅ LLM SPARQL tool | ⚠️ keyword only | ✅ input parser + RAG | — | ❌ |
| 2 | Cross-references | ✅ evidence card | ⚠️ only `estleg:relatedTo` | ❌ relation types hidden | ✅ Normi mõjuahel | ✅ in graph builder | — |
| 3 | Draft impact | ✅ `?draft=` subgraph | ✅ `get_draft_impact` | — | ✅ Normi mõjuahel | ✅ full report | ✅ high-risk findings |
| 4 | Court practice | ⚠️ `?vaade=kohtupraktika` filter | ⚠️ only raw SPARQL | ⚠️ S3 query exists, count only | ✅ Riigikohtu section in mõjuahel | ✅ used in conflict detection | — |
| 5 | EU law | ⚠️ `?vaade=el-seosed` filter | ⚠️ only raw SPARQL | ⚠️ S3 query exists, count only | ✅ EL ülevõtt workflow | ✅ EU-compliance section | — |
| 6 | Analytics (5 sub-areas) | ❌ | ⚠️ raw SPARQL only | ❌ | ❌ all six Section-7 workflows deferred | ❌ | ❌ |

### Key findings

**Finding A — The query layer is uneven.**
`app/docs/impact/queries.py` exercises a rich set of predicates (`estleg:references`, `amendsProvision`, `definesConcept`, `hasTopic`, `interpretsProvision`, `transposesDirective`, `implementsEU`). `app/chat/tools.py:336`, by contrast, only touches `estleg:relatedTo`. So the chat — the most flexible surface — depends on the LLM to write correct SPARQL on the fly for everything else, which is unreliable.

**Finding B — Use case 6 is the largest gap.**
Deontic classification, sanctions indexing, competency mapping, temporal validity, and similarity relationships have **zero** dedicated surfaces. The six deferred Section-7 workflows (Pädevused, Sanktsioonid, Halduskoormus, KOV võrdlus, Avaliku teenuse tervikvaade, Kriisikaart) are exactly these. The ontology data is there; the app side is empty.

**Finding C — Discoverability is missing.**
The Töölaud is an operational work queue (correctly so), but it has no hint of the ontology's capability surface. Navigation is purely "verb"-labelled (Õiguskaart, Nõustaja, Koostaja, Analüüsikeskus, Eelnõud) — a first-time lawyer cannot tell whether they can ask about deontic burden or sanctions. The only place that enumerates capability is the `/chat` InfoBox. There is no global search.

**Finding D — Bridges between surfaces are asymmetric.**
Outbound from Õiguskaart and from Eelnõud is good: the evidence card has four `Tegevused` buttons, the impact report links to Õiguskaart `?draft=`, the Analüüsikeskus has `Küsi nõustajalt` seed buttons. **The chat, however, is a terminus.** Citations link to `/explorer?focus=…` but nothing routes the user onward to Normi mõjuahel, EL ülevõtt, or any analytical workflow. Court practice and EU law are *filter presets* in the Õiguskaart rather than verbs in the Analüüsikeskus — the user has to already be on the map to find them.

**Finding E — The 6th use case has five sub-areas, not three.**
Section 2 lists "deontic classification, sanctions indexing, competency mapping, temporal validity, and similarity relationships" — five sub-capabilities. The first plan iteration only covered three (sanctions, deontic, competency). Temporal validity and similarity were missing.

---

## 2.5 Ontology Audit Results (2026-05-15)

A general-purpose agent audited the ontology source repo (`/Users/henrikaavik/progemoge/law-ontology`, ~1523-line `shacl/estonian_legal_shapes.ttl`, full corpus sample). Cross-checked against current `krr_outputs/` on 2026-05-15. The findings reframe Direction A substantially: **most v1 workflows are buildable on existing predicates** (A1, A5, A6 are fully ready; A2, A3 need one small ontology PR each for the v2 advanced features but ship a v1 today; A4's schema is fully defined, `AmendmentEvent` data is populated corpus-wide, `ProvisionVersion` sample sidecars exist for two acts with full corpus pending ontology issue #208). The original Finding A above (about uneven query layer) is actually a **real bug**, not just an inconsistency.

### A. Direction A predicate status

| # | Workflow | Status | What's there / what's needed |
|---|---|---|---|
| A1 | Sanctions | **✅ Complete** | `estleg:Sanction`, `hasSanction`, `sanctionType`, `{min,max}PenaltyAmount`, `{min,max}PenaltyUnit`, `{min,max}PenaltyCurrency`, `enforcedAtLevel`. Pipeline runs corpus-wide. No ontology change. |
| A2 | Burden | **⚠️ Partial** | `NormativeType` + 4 individuals (Obligation/Right/Permission/Prohibition) + `normativeType` predicate are present (populated corpus-wide). `dutyHolder` literal is populated (thousands of free-text values). **Missing**: `estleg:targetGroup` (multi-valued enum citizen/business/public_body/official/ngo — see section 5.5 issue draft). |
| A3 | Competency | **⚠️ Partial** | `estleg:Competence` class with `institution`, `competenceType`, `appliesToProvision`, `appliesToProvisionCount` — populated across institution files (~113 institutions, several hundred Competence nodes total). **Missing**: SHACL shape for `Competence`, `grantedBy` (Act), `competenceArea` (thematic enum). |
| A4 | Temporal | **⚠️ Schema + sample data present, full corpus pending** | `ProvisionVersion`, `AmendmentEvent`, `versionValidFrom`, `versionValidTo`, `supersededByVersion` all defined in SHACL. `AmendmentEvent` data populated corpus-wide. `ProvisionVersion` **sample sidecars exist** (`krr_outputs/provision_versions/kaibemaksuseadus.jsonld` and `tulumaksuseadus.jsonld`, ~100+ ProvisionVersion nodes each); full-corpus ingestion + loader integration tracked by ontology issue #208. A4 v1 ships on act-level data; A4 v2 expands per-§ once #208 is complete. |
| A5 | Similarity | **✅ Complete** | `estleg:semanticallySimilarTo` with inline `estleg:similarityScore` — populated corpus-wide (keyword_jaccard v2, tens of thousands of pairs). `estleg:requestedCluster` populated corpus-wide; `estleg:topicCluster` is SHACL-defined alias but unused in current data — keep both in queries for compatibility, treat `requestedCluster` as the canonical populated predicate. No ontology change. |
| A6 | EU deadlines | **✅ Complete** | `estleg:transpositionDeadline` populated on 2,600+ directives. `transposesDirective`, `transposedBy`, `transpositionStatus` all present. No ontology change. |

*Counts are approximate. Exact corpus snapshots drift between ontology releases; if precise numbers are needed for a milestone gate, generate them via a named audit script and commit the report.*

### B. C-suund predicate names — Seadusloome bug found

The audit cross-checked `app/docs/impact/queries.py` against the actual ontology. Several predicate names used in Seadusloome **do not exist** in the source ontology — current impact queries include UNION branches that silently return zero rows.

| Seadusloome uses | Ontology actually has | Where Seadusloome uses it |
|---|---|---|
| `estleg:interpretsProvision` | `estleg:interpretsLaw` + `estleg:interpretedBy` (inverse) | `impact/queries.py`, dead UNION |
| `estleg:amendsProvision` | `estleg:amends` (on AmendmentEvent) + `estleg:amendedBy` (on Provision) | `impact/queries.py`, dead UNION |
| `estleg:hasTopic` | `estleg:topicCluster` (alias) + `estleg:requestedCluster` | `impact/queries.py`, dead UNION |
| `estleg:implementsEU` | (redundant — same as `transposesDirective`) | `impact/queries.py`, dead alias |
| `estleg:relatedTo` | **Does not exist** | `chat/tools.py:336` — main "related provisions" tool returns nothing |

This is a P0 bug. Today's impact reports under-report conflicts (no `interpretedBy` matches), under-report amendments (no `amendedBy` history), and the chat "related provisions" tool returns empty because `estleg:relatedTo` isn't a real predicate. The fix is a Seadusloome-side rename — no ontology change needed.

### C. Required ontology changes

Two PRs to file as issues against `henrikaavik/estonian-legal-ontology`:

1. **`estleg:targetGroup` predicate** on `LegalProvisionShape` — **multi-valued** enum `citizen | business | public_body | official | ngo` (no `mixed` value; multi-value semantically covers it). Needed for A2 Halduskoormus. Includes a backfill pipeline mapping the existing `dutyHolder` literals onto the enum (rule-based + LLM fallback). Effort: low schema + medium data. Detailed body in section 5.5.
2. **`CompetenceShape` SHACL + `estleg:grantedBy` + `estleg:competenceArea`** — adds a SHACL shape for the existing-but-unconstrained `Competence` class, plus two new predicates. `grantedBy` auto-derivable from majority `sourceAct` of `appliesToProvision`. `competenceArea` needs a ~20-area thematic dictionary. Needed for A3 Pädevused. Effort: low schema + medium data.

Drafts of both issue bodies are in section 5.5 below. To be filed once this plan is approved.

### D. Predicate aliases NOT to add

The audit recommends **against** adding `owl:equivalentProperty` aliases for the Seadusloome-side wrong names. Aliases would let old code keep working but hide the underlying mismatch. Better to fix Seadusloome in C0 (one PR) and standardise on the canonical ontology vocabulary going forward.

### E. What this means for the plan

- **No new ontology dependency for A1, A4, A5, A6** (audit confirms they're ready to build on existing data)
- **A2 and A3 depend on ontology issues being filed and the schema PRs being merged + data backfilled**
- **A new task C0** is added at the front of Direction C: Seadusloome SPARQL predicate rename. Must ship first because every Direction C and A workflow uses these queries
- **A5 hybrid is cheaper than expected**: both ontology similarity edges (98k) and Voyage embeddings are already populated — full hybrid in v1 is mostly query/UI work
- **A4 can ship a v1 with act-level data** (`AmendmentEvent`, `entryIntoForce`, `lastAmendmentDate`) while the provision-level data is backfilled in parallel via ontology issue #208. V2 evolves when issue #208 completes.

---

## 3. Goal

After this plan ships:

- **Every one of the six use cases has at least one verb-named workflow** that takes a natural input (provision URI, CELEX, free-text reference) and produces a structured result.
- **Every workflow is discoverable** from at least three places: the global search bar (top of every page), the Töölaud capability map, and the Analüüsikeskus directory.
- **The bridges between surfaces are symmetric.** Chat answers route outward to analytical workflows; analytical workflows route inward to chat for follow-up questions; the Õiguskaart is reachable from all of them; the impact report is reachable from the analytical workflows; and so on.
- **The ontology's relationship vocabulary becomes the user-facing language.** When the user sees a finding, they see *how* it is related (`muudab`, `tõlgendab`, `transponeerib`) rather than a flat "found" label.

---

## 4. Three Directions

The plan groups work into three **parallel workstreams**, not a strict execution order. Within each workstream tasks may have sequential dependencies (e.g. B3 before B1/B2), but the workstreams themselves can run concurrently — see section 7 for the day-0 parallel sequencing. The C/B/A labelling reflects *leverage*, not order: C is highest leverage per effort, B is universal-front-door, A is the largest scope.

### Direction C — Bridges and relationships
**Character:** mostly UI wiring + reuse of existing SPARQL helpers, with one P0 bug fix (C0) and one canonical-vocabulary extraction (also C0). Fixes felt-friction that every user encounters today and makes Direction B and A workflows more valuable when they connect to it.

### Direction B — Discoverability
**Character:** transforms the first-time-user experience. The global search bar and the Töölaud capability map are the front door, surfacing the capabilities that exist today plus every workflow that lands from C or A. Fully independent of C0 — can ship in parallel from day 0.

### Direction A — Analytics workflows (Section-7 deferrals)
**Character:** the largest scope. Six workflows that surface use case 6 (analytics — sanctions, deontic, competency, temporal, similarity) plus A6 (proactive EU deadline monitoring). Some (A1 v1, A6) have no ontology blocker and can start day 0; others (A2 v2, A3 v2) wait on filed ontology issues; A4 v1 ships on act-level data while A4 v2 awaits ontology issue #208. Canonical predicates are confirmed in section 2.5 — use those, no further confirmation needed.

---

## 5. Detailed Tasks

### Direction C — Bridges and relationships (7 tasks: C0–C6)

#### C0 — Fix Seadusloome SPARQL predicate names + extract canonical relations module (P0 bug, blocks downstream tasks)
**What.** Two parts in one PR:

**Part 1 — Create `app/ontology/relations.py` as the single source of truth.** Defines:
- `PREDICATES` — canonical URI constants (one per predicate)
- `INVERSES` — mapping from forward → inverse (e.g. `interpretsLaw` ↔ `interpretedBy`, `amends` ↔ `amendedBy`)
- `LEGAL_PHRASES` — predicate → Estonian legal-language label ("muudab", "tõlgendab", "viitab", "võtab üle direktiivi", "defineerib mõistet", "on harmoneeritud aktiga")
- `RELATION_GROUPS` — predicate → semantic group ("amendment" / "interpretation" / "transposition" / "reference" / "similarity" / "concept")
- Helper functions: `legal_phrase(uri)`, `inverse_of(uri)`, `is_amendment_relation(uri)`, etc.

Replaces the inline `_RELATION_LEGAL_PHRASES` dict in `app/explorer/routes.py:158`.

**Part 2 — Rename predicate references** throughout the codebase to match the canonical ontology vocabulary:
- `estleg:interpretsProvision` → `estleg:interpretsLaw` + `estleg:interpretedBy` (inverse)
- `estleg:amendsProvision` → `estleg:amends` (on AmendmentEvent) + `estleg:amendedBy` (on Provision)
- `estleg:hasTopic` → `estleg:topicCluster` + `estleg:requestedCluster`
- `estleg:implementsEU` → drop, use `estleg:transposesDirective`
- `estleg:relatedTo` → drop, use specific predicates (`references`, `semanticallySimilarTo`, `harmonisedWith`)

Touched files: `app/docs/impact/queries.py` (lines 113-126, 199-211); `app/chat/tools.py:336`; `app/drafter/handlers.py:53-112`; `app/explorer/routes.py:158` (replaced by import from `app/ontology/relations.py`).

**Testing.** Two layers:
1. **Unit/integration with seeded fixture graph** — `tests/fixtures/ontology_canonical.ttl` seeds a small graph with each canonical predicate, then assert each query returns the expected rows. Deterministic, isolated, fast.
2. **Corpus smoke test** — a separate `pytest.mark.smoke` test that runs against the live Jena fixture (when present in CI) and asserts the impact queries return non-zero rows for known cases. Skipped in unit-test runs.

Avoid using KarS §211 as the primary regression assertion — corpus data may change. Use the fixture for asserting behaviour; use the smoke test for spot-checking against real data.

**Why.** The audit (section 2.5) found these are dead UNION branches today — impact analysis under-reports conflicts and amendments because the predicate names are wrong. The chat's "related provisions" tool returns empty because `estleg:relatedTo` does not exist in the ontology at all. This is a P0 production bug affecting every impact report and every chat session that asks for related provisions. The `relations.py` extraction is folded in here (not deferred to C5) because every C task and several A tasks need the same constants and helpers — having them appear in one module from the start avoids three separate refactors.

**Dependencies.** None. **Blocks** C1, C2, C3, C4, C5, C6 (every Direction C task downstream) and Direction A workflows that exercise these predicates (A1 before/after deltas, A2, A3, A4, A5).

#### C1 — Nõustaja: outbound action links on chat answers
**What.** Below each chat assistant message that cites entities, render a `Tegevused:` strip with links such as `→ Käivita Normi mõjuahel`, `→ Vaata EL ülevõttu`, `→ Lisa märkus`. The links appear conditionally based on the entity types that the answer cites (provision → mõjuahel; CELEX → EL ülevõtt; etc).

**Why.** Today the chat is a one-way street. The user gets an answer with cited sources, but the only follow-up affordance is `vaata kaardil →`. The asymmetry is felt: every other surface routes onward, the chat doesn't. Adding outbound links converts the chat from a terminus into a launchpad for deeper analysis, which is the natural progression of a lawyer's work ("understand the answer → check the consequences").

**Where.** `app/chat/routes.py:902-960` (the existing collapsible sources block — add a `Tegevused` row alongside the `Allikad` block). The entity-type → action mapping is a small static map (Provision → Normi mõjuahel; CELEX → EL ülevõtt; CourtDecision → chat-seed for related provisions); reading it from `app/ontology/relations.py` (created in C0) is the clean version once C0 has landed.

**Dependencies.** Soft dependency on C0 for shared constants only. **C1 can ship before C0** with a local entity-action map (~10 lines) and be refactored to import from `relations.py` afterwards. The choice is operational, not technical — if C0 is in flight, queue C1 behind it; if there's parallel capacity, start C1 day 0.

#### C2 — Koostaja Step 3: show relationship types on the four research cards
**What.** Today the Step 3 (ontology research) page shows four cards: "Provisions", "EU directives", "Court decisions", "Topic clusters", each with a count and a top-10 list. Replace the flat counts with relation-typed groupings, e.g. "3 sätet, mida see eelnõu otseselt mõjutab; 2 sätet, mis sätestavad sarnaseid mõisteid; 1 säte, mis on EL direktiivi ülevõtt".

**Why.** The drafter currently sees trees but not the forest. They know that "five provisions are related" but not whether those provisions are amended, defined, interpreted, or transposed. The ontology already encodes the relationship; we just need to expose it. This is the Step that most directly determines whether the drafted law cites the right precedents — making the relationship visible improves drafting quality.

**Where.** `app/drafter/handlers.py:53-112` (extend SPARQL queries to project the predicate URI); `app/drafter/_step_renderers.py:432` (`_research_category_card` needs a new signature). Predicate→label mapping reads from `app/ontology/relations.py` (created in C0).

**Dependencies.** C0 (uses canonical predicates and legal-phrase labels from the new relations module).

#### C3 — Analüüsikeskus: new "Kohtupraktika sätte kohta" workflow
**What.** Add a third dedicated workflow at `/analyysikeskus/kohtupraktika`. Input: a provision, act, CELEX, case number, or free-text reference. Output: all court decisions that interpret or apply the input, grouped by court (Riigikohus, Euroopa Kohus, ringkonnakohus where data exists), with citation counts and time trends.

**Why.** Use case 4 (judicial practice integration) is currently a *filter* inside the Õiguskaart (`?vaade=kohtupraktika`). The user must already be on the map to find it. Filters are passive ("show only X-shaped things if you stumble on them"); verbs are active ("start a court-practice analysis"). Lawyers research case law as a directed task — they don't browse for it — so the verb form fits the workflow they actually do.

**Where.** New file `app/analyysikeskus/court_practice.py`. New route in `app/analyysikeskus/routes.py`. Reuse `input_parser.py` regexes for CELEX/EE_CASE/SECTION extraction. Reuse the 5-card result shell (`result_shell.py`). The core query uses `estleg:interpretsLaw` / `estleg:interpretedBy` (canonical from C0's `app/ontology/relations.py`).

**Dependencies.** C0 (canonical predicate names + relation helpers). C4 can share the same query helper once both are landed.

#### C4 — Chat tools: four specialised helpers
**What.** Add four new tool-handlers to `app/chat/tools.py` alongside the existing `query_ontology` and `get_provision_details`:
- `get_court_decisions_for_provision(provision_uri)` — uses `estleg:interpretsLaw` + `estleg:interpretedBy`
- `get_eu_transposition_for_provision(provision_uri)` — uses `estleg:transposesDirective`, `harmonisedWith`, `transpositionStatus`
- `get_provision_amendments(provision_uri)` — uses `estleg:amends` (AmendmentEvent → Provision) + `estleg:amendedBy` (Provision → Act/Draft), returns ordered history
- `get_related_concepts(provision_uri)` — uses `estleg:definesConcept` + `estleg:topicCluster` / `estleg:requestedCluster`

All predicate constants imported from `app/ontology/relations.py` (created in C0).

**Why.** The LLM is currently asked to write arbitrary SPARQL for these common questions. SPARQL hallucination is a real failure mode (wrong predicates, malformed FILTER clauses, queries that time out). Each of these four questions is asked tens of times a day in normal lawyer workflows — the cost of building a deterministic helper is amortised quickly. The system prompt should also be updated to instruct the model to prefer specialised helpers over `query_ontology`.

**Where.** `app/chat/tools.py` for handlers + JSON schemas; `app/chat/system_prompt.py:9-28` to update prefer-specialised guidance. Query bodies can be lifted from `app/docs/impact/queries.py` (corrected in C0).

**Dependencies.** C0 (canonical predicate names + relation helpers).

#### C5 — Impact report rows: show the relation type
**What.** The four sections of the impact report (Affected / Conflicts / EU compliance / Gaps) currently render each row as `entity-label → some metadata`. Add a "Seose liik" (Relation type) column to the left of each row, populated from the predicate URI returned by the SPARQL query, mapped to legal-language phrases ("muudab", "tõlgendab", "viitab", "võtab üle direktiivi", "defineerib mõistet").

**Why.** Same motivation as C2 but at the most consequential surface: the impact report is the artefact the lawyer sends to the minister or attaches to the seletuskiri. A report that says "10 affected provisions" is weaker evidence than "3 muudetakse, 5 tõlgendatakse, 2 on EL direktiivi ülevõtt". This is also the most visited surface for senior lawyers who don't draft themselves but review.

**Where.** `app/docs/impact/queries.py` — project `?relation` in each SELECT; `app/docs/report_routes.py:745-893` — render with `legal_phrase()` imported from `app/ontology/relations.py` (created in C0). No new module needed here — C0 already provides the single source of truth.

**Dependencies.** C0 (canonical predicate names + `legal_phrase()` helper).

---

### Direction B — Discoverability (3 tasks)

**Mobile strategy (per user decision 2026-05-15):** Direction B gets full responsive treatment from day one — these are the universal navigation surfaces and a mobile lawyer needs them most. Direction A workflows ship desktop-first; a focused mobile pass on the workflows that actually get mobile traffic is deferred to a separate epic once analytics shows the demand.

#### B3 — Capability dictionary (foundation for B1 and B2)
**What.** Create `app/ui/capabilities.py` as the authoritative list of every "what can I do" entry in the system. Each entry has: canonical name (Estonian + slug), one-line description, icon, target URL, example input, and which of the six section-2 use cases it serves. Wire this list into:
- The /chat InfoBox (replaces the hand-coded prose list with a generated list)
- The Analüüsikeskus directory page
- The Õiguskaart start panel
- B1 (global search bar dropdown)
- B2 (dashboard capability map)

**Why.** The system currently uses different wording for the same capability across surfaces ("Normi mõjuahel" in Analüüsikeskus vs. "find impact of a provision" in chat suggestions). A single authoritative dictionary means that when a new workflow ships (say, A1 Sanctions), it appears everywhere automatically and is described in the same words. This is the cheapest discoverability win and a prerequisite for both B1 and B2.

**Where.** New file `app/ui/capabilities.py`. Refactor consumers (chat InfoBox, Analüüsikeskus index, Explorer start panel) to read from it.

**Dependencies.** None — does not need C0 or any SPARQL change. Blocks B1 and B2.

#### B1 — Global search bar in TopBar

**Mobile detail (per Q4):** Mobile/≤768px collapses the search bar to a search-icon button (top-right of single-row TopBar); tapping opens a full-screen search page with auto-focused keyboard. Tablet (768-1024px) keeps a 200px bar with shortened placeholder. Desktop keeps the full two-row TopBar with 300-400px bar. Touch-targets ≥44×44px throughout.

**What.** A visible search bar in the top header on every page (above the nav row, making the TopBar two rows). Placeholder text: "Otsi sätet, akti, mõistet... või kirjuta tegevus". Type-as-you-go dropdown with two groups:
1. **Entiteedid** — top 5 matching provisions, acts, court decisions, EU acts, concepts (uses the existing `/api/explorer/search` endpoint)
2. **Tegevused** — top 4-6 matching capability verbs from B3, with the input pre-filled (e.g. `Käivita Normi mõjuahel §211 KarS üle`, `Vaata sanktsioone §211 KarS juures`)

`Cmd+K` / `Ctrl+K` focuses the bar (it does not open a new modal). On mobile/narrow viewport the bar collapses to an icon button that opens a full-screen search page.

**Why.** A command palette like Linear/Notion uses (modal opened by keyboard shortcut) is the wrong pattern for this audience. Ministry lawyers are familiar with Riigi Teataja, Eesti.ee, and Google — all of which use a **visible** search bar in the page header. Hidden affordances defeat the discoverability goal: we want to surface capabilities, and a shortcut you have to know about is the opposite of that. The visible bar is the same UX power as Cmd+K but discoverable for everyone, including touch and tablet users.

**Where.** `app/ui/layout/top_bar.py` — restructure to two rows; `app/ui/layout/page_shell.py` — confirm the restructure doesn't break consumers; new component `app/ui/components/global_search.py`; new JS `app/ui/static/js/global_search.js`. New backend endpoint `/api/global-search` combining the existing entity search with capability matching from B3.

**Dependencies.** Blocked by B3 (capability dictionary).

#### B2 — Töölaud "Mida soovid teha?" capability map
**What.** Add a new section at the top of `/dashboard` titled "Mida soovid teha?" with 6-9 capability cards (one per workflow), each showing icon + name + one-line description + example. Below this section, the existing work-queue panels remain unchanged. The section is collapsible with localStorage persistence — a daily user can hide it; a new user sees it first.

**Why.** The Töölaud is correctly an operational work queue, but it currently offers a new user **zero hint of what the system can do**. A first-time lawyer who has no drafts and no findings sees an empty queue and concludes there's nothing here. The capability map turns the empty-state into an invitation. This is complementary to B1, not a replacement: the search bar serves the user who has something specific in mind; the capability map serves the user who is exploring.

**Where.** `app/templates/dashboard.py` — new widget at the top of the page; new reusable component `app/ui/components/capability_card.py`; reads from B3 capability dictionary.

**Mobile** (Q4 = yes): cards collapse to 1-column list on ≤768px, 2-column on tablet, 3-column on desktop. Default collapsed on mobile (saves screen real estate); default open on desktop. Touch-targets ≥44×44px.

**Dependencies.** Blocked by B3 (capability dictionary).

---

### Direction A — Analytics workflows (6 tasks)

These are the Section-7 deferrals from `docs/2026-05-11-ministry-lawyer-ui-structure.md`. Each one is an independent workflow following the same 5-card result shell as Normi mõjuahel and EL ülevõtt.

**Predicate names.** Use the canonical predicates confirmed by the audit in section 2.5 and imported from `app/ontology/relations.py` (created in C0). The two predicates that are *not yet present* in the ontology (`estleg:targetGroup`, `estleg:CompetenceShape + grantedBy + competenceArea`) are filed as ontology issues in section 5.5 and gate the **v2** of A2 and A3 respectively — v1 of each ships on existing data.

#### A1 — Sanctions index workflow
**What.** New `/analyysikeskus/sanktsioonid`. Input: act / draft / keyword. Output: every sanction (rahatrahv, vangistus, väärteokaristused, muud meetmed) in that scope, grouped by provision, with the penalty range and the deontic context. Compares against sanctions in similar acts.

**Why.** Sanctions are the most user-asked-about subset of any criminal/regulatory provision — "what is the penalty for X?" is a daily lawyer question. Today the answer requires reading the act top-to-bottom. The ontology has structured sanction data; surfacing it as a verb-named workflow eliminates a routine manual task and provides comparability across acts that no manual reading could achieve at scale.

**Where.** New `app/analyysikeskus/sanctions.py`; reuse 5-card shell. Predicates confirmed by audit: `estleg:Sanction`, `hasSanction`, `sanctionType`, `{min,max}PenaltyAmount`, `{min,max}PenaltyUnit`, `{min,max}PenaltyCurrency`, `enforcedAtLevel`. Pipeline already runs corpus-wide.

**Dependencies.**
- **A1 v1 standalone** ("show all sanctions in §X" — no draft delta): no C0 dependency; queries only `estleg:Sanction` predicates which were never affected by the C0 bug. Can ship day 0 in parallel with C0.
- **A1 + C6 integration** (before/after sanction comparisons inside the impact report) and any cross-reference to draft impact: depend on C0 (impact-query predicate fixes).

#### A2 — Administrative burden / deontic view
**What.** New `/analyysikeskus/halduskoormus`. Input: act / draft. Output: counts and lists of new obligations, prohibitions, permissions, and rights, broken down by target group (citizen, business, public body). Burden delta vs. existing law.

**Why.** Administrative-burden analysis is **mandatory** in the Estonian VTK (väljatöötamiskavatsus) process — every new law has to estimate it. Today it's done manually by reading the draft and counting words like "peab", "on kohustatud", "ei tohi". The ontology already encodes deontic classification; automating the count removes a tedious, error-prone manual step and produces a defensible methodology.

**Where.** New `app/analyysikeskus/burden.py`; same shell pattern. Audit confirmed: `estleg:NormativeType` class + 4 individuals (`NormType_Obligation`, `NormType_Right`, `NormType_Permission`, `NormType_Prohibition`); `estleg:normativeType` predicate populated corpus-wide on tens of thousands of provisions; `estleg:dutyHolder` literal populated on thousands of provisions. **Missing**: `estleg:targetGroup` — filed as ontology issue (see section 5.5).

**Dependencies.** C0 (predicate rename). Ontology `targetGroup` issue must merge before grouping by target group becomes possible — until then, fall back to `dutyHolder` string-bucketing in the Seadusloome side.

#### A3 — Competency mapping
**What.** New `/analyysikeskus/padevused`. Input: institution name or competence area. Output: all powers (volitused) assigned to that institution by current law, grouped by act, plus overlaps with other institutions and any gaps (competence areas with no assigned body).

**Why.** Institutional competence is a recurring source of legislative bugs — two ministries claim the same authority, or no one is responsible for an area. The ontology encodes institution↔competence relations; surfacing them produces something no human-readable index of laws can: a complete competence map for cross-checking.

**Where.** New `app/analyysikeskus/competency.py`. Audit confirmed: `estleg:Institution` (290+ institutions); `estleg:Competence` (reified node with `institution`, `competenceType`, `appliesToProvision`, `appliesToProvisionCount`); `estleg:competentAuthority` (Provision → Institution, both `Institution_*` state bodies and `Issuer_*` KOV bodies). **Missing**: SHACL shape for `Competence`, `grantedBy`, `competenceArea` — filed as ontology issue (see section 5.5).

**Dependencies.** C0 (predicate rename). Ontology `CompetenceShape + grantedBy + competenceArea` issue must merge before "grouping by area" and "which act granted this" features become available — until then, ship A3 v1 with institution-level grouping only.

#### A4 — Temporal validity workflow (v1 act-level, v2 provision-level after ontology issue #208)
**What.**

**V1 (ships now)** — new `/analyysikeskus/ajalugu`. Input: provision / act / court decision. Output uses act-level data populated today:
- Act-level timeline: `entryIntoForce`, `repealDate`, `lastAmendmentDate`, `temporalStatus`
- Every `AmendmentEvent`: date, entry-into-force date, RT citation (`rtReference`), which provisions changed (`amends`)
- Court decisions that interpreted the act / provision — dates + URIs
- Impact reports that touched the entity, ordered by time
- Forward look: pending `DraftVersion`s that would amend the provision
- **Persistent banner at the top of the result page** (not a tooltip — lawyers must not miss this): when the input is a provision URI, show an unmissable info-banner: "⚠️ Showing act-level history only. Provision-level versions (§-by-§ text diffs across redactions) are available for sample acts (käibemaksuseadus, tulumaksuseadus) but the full corpus is still being ingested. Full coverage tracked at [ontology issue #208](https://github.com/henrikaavik/estonian-legal-ontology/issues/208). [Learn more]." When the input is an act, the banner is hidden (act-level data is complete for acts).

**V2 (later, after ontology issue #208 completes)** —
- Per-§ text diffs across `ProvisionVersion` chain
- Deterministic "what was in force at 2023-03-15" answer
- `versionText`, `supersededByVersion`, `versionValidFrom`, `versionValidTo`

**Why.** "What was the law on date X?" is a frequent and hard question — required for litigation, transitional provisions in new drafts, and historical audits. The audit (section 2.5) confirms the temporal model is fully defined in SHACL (`ProvisionVersion`, `AmendmentEvent`, `versionValidFrom`, `versionValidTo`, `supersededByVersion`) and the act-level data is richly populated (561 amendment events for KarS alone, thousands corpus-wide). Provision-level data is the gap and is tracked separately by ontology issue #208. Shipping a v1 on act-level data unblocks the workflow today while the data backfill runs in parallel.

A separate workflow (not an extension of `?vaade=ajalugu`) is preferred because the result is tabular and investigative — versions, dates, diffs, court rulings — not graph-visual.

**Where.** New `app/analyysikeskus/history.py`. Visual: vertical timeline (not D3 graph).

**Dependencies.** C0 (predicate rename) for `amends` / `amendedBy` queries. Ontology issue #208 unblocks V2 but is **not** a blocker for V1.

#### A5 — Similarity workflow + Koostaja integration (full hybrid from v1)
**What.** Two-part, hybrid from day one (per user decision 2026-05-15):

**A5a — Analüüsikeskus workflow** `/analyysikeskus/sarnasus`. Input: provision / act / draft URI **or** free-text. Output: top-N similar entities from three sources, merged and de-duplicated with explanations:
1. **Ontology-declared similarity** — `estleg:semanticallySimilarTo` with inline `estleg:similarityScore` (populated corpus-wide via the keyword_jaccard v2 pipeline; tens of thousands of pairs)
2. **Same topic cluster** — `estleg:requestedCluster` is the populated predicate (tens of thousands of provisions). `estleg:topicCluster` is a SHACL-defined alias but currently unused in `krr_outputs` — query both with UNION for forward compatibility
3. **Embedding cosine** — Voyage embeddings already running for RAG (`app/rag/`); pgvector HNSW index

Each result row shows *why* it matched: badge "ontology-declared" / "same cluster" / "similar wording" / multiple. Score merge weights ontology (1.0×) over embedding (0.8×); duplicates collapse to the highest score with all reasons.

**RAG chunk handling (privacy + entity aggregation):**

Actual schema (verified against `migrations/009_rag_chunks.sql` + `016_rag_tenant_scoping.sql`):
- `rag_chunks(id, source_type, source_uri, chunk_index, content, metadata jsonb, embedding, org_id, source_id)`
- `source_type IN ('ontology', 'draft', 'law_text', 'court_decision')` — no `'public_legal'` value exists
- `org_id IS NULL` ⇒ public corpus (currently the only state populated; private-draft ingestion is not yet implemented per the migration 016 header comment)
- No separate `provision_uri` / `act_uri` columns — `source_uri` is the entity URI; for `source_type = 'ontology'` it is already the provision/act/concept URI

A5 must:

1. **Filter to public corpus.** Retrieval filter `WHERE org_id IS NULL` (the canonical retriever predicate from migration 016). When private-draft ingestion lands later, this filter prevents any cross-tenant leak by design.
2. **Handle private query input safely.** Free-text input from a user's draft is the privacy-sensitive case. Do not persist or index the query text. The embedding call itself is delegated to `VoyageProvider` — a SaaS call — and is therefore subject to the project's approved LLM/vendor data-processing controls (per `app/llm/cost_tracker.py` + `app/config.py::is_stub_allowed()`), not "kept in-process". After the embedding returns, the cosine search runs against the public-corpus filter from point 1.
3. **Aggregate chunks → entities.** Multiple chunks per entity will match. Group hits by `source_uri` (already the entity URI for `source_type='ontology'`), score as `max(chunk_cosine)` for the top row and `avg(top-3 chunk_cosine)` for ranking ties. Each entity in the result list shows the highest-matching chunk's text as the snippet. If we later need fine-grained provision-vs-act distinction, the right move is to extend `metadata jsonb` with structured fields rather than change the column shape.
4. **Merge with SPARQL track.** Both tracks return entity URIs; deduplicate by URI; preserve all matched reasons (one entity may be matched by ontology + cluster + embedding simultaneously).

**A5b — Koostaja Step 3 + 4 integration.** Step 3 gets a new "Similar provisions" card using A5a hybrid. Step 4 prompt injects the actual *text* of the closest provision matches (not just act names), so drafted clauses can mirror established wording.

**Why.** Lawyers reuse wording from existing acts both for consistency and legal robustness — phrasing that has been tested in court is safer. The audit (section 2.5) confirmed both data sources are already populated and indexed: ontology-side has 98k similarity edges + 71k provisions in topic clusters; app-side has Voyage embeddings running in production. Doing hybrid from v1 is mostly query/UI work (~+20% over embedding-only) and produces qualitatively better results: ontology-meaning (why) plus embedding-similarity (how close).

**Where.** New `app/analyysikeskus/similarity.py` (SPARQL helpers + score merge); reuse `app/rag/retriever.py` for embedding lookups; modify `app/drafter/handlers.py:255-266` (`_find_similar_laws` → `_find_similar_provisions`) and `app/drafter/_step_renderers.py`.

**Dependencies.** C0 (predicate rename — `requestedCluster` instead of `hasTopic`).

#### A6 — Töölaud widget: EU transposition deadlines
**What.** Add a new widget on the Töölaud (operational dashboard) titled "EL ülevõtu tähtajad". Shows EU directives where the transposition deadline is within the next 90 days **and** Estonia's `transpositionStatus` is "puudub", "osaline", or "ebaselge". Sorted by deadline ascending. Clicking a row opens the corresponding EL ülevõtt workflow pre-filled with the CELEX. A "show all" link expands to a full view.

**Why.** Use case 5 (EU traceability) is today entirely **reactive** — the user opens the EL ülevõtt workflow when they decide to check transposition status. There's no proactive surface that says "you have a transposition debt coming due". For a ministry lawyer that's a real problem: missed transposition deadlines result in infringement procedures. A dashboard widget that surfaces this risk on login changes the workflow from "remember to check" to "see at a glance".

**Where.** `app/templates/dashboard.py` — new widget; new SPARQL helper (extend `app/analyysikeskus/eu_transposition.py`). Audit confirmed `estleg:transpositionDeadline` populated on the great majority of directives in the corpus.

**Dependencies.** None — audit confirmed `estleg:transpositionDeadline` is populated. Benefits from C0 (predicate rename) since same query layer.

---

## 5.5 Ontology change proposals (drafts for issues)

The audit (section 2.5) identified two ontology gaps that block A2 and A3. Per user direction, these are filed as **GitHub issues against `henrikaavik/estonian-legal-ontology`**, not as PRs we make ourselves. Issue body drafts below. To be filed once this plan is approved.

### Proposed issue 1 — Add `estleg:targetGroup` predicate (blocks A2)

**Title:** Add `estleg:targetGroup` predicate to LegalProvisionShape (closed enum)

**Body (draft):**

> **Context.** The Seadusloome application is building a `Halduskoormus` (administrative burden) workflow that needs to bucket every provision's obligation/prohibition/permission/right by the affected group: citizen / business / public_body / official / NGO. Currently `estleg:dutyHolder` carries this information as a free-text literal (thousands of distinct values like "Tööandja", "Töötaja", "Minister") — not queryable as a closed enum.
>
> **Proposed change.** Add a new **multi-valued** property to `estleg:LegalProvisionShape` in `shacl/estonian_legal_shapes.ttl`. Multi-valued because legal provisions routinely affect more than one target group (tax laws apply to both citizens and businesses; labour law applies to both employer and employee; data-protection law applies to citizens, businesses, and public bodies):
> ```turtle
> sh:property [
>     sh:path estleg:targetGroup ;
>     sh:datatype xsd:string ;
>     sh:in ( "citizen" "business" "public_body" "official" "ngo" ) ;
>     rdfs:comment "The bearer(s) of the deontic norm: which group(s) of actors carry the obligation, hold the right, etc. Multi-valued — a provision may target several groups simultaneously." ;
> ] ;
> ```
> Insert after the existing `dutyHolder` block (around line 225). No `maxCount` (multi-valued); `minCount 0` (some provisions may genuinely have no clear target group, e.g. definitions). Drop `mixed` from the enum since multi-value covers that semantically.
>
> **Optional secondary predicate:** consider also `estleg:primaryTargetGroup` (single, `sh:maxCount 1`) for the most affected group, useful for summary aggregation. This is a separate decision — the multi-valued primary predicate is the must-have.
>
> **Data backfill.** New pipeline `scripts/classify_target_group.py`. Map deterministically by dictionary (Tööandja|ettevõtja → business; Töötaja|isik|kodanik → citizen; Minister|amet|inspektsioon|kohus → public_body or official; etc.), with LLM fallback for unmapped strings. Coverage target: 80% of provisions with `dutyHolder`. Manual review on a 200-row sample before commit.
>
> **Why this matters.** Without `targetGroup`, Seadusloome's `Halduskoormus` workflow can only show "X new obligations created by this draft" — not "X new obligations on business, Y on citizens, Z on public bodies". The Estonian VTK (väljatöötamiskavatsus) process requires burden estimation by target group; this is the structured fact that automates it.
>
> **Why this name.** `targetGroup` matches the Estonian VTK terminology ("sihtgrupp"). `dutyBearer` would be too tied to obligations; rights and permissions also have a target group.
>
> **Out of scope.** Sub-dividing `business` further (SME vs large enterprise): not in v1; can be added by extending the enum without schema-shape change. Inferring intensity ("primary" vs "secondary" target): captured optionally by `primaryTargetGroup` if added.
>
> **Related.** Used by Seadusloome workflow `/analyysikeskus/halduskoormus` (task A2 in `Seadusloome/docs/2026-05-15-ontology-six-use-cases-plan.md`).
>
> **DoD.** SHACL property block added; CI validates the constraint (test data covering all 5 enum values + a violation case + a multi-valued case); `classify_target_group.py` lands; backfill run, coverage report in `krr_outputs/target_group_report.json`; `docs/SCHEMA_REFERENCE.md` updated; sample data files include `targetGroup` (at least one example with multiple values).

### Proposed issue 2 — Add `CompetenceShape` SHACL + `grantedBy` + `competenceArea` (blocks A3)

**Title:** Add CompetenceShape SHACL + estleg:grantedBy + estleg:competenceArea predicates

**Body (draft):**

> **Context.** Seadusloome is building a `Pädevuste kaardistus` workflow that needs to answer: which institutions are competent over a given thematic area? Under which act is each competence granted? Are there competence overlaps or gaps? The ontology already has the reified `estleg:Competence` class (used in `krr_outputs/institutions/`), but it has no SHACL shape, and two key facts are missing: `grantedBy` (which act) and `competenceArea` (thematic category).
>
> **Proposed change 1 — CompetenceShape.** Add to `shacl/estonian_legal_shapes.ttl` after `InstitutionShape`:
> ```turtle
> estleg:CompetenceShape
>     a sh:NodeShape ;
>     sh:targetClass estleg:Competence ;
>     sh:property [ sh:path estleg:institution ; sh:minCount 1 ; sh:maxCount 1 ; sh:nodeKind sh:IRI ] ;
>     sh:property [ sh:path estleg:competenceType ; sh:minCount 1 ; sh:maxCount 1 ;
>                   sh:datatype xsd:string ;
>                   sh:in ( "supervision" "licensing" "enforcement" "regulation" "advisory" "general" ) ] ;
>     sh:property [ sh:path estleg:appliesToProvision ; sh:minCount 1 ; sh:nodeKind sh:IRI ] ;
>     sh:property [ sh:path estleg:appliesToProvisionCount ; sh:datatype xsd:integer ; sh:maxCount 1 ] ;
>     sh:property [ sh:path estleg:grantedBy ; sh:nodeKind sh:IRI ; sh:maxCount 1 ;
>                   rdfs:comment "The Act under which this competence is granted." ] ;
>     sh:property [ sh:path estleg:competenceArea ; sh:datatype xsd:string ; sh:maxCount 1 ;
>                   rdfs:comment "Coarse thematic area for cross-institution grouping." ] .
> ```
>
> **Proposed change 2 — `grantedBy` backfill.** Update `scripts/extract_institutional_competence.py` to emit `grantedBy` for each `Competence`: compute `sourceAct` distribution over `appliesToProvision`, pick the majority. Low effort, purely derived.
>
> **Proposed change 3 — `competenceArea` backfill.** Build a ~20-area thematic dictionary (data_protection, tax, environment, health, transport, education, labour, internal_security, justice, general_government, ...). For each `Competence`, set `competenceArea` from the institution's name + EuroVoc subjects of its `appliesToProvision` acts. Manual review of ambiguous mappings. Medium effort; ~290 institutions to classify. Can be a separate PR after change 1.
>
> **Why this matters.** Without these, the `Pädevuste kaardistus` workflow cannot group competences by area for cross-institutional overlap/gap detection. The reified `Competence` class is also unconstrained today — any data producer could emit malformed `Competence` nodes and CI wouldn't catch it.
>
> **Related.** Used by Seadusloome workflow `/analyysikeskus/padevused` (task A3).
>
> **DoD.** `CompetenceShape` in SHACL; CI validates; `extract_institutional_competence.py` emits `grantedBy`; backfill complete; (optional sub-PR) `competenceArea` dictionary + backfill; `SCHEMA_REFERENCE.md` updated; sample files include both new predicates.

### Filing note

Both issues should be filed with the existing `schema` label (verified 2026-05-15). A `seadusloome-blocker` label would be useful for cross-repo traceability but **does not exist yet** — either create it first in the ontology repo's settings, or omit it and rely on the issue body's "Related" section to point back to this plan. GitHub verified (2026-05-15): no open issues for `targetGroup`, `CompetenceShape`, `grantedBy`, or `competenceArea`; ontology issue #208 (full `ProvisionVersion` ingestion) is the only related open ontology issue.

---

### Cross-direction (1 task)

#### C6 — Impact report: sanctions + burden delta + executive summary printout (per user decision 2026-05-15)
**What.** Once A1 (sanctions) and A2 (burden) helpers exist, extend the impact report (`app/docs/impact/analyzer.py` + `app/docs/report_routes.py`) with two new sections **inline + collapsible from day one**, plus a separate executive-summary printout:

**1. Sanctions delta** (collapsible, default open)
- One-line summary: "3 uut sanktsiooni · 1 muudetud · 0 eemaldatud"
- Each delta row: provision, sanction type, penalty range (`{min,max}PenaltyAmount + Unit + Currency`), before/after

**2. Burden delta** (collapsible, default open)
- One-line summary: "5 uut kohustust · 2 keeldu · 1 õigus — koormus skoor +12% vs current law"
- Each delta row: provision, normative type, target group (`estleg:targetGroup` after ontology issue is merged; falls back to `dutyHolder` literal until then)

**3. Executive summary printout** (new "Prindi kokkuvõte" button at the top of the report)
- Generates a 1-2 page `.docx` using `python-docx`:
  - Draft title, author, date
  - One-page summary: affected provisions count, conflicts count, sanctions delta numbers, burden score
  - Intended to be attached to the seletuskiri front page; the full report remains as appendix

**Why.** Use case 3 (draft impact) and use case 6 (analytics) intersect naturally on every draft: a new criminal-law draft modifies sanctions; a new tax-law draft modifies obligations. The impact report is the lawyer's final artefact attached to the seletuskiri — if it doesn't include these dimensions, the analytical workflows (A1, A2) feel disconnected from "real work". Inline placement (rather than a separate analytics view) keeps the report as the single source of truth. The executive summary printout addresses the length concern proactively: lawyers can attach a 1-2 page summary to the seletuskiri front matter while preserving the full report as appendix.

**Schema compatibility.** Old impact reports stay readable (graceful `sanctions_delta = null` fallback). Print stylesheet ensures collapsible sections expand when printed.

**Where.**
- `app/docs/impact/analyzer.py` — new `analyze_sanctions_delta()`, `analyze_burden_delta()`, `build_executive_summary()`
- `app/docs/impact/queries.py` — extend with A1/A2 query patterns
- `app/docs/report_routes.py` — collapsible sections (localStorage state) + summary printout button
- `app/docs/docx_export.py` — new `export_executive_summary()` template

**Dependencies.** Blocked by A1, A2. Benefits from ontology `targetGroup` issue but functions with `dutyHolder` fallback in the interim.

---

## 6. Coverage of the Six Use Cases After This Plan

| # | Use case | After plan |
|---|---|---|
| 1 | Semantic search | ✅ Existing chat + Õiguskaart, plus the **global search bar (B1)** as a new primary entry point, plus **C4 specialised chat tools** for higher answer quality |
| 2 | Cross-references | ✅ **C2** (Koostaja relation types), **C4** (chat amendments + concepts), **C5** (impact report relation column) |
| 3 | Draft impact | ✅ Existing report, plus **C5** (relation types), plus **C6** (sanctions + burden delta) |
| 4 | Court practice | ✅ **C3** dedicated verb workflow + **C4** chat tool |
| 5 | EU law | ✅ Existing EL ülevõtt + **C4** chat tool + **A6** proactive deadline widget |
| 6 | Analytics (5 sub-areas) | ✅ **A1** sanctions, **A2** deontic, **A3** competency, **A4** temporal, **A5** similarity |

Every use case ends up at "✅".

---

## 7. Sequencing

The B-track and parts of the A-track are fully independent of C0 (they don't touch the buggy predicates) — they can run in parallel from day 0. Only the C-track and the C0-dependent half of the A-track must wait for C0 to merge.

```
Day 0 (start everything in parallel):

  C-track (sequential, C0 first)
  └─ C0 Fix SPARQL + extract relations.py  ← P0 bug
       └─ then in parallel:
             ├─ C1 Nõustaja outbound links
             ├─ C2 Koostaja Step 3 relation types
             ├─ C3 Analüüsikeskus Kohtupraktika workflow
             ├─ C4 Chat specialised tools
             └─ C5 Impact report relation column

  B-track (fully independent of C0 — start day 0)
  └─ B3 Capability dictionary
       └─ then in parallel:
             ├─ B1 Global search bar (TopBar, responsive)
             └─ B2 Töölaud capability map (responsive)

  A-track-early (independent of C0 — start day 0)
  ├─ A1 v1 standalone (sanctions index, no draft delta yet)
  └─ A6 EU transposition deadlines widget

  Ontology issues (file day 0, merge timing not under our control)
  ├─ Ontology issue 1: estleg:targetGroup (multi-valued)  ← blocks A2 v2
  └─ Ontology issue 2: CompetenceShape + grantedBy + competenceArea  ← blocks A3 v2

After C0 merges, A-track expands:

  A2 v1 (dutyHolder fallback)            → A2 v2 (targetGroup) after ontology issue 1
  A3 v1 (institution-level only)         → A3 v2 (area-grouped) after ontology issue 2
  A4 v1 (act-level + banner)             → A4 v2 (§-level) after ontology issue #208
  A5 (full hybrid, RAG-privacy-aware)

After A1 + A2 v1 land:

  C6 Impact report sanctions + burden delta + executive summary printout
```

**Total:** 16 implementation tasks (C0–C6, B1–B3, A1–A6) + 1 meta task = 17 tasks.

**Effort estimate (revised from initial 3-4 sprints):**
- **2-3 engineers in parallel:** 4-6 sprints (2-week sprints). C-track ~1.5 sprints; B-track ~1.5 sprints in parallel; A-track 2-3 sprints; C6 closer ~0.5 sprint.
- **1 engineer sequentially:** 6-8 sprints. B1 (~2-3 weeks: responsive search + dropdown + a11y + tests), A5 (~2-3 weeks: hybrid score merge + chunk privacy + UI badges), and C6 (~2-3 weeks: docx export + collapsible + print stylesheet) are each substantially larger than they appear in the bullet form.

**Critical path:** C0 is the only true serial gate. After C0, almost everything fans out. The single-engineer estimate is dominated by sequential execution of B1, A5, C6 (each ~2-3 weeks).

---

## 8. Risks and Open Questions

### Risks

**R1 — ~~Direction A predicate uncertainty~~** ✅ **Resolved 2026-05-15.**
The audit (section 2.5) confirmed all predicates: A1, A5, A6 are 100% covered today; A4 schema present (data partial, tracked by ontology issue #208); A2 + A3 need two small ontology issues filed (drafts in section 5.5). The bigger surprise was a **production bug** in Seadusloome's predicate names — addressed by new task C0.

**R2 — TopBar restructure may regress existing layouts.**
B1 requires the TopBar to grow from one row to two on desktop (mobile stays single-row with collapsed icon). Any custom page chrome or special-case route that uses TopBar height assumptions could break.

*Mitigation:* grep for hard-coded TopBar height; test in narrow viewport; smoke-test every route after merge. Mobile-first dev (per Q4 decision) catches viewport issues earlier.

**R3 — Direction A workflows duplicate the result-shell pattern.**
Six new workflows × the same 5-card shell pattern → high risk of subtle divergence between them.

*Mitigation:* keep the shell as a single reusable component (`app/analyysikeskus/result_shell.py`); review every Direction A PR for shell-pattern compliance; consider extracting a base class or factory if the third workflow diverges.

**R4 (new) — Ontology issue merge timing affects A2/A3 v2.**
A2 and A3 ship in two phases — v1 with current ontology data, v2 once the two filed issues merge. If the ontology team is slow to merge `targetGroup` or `CompetenceShape`, A2 v1 / A3 v1 are visibly less powerful than intended.

*Mitigation:* file both issues immediately with detailed implementation notes (see section 5.5 drafts); offer to submit the SHACL PRs ourselves with the ontology team's approval; clearly label A2/A3 v1 UI as "extended view coming when target-group classification lands".

**R5 (new) — C0 (predicate rename) is destabilising before it's stabilising.**
The fix to `app/docs/impact/queries.py` will *change* what existing impact reports show. A report run before C0 will look different from a report run after C0 on the same draft. This could confuse users who saved old reports.

*Mitigation:* before merging C0, run the new queries against a sample of recent `impact_reports` and diff the results; communicate the change to active users; if differences are large, add a "report re-run available" banner on old reports.

**R6 (new) — Direction A workflows + B3 capability dictionary need merge-time coordination.**
B3 (`app/ui/capabilities.py`) declares every Direction A workflow with `status="planned"` and omits its `_ANALYYSIKESKUS_INPUTS` metadata. When each A workflow PR merges (A1 first, then A2/A3/A4/A5/A6), the live route exists but `/analyysikeskus` will still render a "Tulekul" placeholder card unless the capability is flipped to `"live"` and the input metadata is added in the same merge.

*Mitigation:* the A-workflow PR that merges second (after B3) carries the small "make it discoverable" bridge — flip the corresponding capability `status` to `"live"` in `app/ui/capabilities.py` and append the input metadata in `app/analyysikeskus/routes.py::_ANALYYSIKESKUS_INPUTS`. Documented in inline comments on issues #794 (B3) and #795 (A1) — see also F4 from the 2026-05-15 review. Same pattern applies to every subsequent A workflow.

### Open questions

**Q1 — ~~Direction A predicate confirmation: meta-task or per-workflow?~~** ✅ **Done 2026-05-15.** Audit completed; results in section 2.5.

**Q2 — ~~A5 similarity: SPARQL-only, embedding-only, or hybrid?~~** ✅ **Answered 2026-05-15 — full hybrid from v1.** Both data sources already populated (98k ontology pairs + Voyage embeddings) so hybrid is only +20% over embedding-only.

**Q3 — ~~C6 placement: inline in impact report, or separate analytics view?~~** ✅ **Answered 2026-05-15 — inline + collapsible + executive summary printout from day one.** Keeps the report as single source of truth; summary printout addresses length concern proactively.

**Q4 — ~~Mobile/touch strategy?~~** ✅ **Answered 2026-05-15 — full responsive on Direction B, desktop-first on Direction A, mobile-pass on A workflows deferred to follow-up epic.** Direction B is the universal nav surface where mobile lawyers need it most; A workflows are research-mode work that's mostly desktop.

**Q5 (new, open) — Who files the ontology issues?**
Plan has the issue bodies drafted in section 5.5. The user has stated these should be filed via GitHub issues against the ontology repo, not as PRs. Open: when, and with what label set (the initial filing attempt failed because `seadusloome-blocker` label didn't exist — needs to be created in the ontology repo first, or use `schema` only).

**Q6 (new, open) — When to convert to a Seadusloome Epic?**
The meta task #11 covers conversion of these 17 tasks to GitHub Issues + Epic in the Seadusloome repo. Open: do this immediately after plan approval, or after C0 ships (so the Epic has visible progress from day one)?

---

## 9. Status

This plan is a **draft for discussion**, revised 2026-05-15 after the ontology audit and user review. It is not yet a commitment.

**Current state:**
- 16 implementation tasks (C0–C6, B1–B3, A1–A6) + 1 meta task identified.
- 2 ontology change proposals drafted in section 5.5; **not yet filed** as GitHub issues.
- 4 design decisions captured and finalised (Q1–Q4 in section 8).
- All 6 ontology use cases reach `✅` coverage by end of plan.

**To move from "draft" to "committed":**
1. User reviews this document and approves the shape of the plan, the audit findings, the design decisions, and the proposed ontology issue bodies.
2. File the two ontology issues against `henrikaavik/estonian-legal-ontology` (create or pick an appropriate label set first — initial attempt with `seadusloome-blocker` failed because the label didn't exist).
3. Convert this plan into a GitHub Epic + 16 sub-issues in the Seadusloome repo, organised under the coordinating Epic linked from this document.
4. Start C0 (P0 bug fix) as the first PR. B-track and A-track-early (A1 standalone, A6) can start in parallel.

Until then, the plan can be edited freely in this document.

**Change log:**
- 2026-05-15 (morning): initial draft, 10 tasks proposed across C/B/A.
- 2026-05-15 (midday): added A4 temporal, A5 similarity, A6 EU deadlines, C5 relation column, C6 sanctions/burden delta (5 new tasks after user pointed out missing sub-areas).
- 2026-05-15 (afternoon): B1 reshaped from Cmd+K modal to visible TopBar search after UX discussion.
- 2026-05-15 (afternoon): ontology audit completed (section 2.5); C0 added (P0 SPARQL bug fix); A4 split into v1 act-level + v2 provision-level; A5 confirmed as full hybrid; C6 expanded with executive summary printout; mobile strategy locked to Direction B only; two ontology issue bodies drafted.
- 2026-05-15 (evening): plan re-reviewed by user against the repo. Fixes applied: stale predicate names in C3/C4 corrected; dependency graph made consistent (C2–C5 hard-depend on C0; C1 declared soft-dependent and can ship before C0 with a local entity-action map); `app/ontology/relations.py` extraction folded into C0 instead of C5; `targetGroup` reshaped to multi-valued (no `mixed`); A4 act-level warning promoted from tooltip to persistent banner; A5 RAG privacy + chunk aggregation specified; sequencing rewritten to show B-track and A-track-early can start day 0 in parallel with C0; effort estimate revised to 4-6 sprints (2-3 engineers) or 6-8 sprints (1 engineer); session task IDs removed from Section 9 (durable plan should not reference volatile in-session identifiers); C0 regression test split into fixture-graph unit test + corpus smoke test.
- 2026-05-15 (late evening): user did a fresh pass cross-checking ontology facts against `krr_outputs/`. Fixes applied: A4 status corrected (schema + sample sidecars present, full corpus pending #208 — not "data absent"); exact predicate counts replaced with qualitative descriptors and a snapshot caveat (counts drift between ontology releases); A5 `topicCluster` clarified as SHACL-defined-but-unused (use `requestedCluster` as canonical populated predicate); A3 institution count corrected (~113, not ~290); section 2.5 `targetGroup` description aligned with section 5.5 (multi-valued, no `mixed`); A4 banner copy updated to mention sample-act coverage; filing note records that `schema` label exists but `seadusloome-blocker` does not; A5 RAG schema corrected to match `migrations/009 + 016` (`source_uri` single field; `org_id IS NULL` for public corpus; no separate `provision_uri`/`act_uri` columns); A5 privacy wording corrected (Voyage is a SaaS call subject to vendor data-processing controls, not "in-process embedding"); A1 dependency made explicit (v1 standalone has no C0 dep; before/after deltas and C6 integration do).
